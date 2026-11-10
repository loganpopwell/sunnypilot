"""event_lockd: triggers, locks, the event.json marker, events.jsonl, GET /v1/events and the manifest's
locked flag. Stdlib only.

The impact and bump shapes follow drive 000002a4--9892ade12b (2026-11-09): a rear-end hit at segment 13
+25.4 s reached 2.65 g off the rolling mean while vEgo jumped 3.4 -> 7.1 m/s with the brake on, and the
ordinary road bumps that night peaked near 0.7 g.
"""
import hashlib
import http.client
import json
import os
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path

from openpilot.sunnypilot.phone_relay import event_lock as el
from openpilot.sunnypilot.phone_relay.device import RecordingStore, handler

TOKEN = 'synthetic-test-token-not-a-real-credential'
ROUTE = '000002a4--9892ade12b'
GRAVITY = [0.3, -0.2, 9.81]
CONTRACT_KEYS = {'v', 'id', 'kind', 'wall_time', 'route', 'segment', 'offset_s', 'peak_g', 'speed_mph', 'segments'}


def run(detector, seconds, start=0.0, accel=None, car=None, rate=100):
  """Drive the detector at `rate` Hz. accel(t) -> [x, y, z]; car(t) -> (vEgo, aEgo, brake, gas)."""
  events = []
  for i in range(int(seconds * rate)):
    t = start + i / rate
    if accel:
      events += detector.accelerometer(t, accel(t))
    if car:
      events += detector.car_state(t, *car(t))
  return events


def spike(at, g, width=0.03):
  """Gravity plus a forward jolt of `g` lasting `width` seconds."""
  def accel(t):
    if at <= t < at + width:
      return [GRAVITY[0] + g * el.G, GRAVITY[1], GRAVITY[2]]
    return list(GRAVITY)
  return accel


def cruising(v=8.0, a=0.0, brake=False, gas=False):
  return lambda t: (v, a, brake, gas)


