#!/usr/bin/env python3
"""The smoke test: nyayo as it will run, OpenBench's own worker playing its tests, and every verdict the toy engine can stage.

Linux or WSL only. One server, no recorder or second app between: gunicorn (4 workers of 4 threads, SQLite in WAL) and the
worker that puts games away, on a port and a data directory of their own. A toy engine and a book are served the way GitHub
serves archives, with a stand-in for the part of GitHub's API that the create page asks (NYAYO_GITHUB_API), so a test is
made through the real create page, signed in as the owner. The worker is nyayo's own client (nyayo/client), downloaded from
the server's /client.zip as a worker does, with no checkout of openbench_eg and no password; its timers are shortened in the
copy so a scenario takes seconds.

    python3 -m venv ~/.venvs/nyayo-smoke && ~/.venvs/nyayo-smoke/bin/pip install -e . gunicorn requests psutil py-cpuinfo
    ~/.venvs/nyayo-smoke/bin/python tools/smoke/run.py [--only NAME ...] [--load]

It needs make and g++ (the worker builds the toy engine, and fastchess when it has none: keep a built one at
~/.cache/openbench-smoke/fastchess-ob), and prints how long a full run took. The toy engine, the book and the file
server come from openbench_eg's Scripts/smoke (da629d7), which is OpenBench's, GPL-3.0 as the rest of it.
"""

import argparse
import collections
import hashlib
import math
import os
import platform
import re
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import files  # noqa: E402

ENGINE_NAME, ENGINE_NPS, ENGINE_BENCH = 'smoke', 1000000, 1000000
SOURCE = 'https://github.com/smoke/engine'
INSTANCE = 'smoke'
OWNER, HELPER, PASSWORD = 'owner', 'helper', 'smoke-password'    # people who sign in to the pages; a worker has no password
CLIENT_TIMERS = { 'TIMEOUT_ERROR' : 1, 'TIMEOUT_WORKLOAD' : 1, 'REPORT_INTERVAL' : 2 }
RETRY = 2          # seconds a worker waits before it tries a test again after a verdict that isn't the engine's
BENCH_LIMIT = 3    # seconds before a bench is over its time (Client/bench.py's 60)
SPRT_OPEN = { 'test_mode' : 'SPRT', 'bounds' : '[0.00, 5.00]', 'confidence' : '[0.000001, 0.000001]' }

RIG_SETTINGS = '''
import os, time
from nyayo.settings import *

STEP_RETRY_SECONDS = %(retry)d

# Count what is written: statements, and the write transactions they make (a write outside a transaction is one)
from django.db.backends import utils as _utils
from django.db.backends.base import base as _base

_log = open(os.environ['RIG_WRITES'], 'a', buffering=1)
_execute, _commit = _utils.CursorWrapper.execute, _base.BaseDatabaseWrapper.commit

def _counted(self, sql, params=None):
    if str(sql).lstrip()[:7].upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE')):
        _log.write('%%.3f %%d stmt\\n' %% (time.time(), os.getpid()))
        if self.db.in_atomic_block:
            self.db._rig_dirty = True
        else:
            _log.write('%%.3f %%d tx\\n' %% (time.time(), os.getpid()))
    return _execute(self, sql, params)

def _committed(self):
    if getattr(self, '_rig_dirty', False):
        _log.write('%%.3f %%d tx\\n' %% (time.time(), os.getpid()))
        self._rig_dirty = False
    return _commit(self)

_utils.CursorWrapper.execute = _counted
_base.BaseDatabaseWrapper.commit = _committed
'''


class Failure(Exception):
    pass


