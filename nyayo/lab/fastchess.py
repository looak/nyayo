"""Fastchess from the server. `manage.py fetch_fastchess <version>` downloads an official release's files from
Disservin/fastchess (AndyGrant's has no commits of its own over it) with their sha256 into `DATA_DIR/fastchess/<version>/`,
next to a `manifest.json`; `clientMatchRunnerVersionRef` names the file for a worker's system and architecture, and
`/clientFastchess/<version>/<file>` serves it. The worker checks the sha256 and runs it, and builds from source where there
is no file, or it will not run. Unapproved workers may fetch it: it is a public release and tells nothing of the server."""

import hashlib
import json
import re
import tarfile
import zipfile
from pathlib import Path

from django.conf import settings

from nyayo.lab import toolchains

RELEASES_API = 'https://api.github.com/repos/Disservin/fastchess'
FILE = re.compile(r'^[A-Za-z0-9._+-]+$')


def directory():
    return Path(settings.DATA_DIR) / 'fastchess'


def normalize_os(value):
    name = str(value or '').lower()
    if name.startswith('lin'):
        return 'linux'
    if name.startswith('win'):
        return 'windows'
    if name.startswith(('darwin', 'mac', 'osx')):
        return 'macos'
    return ''


def normalize_arch(value):
    name = str(value or '').lower().replace('_', '-')
    if name in ('x86-64', 'amd64', 'x64', 'x86-64-v2', 'x86-64-v3', 'x86-64-v4') or name.startswith('x86-64'):
        return 'x86-64'
    if 'arm64' in name or 'aarch64' in name or (name.startswith('m') and name[1:].isdigit()):   # m1, m2: Apple silicon
        return 'arm64'
    if 'riscv' in name:
        return 'riscv64'
    return ''


def classify(file_name):
    """(os, arch) of a release file by its name, '' for what it cannot tell: fastchess-linux-x86-64.tar, -windows-..."""
    name = file_name.lower().replace('_', '-')
    system = normalize_os(next((word for word in ('linux', 'windows', 'macos', 'darwin', 'mac') if word in name), ''))
    arch = normalize_arch(next((word for word in ('x86-64', 'amd64', 'aarch64', 'arm64', 'riscv64', 'riscv', 'm1') if word in name), ''))
    return system, arch


def manifests():
    """Every release that has been fetched, newest first: [{ version, files: [{ name, sha256, os, arch, size }] }]."""
    found = []
    for path in sorted(directory().glob('*/manifest.json')) if directory().is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get('files'), list):
            found.append(data)
    return sorted(found, key=lambda m: toolchains.version_tuple(str(m.get('version', '')).lstrip('v').split('-')[0]), reverse=True)


def binary_for(os_name, arch, minimum, base_url):
    """{ url, sha256, version, name } for the newest fetched release that has a file for this system and architecture and
    is at least `minimum`, or None."""
    system, cpu = normalize_os(os_name), normalize_arch(arch)
    if not system or not cpu:
        return None
    wanted = toolchains.version_tuple(str(minimum).split('-')[0])
    for release in manifests():
        version = str(release.get('version', ''))
        if toolchains.version_tuple(version.lstrip('v').split('-')[0]) < wanted:
            continue
        for file in release['files']:
            if file.get('os') == system and file.get('arch') == cpu and (directory() / version / file['name']).is_file():
                return {'url': '%s/clientFastchess/%s/%s' % (base_url.rstrip('/'), version, file['name']),
                        'sha256': file['sha256'], 'version': version, 'name': file['name']}
    return None


def path_of(version, name):
    """The file a route names, or None: only names the manifests list, so nothing is read from outside the directory."""
    if not FILE.match(version) or not FILE.match(name):
        return None
    for release in manifests():
        if release.get('version') == version and any(f.get('name') == name for f in release['files']):
            path = directory() / version / name
            return path if path.is_file() else None
    return None


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def extract_executable(path, archive_name):
    """(file name to serve, its bytes) for the `fastchess` executable in a .tar or .zip release file, or None when the file
    is not an archive or holds none. The name is the archive's without its extension, with .exe when the executable has it."""
    base = re.sub(r'\.(tar\.gz|tgz|tar\.xz|tar|zip)$', '', archive_name)
    wanted = re.compile(r'fastchess(\.exe)?')
    try:
        if archive_name.endswith('.zip'):
            with zipfile.ZipFile(path) as archive:
                member = next((n for n in archive.namelist() if wanted.fullmatch(n.rsplit('/', 1)[-1])), None)
                content, found = (archive.read(member), member) if member else (None, None)
        elif '.tar' in archive_name or archive_name.endswith('.tgz'):
            with tarfile.open(path) as archive:
                member = next((m for m in archive.getmembers() if m.isfile() and wanted.fullmatch(m.name.rsplit('/', 1)[-1])), None)
                content, found = (archive.extractfile(member).read(), member.name) if member else (None, None)
        else:
            return None
    except (OSError, tarfile.TarError, zipfile.BadZipFile, KeyError):
        return None
    if not content:
        return None
    served = base + ('.exe' if found.endswith('.exe') else '')
    return (served, content) if FILE.match(served) else None
