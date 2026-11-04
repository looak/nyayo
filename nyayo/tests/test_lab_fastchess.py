"""Toolchains (what a worker found, and what builds an engine) and fastchess from the server: the fetch command, the manifest,
the file offered for a system, and the route that serves it."""

import hashlib
import io
import json
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from nyayo.lab import fastchess, toolchains
from nyayo.lab.models import EngineConfig

from .test_lab_worker_api import Api

BUILD = {'compilers': ['g', 'clang'], 'cpuflags': ['POPCNT'], 'systems': ['Linux'], 'path': 'ob_build'}


class Toolchains(SimpleTestCase):
    def can(self, tools, build=BUILD):
        return toolchains.can_build({'toolchains': tools}, build)

    def test_g_is_g_plus_plus_or_gcc_and_clang_is_clang_or_clang_plus_plus(self):
        for tool in ('g++', 'gcc', 'clang', 'clang++'):
            self.assertEqual(self.can({tool: '13.3.0', 'make': '4.3'}), (True, []), tool)

    def test_make_is_needed_and_what_is_missing_is_named(self):
        self.assertEqual(self.can({'g++': '13.3.0'}), (False, ['Missing make']))
        self.assertEqual(self.can({'make': '4.3', 'g++': None}), (False, ["Missing ['g', 'clang']"]))

    def test_a_minimum_version_is_kept(self):
        build = {**BUILD, 'compilers': ['g++>=12']}
        self.assertTrue(self.can({'g++': '13.3.0', 'make': '4'}, build)[0])
        self.assertFalse(self.can({'g++': '11.4.0', 'make': '4'}, build)[0])

    def test_only_c_and_c_plus_plus_are_scheduled(self):
        build = {**BUILD, 'compilers': ['rustc', 'cargo']}
        ok, why = self.can({'rustc': '1.80.0', 'cargo': '1.80.0', 'make': '4.3'}, build)
        self.assertFalse(ok)
        self.assertIn('Only C and C++ are scheduled', why[0])
        self.assertFalse(self.can({'dotnet': '8.0.1', 'make': '4.3'}, {**BUILD, 'compilers': ['dotnet']})[0])

    def test_a_worker_that_has_not_described_itself_is_judged_by_what_it_scanned(self):
        self.assertEqual(toolchains.can_build({'compilers': {'Elephant': ['g++', '13']}}, BUILD), (True, []))
        self.assertFalse(toolchains.can_build({}, BUILD)[0])

    def test_the_compiler_for_the_page(self):
        self.assertEqual(toolchains.compiler_of({'toolchains': {'g++': '13.3.0', 'make': '4'}}, BUILD), 'g++ 13.3.0')
        self.assertEqual(toolchains.compiler_of({'toolchains': {'clang': '18.1.3'}}, BUILD), 'clang 18.1.3')
        self.assertEqual(toolchains.compiler_of({'toolchains': {}}, BUILD), '')
        self.assertEqual(toolchains.compiler_of({}, BUILD), '')


class Platforms(SimpleTestCase):
    def test_files_are_told_by_their_names(self):
        for name, expected in (('fastchess-linux-x86-64.tar', ('linux', 'x86-64')), ('fastchess-windows-x86-64.zip', ('windows', 'x86-64')),
                               ('fastchess-linux-aarch64.tar', ('linux', 'arm64')), ('fastchess-macos-m1.tar', ('macos', 'arm64')),
                               ('fastchess-linux-riscv64.tar', ('linux', 'riscv64')), ('source.zip', ('', ''))):
            self.assertEqual(fastchess.classify(name), expected, name)

    def test_a_workers_system_and_architecture_are_normalised(self):
        for given, expected in (('Linux', 'linux'), ('Windows', 'windows'), ('Darwin', 'macos'), ('plan9', '')):
            self.assertEqual(fastchess.normalize_os(given), expected)
        for given, expected in (('x86_64', 'x86-64'), ('AMD64', 'x86-64'), ('aarch64', 'arm64'), ('arm64', 'arm64'), ('riscv64', 'riscv64'), ('sparc', '')):
            self.assertEqual(fastchess.normalize_arch(given), expected)


