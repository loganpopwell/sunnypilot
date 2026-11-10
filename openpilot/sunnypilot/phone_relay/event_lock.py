"""event_lockd: notices a crash, a hard stop or a bookmark and keeps the footage around it.

Detection and logging only. It subscribes to sensor and car messages and never publishes, so it can't
touch controls, CAN or the panda, and loggerd never waits on it. On an event it:
  - sets user.preserve on the event's segment, the 2 before it and the 1 after it (once loggerd opens it),
    so the deleter removes that footage last;
  - writes <segment>/event.json, which Athena listDataDirectory and the homelab archiver can see;
  - appends the event to /data/media/0/events/events.jsonl (outside realdata, never deleted).
Every failure is logged and swallowed.

The detector and the file side are plain Python so the tests and the offline replay run anywhere.
"""
import datetime
import json
import math
import os
import time
from collections import deque
from pathlib import Path

from openpilot.system.loggerd.xattr_cache import _getxattr, _setxattr

G = 9.80665
# impact: an accelerometer sample this far (vector distance) from the mean of the previous ~0.5 s.
IMPACT_DEVIATION = 1.5 * G
MEAN_SAMPLES = 50  # accelerometer runs at 104 Hz, so ~0.48 s
# hard_brake: aEgo at or below this for at least HARD_BRAKE_S.
HARD_BRAKE_ACCEL = -6.0
HARD_BRAKE_S = 0.3
# speed_jump: vEgo rises this much within JUMP_S while the brake is pressed and gas is not (pushed from behind).
JUMP_SPEED = 2.0
JUMP_S = 0.25
DEBOUNCE_S = 10.0
# An event is written this long after it triggers, so the peak and any stronger trigger are included.
SETTLE_S = 0.5
GAP_S = 0.5  # a longer gap in a sensor stream restarts its windows
GPS_MAX_AGE_S = 2.0

# Most to least severe. grab: the phone asked for the last few minutes (POST /v1/grab on the relay); it
# never replaces another event's marker.
KINDS = ('impact', 'speed_jump', 'hard_brake', 'bookmark', 'grab')
LOCK_BEFORE = 2
LOCK_AFTER = 1
PRESERVE_ATTR_NAME = 'user.preserve'  # same as the deleter and loggerd
PRESERVE_ATTR_VALUE = b'1'
LOG_ROOT = '/data/media/0/realdata'
EVENTS_PATH = '/data/media/0/events/events.jsonl'
MARKER = 'event.json'
PENDING_LOCK_S = 15 * 60  # give up on an after-segment that never appears
MPH = 2.2369362920544


