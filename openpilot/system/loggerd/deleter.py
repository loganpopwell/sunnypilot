#!/usr/bin/env python3
import errno
import os
import time
import shutil
import threading
from pathlib import Path
from openpilot.common.hardware.hw import Paths
from openpilot.common.swaglog import cloudlog
from openpilot.system.loggerd.config import get_available_bytes, get_available_percent
from openpilot.system.loggerd.uploader import listdir_by_creation
from openpilot.system.loggerd.xattr_cache import getxattr, _getxattr

MIN_BYTES = 5 * 1024 * 1024 * 1024
MIN_PERCENT = 10

DELETE_LAST = ['boot', 'crash']

PRESERVE_ATTR_NAME = 'user.preserve'
PRESERVE_ATTR_VALUE = b'1'
PRESERVE_COUNT = 5

# Set by phone_relay (POST /v1/backed-up) once the iPhone reports the file verified on the homelab.
BACKED_UP_ATTR_NAME = 'user.phone_relay.backed_up'
BACKED_UP_ATTR_VALUE = b'1'
# The files the phone copies. rlog and dcamera stay on the comma only and don't count.
BACKED_UP_FILES = ('qlog.zst', 'qcamera.ts', 'fcamera.hevc', 'ecamera.hevc')

# Segments seen fully backed up. Files in a closed segment never change, so this only grows
# (pruned to the directories that still exist).
_backed_up_dirs: set[str] = set()


def _has_backed_up_xattr(path: str) -> bool:
  # Uncached: phone_relay sets this from another process while the deleter runs.
  try:
    return _getxattr(path, BACKED_UP_ATTR_NAME) == BACKED_UP_ATTR_VALUE
  except OSError as e:
    if e.errno not in (errno.ENODATA, getattr(errno, 'ENOATTR', errno.ENODATA)):
      cloudlog.exception(f"issue reading backup state of {path}")
    return False


def is_backed_up(d: str) -> bool:
  """True when the segment has at least one phone-copied file and every one present is marked backed up."""
  seg_path = os.path.join(Paths.log_root(), d)
  found = False
  for name in BACKED_UP_FILES:
    path = os.path.join(seg_path, name)
    if not os.path.isfile(path):
      continue
    if not _has_backed_up_xattr(path):
      return False
    found = True
  return found


def get_backed_up_segments(dirs: list[str]) -> set[str]:
  global _backed_up_dirs
  _backed_up_dirs &= set(dirs)
  for d in dirs:
    if d not in _backed_up_dirs and d not in DELETE_LAST and is_backed_up(d):
      _backed_up_dirs.add(d)
  return set(_backed_up_dirs)


def deletion_order(dirs_by_creation: list[str]) -> list[str]:
  """Oldest first within each group: backed up, then not backed up (last resort so recording never
  stops), then preserved segments, then boot/crash logs."""
  preserved_dirs = get_preserved_segments(dirs_by_creation)
  backed_up_dirs = get_backed_up_segments(dirs_by_creation)
  return sorted(dirs_by_creation, key=lambda d: (d in DELETE_LAST, d in preserved_dirs, d not in backed_up_dirs))


def _log_delete(delete_dir: str, delete_path: str, backed_up_dirs: set[str]) -> None:
  if delete_dir in backed_up_dirs or delete_dir in DELETE_LAST:
    cloudlog.info(f"deleting {delete_path}")
  else:
    cloudlog.warning(f"deleting {delete_path} (not backed up to the phone)")


def has_preserve_xattr(d: str) -> bool:
  return getxattr(os.path.join(Paths.log_root(), d), PRESERVE_ATTR_NAME) == PRESERVE_ATTR_VALUE