class DetectorTest(unittest.TestCase):
  def test_impact_fires_once_with_peak_and_speed(self):
    d = el.Detector()
    events = run(d, 5, accel=spike(2.0, 2.65), car=cruising(3.4, -2.1, brake=True))
    self.assertEqual(len(events), 1)
    self.assertEqual(events[0]['kind'], 'impact')
    self.assertAlmostEqual(events[0]['t'], 2.0, delta=0.02)
    self.assertAlmostEqual(events[0]['peak_g'], 2.65, delta=0.05)
    self.assertAlmostEqual(events[0]['speed_mph'], 7.6, delta=0.1)

  def test_road_bumps_do_not_fire(self):
    d = el.Detector()
    bumps = [spike(at, 0.7, width=0.08) for at in (1.0, 1.3, 2.0, 4.5)]
    def accel(t):
      for b in bumps:
        v = b(t)
        if v != GRAVITY:
          return v
      return list(GRAVITY)
    self.assertEqual(run(d, 6, accel=accel, car=cruising(20.0)), [])

  def test_impact_threshold(self):
    self.assertEqual(run(el.Detector(), 3, accel=spike(1.5, 1.4)), [])
    self.assertEqual([e['kind'] for e in run(el.Detector(), 3, accel=spike(1.5, 1.6))], ['impact'])

  def test_needs_a_full_mean_window(self):
    # A jolt before 50 samples have arrived (start of a drive, or after a gap) has no baseline yet.
    self.assertEqual(run(el.Detector(), 2, accel=spike(0.1, 3.0)), [])
    d = el.Detector()
    run(d, 2, accel=lambda t: list(GRAVITY))
    self.assertEqual(run(d, 2, start=5.0, accel=spike(5.1, 3.0)), [])  # 3 s gap restarts the window

  def test_hard_brake_needs_0_3_s(self):
    def short(t):
      return 10.0, -7.0 if 1.0 <= t < 1.2 else -1.0, True, False
    self.assertEqual(run(el.Detector(), 3, car=short), [])
    def hard(t):
      return 10.0, -7.0 if 1.0 <= t < 1.5 else -1.0, True, False
    events = run(el.Detector(), 3, car=hard)
    self.assertEqual([e['kind'] for e in events], ['hard_brake'])
    self.assertAlmostEqual(events[0]['t'], 1.0, delta=0.02)
    self.assertEqual(events[0]['peak_g'], None)  # no accelerometer data fed
    self.assertEqual(events[0]['speed_mph'], 22.4)

  def test_hard_brake_threshold(self):
    self.assertEqual(run(el.Detector(), 3, car=lambda t: (10.0, -5.9, True, False)), [])

  def test_speed_jump_with_brake(self):
    # The 2026-11-09 crash: braking at 3.4 m/s, pushed to 7.1 m/s in under 0.2 s, foot knocked off the brake.
    def car(t):
      if t < 2.0:
        return 3.4, -2.1, True, False
      if t < 2.2:
        return 3.4 + (t - 2.0) / 0.2 * 3.7, 15.0, t < 2.05, False
      return 7.1, -1.0, False, False
    events = run(el.Detector(), 4, car=car)
    self.assertEqual([e['kind'] for e in events], ['speed_jump'])
    self.assertAlmostEqual(events[0]['t'], 2.0, delta=0.02)
    self.assertEqual(events[0]['speed_mph'], 7.6)  # before the push

  def test_speed_jump_needs_brake_and_no_gas(self):
    def jump(brake, gas):
      return lambda t: (3.0 if t < 2.0 else 6.0, 0.0, brake, gas)
    self.assertEqual(run(el.Detector(), 4, car=jump(brake=False, gas=False)), [])
    self.assertEqual(run(el.Detector(), 4, car=jump(brake=True, gas=True)), [])
    self.assertEqual(len(run(el.Detector(), 4, car=jump(brake=True, gas=False))), 1)
    # slower than 2 m/s per 0.25 s is ordinary acceleration
    self.assertEqual(run(el.Detector(), 4, car=lambda t: (3.0 + t * 4.0, 4.0, True, False)), [])
    # a drop is not a jump
    self.assertEqual(run(el.Detector(), 4, car=lambda t: (8.0 if t < 2.0 else 5.0, 0.0, True, False)), [])

  def test_bookmark(self):
    d = el.Detector()
    run(d, 1, car=cruising())
    self.assertEqual(d.bookmark(1.0), [])
    events = run(d, 1, start=1.0, car=cruising())
    self.assertEqual([(e['kind'], e['peak_g']) for e in events], [('bookmark', None)])

  def test_debounced_to_one_per_10_s(self):
    d = el.Detector()
    def accel(t):  # jolts at 1 s, 9 s (inside the 10 s debounce) and 12 s
      return spike(1.0 if t < 5 else 9.0 if t < 10 else 12.0, 2.0)(t)
    events = run(d, 14, accel=accel)
    self.assertEqual([round(e['t']) for e in events], [1, 12])

  def test_stronger_trigger_in_the_same_moment_names_the_event(self):
    d = el.Detector()
    run(d, 2, accel=lambda t: list(GRAVITY), car=cruising(brake=True))
    d.bookmark(2.0)
    events = run(d, 2, start=2.0, accel=spike(2.2, 2.0), car=cruising(brake=True))
    self.assertEqual([e['kind'] for e in events], ['impact'])
    self.assertAlmostEqual(events[0]['t'], 2.0, delta=0.001)

  def test_flush_on_shutdown(self):
    d = el.Detector()
    d.bookmark(1.0)
    self.assertEqual(d.poll(1.1), [])
    self.assertEqual([e['kind'] for e in d.poll(1.1, force=True)], ['bookmark'])


class WallTimeTest(unittest.TestCase):
  def test_gps_when_fixed_and_fresh(self):
    ms = 1794269211804
    self.assertEqual(el.wall_time_ms(100.0, 101.0, (99.5, ms, True)), (ms + 500, 'gps'))
    self.assertEqual(el.iso_utc(ms), '2026-11-10T00:06:51.804Z')

  def test_system_otherwise(self):
    def now():
      return 2000000000.0
    for gps in (None, (99.5, 1794269211804, False), (90.0, 1794269211804, True), (99.5, 0, True)):
      self.assertEqual(el.wall_time_ms(100.0, 101.0, gps, now=now), (1999999999000, 'system'))


