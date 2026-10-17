"""Backup-aware deletion order, stdlib only.

The deleter's runtime dependencies (Params, cereal, swaglog) are native modules that only load on the
device, so they are replaced with small stand-ins while the module is loaded under a private name.
"""
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from openpilot.system.loggerd.xattr_cache import setxattr

DELETER = Path(__file__).resolve().parents[1] / 'deleter.py'
OLD = '00000004--0ac3964c96--{}'
NEW = '00000005--4c4e99b08b--{}'


def load_deleter(root: str, free: list[int]):
  hw = types.ModuleType('hw')
  hw.Paths = type('Paths', (), {'log_root': staticmethod(lambda: root),
                                'log_root_external': staticmethod(lambda: os.path.join(root, 'no-external-drive'))})
  config = types.ModuleType('config')
  config.get_available_bytes = lambda default, path_type='internal': free[0]
  config.get_available_percent = lambda default, path_type='internal': 50.0
  swaglog = types.ModuleType('swaglog')
  swaglog.cloudlog = mock.MagicMock()
  uploader = types.ModuleType('uploader')
  uploader.listdir_by_creation = lambda d: sorted((f for f in os.listdir(d) if os.path.isdir(os.path.join(d, f))),
                                                  key=lambda f: (f.rsplit('--', 1)[0], int(f.rsplit('--', 1)[-1]) if '--' in f else -1))
  stubs = {'openpilot.common.hardware.hw': hw, 'openpilot.system.loggerd.config': config,
           'openpilot.common.swaglog': swaglog, 'openpilot.system.loggerd.uploader': uploader}
  with mock.patch.dict(sys.modules, stubs):
    spec = importlib.util.spec_from_file_location('deleter_under_test', DELETER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
  return module


class TestBackupAwareDeleter(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.root = self.tmp.name
    self.free = [0]  # out of space
    self.deleter = load_deleter(self.root, self.free)

  def segment(self, name, files=('qlog.zst', 'qcamera.ts', 'fcamera.hevc', 'ecamera.hevc', 'rlog.zst'),
              backed_up=(), preserve=False, lock=False):
    d = Path(self.root, name)
    d.mkdir()
    for f in files:
      (d / f).write_bytes(b'x')
      if f in backed_up:
        setxattr(str(d / f), self.deleter.BACKED_UP_ATTR_NAME, self.deleter.BACKED_UP_ATTR_VALUE)
    if preserve:
      setxattr(str(d), self.deleter.PRESERVE_ATTR_NAME, self.deleter.PRESERVE_ATTR_VALUE)
    if lock:
      (d / 'rlog.lock').touch()
    return name

  def deleted_order(self, step=None):
    step = step or self.deleter.deleter_step
    order = []
    while True:
      out_of_space, path = step()
      if path is None:
        return order
      order.append(os.path.basename(path))

  def test_backed_up_requires_every_copied_file(self):
    all_four = ('qlog.zst', 'qcamera.ts', 'fcamera.hevc', 'ecamera.hevc')
    self.segment(OLD.format(0), backed_up=all_four)  # rlog.zst is never copied and doesn't count
    self.segment(OLD.format(1), backed_up=all_four[:3])
    self.segment(OLD.format(2), files=('qlog.zst', 'rlog.zst'), backed_up=('qlog.zst',))  # cameras already gone
    self.segment(OLD.format(3), files=('rlog.zst',))  # nothing the phone copies: not backed up
    self.assertEqual([self.deleter.is_backed_up(OLD.format(i)) for i in range(4)], [True, False, True, False])

  def test_backed_up_first_then_oldest_unbacked(self):
    all_four = ('qlog.zst', 'qcamera.ts', 'fcamera.hevc', 'ecamera.hevc')
    a = self.segment(OLD.format(0))
    b = self.segment(OLD.format(1), backed_up=all_four)
    c = self.segment(OLD.format(2))
    d = self.segment(NEW.format(0), backed_up=all_four)
    e = self.segment(NEW.format(1))
    self.assertEqual(self.deleter.deletion_order([a, b, c, d, e]), [b, d, a, c, e])
    self.assertEqual(self.deleted_order(), [b, d, a, c, e])

  def test_preserved_and_delete_last_still_go_last(self):
    all_four = ('qlog.zst', 'qcamera.ts', 'fcamera.hevc', 'ecamera.hevc')
    boot = self.segment('boot', files=('x',))
    keep = self.segment(OLD.format(5), backed_up=all_four, preserve=True)
    unbacked = self.segment(NEW.format(0))
    backed = self.segment(NEW.format(1), backed_up=all_four)
    self.assertEqual(self.deleted_order(), [backed, unbacked, keep, boot])

  def test_locked_segment_is_skipped(self):
    self.segment(OLD.format(0), lock=True)
    b = self.segment(OLD.format(1))
    self.assertEqual(self.deleted_order(), [b])
    self.assertTrue(Path(self.root, OLD.format(0)).exists())

  def test_marks_seen_by_running_deleter(self):
    # The relay marks files from another process; the deleter must not cache a missing mark.
    all_four = ('qlog.zst', 'qcamera.ts', 'fcamera.hevc', 'ecamera.hevc')
    a = self.segment(OLD.format(0))
    b = self.segment(OLD.format(1))
    self.assertEqual(self.deleter.deletion_order([a, b]), [a, b])
    for f in all_four:
      setxattr(str(Path(self.root, b, f)), self.deleter.BACKED_UP_ATTR_NAME, self.deleter.BACKED_UP_ATTR_VALUE)
    self.assertEqual(self.deleter.deletion_order([a, b]), [b, a])

  def test_no_delete_with_space(self):
    self.segment(OLD.format(0))
    self.free[0] = 100 * 1024 ** 3
    self.assertEqual(self.deleter.deleter_step(), (False, None))

  def test_thread_uses_same_order(self):
    import threading
    all_four = ('qlog.zst', 'qcamera.ts', 'fcamera.hevc', 'ecamera.hevc')
    a = self.segment(OLD.format(0))
    b = self.segment(OLD.format(1), backed_up=all_four)
    exit_event = threading.Event()
    deleted = []
    real_rmtree = self.deleter.shutil.rmtree

    def rmtree(path):
      deleted.append(os.path.basename(path))
      real_rmtree(path)
      if len(deleted) == 2:
        exit_event.set()
    with mock.patch.object(self.deleter.shutil, 'rmtree', rmtree):
      t = threading.Thread(target=self.deleter.deleter_thread, args=(exit_event,))
      t.start()
      t.join(10)
      exit_event.set()
    self.assertEqual(deleted, [b, a])


if __name__ == '__main__':
  unittest.main()