def check(condition, message):
    if not condition:
        raise Failure(message)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def stop(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (subprocess.TimeoutExpired, ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)


def preflight():
    missing = [what for what, found in (('Linux (run it in WSL)', platform.system() == 'Linux'), ('make', shutil.which('make')),
                                        ('g++', shutil.which('g++'))) if not found]
    if missing:
        raise Failure('the smoke test needs: %s' % (', '.join(missing)))


class Person:

    # Someone signed in to the pages over HTTP, as a browser is
    def __init__(self, url, username):
        self.url, self.session = url, requests.Session()
        self.session.get(url + '/sign-in', timeout=10)
        reply = self.session.post(url + '/sign-in', data={ 'username' : username, 'password' : PASSWORD, 'next' : '/',
                                  'csrfmiddlewaretoken' : self.session.cookies['csrftoken'] }, allow_redirects=False, timeout=10)
        check(reply.status_code == 302, '%s could not sign in: %d' % (username, reply.status_code))

    def post(self, path, **data):
        data['csrfmiddlewaretoken'] = self.session.cookies['csrftoken']
        return self.session.post(self.url + path, data=data, allow_redirects=False, timeout=30)

    def create(self, **form):
        # (test id, None), or (None, what the page said) when it refused
        reply = self.post('/create/test', **form)
        if reply.status_code == 302:
            return int(reply.headers['Location'].rsplit('-', 1)[1]), None
        said = re.findall(r'<li>([^<]+)</li>', (re.search(r'(?s)<div class="errors">.*?</div>', reply.text) or [''])[0])
        return None, '%d: %s' % (reply.status_code, ' | '.join(said) or 'no error shown')

    def act(self, test_id, action):
        return self.post('/tests/%d/%s' % (test_id, action)).status_code


class Rig:

    def __init__(self, args):
        self.args = args
        self.temp = Path(tempfile.mkdtemp(prefix='nyayo-smoke-'))
        self.port = free_port()
        self.url = 'http://127.0.0.1:%d' % (self.port)
        self.procs, self.clients, self.extras, self.nonce, self.branches = [], [], [], str(time.time()), 0
        self.file_server = files.FileServer().__enter__()
        (self.temp / 'rigsettings.py').write_text(RIG_SETTINGS % { 'retry' : RETRY })
        # the client nyayo hands out: a copy the server serves, so a scenario can raise its version
        self.served = self.temp / 'served'
        shutil.copytree(REPO / 'nyayo' / 'client', self.served, ignore=shutil.ignore_patterns('__pycache__', 'tests', '.gitattributes'))
        self.env = { k : v for k, v in os.environ.items() if not k.startswith('NYAYO_') }
        self.env.update(
            DJANGO_SETTINGS_MODULE='rigsettings', PYTHONPATH=os.pathsep.join([str(self.temp), str(REPO)]),
            NYAYO_DATA_DIR=str(self.temp / 'data'), NYAYO_SECRET_KEY='smoke', NYAYO_INSTANCE=INSTANCE,
            NYAYO_GITHUB_API=self.file_server.url + '/repos', NYAYO_CLIENT_DIR=str(self.served), NYAYO_ALLOWED_HOSTS='127.0.0.1,localhost',
            RIG_WRITES=str(self.temp / 'writes.log'), PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1')

    # nyayo

    def run(self, *argv, **kw):
        return subprocess.run([sys.executable, *argv], cwd=REPO, env=self.env, check=True, capture_output=True, text=True, **kw)

    def start_nyayo(self):
        self.run('manage.py', 'migrate', '-v0')
        self.run('manage.py', 'collectstatic', '--noinput', '-v0')    # static files have hashed names when deployed
        os.environ.update(self.env)
        sys.path[:0] = [str(self.temp), str(REPO)]
        import django
        django.setup()
        self.seed()
        for name, argv in (
                ('server', ['-m', 'gunicorn', 'nyayo.wsgi', '--workers', '4', '--worker-class', 'gthread', '--threads', '4',
                            '--bind', '127.0.0.1:%d' % (self.port), '--access-logfile', str(self.temp / 'access.log'),
                            '--access-logformat', '%(r)s %(s)s %(L)s']),
                ('worker', ['manage.py', 'worker'])):
            log = open(self.temp / ('%s.log' % (name)), 'w')
            self.procs.append(subprocess.Popen([sys.executable, *argv], cwd=REPO, env=self.env, stdout=log,
                                               stderr=subprocess.STDOUT, start_new_session=True))
        self.wait_for(lambda: self.get('/healthz') == 200, 30, 'nyayo to answer')
        self.owner, self.helper = Person(self.url, OWNER), Person(self.url, HELPER)

    def seed(self):
        # What the deploy checklist does by hand: the people, the engine and the book
        from django.contrib.auth.models import User
        from nyayo.lab.models import Book, EngineConfig
        User.objects.create_superuser(OWNER, password=PASSWORD)
        User.objects.create_user(HELPER, password=PASSWORD, is_staff=True)
        EngineConfig.objects.all().delete()
        EngineConfig.objects.create(
            name=ENGINE_NAME, nps=ENGINE_NPS, source=SOURCE, build_path='.', build_compilers='g++', build_cpuflags='',
            build_systems='Linux', presets={ 'test_presets' : { 'default' : {} } })
        Book.objects.all().delete()
        Book.objects.create(name=files.BOOK_NAME, sha=files.book_sha(), source=self.file_server.add('/books/smoke.zip', files.book_zip()))

    def get(self, path, timeout=10):
        try:
            return requests.get(self.url + path, timeout=timeout).status_code
        except requests.RequestException:
            return 0

    def page(self, path):
        reply = requests.get(self.url + path, timeout=10)
        check(reply.status_code == 200, '%s answered %d' % (path, reply.status_code))
        return reply.text

    def logged(self, what):
        # How many times the server's logs say what
        return sum(1 for log in ('server.log', 'worker.log') for line in (self.temp / log).read_text(errors='replace').splitlines() if what in line)

    # the worker

    def prepare_client(self, number):
        # What a worker does to start: downloads the client from the server (no GitHub, no checkout) and unpacks it. Its
        # timers are then shortened, and a built fastchess is put beside it, if one is kept
        import io
        import zipfile
        import requests
        client = self.temp / ('Client%d' % (number))
        reply = requests.get(self.url + '/client.zip', timeout=30)
        check(reply.status_code == 200, '/client.zip answered %d' % (reply.status_code))
        zipfile.ZipFile(io.BytesIO(reply.content)).extractall(client)
        worker = (client / 'worker.py').read_text()
        for name, value in CLIENT_TIMERS.items():
            worker, count = re.subn(r'^(%s\s*=\s*)\d+' % (name), r'\g<1>%d' % (value), worker, flags=re.M)
            check(count == 1, 'worker.py has no %s to shorten' % (name))
        worker, count = re.subn(r'results_queue\.get\(timeout=5\)', 'results_queue.get(timeout=0.5)', worker)
        check(count == 1, 'worker.py has no results_queue wait to shorten')
        (client / 'worker.py').write_text(worker)
        bench = (client / 'bench.py').read_text()
        bench, count = re.subn(r'^(MAX_BENCH_TIME_SECONDS\s*=\s*)\d+', r'\g<1>%d' % (BENCH_LIMIT), bench, flags=re.M)
        check(count == 1, 'bench.py has no MAX_BENCH_TIME_SECONDS to shorten')
        (client / 'bench.py').write_text(bench)
        cache = Path.home() / '.cache' / 'openbench-smoke' / 'fastchess-ob'
        if cache.is_file():
            shutil.copy2(cache, client / 'fastchess-ob')
        return client

    def start_client(self, number, name=None, join=None, extra=False, updates=False):
        # No username and no password: the worker says hello with its name (and a join code, if it has one), is given a
        # token, which it keeps in worker.token beside client.py, and waits to be approved
        client = self.temp / ('Client%d' % (number))
        env = dict(os.environ, PYTHONUNBUFFERED='1', PYTHON=sys.executable,
                   PATH=os.path.dirname(sys.executable) + os.pathsep + os.environ['PATH'])
        command = [sys.executable, '-u', str(client / 'client.py'), '-S', self.url, '-T', '1', '-N', '1', '-I', name or 'smoke-%d' % (number),
                   ] + ([] if updates else ['--no-client-downloads']) + (['--join', join] if join else [])
        log = open(self.temp / ('client%d.log' % (number)), 'a')
        process = subprocess.Popen(command, cwd=client, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        (self.extras if extra else self.clients).append(process)
        return process

    def start_clients(self, one_at_a_time_for=None, approve=True):
        # one_at_a_time_for: a test id; each worker is started once the last has passed its walk on it, as the
        # first workloads of several workers at once make the toy engine fail to execute (a race in the machine)
        for number in range(self.args.workers):
            self.prepare_client(number)
            self.start_client(number)
            if approve:
                self.approve('smoke-%d' % (number))
            if one_at_a_time_for:
                from nyayo.lab.models import Step
                self.wait_for(lambda: Step.objects.filter(result__test_id=one_at_a_time_for, step='running', state='passed').count() > number,
                              240, 'worker %d to pass its walk' % (number))

    def machine(self, name):
        from nyayo.lab.models import Machine
        return Machine.objects.filter(name=name).first()

    def approve(self, name, timeout=90):
        # What the owner does on the Machines page, once the worker has said hello and described itself
        self.wait_for(lambda: (self.machine(name) or 0) and self.machine(name).described, timeout, '%s to say hello and describe itself' % (name))
        machine = self.machine(name)
        check(self.owner.post('/machine-action', id=machine.id, action='approve').status_code == 302, 'the owner could not approve %s' % (name))

    def wait_for(self, predicate, timeout, what):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for client in self.clients:
                if client.poll() is not None:
                    raise Failure('a client exited while waiting for %s' % (what))
            if predicate():
                return
            time.sleep(0.25)
        raise Failure('timed out after %ds waiting for %s' % (timeout, what))

    # tests

    def engine(self, label, bench=ENGINE_BENCH, broken=False, bench_mode='', typed=False):
        # A branch of the stand-in GitHub. Its commit message says what the bench is, unless typed: the page is
        # then told, as it has to be for a commit without a Bench: line
        self.branches += 1
        name = '%s-%d' % (label, self.branches)
        sha = hashlib.sha1(('%s %s' % (self.nonce, name)).encode()).hexdigest()
        message = 'Smoke %s' % (name) if typed else 'Smoke %s\n\nBench: %d' % (name, bench)
        self.file_server.add_branch('smoke/engine', name, sha, message, files.engine_zip('smoke-%s' % (sha), broken, bench_mode))
        return { 'branch' : name, 'sha' : sha, 'bench' : bench, 'typed' : typed }

    def form(self, dev=None, base=None, **overrides):
        dev, base = dev or self.engine('dev'), base or self.engine('base')
        form = {
            'engine' : ENGINE_NAME, 'dev_branch' : dev['branch'], 'base_branch' : base['branch'],
            'dev_bench' : str(dev['bench']) if dev['typed'] else '', 'base_bench' : str(base['bench']) if base['typed'] else '',
            'dev_options' : 'Threads=1 Hash=16', 'base_options' : 'Threads=1 Hash=16', 'dev_tc' : 'N=1000', 'base_tc' : 'N=1000',
            'test_mode' : 'GAMES', 'max_games' : '8', 'workload_size' : '2', 'priority' : '1', 'throughput' : '100',
            'book' : files.BOOK_NAME,
        }
        form.update(overrides)
        return form

    def create_test(self, who=None, dev=None, base=None, **overrides):
        # Through the create page; the test is reported from it and opened to the workers if its author may approve
        test_id, said = (who or self.owner).create(**self.form(dev, base, **overrides))
        check(test_id, 'the create page refused: %s' % (said))
        return test_id

    def test(self, test_id):
        from nyayo.lab.models import Test
        return Test.objects.get(id=test_id)

    def match(self, test_id):
        from nyayo.lab.ids import match_id
        from nyayo.matches.models import Match
        return Match.objects.get(pk=match_id(test_id))

    def step(self, test_id, step, side):
        from nyayo.lab.models import Step
        return Step.objects.filter(result__test_id=test_id, step=step, side=side).first()

    def stop_test(self, test_id):
        check(self.owner.act(test_id, 'stop') == 302, 'the stop button did not redirect')
        time.sleep(RETRY + 3)

    def settled(self, test_id, state, timeout=15):
        # The match is updated in the request that changed the test, so it is there as soon as the test is
        self.wait_for(lambda: self.match(test_id).state == state, timeout, 'the match of test %d to be %s' % (test_id, state))

    def verdict(self, test_id, timeout=20):
        # The match's verdict, which arrives in a report of its own, a moment after the state it explains
        self.wait_for(lambda: self.match(test_id).verdict, timeout, 'the verdict of test %d' % (test_id))
        return self.match(test_id).verdict

    def put_away(self, match, timeout=60, expect=0):
        # expect: the games to be in, as the uploads of a workload's last games may still be on their way
        from django.db.models import Sum
        from nyayo.matches.models import Upload
        total = lambda: Upload.objects.filter(match=match).aggregate(total=Sum('appended'))['total'] or 0
        self.wait_for(lambda: Upload.objects.filter(match=match).exists() and not Upload.objects.filter(match=match, state='pending').exists() and total() >= expect,
                      timeout, 'the games of %s to be put away' % (match.pk))
        check(not Upload.objects.filter(match=match, state='refused').exists(), 'an upload was refused: %s' % (
            list(Upload.objects.filter(match=match, state='refused').values_list('reason', flat=True))))
        return Upload.objects.filter(match=match).aggregate(total=Sum('appended'))['total'] or 0

    def agrees(self, test_id):
        # nyayo's match and the test it was reported from say the same of everything fixed, and of the counts
        from nyayo.lab.document import build_document
        document, match = build_document(self.test(test_id)), self.match(test_id)
        fixed = document['match']
        problems = [name for name in ('name', 'kind') if getattr(match, name) != fixed[name]]
        problems += ['settings'] if match.settings != fixed['settings'] else []
        problems += ['subject'] if match.subject.commit != document['subject']['commit'] else []
        result = match.results.get()
        problems += ['%s: %s, not %s' % (name, getattr(result, name), document['results'][0][name]) for name in ('games', 'wins', 'draws', 'losses')
                     if getattr(result, name) != document['results'][0][name]]
        problems += ['the sprt'] if document['sprt'] and (not match.sprt or abs(match.sprt['llr'] - document['sprt']['llr']) > 1e-9) else []
        check(not problems, 'nyayo disagrees with test %d on %s' % (test_id, ', '.join(problems)))

    def close(self, keep):
        for client in self.clients + self.extras:
            stop(client)
        for proc in self.procs:
            stop(proc)
        self.file_server.__exit__(None, None, None)
        if keep:
            print('Kept %s' % (self.temp))
        else:
            shutil.rmtree(self.temp, ignore_errors=True)


# Scenarios: each makes a test through the page, waits for what a real worker does with it, and checks nyayo's rows and pages

def passing_games(r):
    base = r.engine('base', typed=True)     # a commit without a Bench: line, as Elephant's main is: its bench is typed in
    test_id = r.create_test(base=base, name='Smoke: eight games', workload_size=1)
    check(r.match(test_id), 'the match is not there as the page returns')
    r.wait_for(lambda: r.test(test_id).finished, 90, 'test %d to finish' % (test_id))
    test = r.test(test_id)
    check(test.games == 8 and test.wins + test.draws + test.losses == 8, 'games: %s' % (test.games))
    check(test.LL + test.LD + test.DD + test.DW + test.WW == 4, 'the pentanomial is not half the games')
    check(test.base.bench == ENGINE_BENCH, 'the typed bench is %s' % (test.base.bench))
    r.settled(test_id, 'finished')
    r.agrees(test_id)
    match = r.match(test_id)
    check(match.name == 'Smoke: eight games', 'the match is called %r' % (match.name))
    from nyayo.lab.models import Step
    states = {(s.step, s.side): s.state for s in Step.objects.filter(result__test_id=test_id)}
    check(len(states) == 6 and set(states.values()) == { 'passed' }, 'the walk: %s' % (states))
    kept = r.put_away(match, expect=8)
    check(kept == 8, 'nyayo holds %d games, expected 8' % (kept))
    pages = [r.get(path) for path in ('/matches', '/matches/%s' % (match.pk), '/games')]
    check(pages == [200, 200, 200], 'pages answer %s' % (pages))
    page = r.page('/matches/%s' % (match.pk))
    check('Smoke: eight games' in page and '<dt>Ended</dt>' in page, 'the match page lacks its name or its end: %s' % (
        re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', re.sub(r'(?s)<style.*?</style>', '', page)))[:1200]))
    check(match.pk in r.page('/matches'), 'the match is not on the list')
    # the round trip, read from the kernel's TCP_INFO of the connection the worker's calls came in on, under gunicorn
    from nyayo.lab.models import Machine
    rtt = Machine.objects.filter(name='smoke-0').order_by('-id').first().rtt_ms
    check(rtt is not None and 0 < rtt < 1000, "the worker's round trip was not measured: %s" % (rtt))
    check('%s ms' % ('%.1f' % rtt if rtt < 10 else '%.0f' % rtt) in r.page('/machines'), 'the Machines page does not show %s ms' % (rtt))
    return 'test %d: 8 games, named, base bench typed, the walk passed, %d games put away, pages answer, round trip %.3f ms' % (test_id, kept, rtt)


def sprt_to_result(r):
    test_id = r.create_test(test_mode='SPRT', bounds='[0.00, 200.00]', confidence='[0.05, 0.05]', base_options='Threads=1 Hash=16 Strength=0')
    r.wait_for(lambda: r.test(test_id).finished, 120, 'SPRT test %d to finish' % (test_id))
    test = r.test(test_id)
    check(test.passed or test.failed, 'finished with neither a pass nor a fail')
    r.settled(test_id, 'finished')
    r.agrees(test_id)
    match = r.match(test_id)
    check(match.sprt['result'] == ('passed' if test.passed else 'failed'), 'sprt %s' % (match.sprt))
    return 'test %d: %s, LLR %.2f, %d games' % (test_id, 'passed' if test.passed else 'failed', test.currentllr, test.games)


def page_refuses(r):
    from nyayo.lab.models import Test
    before = Test.objects.count()
    for what, form, wanted in (
            ('a commit without a Bench: line', r.form(base=r.engine('base', typed=True), base_bench=''), 'Bench'),
            ('a branch GitHub has not got', r.form(dev_branch='nowhere'), 'could not be found'),
            ('a time control nobody can read', r.form(dev_tc='fast'), 'Time Control')):
        test_id, said = r.owner.create(**form)
        check(test_id is None and wanted in said, '%s: %s' % (what, said))
    check(Test.objects.count() == before, 'a refused form made a test')
    return 'the page refused a missing bench, an unknown branch and a bad time control, and made no test'


def approval(r):
    test_id = r.create_test(who=r.helper, max_games=4)
    match = r.match(test_id)
    check(match.state == 'waiting' and not r.test(test_id).approved, 'a helper\'s test is %s' % (match.state))
    time.sleep(RETRY + 3)
    check(r.test(test_id).games == 0, 'a test nobody approved was played')
    check(r.helper.act(test_id, 'approve') == 403, 'a helper may approve')
    check(r.owner.act(test_id, 'approve') == 302, 'the owner could not approve')
    r.wait_for(lambda: r.test(test_id).finished, 90, 'test %d to be played' % (test_id))
    r.settled(test_id, 'finished')
    r.agrees(test_id)
    return 'test %d: waiting for approval, refused to the helper, approved by the owner, played' % (test_id)


def stop_and_restart(r):
    test_id = r.create_test(max_games=400, workload_size=2)
    r.wait_for(lambda: r.test(test_id).games > 0, 90, 'test %d to play' % (test_id))
    r.stop_test(test_id)
    r.settled(test_id, 'stopped')
    games = r.test(test_id).games
    time.sleep(RETRY + 2)
    check(r.test(test_id).games == games, 'the worker played on after the stop')
    check(r.owner.act(test_id, 'restart') == 302, 'restart did not redirect')
    r.wait_for(lambda: r.test(test_id).games > games, 90, 'test %d to play again' % (test_id))
    r.stop_test(test_id)
    r.settled(test_id, 'stopped')
    r.agrees(test_id)
    return 'test %d: stopped at %d games, the worker stopped, restarted and played to %d, stopped' % (test_id, games, r.test(test_id).games)


def unapprove_and_delete(r):
    from nyayo.lab.models import Result, Test
    from nyayo.matches.models import Match, Tombstone
    # unapproving: a test nobody has played, approved and taken back at once, is not given to a worker
    waiting = r.create_test(who=r.helper, max_games=4)
    check(r.owner.act(waiting, 'approve') == 302, 'the owner could not approve')
    said = r.owner.act(waiting, 'unapprove')
    if r.test(waiting).games == 0:
        check(said == 302 and not r.test(waiting).approved, 'unapprove did not take a test with no games back (%d)' % (said))
        time.sleep(RETRY + 3)
        check(r.test(waiting).games == 0, 'a worker played an unapproved test')
    r.owner.act(waiting, 'stop')
    # a test with games cannot be unapproved, and is deleted while it is being played
    test_id = r.create_test(max_games=400, workload_size=2)
    r.wait_for(lambda: r.test(test_id).games > 0, 90, 'test %d to play' % (test_id))
    check(r.owner.act(test_id, 'unapprove') == 409, 'a test with games was unapproved')
    match = r.match(test_id)
    reply = r.owner.post('/matches-remove', id=match.pk, mode='delete', confirm=match.name or match.pk)
    check(reply.status_code == 302, 'deleting the match answered %d' % (reply.status_code))
    check(not Test.objects.filter(pk=test_id).exists() and not Match.objects.filter(pk=match.pk).exists(), 'the test or its match is still there')
    check(Tombstone.objects.filter(pk=match.pk).exists(), 'no tombstone')
    time.sleep(RETRY + 5)
    check(not Result.objects.filter(test_id=test_id).exists(), 'a worker went on reporting results of the deleted test')
    check('was deleted' in r.owner.session.get(r.url + '/matches/%s' % match.pk).text, 'the deleted page does not say so')
    # the worker is alive and plays the next test
    after = r.create_test(max_games=4, workload_size=2)
    r.wait_for(lambda: r.test(after).finished, 90, 'the next test to be played after the delete')
    r.settled(after, 'finished')
    return 'test %d: unapprove with no games taken back and not played, refused with games (409); test %d deleted at %d games while played, tombstone, no more results; test %d played after' % (waiting, test_id, r.test(after).games, after)


def crash_then_stop(r):
    from nyayo.lab.models import Result
    test_id = r.create_test(dev=r.engine('dev'), dev_options='Threads=1 Hash=16 CrashAfter=6', max_games=400)
    crashes = lambda: sum(Result.objects.filter(test_id=test_id).values_list('crashes', flat=True))
    r.wait_for(lambda: crashes() > 0, 90, 'test %d to crash' % (test_id))
    r.stop_test(test_id)
    r.settled(test_id, 'stopped')
    r.agrees(test_id)
    kept = r.match(test_id).results.get().crashes
    check(kept.get('subject', 0) > 0, 'nyayo has no crash: %s' % (kept))
    return 'test %d: %d crashes of the engine, the match counts them, stopped' % (test_id, crashes())


def two_tests(r):
    first = r.create_test(max_games=6, workload_size=1, priority=2)
    second = r.create_test(max_games=6, workload_size=1, priority=1)
    r.wait_for(lambda: r.test(first).finished and r.test(second).finished, 120, 'both tests to finish')
    for test_id in (first, second):
        r.settled(test_id, 'finished')
        r.agrees(test_id)
        check(r.match(test_id).results.get().games == 6 and r.test(test_id).games == 6, 'test %d has %d games' % (test_id, r.test(test_id).games))
    return 'tests %d and %d: 6 games each, on the right matches' % (first, second)


def engine_fails(dev, step, summary):
    def run(r):
        test_id = r.create_test(dev=dev(r), **SPRT_OPEN)
        r.wait_for(lambda: r.test(test_id).finished, 60, 'the verdict to stop test %d' % (test_id))
        row = r.step(test_id, step, 'dev')
        check(row.blame == 'engine', 'blame %r' % (row.blame))
        check(r.step(test_id, step, 'base').state == 'passed', 'base %s beside the failed dev' % (r.step(test_id, step, 'base').state))
        check(r.test(test_id).games == 0, 'games were played')
        r.settled(test_id, 'failed_prechecks')
        verdict = r.verdict(test_id)
        check(verdict['blame'] == 'engine' and verdict['summary'] == summary, 'verdict %s' % (verdict))
        r.agrees(test_id)
        check('Stopped' in r.page('/matches/%s' % (r.match(test_id).pk)), 'the page does not say it stopped')
        return 'test %d: %s' % (test_id, summary)
    return run


def judged_twice(r, test_id, step):
    # The dev step has failed twice and been judged
    row = r.step(test_id, step, 'dev')
    return bool(row and row.tries >= 2 and row.blame)


def worker_trouble(dev, base, step, log=None):
    def run(r):
        test_id = r.create_test(dev=dev(r), base=base(r), **SPRT_OPEN)
        r.wait_for(lambda: judged_twice(r, test_id, step), 60, 'the worker to try test %d twice' % (test_id))
        row = r.step(test_id, step, 'dev')
        check(row.blame == 'worker', 'blame %r' % (row.blame))
        check(log is None or log in row.log, 'the step\'s log lacks %r: %s' % (log, row.log[-200:]))
        check(not r.test(test_id).finished and r.test(test_id).games == 0, 'the test stopped or played')
        r.settled(test_id, 'waiting')
        verdict = r.verdict(test_id)
        check(verdict and verdict['blame'] == 'worker', 'verdict %s' % (verdict))
        r.agrees(test_id)
        r.stop_test(test_id)
        return 'test %d: the worker\'s failure, two tries, the test went on' % (test_id)
    return run


def unclear(dev, base, step, summary, restart=False):
    def run(r):
        from nyayo.lab import steps
        test_id = r.create_test(dev=dev(r), base=base(r), **SPRT_OPEN)
        held = lambda: judged_twice(r, test_id, step)
        r.wait_for(held, 60, 'the worker to try test %d twice' % (test_id))
        check(r.step(test_id, step, 'dev').blame == 'unclear', 'blame %r' % (r.step(test_id, step, 'dev').blame))
        check(not r.test(test_id).finished, 'a failure nobody can place stopped the test')
        r.settled(test_id, 'held')
        verdict = r.verdict(test_id)
        check(verdict['summary'] == summary, 'verdict %s' % (verdict))
        check('Last report' in r.page('/matches/%s' % (r.match(test_id).pk)) and '<dt>Stopped</dt>' not in r.page('/matches/%s' % (r.match(test_id).pk)),
              'the time card of a held test')
        if restart:
            # What the restart button does: every worker's walk starts over, and the hold is let go
            check(r.owner.act(test_id, 'restart') == 302, 'restart did not redirect')
            r.wait_for(held, 60, 'the worker to try test %d twice again' % (test_id))
            r.settled(test_id, 'held')
        r.agrees(test_id)
        r.stop_test(test_id)
        return 'test %d: nobody could place it, held%s' % (test_id, ', restarted by hand and held again' if restart else '')
    return run


def later_workload(r):
    test_id = r.create_test(workload_size=1, dev=r.engine('dev', bench_mode='flip'), **SPRT_OPEN)
    r.wait_for(lambda: r.test(test_id).finished, 90, 'the verdict to stop test %d' % (test_id))
    check(r.step(test_id, 'bench', 'dev').blame == 'engine' and r.test(test_id).games > 0, 'verdict or games')
    r.settled(test_id, 'error')
    r.agrees(test_id)
    return 'test %d: bench right, then wrong on a later workload, %d games kept, error' % (test_id, r.test(test_id).games)


def registering(r):
    # hello, pending (told to wait, no test), describe, approve, play. The worker has no password anywhere
    from nyayo.lab.models import Machine, Result
    r.wait_for(lambda: r.machine('smoke-0') and r.machine('smoke-0').described, 90, 'the worker to say hello and describe itself')
    machine = r.machine('smoke-0')
    check(machine.state == 'pending' and machine.token_hash and not machine.user_id, 'the worker is %s' % (machine.state))
    check(machine.info['toolchains'].get('make') and 'cpu_flags' in machine.info, 'it said nothing of its tools: %s' % (machine.info.get('toolchains')))
    arguments = ' '.join(r.clients[0].args)
    check('-U' not in r.clients[0].args and '-P' not in r.clients[0].args and 'password' not in arguments.lower(), 'the worker was started with credentials: %s' % (arguments))
    token_file = r.temp / 'Client0' / 'worker.token'
    check(token_file.is_file() and (token_file.stat().st_mode & 0o077) == 0, 'worker.token is missing or readable by others')
    import hashlib
    check(hashlib.sha256(token_file.read_text().strip().encode()).hexdigest() == machine.token_hash, 'the token file is not the token the server hashed')
    test_id = r.create_test(max_games=4, workload_size=1)
    time.sleep(RETRY + 4)
    check(not Result.objects.filter(test_id=test_id).exists() and r.test(test_id).games == 0, 'a pending worker was handed a test')
    check(r.match(test_id).state == 'waiting', 'the match is %s' % (r.match(test_id).state))
    check('Waiting for approval' in r.page('/machines'), 'the Machines page does not say it is waiting for approval')
    r.approve('smoke-0')
    r.wait_for(lambda: r.test(test_id).finished, 90, 'test %d to be played' % (test_id))
    r.settled(test_id, 'finished')
    r.agrees(test_id)
    check(Machine.objects.filter(name='smoke-0').count() == 1, 'more than one row for the worker')
    return 'test %d: the worker said hello and described itself, was pending and played nothing, was approved, played 4 games; no password' % (test_id)


def disable_and_enable(r):
    from nyayo.lab.models import Machine
    test_id = r.create_test(max_games=1000, workload_size=1)
    r.wait_for(lambda: r.test(test_id).games > 0, 90, 'test %d to play' % (test_id))
    machine = r.machine('smoke-0')
    check(r.owner.post('/machine-action', id=machine.id, action='disable').status_code == 302, 'disable did not redirect')
    time.sleep(RETRY + 3)                    # a workload it has finishes
    games = r.test(test_id).games
    time.sleep(RETRY + 3)
    check(r.test(test_id).games == games, 'a disabled worker was given a new workload')
    check('Disabled' in r.page('/machines') and not r.machine('smoke-0').enabled, 'the worker is not shown disabled')
    check(r.owner.post('/machine-action', id=machine.id, action='enable').status_code == 302, 'enable did not redirect')
    r.wait_for(lambda: r.test(test_id).games > games, 90, 'the worker to play again')
    r.stop_test(test_id)
    return 'test %d: disabled after %d games, nothing new was handed out, enabled, played on' % (test_id, games)


def join_code(r):
    from nyayo.lab.models import JoinCode
    page = r.owner.post('/machine-join-code', hours=1).text
    code = re.search(r'class="join-code">([^<]+)<', page).group(1)
    r.prepare_client(1)
    r.start_client(1, name='smoke-join', join=code, extra=True)
    r.wait_for(lambda: r.machine('smoke-join') and r.machine('smoke-join').described, 90, 'the worker with a join code to say hello')
    machine = r.machine('smoke-join')
    check(machine.state == 'approved', 'the worker with a join code is %s' % (machine.state))
    check(JoinCode.objects.get().used_by_id == machine.id, 'the code was not used up')
    stop(r.extras.pop())
    return 'a worker started with --join was approved on hello, and the code is used up'


def turned_away(r):
    from nyayo.lab.models import Machine
    r.prepare_client(2)
    away = r.start_client(2, name='smoke-away', extra=True)
    r.wait_for(lambda: r.machine('smoke-away') and r.machine('smoke-away').described, 90, 'the worker to say hello')
    machine = r.machine('smoke-away')
    check(machine.state == 'pending', 'it is %s' % (machine.state))
    check(r.owner.post('/machine-action', id=machine.id, action='turn_away').status_code == 302, 'turn away did not redirect')
    log = r.temp / 'client2.log'
    r.wait_for(lambda: 'Waiting: turned away' in log.read_text(errors='replace'), 60, 'the turned-away worker to be told to wait')
    stop(r.extras.pop())
    # without its token it is a new hello, refused by its name
    (r.temp / 'Client2' / 'worker.token').unlink()
    again = r.start_client(2, name='smoke-away', extra=True)
    r.wait_for(lambda: again.poll() is not None, 60, 'the refused worker to exit')
    check(again.returncode != 0 and 'turned away' in log.read_text(errors='replace').lower(), 'the refused worker did not say so: %s' % (log.read_text(errors='replace')[-300:]))
    check(Machine.objects.filter(name__istartswith='smoke-away').count() == 1, 'a new row was made for a turned-away name')
    r.extras.pop()
    return 'a turned-away worker was told to wait, and without its token was refused by name, with no new row'


def restart_keeps_the_row(r):
    from nyayo.lab.models import Machine, Result
    test_id = r.create_test(max_games=1000, workload_size=1)
    r.wait_for(lambda: r.test(test_id).games > 0, 90, 'test %d to play' % (test_id))
    before = r.machine('smoke-0')
    games = Result.objects.get(test_id=test_id, machine=before).games
    stop(r.clients.pop())
    r.start_client(0)
    r.wait_for(lambda: Machine.objects.get(pk=before.id).secret != before.secret, 60, 'the restarted worker to start a session')
    after = Machine.objects.get(pk=before.id)
    check(Machine.objects.filter(name='smoke-0').count() == 1 and after.state == 'approved', 'the restart made a row or lost its approval')
    r.wait_for(lambda: Result.objects.get(test_id=test_id, machine=before).games > games, 90, 'the restarted worker to play on the same result')
    check(Result.objects.filter(test_id=test_id).count() == 1, 'the restart made another result')
    r.stop_test(test_id)
    return 'test %d: restarted at %d games: the same row, a new session secret, the same result played on' % (test_id, games)


def fastchess_from_the_server(r):
    # A release fetched into the data directory, offered to a system it has a file for (one no worker here is), and served
    import hashlib
    import json
    import requests
    binary = b'riscv fastchess'
    sha = hashlib.sha256(binary).hexdigest()
    base = r.file_server.url
    r.file_server.add('/downloads/fastchess-linux-riscv64.tar', binary, 'application/octet-stream')
    r.file_server.add('/repos/Disservin/fastchess/releases/tags/v9.9.9-smoke', json.dumps({'tag_name': 'v9.9.9-smoke', 'assets': [
        {'name': 'fastchess-linux-riscv64.tar', 'browser_download_url': base + '/downloads/fastchess-linux-riscv64.tar', 'digest': 'sha256:' + sha}]}).encode(), 'application/json')
    r.run('manage.py', 'fetch_fastchess', '9.9.9-smoke', '--api', base + '/repos/Disservin/fastchess')
    answer = requests.get(r.url + '/clientMatchRunnerVersionRef/', params={'os_name': 'Linux', 'arch': 'riscv64'}, timeout=10).json()
    check(answer.get('binary', {}).get('sha256') == sha, 'the file is not offered: %s' % (answer))
    served = requests.get(answer['binary']['url'], timeout=10)
    check(served.status_code == 200 and hashlib.sha256(served.content).hexdigest() == sha, 'the file served is not the file fetched')
    check('binary' not in requests.get(r.url + '/clientMatchRunnerVersionRef/', params={'os_name': 'Linux', 'arch': 'x86_64'}, timeout=10).json(),
          'a file was offered to a system it is not for')
    return 'fetch_fastchess kept v9.9.9-smoke with its sha256, offered it to linux riscv64 only, and served it'


def client_update(r):
    # A worker started from the client it downloaded here updates itself from here when the version goes up, and keeps its
    # token and its row. Last: the version then no longer matches the workers that were started before
    import re as regex
    from nyayo.lab.models import Machine
    page = r.owner.post('/machine-join-code', hours=1).text
    code = regex.search(r'class="join-code">([^<]+)<', page).group(1)
    r.prepare_client(3)
    r.start_client(3, name='smoke-update', join=code, extra=True, updates=True)
    r.wait_for(lambda: r.machine('smoke-update') and r.machine('smoke-update').described and r.machine('smoke-update').state == 'approved', 90,
               'the worker started from the downloaded client to be approved')
    before = r.machine('smoke-update')
    token = (r.temp / 'Client3' / 'worker.token').read_text()
    log = r.temp / 'client3.log'
    while r.clients:    # started with --no-client-downloads, and not of this version once it goes up: it would refuse to update
        stop(r.clients.pop())
    worker = r.served / 'worker.py'
    worker.write_text(worker.read_text().replace('CLIENT_VERSION   = 54', 'CLIENT_VERSION   = 55', 1))
    r.wait_for(lambda: 'Downloading newer version of Client' in log.read_text(errors='replace'), 90, 'the worker to be told to update')
    r.wait_for(lambda: Machine.objects.get(pk=before.id).secret != before.secret and Machine.objects.get(pk=before.id).info.get('client_ver') == 55, 90,
               'the updated worker to start a session as client 55')
    after = Machine.objects.get(pk=before.id)
    check('CLIENT_VERSION   = 55' in (r.temp / 'Client3' / 'worker.py').read_text(), 'its worker.py is not the new one')
    check((r.temp / 'Client3' / 'worker.token').read_text() == token, 'the update changed its token')
    check(Machine.objects.filter(name='smoke-update').count() == 1 and after.state == 'approved', 'the update made a row or lost its approval')
    return 'a worker from the downloaded client updated to version 55 from here, kept its token, its row and its approval'


def idle_then_quiet(r):
    # The worker has nothing to play: it polls, and the Machines page says idle. Stopped, it says nothing more, and
    # five minutes later the page says gone quiet (the clock is moved on, as five minutes is too long to wait)
    import datetime
    from django.utils import timezone
    from nyayo.lab.models import Machine, Test
    r.wait_for(lambda: not Test.objects.filter(finished=False).exists(), 60, 'every test to have ended')
    r.wait_for(lambda: Machine.objects.filter(name='smoke-0').exists(), 60, 'the worker to sign in')
    before = Machine.objects.filter(name='smoke-0').order_by('-id').first().updated
    r.wait_for(lambda: Machine.objects.filter(name='smoke-0').order_by('-id').first().updated > before, 30,
               'the idle worker to be heard from')
    check('<span class="muted">idle</span>' in r.page('/machines'), 'the page does not say idle: %s' % (
        re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', re.sub(r'(?s)<style.*?</style>', '', r.page('/machines'))))[:600]))
    stop(r.clients.pop())
    time.sleep(RETRY + 1)
    machine = Machine.objects.filter(name='smoke-0').order_by('-id').first()
    time.sleep(RETRY + 1)
    check(Machine.objects.get(pk=machine.pk).updated == machine.updated, 'a stopped worker was heard from')
    check('<span class="muted">idle</span>' in r.page('/machines'), 'a worker stopped a moment ago is not idle yet')
    Machine.objects.filter(pk=machine.pk).update(updated=timezone.now() - datetime.timedelta(minutes=6))
    check('<span class="worse">gone quiet</span>' in r.page('/machines'), 'the page does not say gone quiet')
    return 'idle while it polled with nothing to play, silent once stopped, gone quiet after 5 minutes'


E = lambda **kw: (lambda r: r.engine('dev', **kw))
B = lambda **kw: (lambda r: r.engine('base', **kw))

SCENARIOS = [
    ('register', registering),     # first: the worker starts pending, and this approves it
    ('games', passing_games),
    ('sprt', sprt_to_result),
    ('refused', page_refuses),
    ('disable-enable', disable_and_enable),
    ('join-code', join_code),
    ('turned-away', turned_away),
    ('restart-keeps-row', restart_keeps_the_row),
    ('fastchess', fastchess_from_the_server),
    ('approval', approval),
    ('stop-restart', stop_and_restart),
    ('unapprove-delete', unapprove_and_delete),
    ('crash-stop', crash_then_stop),
    ('two-tests', two_tests),
    ('compile-source', engine_fails(E(broken='source'), 'compile', "Dev doesn't compile")),
    ('bench-wrong', engine_fails(E(bench=ENGINE_BENCH + 1), 'bench', "Dev's bench doesn't match its commit")),
    ('bench-slow', engine_fails(E(bench_mode='slow'), 'bench', "Dev's bench runs over 60 seconds")),
    ('bench-silent', engine_fails(E(bench_mode='silent'), 'bench', 'Dev failed to run its bench')),
    ('compile-space', worker_trouble(E(broken='space'), B(), 'compile', log='No space left on device')),
    ('compile-both', worker_trouble(E(broken='source'), B(broken='source'), 'compile')),
    ('bench-slow-both', worker_trouble(E(bench_mode='slow'), B(bench_mode='slow'), 'bench')),
    ('bench-silent-both', worker_trouble(E(bench_mode='silent'), B(bench_mode='silent'), 'bench')),
    ('compile-killed', unclear(E(broken='killed'), B(), 'compile', "Dev's compile was killed, and no other worker has tried it", restart=True)),
    ('bench-wrong-both', unclear(E(bench=ENGINE_BENCH + 1), B(bench=ENGINE_BENCH + 1), 'bench', "Dev's bench doesn't match its commit, and no other worker has tried it")),
    ('bench-flip', later_workload),
    ('idle', idle_then_quiet),     # it stops the worker
    ('client-update', client_update),     # last: it raises the client's version
]


def load(r):
    # One to a few workers on a long test while pages are read: writes a minute, and whether anything waited a second
    test_id = r.create_test(test_mode='GAMES', max_games=r.args.load_games, workload_size=2)
    r.start_clients(one_at_a_time_for=test_id)
    began = time.time()
    waits, stop_poll = [], threading.Event()

    def poll():
        match = 'openbench-%s-%d' % (INSTANCE, test_id)
        while not stop_poll.is_set():
            for path in ('/matches', '/matches/%s' % (match), '/healthz'):
                started = time.time()
                status = r.get(path, timeout=30)
                waits.append((time.time() - started, path, status))
            time.sleep(0.3)

    poller = threading.Thread(target=poll, daemon=True)
    poller.start()
    r.wait_for(lambda: r.test(test_id).finished, r.args.load_games * 3 + 60, 'the load test to finish')
    r.settled(test_id, 'finished')
    ended = time.time()
    r.put_away(r.match(test_id), 120)
    stop_poll.set()
    poller.join()
    return report_load(r, test_id, began, ended, waits)


def report_load(r, test_id, began, ended, waits):
    minutes = (ended - began) / 60
    rows = [line.split() for line in (r.temp / 'writes.log').read_text().splitlines()]
    window = [row for row in rows if began <= float(row[0]) <= ended]
    statements, transactions = sum(1 for x in window if x[2] == 'stmt'), sum(1 for x in window if x[2] == 'tx')
    reports = by_endpoint(r.temp / 'access.log')
    slow = [(path, round(longest, 2)) for path, (count, longest, seconds) in reports.items() if longest >= 1.0]
    page_waits = [x[0] for x in waits]
    lines = ['load: %d worker(s), test %d, %d games in %.0f s' % (r.args.workers, test_id, r.test(test_id).games, ended - began),
             'writes: %d statements, %d write transactions: %.0f and %.0f a minute' % (statements, transactions, statements / minutes, transactions / minutes),
             'requests by the workers and pages (count, slowest s, median s):']
    for path, (count, longest, seconds) in sorted(reports.items(), key=lambda x: -x[1][0]):
        lines.append('  %-28s %5d  %.3f  %.3f' % (path, count, longest, statistics.median(seconds)))
    lines.append('page reads while it ran: %d, slowest %.3f s, median %.3f s, %d not 200' % (
        len(page_waits), max(page_waits, default=0), statistics.median(page_waits or [0]), sum(1 for x in waits if x[2] != 200)))
    lines.append('requests of a second or more: %s; "database is locked" in the logs: %d' % (slow or 'none', r.logged('database is locked')))
    return '\n'.join(lines)


def by_endpoint(path):
    found = {}
    for line in path.read_text().splitlines():
        match = re.match(r'(GET|POST|PUT) (\S+?)(?:\?\S*)? HTTP/\S+ (\d+) (\d+\.\d+)', line)
        if match:
            endpoint = re.sub(r'/matches/[^/ ]+', '/matches/<id>', match.group(2))
            count, longest, seconds = found.get(endpoint, (0, 0.0, []))
            found[endpoint] = (count + 1, max(longest, float(match.group(4))), seconds + [float(match.group(4))])
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--only', nargs='*', help='scenario names: ' + ', '.join(name for name, _ in SCENARIOS))
    parser.add_argument('--load', action='store_true', help='measure the load of --workers workers on a long test, instead of the scenarios')
    parser.add_argument('--load-games', type=int, default=120)
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--keep', action='store_true')
    args = parser.parse_args()

    preflight()
    # a stop from outside still stops the server and the workers
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, lambda *_: sys.exit(1))
    rig = Rig(args)
    failures, ran, began = 0, 0, time.time()
    try:
        rig.start_nyayo()
        if args.load:
            print(load(rig))
        else:
            chosen = [name for name, _ in SCENARIOS if not args.only or name in args.only]
            rig.start_clients(approve='register' not in chosen)
            for name, run in SCENARIOS:
                if args.only and name not in args.only:
                    continue
                ran += 1
                scenario_began, unreported = time.time(), rig.logged('could not be reported')
                try:
                    said = run(rig)
                    check(rig.logged('could not be reported') == unreported, 'nyayo refused a report of the test (see server.log)')
                    print('PASS %-18s %5.1fs  %s' % (name, time.time() - scenario_began, said), flush=True)
                except Exception as error:
                    failures += 1
                    print('FAIL %-18s %s: %s' % (name, type(error).__name__, error), flush=True)
    finally:
        rig.close(args.keep or failures)
    if not args.load:
        print('%d of %d scenarios passed in %.0f s (%.1f minutes)' % (ran - failures, ran, time.time() - began, (time.time() - began) / 60))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
