"""The client nyayo hands out: its zip, the version it carries, the route that serves it without a sign-in, the page that says how
to start a worker, and (where its requirements are installed) the client's own tests, which are pytest's, in tests/client/."""

import hashlib
import importlib.util
import io
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings

from nyayo.lab import clientzip, config


class Zip(SimpleTestCase):
    def names(self):
        data, _ = clientzip.build()
        return sorted(zipfile.ZipFile(io.BytesIO(data)).namelist())

    def test_it_holds_the_client_and_nothing_that_is_not_the_clients(self):
        names = self.names()
        for needed in ('client.py', 'worker.py', 'utils.py', 'bench.py', 'genfens.py', 'isa_detector.py', 'pgn_util.py', 'requirements.txt'):
            self.assertIn(needed, names)
        self.assertEqual([n for n in names if '/' in n or n.endswith(('.pyc', '.token', '.gitattributes'))], [])

    def test_it_is_the_same_zip_every_time(self):
        first, second = clientzip.build(), clientzip.build()
        self.assertEqual(first, second)
        self.assertEqual(first[1], hashlib.sha256(first[0]).hexdigest())

    def test_it_is_the_files_as_they_are(self):
        data, _ = clientzip.build()
        archive = zipfile.ZipFile(io.BytesIO(data))
        for name in ('client.py', 'worker.py', 'requirements.txt'):
            self.assertEqual(archive.read(name), (clientzip.BUNDLED / name).read_bytes())
        self.assertNotIn(b'\r\n', archive.read('client.py'))

    def test_the_version_is_the_workers_and_54_is_what_it_still_is(self):
        self.assertEqual(clientzip.version(), 54)
        self.assertEqual(config.client_version(), 54)

    @override_settings(NYAYO_CLIENT_VERSION=60)
    def test_a_setting_overrides_it(self):
        self.assertEqual(config.client_version(), 60)

    def test_another_copy_may_be_handed_out_and_a_new_version_is_a_new_zip(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder, True)
        (folder / 'worker.py').write_text('CLIENT_VERSION   = 54 # a comment\n')
        (folder / 'client.py').write_text('print(1)\n')
        (folder / 'worker.token').write_text('never handed out')
        (folder / '__pycache__').mkdir()
        (folder / '__pycache__' / 'x.pyc').write_bytes(b'x')
        (folder / 'tests').mkdir()
        (folder / 'tests' / 't.py').write_text('x')
        with override_settings(NYAYO_CLIENT_DIR=str(folder)):
            first = clientzip.build()
            self.assertEqual(sorted(zipfile.ZipFile(io.BytesIO(first[0])).namelist()), ['client.py', 'worker.py'])
            self.assertEqual(config.client_version(), 54)
            (folder / 'worker.py').write_text('CLIENT_VERSION   = 55 # a comment\n')
            second = clientzip.build()
            self.assertEqual(config.client_version(), 55)
            self.assertNotEqual(first[1], second[1])

    def test_a_directory_with_no_worker_has_no_version(self):
        self.assertIsNone(clientzip.version(tempfile.gettempdir() + '/nowhere-at-all'))


class Serving(TestCase):
    def test_it_is_served_to_anyone_with_its_checksum(self):
        reply = self.client.get('/client.zip')
        self.assertEqual((reply.status_code, reply['Content-Type']), (200, 'application/zip'))
        data = reply.content
        self.assertEqual(hashlib.sha256(data).hexdigest(), clientzip.build()[1])
        self.assertEqual(reply['ETag'], '"%s"' % clientzip.build()[1])
        self.assertIn('worker.py', zipfile.ZipFile(io.BytesIO(data)).namelist())

    def test_clientversionref_points_at_it_with_the_checksum_of_the_same_bytes(self):
        answer = self.client.post('/clientVersionRef/').json()
        self.assertEqual(answer['client_url'], 'http://testserver/client.zip')
        self.assertEqual(answer['client_sha256'], hashlib.sha256(self.client.get('/client.zip').content).hexdigest())

    def test_the_page_says_how_to_start_one_with_this_servers_address(self):
        page = self.client.get('/worker').content.decode()
        for expected in ('curl -fLo client.zip http://testserver/client.zip', 'python3 -m zipfile -e client.zip nyayo-worker',
                         'pip install -r nyayo-worker/requirements.txt', 'client.py --server http://testserver', '--join CODE'):
            self.assertIn(expected, page)
        self.assertNotIn('password', page.lower())

    def test_the_box_can_be_put_on_another_page(self):
        from django.template.loader import render_to_string
        text = render_to_string('lab/_get_a_worker.html', {'server': 'http://bobby:8001'})
        self.assertIn('--server http://bobby:8001', text)


def pytest_ready():
    return all(importlib.util.find_spec(name) for name in ('pytest', 'requests', 'psutil', 'cpuinfo', 'chess'))


class ClientsOwnTests(SimpleTestCase):
    """The tests that moved with the client are pytest's. They run here when pytest and the client's requirements are
    installed (pip install -r nyayo/client/requirements.txt pytest), and are skipped, not failed, when they are not."""

    def test_pytest_on_tests_client(self):
        if not pytest_ready():
            self.skipTest('pytest and the client requirements are not installed')
        folder = Path(__file__).resolve().parent / 'client'
        run = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', str(folder)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout[-3000:] + run.stderr[-1000:])


class OnTheMachinesPage(TestCase):
    def test_the_box_is_on_the_list_with_this_servers_address(self):
        page = self.client.get('/machines').content.decode()
        self.assertIn('Get a worker going', page)
        self.assertIn('client.py --server http://testserver', page)
        self.assertEqual(self.client.post('/machine-join-code').status_code, 302)
