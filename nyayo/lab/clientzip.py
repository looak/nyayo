"""The client, as nyayo serves it: a zip of `nyayo/client/` (OpenBench's worker, with its registration and its update from here),
built on request and kept while the files do not change, at `/client.zip` with no sign-in. A worker downloads it to start
(`docs/deploy.md`, *Workers register themselves*) and again to update itself when the version goes up.

The version is the `CLIENT_VERSION` of `nyayo/client/worker.py`, so the server and the client it hands out cannot disagree;
`NYAYO_CLIENT_VERSION` overrides it, and `NYAYO_CLIENT_DIR` points at another copy of the client (the smoke test serves one it
can change)."""

import hashlib
import io
import os
import re
import zipfile
from pathlib import Path

from django.conf import settings

BUNDLED = Path(__file__).resolve().parent.parent / 'client'

# What is not part of the client when it is handed out
SKIP_DIRS = {'__pycache__', 'tests', '.pytest_cache'}
SKIP_NAMES = {'.gitattributes', 'worker.token'}
SKIP_SUFFIXES = {'.pyc', '.pyo'}
STAMP = (2026, 1, 1, 0, 0, 0)    # a fixed time in every entry, so the same files are the same zip, and the same checksum

_cache = {}


def directory():
    return Path(getattr(settings, 'NYAYO_CLIENT_DIR', None) or os.environ.get('NYAYO_CLIENT_DIR') or BUNDLED)


def files(root=None):
    """The client's files, by their path inside the zip, in order."""
    root = Path(root or directory())
    found = []
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if (path.is_file() and not (set(relative.parts[:-1]) & SKIP_DIRS) and path.name not in SKIP_NAMES
                and path.suffix not in SKIP_SUFFIXES):
            found.append((relative.as_posix(), path))
    return found


_versions = {}


def version(root=None):
    """The `CLIENT_VERSION` of the worker that is handed out, or None when there is none to read. Asked on every call of a
    worker, so the file is read again only when its time or size has changed."""
    path = Path(root or directory()) / 'worker.py'
    try:
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
        if _versions.get(path, (None,))[0] != key:
            found = re.search(r'^CLIENT_VERSION\s*=\s*(\d+)', path.read_text(encoding='utf-8'), re.M)
            _versions[path] = (key, int(found.group(1)) if found else None)
        return _versions[path][1]
    except OSError:
        return None


def build(root=None):
    """(zip bytes, their sha256), kept while the files' sizes and times stay the same."""
    listed = files(root)
    key = tuple((name, path.stat().st_mtime_ns, path.stat().st_size) for name, path in listed)
    if _cache.get('key') == key:
        return _cache['zip'], _cache['sha256']
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, path in listed:
            info = zipfile.ZipInfo(name, STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if name.endswith(('.sh', 'client.py')) else 0o644) << 16
            archive.writestr(info, path.read_bytes())
    data = buffer.getvalue()
    _cache.update(key=key, zip=data, sha256=hashlib.sha256(data).hexdigest())
    return data, _cache['sha256']
