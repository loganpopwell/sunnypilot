"""TLS file server for the phone. Disabled without external pairing configuration.

Read-only except for one write: an xattr marking a closed file as backed up (POST /v1/backed-up).
Only closed logger segments (no .lock files), bounded requests, no Param writes.
While driving, all reads share a low byte-rate budget. Files are never read just to hash them while
listing: a file's SHA-256 is computed from the bytes as they are sent to the phone, kept per resumed
download, and cached on disk once the whole file has gone out. GET /v1/hash/<path> returns it.
"""
import errno
import hashlib
import hmac
import json
import os
import select
import socket
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
# Bytes/s shared by every read while driving. Both cameras record ~1.3 MB/s together and each file is
# read once (hashed while it is sent), so this leaves room to catch up on a backlog.
ONROAD_RATE = 8 * 1024 * 1024
# Requests that read file contents (downloads, /v1/hash) share these. Status, manifest and
# /v1/backed-up have their own, so they always get through while downloads are busy.
DOWNLOAD_SLOTS = 4
CONTROL_SLOTS = 4
CONNECTIONS = 12  # open connections, including ones still in the TLS handshake
PAGE = 32
PARTIAL_HASHES = 32  # interrupted downloads whose running hash is kept for the resume
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


class StreamHash:
    """Running SHA-256 of a file, built from the bytes sent to the phone. Covers [0, offset)."""
    def __init__(self, h=None, offset=0):
        self.h, self.offset = h or hashlib.sha256(), offset
    def feed(self, pos, block):
        # A resumed download may resend bytes already hashed; only new bytes extend the state.
        if pos <= self.offset < pos + len(block):
            self.h.update(memoryview(block)[self.offset - pos:])
            self.offset = pos + len(block)


def stamp(info):
    return [info.st_ino, info.st_size, info.st_mtime_ns]