class Release:
    """A fake of GitHub's releases API and its downloads, for urlopen."""

    def __init__(self, tag='v1.8.2-alpha', files=None, bad_digest=False, missing=False):
        self.tag, self.missing, self.requests = tag, missing, []
        self.files = files or {'fastchess-linux-x86-64.tar': b'linux binary', 'fastchess-windows-x86-64.zip': b'windows binary',
                               'fastchess-linux-riscv64.tar': b'riscv binary'}
        self.assets = [{'name': name, 'browser_download_url': 'https://download.test/' + name,
                        'digest': 'sha256:' + ('0' * 64 if bad_digest else hashlib.sha256(data).hexdigest())} for name, data in self.files.items()]

    def urlopen(self, request, timeout=None):
        import urllib.error
        url = request.full_url
        self.requests.append(url)
        if url.endswith('/releases/tags/' + self.tag) and not self.missing:
            return io.BytesIO(json.dumps({'tag_name': self.tag, 'assets': self.assets}).encode())
        if url.startswith('https://download.test/'):
            return io.BytesIO(self.files[url.rsplit('/', 1)[1]])
        raise urllib.error.HTTPError(url, 404, 'not found', {}, None)


class Fetching(TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.data, True)
        self.settings = override_settings(DATA_DIR=Path(self.data))
        self.settings.enable()
        self.addCleanup(self.settings.disable)

    def fetch(self, release, version='1.8.2-alpha'):
        with mock.patch('urllib.request.urlopen', release.urlopen):
            call_command('fetch_fastchess', version, stdout=io.StringIO())

    def test_a_release_is_fetched_with_its_checksums_into_the_data_directory(self):
        self.fetch(Release())
        manifest = json.loads((Path(self.data) / 'fastchess' / 'v1.8.2-alpha' / 'manifest.json').read_text())
        self.assertEqual(manifest['version'], 'v1.8.2-alpha')
        by_name = {f['name']: f for f in manifest['files']}
        self.assertEqual(by_name['fastchess-linux-x86-64.tar']['sha256'], hashlib.sha256(b'linux binary').hexdigest())
        self.assertEqual((by_name['fastchess-linux-x86-64.tar']['os'], by_name['fastchess-linux-x86-64.tar']['arch']), ('linux', 'x86-64'))
        self.assertEqual((Path(self.data) / 'fastchess' / 'v1.8.2-alpha' / 'fastchess-windows-x86-64.zip').read_bytes(), b'windows binary')

    def test_a_file_that_does_not_match_githubs_checksum_is_refused_and_not_kept(self):
        with self.assertRaises(CommandError):
            self.fetch(Release(bad_digest=True))
        self.assertFalse((Path(self.data) / 'fastchess' / 'v1.8.2-alpha' / 'manifest.json').exists())
        self.assertEqual(list((Path(self.data) / 'fastchess' / 'v1.8.2-alpha').glob('*.part')), [])

    def test_an_unknown_release_is_said(self):
        with self.assertRaisesMessage(CommandError, 'There is no release'):
            self.fetch(Release(missing=True))

    def test_the_tag_may_be_given_without_its_v(self):
        release = Release()
        self.fetch(release, '1.8.2-alpha')
        self.assertTrue(any(url.endswith('/tags/v1.8.2-alpha') for url in release.requests))


def tar_of(member='fastchess-v1/fastchess', content=b'ELF executable'):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as archive:
        for name, data in ((member, content), ('fastchess-v1/src/main.cpp', b'source')):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def zip_of(member='fastchess.exe', content=b'MZ executable'):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('src/main.cpp', b'source')
        archive.writestr(member, content)
    return buffer.getvalue()


class Archives(TestCase):
    def setUp(self):
        self.data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.data, True)
        settings = override_settings(DATA_DIR=Path(self.data))
        settings.enable()
        self.addCleanup(settings.disable)

    def test_the_executable_alone_is_kept_and_served_with_its_own_checksum(self):
        files = {'fastchess-linux-x86-64.tar': tar_of(), 'fastchess-windows-x86-64.zip': zip_of(), 'fastchess-linux-riscv64.tar': tar_of(member='fastchess')}
        with mock.patch('urllib.request.urlopen', Release(files=files).urlopen):
            call_command('fetch_fastchess', '1.8.2-alpha', stdout=io.StringIO())
        folder = Path(self.data) / 'fastchess' / 'v1.8.2-alpha'
        manifest = {f['name']: f for f in json.loads((folder / 'manifest.json').read_text())['files']}
        self.assertEqual(sorted(manifest), ['fastchess-linux-riscv64', 'fastchess-linux-x86-64', 'fastchess-windows-x86-64.exe'])
        self.assertEqual((folder / 'fastchess-linux-x86-64').read_bytes(), b'ELF executable')
        self.assertEqual(manifest['fastchess-linux-x86-64']['sha256'], hashlib.sha256(b'ELF executable').hexdigest())
        self.assertEqual((manifest['fastchess-windows-x86-64.exe']['os'], manifest['fastchess-windows-x86-64.exe']['arch']), ('windows', 'x86-64'))
        self.assertFalse((folder / 'fastchess-linux-x86-64.tar').exists())
        binary = fastchess.binary_for('Windows', 'AMD64', '1.8.1', 'http://nyayo')
        self.assertEqual((binary['name'], binary['sha256']), ('fastchess-windows-x86-64.exe', hashlib.sha256(b'MZ executable').hexdigest()))

    def test_an_archive_without_the_executable_or_no_archive_is_kept_as_it_came(self):
        files = {'fastchess-linux-x86-64.tar': tar_of(member='fastchess-v1/other'), 'fastchess-windows-x86-64.zip': b'not a zip'}
        with mock.patch('urllib.request.urlopen', Release(files=files).urlopen):
            call_command('fetch_fastchess', '1.8.2-alpha', stdout=io.StringIO())
        folder = Path(self.data) / 'fastchess' / 'v1.8.2-alpha'
        self.assertTrue((folder / 'fastchess-linux-x86-64.tar').exists())
        self.assertEqual((folder / 'fastchess-windows-x86-64.zip').read_bytes(), b'not a zip')