class FilesTest(unittest.TestCase):
  def setUp(self):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    self.root = Path(tmp.name) / 'realdata'
    self.root.mkdir()
    self.events_path = Path(tmp.name) / 'events' / 'events.jsonl'
    self.recorder = el.Recorder(self.root, self.events_path)

  def segment(self, n, start=None, lock=False, route=ROUTE):
    d = self.root / f'{route}--{n}'
    d.mkdir()
    (d / 'rlog.zst').write_bytes(b'x')
    if lock or start is not None:
      (d / 'rlog.lock').touch()
      if start is not None:
        os.utime(d / 'rlog.lock', (start, start))
    return d

  def test_record_locks_marks_and_logs(self):
    for n in range(10, 14):
      self.segment(n)
    event = {'kind': 'impact', 't': 1.0, 'peak_g': 2.65, 'speed_mph': 7.6}
    marker = self.recorder.record(event, ROUTE, 13, 25.37, 1794269211804)

    self.assertEqual(set(marker), CONTRACT_KEYS)
    self.assertEqual(marker['id'], f'{ROUTE}--13--1794269211804')
    self.assertEqual(marker['wall_time'], '2026-11-10T00:06:51.804Z')
    self.assertEqual(marker['segments'], [f'{ROUTE}--{n}' for n in (11, 12, 13, 14)])
    self.assertEqual(json.loads((self.root / f'{ROUTE}--13' / 'event.json').read_text()), marker)
    self.assertFalse((self.root / f'{ROUTE}--13' / 'event.json.tmp').exists())
    self.assertEqual([json.loads(line) for line in self.events_path.read_text().splitlines()], [marker])

    self.assertEqual([el.is_locked(self.root / f'{ROUTE}--{n}') for n in range(10, 14)], [False, True, True, True])
    # the after-segment is locked once loggerd opens it
    self.assertEqual(list(self.recorder.pending), [f'{ROUTE}--14'])
    self.recorder.apply_pending()
    self.assertEqual(list(self.recorder.pending), [f'{ROUTE}--14'])
    self.segment(14)
    self.recorder.apply_pending()
    self.assertTrue(el.is_locked(self.root / f'{ROUTE}--14'))
    self.assertEqual(self.recorder.pending, {})

  def test_first_segments_and_deadline(self):
    self.segment(0)
    marker = self.recorder.record({'kind': 'bookmark', 't': 0.0, 'peak_g': None, 'speed_mph': None}, ROUTE, 0, 3.0, 1794269211804,
                                  mono=lambda: 100.0)
    self.assertEqual(marker['segments'], [f'{ROUTE}--0', f'{ROUTE}--1'])
    self.recorder.apply_pending(mono=lambda: 100.0 + el.PENDING_LOCK_S + 1)  # the drive ended first
    self.assertEqual(self.recorder.pending, {})

  def test_marker_keeps_the_most_severe_event(self):
    self.segment(5)
    def rec(kind, ms):
      self.recorder.record({'kind': kind, 't': 0.0, 'peak_g': None, 'speed_mph': None}, ROUTE, 5, 1.0, ms)
    rec('bookmark', 1)
    rec('impact', 2)
    rec('hard_brake', 3)
    self.assertEqual(json.loads((self.root / f'{ROUTE}--5' / 'event.json').read_text())['kind'], 'impact')
    self.assertEqual([e['kind'] for e in el.read_events(self.events_path)], ['hard_brake', 'impact', 'bookmark'])

  def test_failures_are_swallowed(self):
    logged = []
    log = type('Log', (), {'exception': lambda self, msg: logged.append(msg)})()
    recorder = el.Recorder(self.root, Path('/dev/null/events.jsonl'), log)
    marker = recorder.record({'kind': 'impact', 't': 0.0, 'peak_g': 2.0, 'speed_mph': 1.0}, ROUTE, 3, 1.0, 1)
    self.assertEqual(marker['kind'], 'impact')
    self.assertTrue(any('marker' in m for m in logged) and any('events.jsonl' in m for m in logged))

  def test_resume_after_restart(self):
    self.segment(7)
    self.recorder.record({'kind': 'impact', 't': 0.0, 'peak_g': 2.0, 'speed_mph': 1.0}, ROUTE, 7, 1.0, 1)
    self.segment(8)
    other = el.Recorder(self.root, self.events_path)
    other.resume('00000001--aaaaaaaaaa')
    self.assertFalse(el.is_locked(self.root / f'{ROUTE}--8'))
    other.resume(ROUTE)
    self.assertTrue(el.is_locked(self.root / f'{ROUTE}--8'))

  def test_read_events_skips_bad_lines(self):
    self.events_path.parent.mkdir()
    self.events_path.write_text('{"id":"a"}\nnot json\n[1]\n{"id":"b"}\n')
    self.assertEqual(el.read_events(self.events_path), [{'id': 'b'}, {'id': 'a'}])
    self.assertEqual(el.read_events(self.events_path, limit=1), [{'id': 'b'}])
    self.assertEqual(el.read_events(self.root / 'missing.jsonl'), [])

  def test_segment_tracker_follows_loggerd(self):
    self.segment(0, route='00000001--aaaaaaaaaa')
    self.segment(3, start=1000.0)
    self.segment(4, start=1060.0, lock=True)
    tracker = el.SegmentTracker(self.root)
    tracker.update(None)
    self.assertIsNone(tracker.route)
    tracker.update(ROUTE)
    self.assertEqual(tracker.locate(1061.5), (4, 1.5))
    self.segment(5, start=1120.0, lock=True)
    tracker.update(ROUTE)
    self.assertEqual(tracker.locate(1119.9), (4, 59.9))  # rotated between the trigger and the write
    self.assertEqual(tracker.locate(1130.0), (5, 10.0))
    self.assertEqual(tracker.locate(900.0), (4, 0.0))  # before we started watching: earliest known


