"""POST /v1/grab: locking the last minutes, the "grab" event, the response, partial reads of the recording
segment and the grab byte budget. Stdlib only."""
import hashlib
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from openpilot.sunnypilot.phone_relay import device
from openpilot.sunnypilot.phone_relay import event_lock as el
from openpilot.sunnypilot.phone_relay.device import BoundedServer, RecordingStore, handler, is_backed_up

TOKEN = 'synthetic-test-token-not-a-real-credential'
ROUTE = '000002a5--79c8a1c749'
OLD = '000002a4--9892ade12b'
BASE = 1794269000.0  # wall time route ROUTE started


class Clock:
  def __init__(self, wall, mono=1000.0):
    self.wall, self.mono_t = wall, mono
  def now(self):
    return self.wall
  def mono(self):
    return self.mono_t


class GrabTest(unittest.TestCase):
  def setUp(self):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    self.root = Path(tmp.name) / 'realdata'
    self.root.mkdir()
    self.events_path = Path(tmp.name) / 'events' / 'events.jsonl'
    self.data = {}

  def segment(self, route, n, start, recording=False, closed_cams=(), size=1000):
    """A segment that started at wall time `start`; closed ones end (mtime) 60 s later."""
    d = self.root / f'{route}--{n}'
    d.mkdir()
    for f in ('qcamera.ts', 'fcamera.hevc', 'ecamera.hevc', 'qlog.zst', 'rlog.zst'):
      data = os.urandom(size)
      (d / f).write_bytes(data)
      self.data[f'{d.name}/{f}'] = data
      if not recording:
        os.utime(d / f, (start + 60, start + 60))
    if recording:
      (d / 'rlog.lock').touch()
      os.utime(d / 'rlog.lock', (start, start))
      for f in ('qcamera.ts', 'fcamera.hevc', 'ecamera.hevc'):
        if f not in closed_cams:
          (d / f'{f}.lock').touch()
    return d

  def drive(self, recording=True):
    """An older route, then ROUTE segments 0..10; segment 10 recording (30 s in) unless parked."""
    for n in range(3):
      self.segment(OLD, n, BASE - 7200 + 60 * n)
    for n in range(10):
      self.segment(ROUTE, n, BASE + 60 * n)
    self.segment(ROUTE, 10, BASE + 600, recording=recording, closed_cams=('ecamera.hevc',))

  def start(self, store):
    server = BoundedServer(('127.0.0.1', 0), handler(store, hashlib.sha256(TOKEN.encode()).hexdigest()))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    def stop():
      server.shutdown()
      server.server_close()
    self.addCleanup(stop)
    self.port = server.server_address[1]

  def store(self, clock, onroad=False):
    store = RecordingStore(self.root, onroad=lambda: onroad, events=self.events_path, now=clock.now, mono=clock.mono)
    self.start(store)
    return store

  def request(self, method, path, body=None, headers=None, token=TOKEN):
    c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    c.request(method, path, body=data, headers={'Authorization': 'Bearer ' + token, **(headers or {})})
    r = c.getresponse()
    out = r.read()
    c.close()
    return r, out

  def test_grab_while_recording_locks_marks_and_answers(self):
    self.drive()
    clock = Clock(BASE + 630)
    store = self.store(clock, onroad=True)
    t = time.monotonic()
    r, body = self.request('POST', '/v1/grab', {'minutes': 5})
    self.assertLess(time.monotonic() - t, 1)
    self.assertEqual(r.status, 200)
    value = json.loads(body)

    # Last 5 minutes from 10:30 into the drive: segments 5 (ends 6:00) through 10 (recording), oldest first.
    names = [f'{ROUTE}--{n}' for n in range(5, 11)]
    self.assertEqual(value['route'], ROUTE)
    self.assertEqual([s['name'] for s in value['segments']], names)
    self.assertEqual([s['recording'] for s in value['segments']], [False] * 5 + [True])
    closed = value['segments'][0]['files']
    self.assertEqual(set(closed), {'qcamera.ts', 'fcamera.hevc', 'ecamera.hevc', 'qlog.zst', 'rlog.zst'})
    self.assertEqual(closed['fcamera.hevc'], {'size': 1000, 'closed': True})
    live = value['segments'][-1]['files']
    self.assertEqual({k: v['closed'] for k, v in live.items()},
                     {'qcamera.ts': False, 'fcamera.hevc': False, 'ecamera.hevc': True, 'qlog.zst': False, 'rlog.zst': False})

    self.assertEqual([el.is_locked(self.root / f'{ROUTE}--{n}') for n in range(3, 11)], [False, False] + [True] * 6)
    self.assertFalse(any(el.is_locked(self.root / f'{OLD}--{n}') for n in range(3)))

    marker = json.loads((self.root / f'{ROUTE}--10' / 'event.json').read_text())
    self.assertEqual(set(marker), {'v', 'id', 'kind', 'wall_time', 'route', 'segment', 'offset_s', 'peak_g', 'speed_mph', 'segments'})
    self.assertEqual((marker['kind'], marker['id'], marker['segment'], marker['offset_s'], marker['peak_g']),
                     ('grab', value['event_id'], 10, 30.0, None))
    self.assertEqual(marker['id'], f'{ROUTE}--10--{int((BASE + 630) * 1000)}')
    self.assertEqual(marker['segments'], names + [f'{ROUTE}--11'])
    self.assertEqual(el.read_events(self.events_path), [marker])
    r, body = self.request('GET', '/v1/events')
    self.assertEqual(json.loads(body)['events'][0]['kind'], 'grab')

    # The next segment is locked once loggerd opens it.
    self.segment(ROUTE, 11, BASE + 660, recording=True)
    deadline = time.monotonic() + 5
    while not el.is_locked(self.root / f'{ROUTE}--11') and time.monotonic() < deadline:
      time.sleep(0.05)
    self.assertTrue(el.is_locked(self.root / f'{ROUTE}--11'))

  def test_grab_offroad_uses_latest_route(self):
    self.drive(recording=False)
    # A date-named route sorts after ROUTE by name but is older; the latest is found by time.
    for n in range(2):
      self.segment('2024-01-01--10-00-00', n, BASE - 90000 + 60 * n)
    self.store(Clock(BASE + 5000))
    r, body = self.request('POST', '/v1/grab', b'', headers={'Content-Length': '0'})  # empty body: 5 minutes
    value = json.loads(body)
    self.assertEqual(r.status, 200)
    self.assertEqual(value['route'], ROUTE)
    # Ended at 11:00; the last 5 minutes are segments 6..10 (segment 5 ended at 6:00).
    self.assertEqual([s['name'] for s in value['segments']], [f'{ROUTE}--{n}' for n in range(6, 11)])
    self.assertFalse(any(s['recording'] for s in value['segments']))
    marker = el.read_events(self.events_path)[0]
    self.assertEqual(marker['segments'], [f'{ROUTE}--{n}' for n in range(6, 11)])  # nothing comes after a parked route
    r, body = self.request('POST', '/v1/grab', {'minutes': 1})
    self.assertEqual([s['name'] for s in json.loads(body)['segments']], [f'{ROUTE}--10'])

  def test_grab_never_replaces_a_crash_marker_and_grabs_merge(self):
    self.drive()
    clock = Clock(BASE + 630)
    self.store(clock)
    impact = el.Recorder(self.root, self.events_path).record({'kind': 'impact', 't': 0.0, 'peak_g': 2.65, 'speed_mph': 7.6},
                                                             ROUTE, 10, 20.0, 1794269620000)
    self.request('POST', '/v1/grab', {'minutes': 1})
    self.assertEqual(json.loads((self.root / f'{ROUTE}--10' / 'event.json').read_text()), impact)
    self.assertEqual([e['kind'] for e in el.read_events(self.events_path)], ['grab', 'impact'])

    # Two grabs whose last segment is the same: one marker covering both.
    (self.root / f'{ROUTE}--9' / 'event.json').write_text(json.dumps(el.Recorder.marker(
      {'kind': 'grab'}, ROUTE, 9, 1.0, 1, [f'{ROUTE}--7', f'{ROUTE}--8', f'{ROUTE}--9'])))
    el.Recorder(self.root, None).write_marker(self.root / f'{ROUTE}--9', el.Recorder.marker(
      {'kind': 'grab'}, ROUTE, 9, 2.0, 2, [f'{ROUTE}--8', f'{ROUTE}--9', f'{ROUTE}--10']))
    merged = json.loads((self.root / f'{ROUTE}--9' / 'event.json').read_text())
    self.assertEqual((merged['id'], merged['segments']), (f'{ROUTE}--9--2', [f'{ROUTE}--{n}' for n in (7, 8, 9, 10)]))

  def test_bad_requests(self):
    self.store(Clock(BASE))
    r, _ = self.request('POST', '/v1/grab', {'minutes': 5})
    self.assertEqual(r.status, 404)  # nothing recorded yet
    self.drive()
    for minutes in (0, 16, '5', True, 2.5, None):
      r, _ = self.request('POST', '/v1/grab', {'minutes': minutes})
      self.assertEqual(r.status, 400, minutes)
    r, _ = self.request('POST', '/v1/grab', b'[]')
    self.assertEqual(r.status, 400)
    r, _ = self.request('POST', '/v1/grab', {'minutes': 5}, token='wrong')
    self.assertEqual(r.status, 401)
    self.assertFalse(any(el.is_locked(p) for p in self.root.iterdir()))
    self.assertEqual(el.read_events(self.events_path), [])

  def test_partial_read_is_a_snapshot_with_range_and_never_hashed_or_marked(self):
    self.drive()
    cam = f'{ROUTE}--10/fcamera.hevc'
    path = self.root / cam
    first = os.urandom(3 * 1024 * 1024 + 5)
    path.write_bytes(first)
    clock = Clock(BASE + 630)
    store = self.store(clock, onroad=True)
    store.budget.rate = 1024 * 1024  # 1 MiB/s: the 3 MiB read takes ~3 s while loggerd keeps writing

    result = {}
    def fetch():
      result['r'], result['body'] = self.request('GET', '/v1/files/' + cam)
    t = threading.Thread(target=fetch)
    t.start()
    time.sleep(0.5)
    more = os.urandom(2 * 1024 * 1024)
    with open(path, 'ab') as f:
      f.write(more)
    t.join(15)
    r = result['r']
    self.assertEqual((r.status, r.getheader('X-Comma-Partial'), r.getheader('Content-Length')), (200, '1', str(len(first))))
    self.assertEqual(result['body'], first)  # exactly the bytes written when it was opened
    etag = r.getheader('ETag')

    # The rest, later, with the same validator.
    r, body = self.request('GET', '/v1/files/' + cam, headers={'Range': f'bytes={len(first)}-', 'If-Range': etag})
    self.assertEqual((r.status, body, r.getheader('Content-Range')), (206, more, f'bytes {len(first)}-{len(first) + len(more) - 1}/*'))
    r, _ = self.request('GET', '/v1/files/' + cam, headers={'Range': f'bytes={len(first) + len(more)}-'})
    self.assertEqual(r.status, 416)  # nothing new yet

    # Logs stay closed-only; partial files are never hashed, cached or marked.
    r, _ = self.request('GET', f'/v1/files/{ROUTE}--10/qlog.zst')
    self.assertEqual(r.status, 404)
    r, _ = self.request('GET', '/v1/hash/' + cam)
    self.assertEqual(r.status, 404)
    self.assertEqual((store.hashes, store.partials), ({}, {}))
    r, body = self.request('POST', '/v1/backed-up', {'files': [{'path': cam, 'size': len(first) + len(more)}]})
    self.assertEqual(json.loads(body), {'marked': 0, 'rejected': 1})
    self.assertFalse(is_backed_up(path))
    self.assertNotIn(cam, [f['path'] for f in store.manifest()['files']])

    # Once loggerd closes the segment, the old validator still resumes the range, and a full read is hashed.
    for f in (self.root / f'{ROUTE}--10').glob('*.lock'):
      f.unlink()
    r, body = self.request('GET', '/v1/files/' + cam, headers={'Range': f'bytes={len(first)}-', 'If-Range': etag})
    self.assertEqual((r.status, r.getheader('X-Comma-Partial'), body), (206, None, more))
    store.budget.rate = device.ONROAD_RATE
    r, body = self.request('GET', '/v1/files/' + cam)
    self.assertEqual((r.status, body), (200, first + more))
    deadline = time.monotonic() + 5  # stored by the serving thread just after the last byte goes out
    while store.cached_digest(cam, os.stat(path)) is None and time.monotonic() < deadline:
      time.sleep(0.05)
    self.assertEqual(store.cached_digest(cam, os.stat(path)), hashlib.sha256(first + more).hexdigest())

  def test_grab_budget_for_ten_minutes(self):
    self.drive()
    clock = Clock(BASE + 630)
    store = self.store(clock, onroad=True)
    for n in (4, 10):
      (self.root / f'{ROUTE}--{n}' / 'ecamera.hevc').write_bytes(os.urandom(2 * 1024 * 1024))
    os.utime(self.root / f'{ROUTE}--4' / 'ecamera.hevc', (BASE + 300, BASE + 300))
    store.budget.rate = 64 * 1024  # a 2 MiB file would take 32 s on the shared budget
    r, body = self.request('GET', '/v1/status')
    self.assertFalse(json.loads(body)['grab_active'])
    self.assertIs(store.budget_for(f'{ROUTE}--10/ecamera.hevc'), store.budget)

    self.request('POST', '/v1/grab', {'minutes': 5})
    r, body = self.request('GET', '/v1/status')
    self.assertEqual((json.loads(body)['grab_active'], json.loads(body)['grab_rate']), (True, device.GRAB_RATE))
    self.assertIs(store.budget_for(f'{ROUTE}--10/ecamera.hevc'), store.grab_budget)
    self.assertIs(store.budget_for(f'{ROUTE}--11/ecamera.hevc'), store.grab_budget)  # the next one, when it comes
    self.assertIs(store.budget_for(f'{ROUTE}--4/ecamera.hevc'), store.budget)  # not part of the grab
    t = time.monotonic()
    r, body = self.request('GET', f'/v1/files/{ROUTE}--10/ecamera.hevc')
    self.assertEqual((r.status, len(body)), (200, 2 * 1024 * 1024))
    self.assertLess(time.monotonic() - t, 2)

    clock.mono_t += device.GRAB_WINDOW_S + 1
    self.assertIs(store.budget_for(f'{ROUTE}--10/ecamera.hevc'), store.budget)
    r, body = self.request('GET', '/v1/status')
    self.assertFalse(json.loads(body)['grab_active'])


if __name__ == '__main__':
  unittest.main()
