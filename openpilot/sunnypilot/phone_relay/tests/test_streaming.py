"""Driving behaviour of the relay: the manifest never reads files, downloads hash what they send,
and status/manifest stay reachable while downloads hold every slot. Stdlib only.

Reproduces the 2026-10-17 road test stall: onroad, the manifest hashed its 32 newest files inline at
the 8 MiB/s budget (minutes for two cameras), the phone timed out and retried, retries filled the
server's slots and /v1/status was refused.
"""
import hashlib
import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from openpilot.sunnypilot.phone_relay import device
from openpilot.sunnypilot.phone_relay.device import BoundedServer, RecordingStore, handler

TOKEN = 'synthetic-test-token-not-a-real-credential'
SEG = '00000004--0ac3964c96--{}'


class StreamingTest(unittest.TestCase):
  def setUp(self):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    self.dir = Path(tmp.name)
    self.root = self.dir / 'realdata'
    self.data = {}

  def file(self, name, size):
    path = self.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    data = os.urandom(size)
    path.write_bytes(data)
    self.data[name] = data
    return name

  def start(self, store, **kwargs):
    server = BoundedServer(('127.0.0.1', 0), handler(store, hashlib.sha256(TOKEN.encode()).hexdigest()), **kwargs)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    def stop():
      server.shutdown()
      server.server_close()
    self.addCleanup(stop)
    self.server = server
    return server.server_address[1]

  def get(self, port, path, headers=None, timeout=5):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    c.request('GET', path, headers={'Authorization': 'Bearer ' + TOKEN, **(headers or {})})
    r = c.getresponse()
    body = r.read()
    c.close()
    return r, body

  def test_manifest_is_immediate_while_driving(self):
    # Old behaviour: 32 newest files hashed inline at the onroad budget. Here the budget is so slow
    # that reading even one 4 MiB file would take minutes; the manifest must not touch contents.
    for i in range(10):
      for f in ('qlog.zst', 'qcamera.ts', 'fcamera.hevc', 'ecamera.hevc'):
        self.file(SEG.format(i) + '/' + f, 4 * 1024 * 1024 if f.endswith('hevc') else 1024)
    store = RecordingStore(self.root, onroad=lambda: True)
    store.budget.rate = 1024  # bytes/s
    port = self.start(store)
    t = time.monotonic()
    r, body = self.get(port, '/v1/manifest?offset=0')
    self.assertLess(time.monotonic() - t, 1)
    page = json.loads(body)
    self.assertEqual((r.status, page['version'], len(page['files']), page['next']), (200, 2, 32, 32))
    self.assertTrue(all('sha256' not in f and f['size'] > 0 for f in page['files']))
    self.assertEqual(page['files'][0]['path'], SEG.format(9) + '/qlog.zst')  # newest first
    r, body = self.get(port, '/v1/status')
    self.assertEqual((json.loads(body)['hashed_files'], json.loads(body)['unhashed_files']), (0, 40))

  def test_download_hashes_what_it_sends_and_caches_it(self):
    name = self.file(SEG.format(1) + '/fcamera.hevc', 3 * 1024 * 1024 + 17)
    cache = self.dir / 'hashes.json'
    store = RecordingStore(self.root, cache=cache)
    port = self.start(store)
    r, body = self.get(port, '/v1/files/' + name)
    self.assertEqual(body, self.data[name])
    self.assertIsNone(r.getheader('X-Content-SHA256'))  # not known before the first full send
    want = hashlib.sha256(self.data[name]).hexdigest()
    # Known now without reading the file again: /v1/hash, the manifest and the next download's header.
    with unittest.mock.patch.object(store, 'budget') as budget:
      r, body = self.get(port, '/v1/hash/' + name)
      self.assertEqual(budget.take.call_count, 0)
    self.assertEqual(json.loads(body)['sha256'], want)
    self.assertEqual(store.manifest()['files'][0]['sha256'], want)
    r, _ = self.get(port, '/v1/files/' + name, {'Range': 'bytes=0-9'})
    self.assertEqual(r.getheader('X-Content-SHA256'), want)
    # Survives a restart (the serving thread writes the cache just after the last byte goes out).
    deadline = time.monotonic() + 5
    while name not in (json.loads(cache.read_text()) if cache.exists() else {}) and time.monotonic() < deadline:
      time.sleep(0.05)
    self.assertEqual(RecordingStore(self.root, cache=cache).manifest()['files'][0]['sha256'], want)

  def test_resumed_download_continues_the_hash(self):
    name = self.file(SEG.format(2) + '/ecamera.hevc', 5 * 1024 * 1024 + 3)
    store = RecordingStore(self.root)
    port = self.start(store)
    # First attempt stops part way; the resume overlaps bytes the server already counted.
    r, body = self.get(port, '/v1/files/' + name, {'Range': 'bytes=0-2500000'})
    self.assertEqual(body, self.data[name][:2500001])
    r, body = self.get(port, '/v1/files/' + name, {'Range': 'bytes=1000000-'})
    self.assertEqual((r.status, body), (206, self.data[name][1000000:]))
    info = os.stat(self.root / name)
    self.assertEqual(store.cached_digest(name, info), hashlib.sha256(self.data[name]).hexdigest())

  def test_hash_falls_back_to_reading_when_no_stream_covered_it(self):
    name = self.file(SEG.format(3) + '/qlog.zst', 2 * 1024 * 1024 + 5)
    store = RecordingStore(self.root)
    port = self.start(store)
    self.get(port, '/v1/files/' + name, {'Range': 'bytes=100-'})  # a resume after a restart: no state
    self.assertIsNone(store.cached_digest(name, os.stat(self.root / name)))
    r, body = self.get(port, '/v1/hash/' + name)
    self.assertEqual(json.loads(body)['sha256'], hashlib.sha256(self.data[name]).hexdigest())
    r, _ = self.get(port, '/v1/hash/' + SEG.format(3) + '/rlog.zst')
    self.assertEqual(r.status, 404)

  def test_status_and_manifest_reachable_while_downloads_hold_every_slot(self):
    names = [self.file(SEG.format(i) + '/fcamera.hevc', 8 * 1024 * 1024) for i in range(device.DOWNLOAD_SLOTS + 1)]
    store = RecordingStore(self.root, onroad=lambda: True)
    store.budget.rate = 2 * 1024 * 1024  # each 1 MiB block waits; downloads stay open for seconds
    port = self.start(store)
    held = []
    for name in names[:device.DOWNLOAD_SLOTS]:
      c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
      c.request('GET', '/v1/files/' + name, headers={'Authorization': 'Bearer ' + TOKEN})
      held.append(c)
    deadline = time.monotonic() + 5
    while self.server.downloads_active < device.DOWNLOAD_SLOTS and time.monotonic() < deadline:
      time.sleep(0.05)
    self.assertEqual(self.server.downloads_active, device.DOWNLOAD_SLOTS)
    r, _ = self.get(port, '/v1/files/' + names[-1])
    self.assertEqual((r.status, r.getheader('Retry-After')), (503, '5'))  # refused cleanly, not reset
    for path in ('/v1/status', '/v1/manifest?offset=0', '/v1/status'):
      t = time.monotonic()
      r, body = self.get(port, path)
      self.assertEqual(r.status, 200)
      self.assertLess(time.monotonic() - t, 1)
    self.assertEqual(json.loads(body)['downloads_active'], device.DOWNLOAD_SLOTS)
    # The phone gives up on its downloads: their threads stop and the slots come back.
    for c in held:
      c.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b'\x01\x00\x00\x00\x00\x00\x00\x00')  # RST on close
      c.close()
    deadline = time.monotonic() + 8
    while self.server.downloads_active and time.monotonic() < deadline:
      time.sleep(0.05)
    self.assertEqual(self.server.downloads_active, 0)
    r, _ = self.get(port, '/v1/files/' + names[-1], {'Range': 'bytes=0-0'})
    self.assertEqual(r.status, 206)

  def test_hash_stops_when_client_leaves(self):
    name = self.file(SEG.format(4) + '/fcamera.hevc', 16 * 1024 * 1024)
    store = RecordingStore(self.root, onroad=lambda: True)
    store.budget.rate = 2 * 1024 * 1024  # a full read would take 8 s
    port = self.start(store)
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    c.request('GET', '/v1/hash/' + name, headers={'Authorization': 'Bearer ' + TOKEN})
    time.sleep(1)
    self.assertEqual(self.server.downloads_active, 1)
    c.close()
    deadline = time.monotonic() + 3
    while self.server.downloads_active and time.monotonic() < deadline:
      time.sleep(0.05)
    self.assertEqual(self.server.downloads_active, 0)
    # What was read is kept, so the next request only reads the rest.
    self.assertEqual(len(store.partials), 1)
    self.assertGreater(next(iter(store.partials.values())).offset, 0)


if __name__ == '__main__':
  unittest.main()
