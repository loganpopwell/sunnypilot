"""Read-only TLS file server. Disabled without external pairing configuration.

Only closed logger segments, only offroad, bounded requests, no Param writes.
"""
import hashlib
import hmac
import json
import os
import ssl
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from .common import FILE, MAX_FILE, atomic_json


SERVED = ('qlog.zst', 'fcamera.hevc')


class RecordingStore:
    def __init__(self, root, offroad, served=SERVED, cache=None):
        self.root = Path(root).resolve()
        self.offroad = offroad
        self.served = tuple(served)
        # Hashes keyed by path and (inode, size, mtime) so each file is read once.
        self.cache_path = Path(cache) if cache else None
        self.hashes = {}
        if self.cache_path and self.cache_path.exists():
            try:
                self.hashes = json.loads(self.cache_path.read_text())
            except ValueError:
                self.hashes = {}

    def open_file(self, name):
        if not self.offroad():
            raise PermissionError('Park and switch off driving session to sync')
        if not FILE.fullmatch(name) or name.split('/')[1] not in self.served:
            raise FileNotFoundError()
        segment, filename = name.split('/')
        directory = self.root / segment
        if directory.is_symlink() or (directory / '.lock').exists() or list(directory.glob('*.lock')):
            raise FileNotFoundError()
        fd = os.open(directory / filename, os.O_RDONLY | os.O_NOFOLLOW)
        f = os.fdopen(fd, 'rb')
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_FILE:
            f.close()
            raise FileNotFoundError()
        return f

    def manifest(self, offset=0):
        # Pagination is stable for a stationary recording directory; repeated scans dedupe by hash.
        priority = {'qlog.zst': 0, 'qcamera.ts': 1, 'rlog.zst': 2, 'fcamera.hevc': 3, 'ecamera.hevc': 4, 'dcamera.hevc': 5}
        def order(name):
            segment, filename = name.split('/')
            route, number = segment.rsplit('--', 1)
            return route, int(number), -priority[filename]
        names = sorted((n for n in (str(p.relative_to(self.root)) for p in self.root.glob('*/*'))
                        if FILE.fullmatch(n) and n.split('/')[1] in self.served), key=order, reverse=True)
        batch = names[offset:offset + 32]
        files = []
        changed = False
        for name in batch:
            try:
                with self.open_file(name) as f:
                    before = os.fstat(f.fileno())
                    stamp = [before.st_ino, before.st_size, before.st_mtime_ns]
                    cached = self.hashes.get(name)
                    if cached and cached[:3] == stamp:
                        sha = cached[3]
                    else:
                        h = hashlib.sha256()
                        for block in iter(lambda: f.read(1024 * 1024), b''):
                            if not self.offroad():
                                raise PermissionError()
                            h.update(block)
                        after = os.fstat(f.fileno())
                        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                            continue
                        sha = h.hexdigest()
                        self.hashes[name] = stamp + [sha]
                        changed = True
                    files.append({'path': name, 'size': before.st_size, 'sha256': sha, 'mtime': before.st_mtime_ns / 1e9})
            except (FileNotFoundError, OSError):
                continue
        if changed and self.cache_path:
            live = set(names)
            self.hashes = {k: v for k, v in self.hashes.items() if k in live}
            atomic_json(self.cache_path, self.hashes)
        return {'version': 1, 'files': files, 'next': offset + 32 if offset + 32 < len(names) else None}


class BoundedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(2)
        super().__init__(*args, **kwargs)
    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise
    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


def handler(store, token_hash):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(30)
        def log_message(self, *_):
            pass
        def do_GET(self):
            auth = self.headers.get('Authorization', '')
            if not auth.startswith('Bearer ') or not hmac.compare_digest(hashlib.sha256(auth[7:].encode()).hexdigest(), token_hash):
                self.send_error(401)
                return
            try:
                url = urlsplit(self.path)
                if url.path == '/v1/manifest':
                    if not store.offroad():
                        raise PermissionError()
                    offset = int(parse_qs(url.query).get('offset', ['0'])[0])
                    if not 0 <= offset <= 1000000:
                        raise ValueError()
                    data = json.dumps(store.manifest(offset)).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(data)
                elif url.path.startswith('/v1/files/'):
                    with store.open_file(unquote(url.path[len('/v1/files/'):])) as f:
                        info = os.fstat(f.fileno())
                        size = info.st_size
                        etag = '"' + hashlib.sha256(f'{info.st_ino}:{size}:{info.st_mtime_ns}'.encode()).hexdigest() + '"'
                        start, end = 0, size - 1
                        status = 200
                        value = self.headers.get('Range')
                        if self.headers.get('If-Range', etag) != etag:
                            value = None
                        if value:
                            import re
                            match = re.fullmatch(r'bytes=(\d+)-(\d*)', value)
                            if not match:
                                raise ValueError()
                            start = int(match[1]); end = min(int(match[2]), size-1) if match[2] else size-1
                            if start > end:
                                self.send_error(416); return
                            status = 206
                        self.send_response(status)
                        self.send_header('Content-Type', 'application/octet-stream')
                        self.send_header('Content-Length', str(end-start+1))
                        self.send_header('Accept-Ranges', 'bytes')
                        self.send_header('ETag', etag)
                        self.send_header('Cache-Control', 'no-store')
                        if status == 206:
                            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                        self.end_headers()
                        f.seek(start)
                        remaining = end-start+1
                        while remaining:
                            if not store.offroad():
                                self.close_connection = True
                                return
                            block = f.read(min(256*1024, remaining))
                            if not block:
                                return
                            self.wfile.write(block)
                            remaining -= len(block)
                            time.sleep(0.004)  # Yield briefly between 256 KiB blocks (~60 MiB/s cap); still offroad-only.
                else:
                    self.send_error(404)
            except PermissionError:
                self.send_error(423)
            except (ValueError, KeyError):
                self.send_error(400)
            except FileNotFoundError:
                self.send_error(404)
            except (OSError, TimeoutError):
                self.close_connection = True
    return Handler


def main():
    config_path = Path('/data/phone-relay/config.json')
    if not config_path.exists():
        return
    config = json.loads(config_path.read_text())
    # No implicit TLS fallback. Pairing material lives outside the updated checkout.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain('/data/phone-relay/cert.pem', '/data/phone-relay/key.pem')
    from openpilot.common.params import Params
    params = Params()
    def offroad():
        return params.get_bool('IsOffroad')
    store = RecordingStore('/data/media/0/realdata', offroad, config.get('files', SERVED), '/data/phone-relay/hashes.json')
    os.nice(15)
    server = BoundedServer(('0.0.0.0', 8743), handler(store, config['token_sha256']))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
