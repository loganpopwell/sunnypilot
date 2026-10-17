"""POST /v1/backed-up and the status backup counts. Stdlib only."""
import hashlib
import http.client
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

from openpilot.sunnypilot.phone_relay.device import BACKED_UP_ATTR, MAX_BACKED_UP, RecordingStore, handler, is_backed_up

TOKEN = 'synthetic-test-token-not-a-real-credential'
SEG = '00000004--0ac3964c96--3'


class BackedUpTest(unittest.TestCase):
  def setUp(self):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    self.root = Path(tmp.name) / 'realdata'
    self.store = RecordingStore(self.root)
    server = HTTPServer(('127.0.0.1', 0), handler(self.store, hashlib.sha256(TOKEN.encode()).hexdigest()))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    def stop():
      server.shutdown()
      server.server_close()
      t.join()
    self.addCleanup(stop)
    self.base = f'http://127.0.0.1:{server.server_port}'

  def file(self, name, data=b'0123456789'):
    path = self.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path

  def post(self, body, token=TOKEN, raw=None):
    data = raw if raw is not None else json.dumps(body).encode()
    req = urllib.request.Request(self.base + '/v1/backed-up', data=data, method='POST',
                                 headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req) as r:
      return json.load(r)

  def status(self):
    req = urllib.request.Request(self.base + '/v1/status', headers={'Authorization': 'Bearer ' + TOKEN})
    with urllib.request.urlopen(req) as r:
      return json.load(r)

  def test_marks_exact_files_and_is_idempotent(self):
    qlog = self.file(SEG + '/qlog.zst')
    cam = self.file(SEG + '/fcamera.hevc', b'x' * 100)
    body = {'files': [{'path': SEG + '/qlog.zst', 'size': 10}, {'path': SEG + '/fcamera.hevc', 'size': 100}]}
    self.assertEqual(self.post(body), {'marked': 2, 'rejected': 0})
    self.assertTrue(is_backed_up(qlog) and is_backed_up(cam))
    self.assertEqual(self.post(body), {'marked': 2, 'rejected': 0})
    self.assertEqual(self.store.manifest()['files'][0]['size'] in (10, 100), True)  # marking leaves files readable

  def test_rejects_anything_not_exact_and_closed(self):
    self.file(SEG + '/qlog.zst')
    self.file(SEG + '/rlog.zst')
    locked = '00000004--0ac3964c96--4'
    self.file(locked + '/qlog.zst')
    (self.root / locked / 'rlog.lock').touch()
    link = '00000004--0ac3964c96--5'
    (self.root / link).mkdir()
    (self.root / link / 'qlog.zst').symlink_to(self.root / SEG / 'qlog.zst')
    bad = [
      {'path': SEG + '/qlog.zst', 'size': 11},            # size changed
      {'path': SEG + '/qlog.zst', 'size': '10'},          # wrong type
      {'path': SEG + '/rlog.zst', 'size': 10},            # not a served file
      {'path': SEG + '/../' + SEG + '/qlog.zst', 'size': 10},
      {'path': '/etc/passwd', 'size': 10},
      {'path': SEG + '/ecamera.hevc', 'size': 10},        # missing
      {'path': locked + '/qlog.zst', 'size': 10},         # still recording
      {'path': link + '/qlog.zst', 'size': 10},           # symlink
      'not an object',
    ]
    self.assertEqual(self.post({'files': bad}), {'marked': 0, 'rejected': len(bad)})
    self.assertFalse(is_backed_up(self.root / SEG / 'qlog.zst'))
    self.assertFalse(is_backed_up(self.root / locked / 'qlog.zst'))

  def test_auth_and_bad_bodies(self):
    for body in ({'files': []}, {'files': [{'path': SEG + '/qlog.zst', 'size': 10}] * 2000}):
      with self.assertRaises(urllib.error.HTTPError) as e:  # a status, not a reset, even with a large body
        self.post(body, token='wrong')
      self.assertEqual(e.exception.code, 401)
    for raw in (b'{', b'[]', b'{"files": 3}', json.dumps({'files': [{}] * (MAX_BACKED_UP + 1)}).encode()):
      with self.assertRaises(urllib.error.HTTPError) as e:
        self.post(None, raw=raw)
      self.assertEqual(e.exception.code, 400)
    # Oversized bodies are refused from the header alone, before anything is read.
    conn = http.client.HTTPConnection('127.0.0.1', int(self.base.rsplit(':', 1)[1]), timeout=5)
    conn.putrequest('POST', '/v1/backed-up')
    conn.putheader('Authorization', 'Bearer ' + TOKEN)
    conn.putheader('Content-Length', str(1024 * 1024 + 1))
    conn.endheaders()
    self.assertEqual(conn.getresponse().status, 413)
    conn.close()

  def test_status_counts_unbacked_footage(self):
    self.file(SEG + '/qlog.zst')
    self.file(SEG + '/qcamera.ts', b'y' * 20)
    self.file(SEG + '/rlog.zst', b'z' * 1000)  # not served, not counted
    self.file('00000004--0ac3964c96--4/qlog.zst', b'q' * 5)
    self.post({'files': [{'path': SEG + '/qlog.zst', 'size': 10}, {'path': SEG + '/qcamera.ts', 'size': 20}]})
    value = self.status()
    self.assertEqual((value['unbacked_segments'], value['unbacked_files'], value['unbacked_bytes'], value['backed_up_bytes']),
                     (1, 1, 5, 30))
    self.assertEqual(BACKED_UP_ATTR, 'user.phone_relay.backed_up')


if __name__ == '__main__':
  unittest.main()
