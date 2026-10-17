"""TLS file server for the phone. Disabled without external pairing configuration.

Read-only except for one write: an xattr marking a closed file as backed up (POST /v1/backed-up).
Only closed logger segments (no .lock files), bounded requests, no Param writes.
While driving, all reads (hashing and transfers) share a low byte-rate budget.
"""
import errno
import hashlib
import hmac
import json
import os
import ssl
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from .common import FILE, MAX_FILE, atomic_json


# qcamera.ts is small (~2 MB/min) and carries the microphone track when Record Audio is on.
SERVED = ('qlog.zst', 'fcamera.hevc', 'ecamera.hevc', 'qcamera.ts')
# Bytes/s shared by every read while driving. Both cameras record ~1.2 MB/s and each file is read twice
# (hash, then send), so this leaves room to catch up on a backlog.
ONROAD_RATE = 8 * 1024 * 1024
# Set by the phone (POST /v1/backed-up) once the homelab has verified a file. The deleter removes
# segments whose served files all carry it before touching footage that exists only on the comma.
BACKED_UP_ATTR = 'user.phone_relay.backed_up'
BACKED_UP_VALUE = b'1'
MAX_BACKED_UP = 5000
MAX_BODY = 1024 * 1024

if sys.platform == 'darwin':  # tests on a Mac; the comma is Linux and uses os.*xattr
    import ctypes
    _libc = ctypes.CDLL(None, use_errno=True)
    _libc.fsetxattr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    _libc.getxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    _libc.getxattr.restype = ctypes.c_ssize_t

    def _fsetxattr(fd, name, value):
        if _libc.fsetxattr(fd, name.encode(), value, len(value), 0, 0) == -1:
            e = ctypes.get_errno()
            raise OSError(e, os.strerror(e))

    def _getxattr(path, name):
        buf = ctypes.create_string_buffer(16)
        n = _libc.getxattr(os.fsencode(path), name.encode(), buf, 16, 0, 1)  # XATTR_NOFOLLOW
        if n == -1:
            e = ctypes.get_errno()
            if e == errno.ENOATTR:
                return None
            raise OSError(e, os.strerror(e))
        return buf.raw[:n]
else:
    def _fsetxattr(fd, name, value):
        os.setxattr(fd, name, value)

    def _getxattr(path, name):
        try:
            return os.getxattr(path, name, follow_symlinks=False)
        except OSError as e:
            if e.errno == errno.ENODATA:
                return None
            raise


def is_backed_up(path):
    try:
        return _getxattr(path, BACKED_UP_ATTR) == BACKED_UP_VALUE
    except OSError:
        return False


class Budget:
    """Token bucket shared across threads. No limit while parked."""
    def __init__(self, onroad, rate=ONROAD_RATE):
        self.onroad, self.rate = onroad, rate
        self.lock = threading.Lock()
        self.next = time.monotonic()
    def take(self, n):
        if not self.onroad():
            return
        with self.lock:
            now = time.monotonic()
            self.next = max(self.next, now) + n / self.rate
            wait = self.next - n / self.rate - now
        if wait > 0:
            time.sleep(wait)