class Detector:
  """Feed messages in time order (seconds on one monotonic clock). Each feed returns finished events:
  {'kind', 't', 'peak_g', 'speed_mph'}, at most one per DEBOUNCE_S."""

  def __init__(self):
    self.acc = deque()
    self.acc_sum = [0.0, 0.0, 0.0]
    self.acc_t = None
    self.deviations = deque()  # (t, m/s^2) for the peak
    self.car = deque()  # (t, vEgo, brake, gas) over the last JUMP_S
    self.car_t = None
    self.brake_start = None
    self.brake_speed = None
    self.v_ego = None
    self.pending = None
    self.last_trigger = -math.inf

  def accelerometer(self, t, v):
    if self.acc_t is not None and t - self.acc_t > GAP_S:
      self.acc.clear()
      self.acc_sum = [0.0, 0.0, 0.0]
    self.acc_t = t
    if len(self.acc) == MEAN_SAMPLES:
      n = len(self.acc)
      deviation = math.sqrt(sum((v[i] - self.acc_sum[i] / n) ** 2 for i in range(3)))
      self.deviations.append((t, deviation))
      while self.deviations[0][0] < t - 2 * SETTLE_S - 1.0:
        self.deviations.popleft()
      if deviation >= IMPACT_DEVIATION:
        self._trigger('impact', t)
      old = self.acc.popleft()
      for i in range(3):
        self.acc_sum[i] -= old[i]
    self.acc.append(v)
    for i in range(3):
      self.acc_sum[i] += v[i]
    return self.poll(t)

  def car_state(self, t, v_ego, a_ego, brake, gas):
    if self.car_t is not None and t - self.car_t > GAP_S:
      self.car.clear()
      self.brake_start = None
    self.car_t = t
    self.v_ego = v_ego

    if a_ego <= HARD_BRAKE_ACCEL:
      if self.brake_start is None:
        self.brake_start, self.brake_speed = t, v_ego
      elif t - self.brake_start >= HARD_BRAKE_S:
        self._trigger('hard_brake', self.brake_start, self.brake_speed)
    else:
      self.brake_start = None

    self.car.append((t, v_ego, bool(brake), bool(gas)))
    while self.car[0][0] < t - JUMP_S:
      self.car.popleft()
    low = min(reversed(self.car), key=lambda s: s[1])  # latest lowest sample: where the jump starts
    if v_ego - low[1] >= JUMP_SPEED and low[0] < t and any(s[2] for s in self.car) and not any(s[3] for s in self.car):
      self._trigger('speed_jump', low[0], low[1])
    return self.poll(t)

  def bookmark(self, t):
    self._trigger('bookmark', t)
    return self.poll(t)

  def _trigger(self, kind, t, v_ego=None):
    """v_ego: speed when it started (before a hard stop or a push), else the latest."""
    if self.pending is not None:
      if KINDS.index(kind) < KINDS.index(self.pending['kind']):
        self.pending['kind'] = kind  # a stronger trigger in the same moment names the event
      return
    if t - self.last_trigger < DEBOUNCE_S:
      return
    self.last_trigger = t
    v_ego = self.v_ego if v_ego is None else v_ego
    speed = None if v_ego is None else round(v_ego * MPH, 1)
    self.pending = {'kind': kind, 't': t, 'speed_mph': speed}

  def poll(self, now, force=False):
    """The pending event once SETTLE_S has passed (or right away with force)."""
    event = self.pending
    if event is None or not (force or now - event['t'] >= SETTLE_S):
      return []
    self.pending = None
    peaks = [d for t, d in self.deviations if event['t'] - SETTLE_S <= t <= event['t'] + SETTLE_S]
    event['peak_g'] = round(max(peaks) / G, 2) if peaks and event['kind'] != 'bookmark' else None
    return [event]


def wall_time_ms(event_t, latest_t, gps=None, now=time.time):  # noqa: TID251 (wall clock on purpose)
  """Event time in Unix ms: from GPS when it has a fix within GPS_MAX_AGE_S of the event, else system
  time minus how long ago the event happened. gps is (t, unixTimestampMillis, hasFix)."""
  if gps is not None:
    gps_t, gps_ms, fix = gps
    if fix and abs(event_t - gps_t) <= GPS_MAX_AGE_S and 1.6e12 < gps_ms < 4.1e12:
      return int(round(gps_ms + (event_t - gps_t) * 1000)), 'gps'
  return int(round((now() - max(0.0, latest_t - event_t)) * 1000)), 'system'


def iso_utc(ms):
  return datetime.datetime.fromtimestamp(ms / 1000, datetime.UTC).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def is_locked(path):
  try:
    return _getxattr(str(path), PRESERVE_ATTR_NAME) == PRESERVE_ATTR_VALUE
  except OSError:
    return False


def lock(path):
  """Set user.preserve on an existing segment directory. False if it isn't there (yet)."""
  path = Path(path)
  if path.is_symlink() or not path.is_dir():
    return False
  if not is_locked(path):
    _setxattr(str(path), PRESERVE_ATTR_NAME, PRESERVE_ATTR_VALUE)
  return True


class SegmentTracker:
  """Which segment loggerd is writing, from what it publishes: the CurrentRoute param and the
  rlog.lock it keeps in the open segment (created when the segment opens, so its mtime is the start)."""

  def __init__(self, root):
    self.root = Path(root)
    self.route = None
    self.starts = {}  # segment number -> wall-clock start (s)

  def update(self, route, now=time.time):  # noqa: TID251 (wall clock on purpose)
    if not route:
      return
    if route != self.route:
      self.route, self.starts = route, {}
      numbers = [int(p.name.rsplit('--', 1)[1]) for p in self.root.glob(f'{route}--*') if p.name.rsplit('--', 1)[1].isdigit()]
      n = max(numbers, default=None)
    else:
      n = max(self.starts, default=None)
      while n is not None and (self.root / f'{route}--{n + 1}').is_dir():
        n += 1
    if n is None or n in self.starts:
      return
    try:
      start = (self.root / f'{route}--{n}' / 'rlog.lock').stat().st_mtime
    except OSError:
      start = now()  # loggerd opened it between our checks; close enough
    self.starts[n] = start
    for old in [k for k in self.starts if k < n - LOCK_BEFORE - 2]:
      del self.starts[old]

  def locate(self, wall_s):
    """(segment, offset_s) of a system-clock time in the current route, or None with no route yet."""
    found = None
    for n in sorted(self.starts):
      if self.starts[n] <= wall_s:
        found = n
    if found is None:
      return None if not self.starts else (min(self.starts), 0.0)
    return found, round(max(0.0, wall_s - self.starts[found]), 2)