class RecordingStore:
    def __init__(self, root, onroad=lambda: False, served=SERVED, cache=None):
        self.root = Path(root).resolve()
        self.budget = Budget(onroad)
        self.served = tuple(served)
        # Digests keyed by path, valid while (inode, size, mtime) match: {path: [ino, size, mtime_ns, sha256]}.
        self.cache_path = Path(cache) if cache else None
        self.lock = threading.Lock()
        self.save_lock = threading.Lock()
        self.hashes = {}
        self.partials = {}  # (path, ino, size, mtime_ns) -> StreamHash of an unfinished download
        if self.cache_path and self.cache_path.exists():
            try:
                self.hashes = json.loads(self.cache_path.read_text())
            except ValueError:
                self.hashes = {}

    def names(self):
        """Served files, newest segment first."""
        priority = {'qlog.zst': 0, 'qcamera.ts': 1, 'rlog.zst': 2, 'fcamera.hevc': 3, 'ecamera.hevc': 4, 'dcamera.hevc': 5}
        def order(name):
            segment, filename = name.split('/')
            route, number = segment.rsplit('--', 1)
            return route, int(number), -priority[filename]
        return sorted((n for n in (str(p.relative_to(self.root)) for p in self.root.glob('*/*'))
                       if FILE.fullmatch(n) and n.split('/')[1] in self.served), key=order, reverse=True)

    def cached_digest(self, name, info):
        with self.lock:
            cached = self.hashes.get(name)
        return cached[3] if cached and cached[:3] == stamp(info) else None

    def _store_digest(self, name, info, sha):
        with self.lock:
            self.hashes[name] = stamp(info) + [sha]
            self.partials.pop((name, *stamp(info)), None)
        if not self.cache_path:
            return
        with self.save_lock:  # one writer; the snapshot is taken inside so the newest state wins
            live = set(self.names())
            with self.lock:
                self.hashes = {k: v for k, v in self.hashes.items() if k in live}
                snapshot = dict(self.hashes)
            atomic_json(self.cache_path, snapshot)

    def begin_stream(self, name, info, start):
        """Hash state for a download starting at byte `start`, or None when it can't help."""
        if self.cached_digest(name, info):
            return None
        key = (name, *stamp(info))
        with self.lock:
            partial = self.partials.get(key)
            if partial is not None and partial.offset >= start:
                return self.partials.pop(key)  # this stream owns it now
        return StreamHash() if start == 0 and partial is None else None

    def end_stream(self, name, info, state):
        if state is None:
            return
        if state.offset >= info.st_size:
            self._store_digest(name, info, state.h.hexdigest())
            return
        key = (name, *stamp(info))
        with self.lock:
            kept = self.partials.get(key)
            if kept is None or kept.offset < state.offset:
                self.partials.pop(key, None)
                self.partials[key] = state
            while len(self.partials) > PARTIAL_HASHES:
                self.partials.pop(next(iter(self.partials)))

    def digest(self, name, client_gone=lambda: False):
        """SHA-256 of a closed file: cached, else finished from an interrupted download's state,
        else read in full (within the budget). Stops early if the client goes away."""
        with self.open_file(name) as f:
            info = os.fstat(f.fileno())
            sha = self.cached_digest(name, info)
            if sha:
                return info, sha
            key = (name, *stamp(info))
            with self.lock:
                state = self.partials.pop(key, None) or StreamHash()
            try:
                f.seek(state.offset)
                for block in iter(lambda: f.read(1024 * 1024), b''):
                    if client_gone():
                        raise TimeoutError()
                    self.budget.take(len(block))
                    state.feed(state.offset, block)
                if state.offset != info.st_size or stamp(os.fstat(f.fileno())) != stamp(info):
                    raise FileNotFoundError()  # changed underneath us; closed segments never do
            except BaseException:
                self.end_stream(name, info, state)
                raise
            sha = state.h.hexdigest()
            self._store_digest(name, info, sha)
            return info, sha

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
        with self.lock:
            hashed = set(self.hashes)
            in_progress = len(self.partials)
        hashed_files = unhashed_files = 0
        for directory in segments:
            missing = False
            for filename in self.served:
                path = directory / filename
                try:
                    size = path.lstat().st_size
                except OSError:
                    continue
                if f'{directory.name}/{filename}' in hashed:
                    hashed_files += 1
                else:
                    unhashed_files += 1
                if is_backed_up(path):
                    backed_up_bytes += size
                else:
                    missing = True
                    unbacked_files += 1
                    unbacked_bytes += size
            unbacked_segments += missing
        return {'unbacked_segments': unbacked_segments, 'unbacked_files': unbacked_files,
                'unbacked_bytes': unbacked_bytes, 'backed_up_bytes': backed_up_bytes,
                # Digest progress: files with a cached SHA-256, without one, and downloads holding a partial one.
                'hashed_files': hashed_files, 'unhashed_files': unhashed_files, 'hashes_in_progress': in_progress}

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
        """Path, size and mtime of each closed served file; sha256 only when already known. Never reads
        file contents, so it answers immediately even while driving. Pagination is stable for a
        stationary recording directory; repeated scans dedupe by path."""
        names = self.names()
        files = []
        for name in names[offset:offset + PAGE]:
            try:
                with self.open_file(name) as f:
                    info = os.fstat(f.fileno())
            except OSError:
                continue
            item = {'path': name, 'size': info.st_size, 'mtime': info.st_mtime_ns / 1e9}
            sha = self.cached_digest(name, info)
            if sha:
                item['sha256'] = sha
            files.append(item)
        return {'version': 2, 'files': files, 'next': offset + PAGE if offset + PAGE < len(names) else None}


class BoundedServer(ThreadingMixIn, HTTPServer):
    """TLS handshakes happen on the worker thread with a timeout, so a client that vanishes
    mid-handshake (e.g. Wi-Fi dropped) can never block accept() for everyone else. Downloads and
    control requests have separate slots so the phone can always reach status and the manifest."""
    daemon_threads = True
    def __init__(self, *args, tls=None, slots=DOWNLOAD_SLOTS, control=CONTROL_SLOTS, connections=CONNECTIONS, **kwargs):
        self.connections = threading.BoundedSemaphore(connections)
        self.download_slots = threading.BoundedSemaphore(slots)
        self.control_slots = threading.BoundedSemaphore(control)
        self.tls = tls
        self.last_activity = 0.0  # monotonic time of the last served request
        self.active = 0  # connections being served now
        self.downloads_active = 0
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
        if not self.connections.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.connections.release()
            raise
    def process_request_thread(self, request, address):
        self.active += 1
        try:
            super().process_request_thread(request, address)
        finally:
            self.active -= 1
            self.connections.release()