class Offering(Api):
    def setUp(self):
        super().setUp() if hasattr(super(), 'setUp') else None
        self.data = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.data, True)
        self.settings = override_settings(DATA_DIR=Path(self.data))
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        with mock.patch('urllib.request.urlopen', Release().urlopen):
            call_command('fetch_fastchess', '1.8.2-alpha', stdout=io.StringIO())

    def ask(self, os_name='Linux', arch='x86_64'):
        return self.client.get('/clientMatchRunnerVersionRef/', {'os_name': os_name, 'arch': arch}).json()

    def test_the_file_for_a_system_is_named_with_its_checksum_and_a_route(self):
        binary = self.ask()['binary']
        self.assertEqual(binary['version'], 'v1.8.2-alpha')
        self.assertEqual(binary['sha256'], hashlib.sha256(b'linux binary').hexdigest())
        self.assertTrue(binary['url'].endswith('/clientFastchess/v1.8.2-alpha/fastchess-linux-x86-64.tar'))
        self.assertEqual(self.ask('Windows', 'AMD64')['binary']['name'], 'fastchess-windows-x86-64.zip')
        self.assertEqual(self.ask('Linux', 'riscv64')['binary']['name'], 'fastchess-linux-riscv64.tar')

    def test_a_system_with_no_file_is_not_offered_one_and_the_build_stays_the_fallback(self):
        for os_name, arch in (('Darwin', 'arm64'), ('Linux', 'aarch64'), ('Linux', ''), ('', 'x86_64')):
            answer = self.ask(os_name, arch)
            self.assertNotIn('binary', answer)
            self.assertIn('fastchess_repo_url', answer)

    @override_settings(NYAYO_FASTCHESS_MIN_VERSION='1.9.0')
    def test_a_release_older_than_the_minimum_is_not_offered(self):
        self.assertNotIn('binary', self.ask())

    def test_the_file_is_served_to_any_worker_and_only_what_the_manifest_lists(self):
        body = self.client.get('/clientFastchess/v1.8.2-alpha/fastchess-linux-x86-64.tar')
        self.assertEqual((body.status_code, b''.join(body.streaming_content)), (200, b'linux binary'))
        for path in ('v1.8.2-alpha/nope.tar', 'v9/fastchess-linux-x86-64.tar', 'v1.8.2-alpha/manifest.json', '..%2F..%2Fnyayo.sqlite3/x'):
            self.assertEqual(self.client.get('/clientFastchess/' + path).status_code, 404, path)

    def test_a_pending_worker_may_fetch_it(self):
        self.sign_in(approve=False)
        self.assertEqual(self.client.get('/clientFastchess/v1.8.2-alpha/fastchess-linux-x86-64.tar').status_code, 200)

    def test_the_newest_of_several_releases_is_the_one_offered(self):
        with mock.patch('urllib.request.urlopen', Release('v1.9.0-alpha', {'fastchess-linux-x86-64.tar': b'newer'}).urlopen):
            call_command('fetch_fastchess', '1.9.0-alpha', stdout=io.StringIO())
        self.assertEqual(self.ask()['binary']['version'], 'v1.9.0-alpha')


class Scheduling(Api):
    def test_a_workers_toolchains_decide_whether_it_supports_an_engine(self):
        self.sign_in(toolchains={'clang++': '18.1.3', 'make': '4.3'})
        self.assertEqual(Machine_info()['supported'], ['Elephant'])
        EngineConfig.objects.filter(name='Elephant').update(build_compilers='rustc cargo')
        self.sign_in(toolchains={'rustc': '1.80.0', 'cargo': '1.80.0', 'make': '4.3'})
        self.assertEqual(Machine_info()['supported'], [])


def Machine_info():
    from nyayo.lab.models import Machine
    return Machine.objects.get().info