class RecordingStore:
    def __init__(self, root, onroad=lambda: False, served=SERVED, cache=None):
        self.root = Path(root).resolve()
        self.budget = Budget(onroad)
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
        if not FILE.fullmatch(name) or name.split('/')[1] not in self.served:
            raise FileNotFoundError()
        segment, filename = name.split('/')
        directory = self._closed_dir(segment)
        fd = os.open(directory / filename, os.O_RDONLY | os.O_NOFOLLOW)
        f = os.fdopen(fd, 'rb')
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_FILE:
            f.close()
            raise FileNotFoundError()
        return f

    def _closed_dir(self, segment):
        directory = self.root / segment
        if directory.is_symlink() or (directory / '.lock').exists() or list(directory.glob('*.lock')):
            raise FileNotFoundError()
        return directory

    def mark_backed_up(self, entries):
        """Tag files the phone reports as verified on the homelab. Only exact, current, closed files."""
        if not isinstance(entries, list) or len(entries) > MAX_BACKED_UP:
            raise ValueError()
        marked = rejected = 0
        for entry in entries:
            try:
                if not isinstance(entry, dict) or type(entry.get('size')) is not int or not isinstance(entry.get('path'), str):
                    raise ValueError()
                name = entry['path']
                if not FILE.fullmatch(name) or name.split('/')[1] not in self.served:
                    raise ValueError()
                segment, filename = name.split('/')
                directory = self._closed_dir(segment)
                fd = os.open(directory / filename, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_size != entry['size'] or not 0 < info.st_size <= MAX_FILE:
                        raise ValueError()
                    _fsetxattr(fd, BACKED_UP_ATTR, BACKED_UP_VALUE)
                finally:
                    os.close(fd)
                marked += 1
            except (ValueError, OSError):
                rejected += 1
        return {'marked': marked, 'rejected': rejected}

    def backup_summary(self, segments):
        """Footage that exists only on the comma. One getxattr per served file, no reads."""
        unbacked_segments = unbacked_bytes = unbacked_files = backed_up_bytes = 0
        for directory in segments:
            missing = False
            for filename in self.served:
                path = directory / filename
                try:
                    size = path.lstat().st_size
                except OSError:
                    continue
                if is_backed_up(path):
                    backed_up_bytes += size
                else:
                    missing = True
                    unbacked_files += 1
                    unbacked_bytes += size
            unbacked_segments += missing
        return {'unbacked_segments': unbacked_segments, 'unbacked_files': unbacked_files,
                'unbacked_bytes': unbacked_bytes, 'backed_up_bytes': backed_up_bytes}

    def status(self):
        """Small summary for the phone's test mode. No file contents, no hashing."""
        segments = sorted((p for p in self.root.glob('*--*') if p.is_dir() and not p.is_symlink()),
                          key=lambda p: (p.name.rsplit('--', 1)[0], int(p.name.rsplit('--', 1)[1]) if p.name.rsplit('--', 1)[1].isdigit() else -1))
        newest = segments[-1] if segments else None
        recording = bool(newest and (list(newest.glob('*.lock')) or (newest / '.lock').exists()))
        usage = os.statvfs(self.root)
        return {'onroad': bool(self.budget.onroad()), 'recording': recording, 'segments': len(segments),
                'newest': newest.name if newest else None, 'newest_mtime': newest.stat().st_mtime if newest else None,
                'free_bytes': usage.f_bavail * usage.f_frsize, 'served': list(self.served),
                'onroad_rate': self.budget.rate, 'time': time.time(), **self.backup_summary(segments)}

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
                            self.budget.take(len(block))
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
    """TLS handshakes happen on the worker thread with a timeout, so a client that vanishes
    mid-handshake (e.g. Wi-Fi dropped) can never block accept() for everyone else."""
    daemon_threads = True
    def __init__(self, *args, tls=None, slots=4, **kwargs):
        self.slots = threading.BoundedSemaphore(slots)
        self.tls = tls
        self.last_activity = 0.0  # monotonic time of the last served request
        self.active = 0  # connections being served now
        super().__init__(*args, **kwargs)
    def finish_request(self, request, address):
        request.settimeout(20)
        if self.tls is not None:
            try:
                request = self.tls.wrap_socket(request, server_side=True)
            except (OSError, ValueError):
                request.close()
                return
        self.last_activity = time.monotonic()
        try:
            super().finish_request(request, address)
        finally:
            self.last_activity = time.monotonic()
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
        self.active += 1
        try:
            super().process_request_thread(request, address)
        finally:
            self.active -= 1
            self.slots.release()


def handler(store, token_hash, wifi_state=None, server_slots=None):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(30)
        def log_message(self, *_):
            pass
        def authorized(self):
            auth = self.headers.get('Authorization', '')
            if not auth.startswith('Bearer ') or not hmac.compare_digest(hashlib.sha256(auth[7:].encode()).hexdigest(), token_hash):
                self.send_error(401)
                return False
            return True
        def send_json(self, value):
            data = json.dumps(value).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)
        def do_POST(self):
            if not self.authorized():
                return
            try:
                if urlsplit(self.path).path != '/v1/backed-up':
                    self.send_error(404)
                    return
                length = int(self.headers.get('Content-Length', ''))
                if not 0 < length <= MAX_BODY:
                    self.close_connection = True
                    self.send_error(413)
                    return
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError()
                self.send_json(store.mark_backed_up(body.get('files')))
            except ValueError:  # includes malformed JSON and a missing Content-Length
                self.close_connection = True
                self.send_error(400)
            except (OSError, TimeoutError):
                self.close_connection = True
        def do_GET(self):
            if not self.authorized():
                return
            try:
                url = urlsplit(self.path)
                if url.path == '/v1/status':
                    value = store.status()
                    value['wifi'] = wifi_state() if wifi_state else None
                    value['active_connections'] = server_slots() if server_slots else None
                    self.send_json(value)
                elif url.path == '/v1/manifest':
                    offset = int(parse_qs(url.query).get('offset', ['0'])[0])
                    if not 0 <= offset <= 1000000:
                        raise ValueError()
                    self.send_json(store.manifest(offset))
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
                            block = f.read(min(1024*1024, remaining))
                            if not block:
                                return
                            self.wfile.write(block)
                            remaining -= len(block)
                            store.budget.take(len(block))
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
    def onroad():
        return not params.get_bool('IsOffroad')
    store = RecordingStore('/data/media/0/realdata', onroad, config.get('files', SERVED), '/data/phone-relay/hashes.json')
    state = {}
    server = BoundedServer(('0.0.0.0', 8743), None, tls=context)
    server.RequestHandlerClass = handler(store, config['token_sha256'], lambda: state.get('wifi'),
                                         lambda: server.active)
    hotspot = config.get('hotspot')
    if hotspot:  # optional: own Wi-Fi for the phone when no saved network is around
        from .hotspot import run as run_hotspot
        busy = lambda: time.monotonic() - server.last_activity < 120  # phone is still pulling footage
        threading.Thread(target=run_hotspot, args=(hotspot['ssid'], hotspot['psk'], onroad, threading.Event(), busy, state),
                         daemon=True).start()
    os.nice(19)
    os.system(f'ionice -c3 -p {os.getpid()} >/dev/null 2>&1')  # idle I/O class: never competes with loggerd
    server.serve_forever()


if __name__ == '__main__':
    main()