def handler(store, token_hash, wifi_state=None, server_slots=None):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(30)
        def log_message(self, *_):
            pass
        def slot(self, download):
            """Take a download or control slot; None (after a 503) when there is none."""
            server = self.server
            sem = getattr(server, 'download_slots' if download else 'control_slots', None)
            if sem is None:
                return lambda: None
            if not (sem.acquire(blocking=False) if download else sem.acquire(timeout=5)):
                self.send_response(503)
                self.send_header('Retry-After', '5')
                self.send_header('Content-Length', '0')
                self.end_headers()
                self.close_connection = True
                return None
            if download:
                server.downloads_active += 1
            def release():
                if download:
                    server.downloads_active -= 1
                sem.release()
            return release
        def client_gone(self):
            """True once the client has closed the connection (it sends nothing after its request)."""
            sock = self.connection
            try:
                if not select.select([sock], [], [], 0)[0]:
                    return False
                timeout = sock.gettimeout()
                sock.settimeout(0.05)
                try:
                    return sock.recv(1) == b''
                finally:
                    sock.settimeout(timeout)
            except (ssl.SSLWantReadError, socket.timeout, BlockingIOError):
                return False
            except OSError:
                return True
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
            # The (bounded) body is read before answering, even a 401, so the client sees the
            # status instead of a reset connection.
            try:
                length = int(self.headers.get('Content-Length', ''))
                if not 0 < length <= MAX_BODY:
                    self.close_connection = True
                    self.send_error(413 if length > MAX_BODY else 400)
                    return
                raw = self.rfile.read(length)
                if not self.authorized():
                    return
                if urlsplit(self.path).path != '/v1/backed-up':
                    self.send_error(404)
                    return
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ValueError()
                release = self.slot(download=False)
                if release is None:
                    return
                try:
                    self.send_json(store.mark_backed_up(body.get('files')))
                finally:
                    release()
            except ValueError:  # includes malformed JSON and a missing Content-Length
                self.close_connection = True
                self.send_error(400)
            except (OSError, TimeoutError):
                self.close_connection = True
        def do_GET(self):
            if not self.authorized():
                return
            url = urlsplit(self.path)
            release = self.slot(download=url.path.startswith(('/v1/files/', '/v1/hash/')))
            if release is None:
                return
            try:
                self.get(url)
            finally:
                release()
        def get(self, url):
            try:
                if url.path == '/v1/status':
                    value = store.status()
                    value['wifi'] = wifi_state() if wifi_state else None
                    value['active_connections'] = server_slots() if server_slots else None
                    value['downloads_active'] = getattr(self.server, 'downloads_active', None)
                    self.send_json(value)
                elif url.path == '/v1/manifest':
                    offset = int(parse_qs(url.query).get('offset', ['0'])[0])
                    if not 0 <= offset <= 1000000:
                        raise ValueError()
                    self.send_json(store.manifest(offset))
                elif url.path.startswith('/v1/hash/'):
                    name = unquote(url.path[len('/v1/hash/'):])
                    info, sha = store.digest(name, self.client_gone)
                    self.send_json({'path': name, 'size': info.st_size, 'mtime': info.st_mtime_ns / 1e9, 'sha256': sha})
                elif url.path.startswith('/v1/files/'):
                    name = unquote(url.path[len('/v1/files/'):])
                    with store.open_file(name) as f:
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
                        known = store.cached_digest(name, info)
                        if known:
                            self.send_header('X-Content-SHA256', known)  # whole file, also for ranges
                        self.end_headers()
                        state = store.begin_stream(name, info, start)
                        try:
                            f.seek(start)
                            pos, remaining = start, end-start+1
                            while remaining:
                                block = f.read(min(1024*1024, remaining))
                                if not block:
                                    return
                                self.wfile.write(block)  # raises once the client is gone
                                if state is not None:
                                    state.feed(pos, block)  # counted only after it was handed to the socket
                                pos += len(block)
                                remaining -= len(block)
                                store.budget.take(len(block))
                        finally:
                            store.end_stream(name, info, state)
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