class Recorder:
  """Lock, marker and log for one event. Each step is tried even if another failed."""

  def __init__(self, root=LOG_ROOT, events_path=EVENTS_PATH, log=None):
    self.root = Path(root)
    self.events_path = Path(events_path) if events_path else None  # None: no events.jsonl
    self.log = log
    self.pending = {}  # segment dir name -> monotonic deadline

  def _error(self, what):
    if self.log is not None:
      self.log.exception(f'event_lockd: {what}')

  @staticmethod
  def marker(event, route, segment, offset_s, wall_ms, names=None):
    """The contract's event.json. names: segments to lock, default the 2 before to the 1 after."""
    if names is None:
      names = [f'{route}--{n}' for n in range(max(0, segment - LOCK_BEFORE), segment + LOCK_AFTER + 1)]
    return {'v': 1, 'id': f'{route}--{segment}--{wall_ms}', 'kind': event['kind'], 'wall_time': iso_utc(wall_ms),
            'route': route, 'segment': segment, 'offset_s': float(offset_s), 'peak_g': event.get('peak_g'),
            'speed_mph': event.get('speed_mph'), 'segments': list(names)}

  def lock_all(self, names, mono=time.monotonic):
    """Lock each segment now, or once it appears (an after-segment loggerd hasn't opened yet)."""
    for name in names:
      try:
        if not lock(self.root / name):
          self.pending[name] = mono() + PENDING_LOCK_S
      except OSError:
        self._error(f'lock {name}')

  def write(self, marker):
    """event.json and events.jsonl for a marker."""
    try:
      self.write_marker(self.root / f'{marker["route"]}--{marker["segment"]}', marker)
    except OSError:
      self._error(f'marker {marker["route"]}--{marker["segment"]}')
    try:
      self.append(marker)
    except OSError:
      self._error('events.jsonl')

  def record(self, event, route, segment, offset_s, wall_ms, mono=time.monotonic):
    marker = self.marker(event, route, segment, offset_s, wall_ms)
    self.lock_all(marker['segments'], mono)
    self.write(marker)
    return marker

  def write_marker(self, directory, marker):
    """One event.json per segment. A second event in the same segment replaces it only if it is more severe;
    events.jsonl keeps every event. Two grabs in one segment merge: the newer one, covering both."""
    path = directory / MARKER
    try:
      old = json.loads(path.read_text())
      if old.get('kind') == marker['kind'] == 'grab' and isinstance(old.get('segments'), list):
        names = {n for n in old['segments'] + marker['segments'] if isinstance(n, str)}
        marker = {**marker, 'segments': sorted(names, key=lambda n: (n.rsplit('--', 1)[0], int(n.rsplit('--', 1)[1])))}
      elif KINDS.index(marker['kind']) >= KINDS.index(old.get('kind')):
        return
    except (OSError, ValueError, IndexError):
      pass
    tmp = directory / (MARKER + '.tmp')
    with open(tmp, 'w') as f:
      json.dump(marker, f, separators=(',', ':'))
      f.flush()
      os.fsync(f.fileno())
    os.replace(tmp, path)

  def append(self, marker):
    if self.events_path is None:
      return
    self.events_path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(marker, separators=(',', ':')) + '\n').encode()
    fd = os.open(self.events_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
      os.write(fd, line)
      os.fsync(fd)
    finally:
      os.close(fd)

  def apply_pending(self, mono=time.monotonic):
    """Lock after-segments that loggerd has opened since the event."""
    now = mono()
    for name, deadline in list(self.pending.items()):
      try:
        if lock(self.root / name) or now > deadline:
          del self.pending[name]
      except OSError:
        self._error(f'lock {name}')
        del self.pending[name]

  def resume(self, route, mono=time.monotonic):
    """After a restart mid-drive, finish the locks of this route's events."""
    if self.events_path is None:
      return
    for marker in read_events(self.events_path, limit=50):
      if marker.get('route') != route:
        continue
      for name in marker.get('segments') or []:
        if isinstance(name, str) and name.startswith(route + '--') and '/' not in name:
          try:
            if not lock(self.root / name):
              self.pending[name] = mono() + PENDING_LOCK_S
          except OSError:
            self._error(f'lock {name}')


def read_events(path, limit=1000):
  """Logged events, newest first. Malformed lines are skipped."""
  try:
    with open(path, 'rb') as f:
      lines = f.read().splitlines()
  except OSError:
    return []  # none yet, or unreadable: the relay answers with an empty list rather than an error
  events = []
  for line in reversed(lines):
    try:
      value = json.loads(line)
    except ValueError:
      continue
    if isinstance(value, dict):
      events.append(value)
      if len(events) >= limit:
        break
  return events


def main():
  from openpilot.cereal import messaging
  from openpilot.common.gps import get_gps_location_service
  from openpilot.common.params import Params
  from openpilot.common.swaglog import cloudlog

  try:
    os.nice(5)
  except OSError:
    pass
  params = Params()
  gps_service = get_gps_location_service(params)
  poller = messaging.Poller()
  socks = {name: messaging.sub_sock(name, poller=poller, conflate=False) for name in ('accelerometer', 'carState', 'userBookmark')}
  socks[gps_service] = messaging.sub_sock(gps_service, poller=poller, conflate=True)
  names = {sock: name for name, sock in socks.items()}

  detector = Detector()
  tracker = SegmentTracker(LOG_ROOT)
  recorder = Recorder(LOG_ROOT, EVENTS_PATH, cloudlog)
  gps = None
  latest_t = 0.0
  next_check = 0.0
  resumed = False

  def route():
    try:
      return params.get('CurrentRoute')
    except Exception:
      cloudlog.exception('event_lockd: CurrentRoute')
      return None

  def finish(event):
    try:
      wall_ms, source = wall_time_ms(event['t'], latest_t, gps)
      # rlog.lock times are system time, so the segment is found with system time even when GPS names the event
      where = tracker.locate(time.time() - max(0.0, latest_t - event['t']))  # noqa: TID251
      if tracker.route is None or where is None:
        cloudlog.error(f'event_lockd: {event["kind"]} with no recording segment')
        return
      marker = recorder.record(event, tracker.route, where[0], where[1], wall_ms)
      cloudlog.warning(f'event_lockd: {marker["kind"]} at {marker["id"]} +{marker["offset_s"]}s, ' +
                       f'peak {marker["peak_g"]} g, {marker["speed_mph"]} mph, time from {source}')
    except Exception:
      cloudlog.exception('event_lockd: record')

  try:
    while True:
      for sock in poller.poll(100):
        name = names[sock]
        try:
          msgs = messaging.drain_sock(sock)
        except Exception:
          cloudlog.exception(f'event_lockd: read {name}')
          continue
        for msg in msgs:
          try:
            t = msg.logMonoTime / 1e9
            latest_t = max(latest_t, t)
            done = []
            if name == 'accelerometer':
              if msg.accelerometer.which() == 'acceleration':
                v = list(msg.accelerometer.acceleration.v)
                if len(v) >= 3:
                  done = detector.accelerometer(t, v[:3])
            elif name == 'carState':
              cs = msg.carState
              done = detector.car_state(t, cs.vEgo, cs.aEgo, cs.brakePressed, cs.gasPressed)
            elif name == 'userBookmark':
              done = detector.bookmark(t)
            else:
              location = getattr(msg, name)
              gps = (t, location.unixTimestampMillis, location.hasFix)
            for event in done:
              finish(event)
          except Exception:
            cloudlog.exception(f'event_lockd: {name}')

      now = time.monotonic()
      if now >= next_check:
        next_check = now + 0.25
        try:
          tracker.update(route())
          if tracker.route and not resumed:
            resumed = True
            recorder.resume(tracker.route)
          recorder.apply_pending()
        except Exception:
          cloudlog.exception('event_lockd: segments')
        for event in detector.poll(latest_t):
          finish(event)
  finally:
    for event in detector.poll(latest_t, force=True):  # manager stops us when the car turns off
      finish(event)


if __name__ == '__main__':
  main()