def get_preserved_segments(dirs_by_creation: list[str]) -> set[str]:
  # skip deleting most recent N preserved segments (and their prior segment)
  preserved = set()
  for n, d in enumerate(filter(has_preserve_xattr, reversed(dirs_by_creation))):
    if n == PRESERVE_COUNT:
      break
    date_str, _, seg_str = d.rpartition("--")

    # ignore non-segment directories
    if not date_str:
      continue
    try:
      seg_num = int(seg_str)
    except ValueError:
      continue

    # preserve segment and two prior
    for _seg_num in range(max(0, seg_num - 2), seg_num + 1):
      preserved.add(f"{date_str}--{_seg_num}")

  return preserved


def deleter_step() -> tuple[bool, str | None]:
  out_of_bytes = get_available_bytes(default=MIN_BYTES + 1) < MIN_BYTES
  out_of_percent = get_available_percent(default=MIN_PERCENT + 1) < MIN_PERCENT
  out_of_space = out_of_percent or out_of_bytes
  if not out_of_space:
    return False, None

  dirs = listdir_by_creation(Paths.log_root())

  # remove the earliest directory we can, backed-up footage first
  for delete_dir in deletion_order(dirs):
    delete_path = os.path.join(Paths.log_root(), delete_dir)

    if any(name.endswith(".lock") for name in os.listdir(delete_path)):
      continue

    try:
      _log_delete(delete_dir, delete_path, _backed_up_dirs)
      shutil.rmtree(delete_path)
      return True, delete_path
    except OSError:
      cloudlog.exception(f"issue deleting {delete_path}")
  return True, None


def deleter_thread(exit_event: threading.Event):
  while not exit_event.is_set():
    out_of_bytes = get_available_bytes(default=MIN_BYTES + 1) < MIN_BYTES
    out_of_percent = get_available_percent(default=MIN_PERCENT + 1) < MIN_PERCENT

    if out_of_percent or out_of_bytes:
      dirs = listdir_by_creation(Paths.log_root())

      # remove the earliest directory we can, backed-up footage first
      for delete_dir in deletion_order(dirs):
        delete_path = os.path.join(Paths.log_root(), delete_dir)

        if any(name.endswith(".lock") for name in os.listdir(delete_path)):
          continue

        if Path(Paths.log_root_external()).is_mount():
          out_of_bytes_external = get_available_bytes(default=MIN_BYTES + 1, path_type="external") < MIN_BYTES
          out_of_percent_external = get_available_percent(default=MIN_PERCENT + 1, path_type="external") < MIN_PERCENT

          if out_of_percent_external or out_of_bytes_external:
            dirs_external = listdir_by_creation(Paths.log_root_external())

            # remove the earliest external directory we can
            for delete_dir_external in sorted(dirs_external):
              delete_path_external = os.path.join(Paths.log_root_external(), delete_dir_external)
              try:
                cloudlog.warning(f"deleting {delete_path_external}")
                shutil.rmtree(delete_path_external)
                break
              except OSError:
                cloudlog.exception(f"issue deleting {delete_path_external}")

          # move directory from internal to external
          path_external = os.path.join(Paths.log_root_external(), delete_dir)
          try:
            cloudlog.warning(f"moving {delete_path} to {path_external}")
            start = time.monotonic()
            shutil.move(delete_path, path_external)
            cloudlog.warning(f"moved {delete_path} to {path_external} in {time.monotonic() - start:.2f}s")
            break
          except Exception:
            cloudlog.error(f"issue moving {delete_path} to {path_external}")
            try:
              cloudlog.warning(f"deleting {delete_path}")
              shutil.rmtree(delete_path)
              break
            except OSError:
              cloudlog.exception(f"issue deleting {delete_path}")
          continue

        try:
          _log_delete(delete_dir, delete_path, _backed_up_dirs)
          shutil.rmtree(delete_path)
          break
        except OSError:
          cloudlog.exception(f"issue deleting {delete_path}")
      exit_event.wait(.1)
    else:
      exit_event.wait(30)


def main():
  deleter_thread(threading.Event())


if __name__ == "__main__":
  main()
