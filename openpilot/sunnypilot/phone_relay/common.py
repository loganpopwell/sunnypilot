"""Bounded, versioned phone relay protocol. No driving-control dependencies."""
import hashlib
import json
import os
import re
from pathlib import Path

CHUNK = 8 * 1024 * 1024
MAX_FILE = 2 * 1024**3
FILE = re.compile(r'^((?:\d{4}-\d{2}-\d{2}--\d{2}-\d{2}-\d{2}|[a-f0-9]{8}--[a-f0-9]{10})--\d+)/(qlog\.zst|rlog\.zst|qcamera\.ts|[fed]camera\.hevc|fcamera\.mp4)$')
HEX = re.compile(r'^[a-f0-9]{64}$')


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def descriptor(value):
    if not isinstance(value, dict) or not FILE.fullmatch(value.get('path', '')):
        raise ValueError('Invalid recording path')
    if type(value.get('size')) is not int or not 0 < value['size'] <= MAX_FILE:
        raise ValueError('Invalid recording size')
    if not HEX.fullmatch(value.get('sha256', '')):
        raise ValueError('Invalid checksum')
    item = {k: value[k] for k in ('path', 'size', 'sha256')}
    if isinstance(value.get('mtime'), (int, float)):
        item['mtime'] = float(value['mtime'])
    return item


def identity(device, item):
    return hashlib.sha256((device + '/' + item['path'] + '/' + item['sha256']).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        os.chmod(tmp, 0o600)
        json.dump(value, f, separators=(',', ':'))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