class RelayTest(unittest.TestCase):
  def setUp(self):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    self.root = Path(tmp.name) / 'realdata'
    self.events_path = Path(tmp.name) / 'events' / 'events.jsonl'
    self.store = RecordingStore(self.root, events=self.events_path)
    server = HTTPServer(('127.0.0.1', 0), handler(self.store, hashlib.sha256(TOKEN.encode()).hexdigest()))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    def stop():
      server.shutdown()
      server.server_close()
      t.join()
    self.addCleanup(stop)
    self.port = server.server_port

  def get(self, path, token=TOKEN):
    conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
    try:
      conn.request('GET', path, headers={'Authorization': f'Bearer {token}'} if token else {})
      response = conn.getresponse()
      return response.status, response.read()
    finally:
      conn.close()

  def test_events_newest_first(self):
    self.assertEqual(self.get('/v1/events'), (200, b'{"events": []}'))
    recorder = el.Recorder(self.root, self.events_path)
    self.root.mkdir()
    for i, kind in enumerate(('bookmark', 'impact')):
      recorder.record({'kind': kind, 't': 0.0, 'peak_g': None, 'speed_mph': None}, ROUTE, i, 1.0, 1794269211804 + i)
    status, body = self.get('/v1/events')
    self.assertEqual(status, 200)
    events = json.loads(body)['events']
    self.assertEqual([e['kind'] for e in events], ['impact', 'bookmark'])
    self.assertEqual(set(events[0]), CONTRACT_KEYS)

  def test_events_need_auth(self):
    self.assertEqual(self.get('/v1/events', token=None)[0], 401)
    self.assertEqual(self.get('/v1/events', token='wrong')[0], 401)

  def test_manifest_locked_flag(self):
    for n in (1, 2):
      d = self.root / f'{ROUTE}--{n}'
      d.mkdir(parents=True)
      (d / 'qlog.zst').write_bytes(b'0123456789')
    el.lock(self.root / f'{ROUTE}--2')
    status, body = self.get('/v1/manifest')
    self.assertEqual(status, 200)
    files = {f['path']: f['locked'] for f in json.loads(body)['files']}
    self.assertEqual(files, {f'{ROUTE}--2/qlog.zst': True, f'{ROUTE}--1/qlog.zst': False})


if __name__ == '__main__':
  unittest.main()
