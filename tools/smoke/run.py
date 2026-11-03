#!/usr/bin/env python3
"""A small rig that plays tests against nyayo with OpenBench's real worker, and measures what that costs.

Linux or WSL only. It starts nyayo as it will run (gunicorn, 4 workers of 4 threads, SQLite in WAL, and the worker that
puts games away) on a port and a data directory of its own, serves a toy engine and a book the way GitHub serves
archives, starts one or more copies of openbench_eg's Client/ (the unchanged files of a given commit, extracted by
`git archive`; only the worker's timers are shortened in the copy, as the smoke test does), makes tests in the shell and
checks what nyayo ends up holding: the test, its steps and verdict, the match, its games and its pages.

    git -C ../openbench_eg archive da629d7 Client Scripts/smoke | tar -x -C ~/harness/ob
    ~/.venvs/nyayo-smoke/bin/pip install -r requirements.txt gunicorn scipy numpy requests   (or pyproject's)
    ~/.venvs/nyayo-smoke/bin/python tools/worker_rig.py --ob ~/harness/ob [--only NAME ...] [--load]

Step 5 of the nyayo 2.0 plan moves openbench_eg's whole smoke test (Scripts/smoke) to nyayo; this is the part of it that
step 2 needs, and it uses that directory's engine and book.
"""

import argparse
import json
import os
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
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

ENGINE_NAME, ENGINE_NPS, ENGINE_BENCH = 'smoke', 1000000, 1000000
USERNAME, PASSWORD = 'worker', 'rig-password'
INSTANCE = 'rig'
CLIENT_TIMERS = { 'TIMEOUT_ERROR' : 1, 'TIMEOUT_WORKLOAD' : 1, 'REPORT_INTERVAL' : 2 }
RETRY = 2          # seconds a worker waits before it tries a test again after a verdict that isn't the engine's
BENCH_LIMIT = 3    # seconds before a bench is over its time (Client/bench.py's 60)
SPRT_OPEN = { 'test_mode' : 'SPRT', 'elolower' : 0.0, 'eloupper' : 5.0, 'alpha' : 1e-6, 'beta' : 1e-6 }

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


class Rig:

    def __init__(self, args):
        self.args = args
        self.ob = Path(args.ob).resolve()
        sys.path.insert(0, str(self.ob / 'Scripts'))
        from smoke import files
        self.files = files
        self.temp = Path(tempfile.mkdtemp(prefix='nyayo-rig-'))
        self.port = free_port()
        self.url = 'http://127.0.0.1:%d' % (self.port)
        self.procs, self.clients, self.nonce = [], [], str(time.time())
        (self.temp / 'rigsettings.py').write_text(RIG_SETTINGS % { 'retry' : RETRY })
        self.env = { k : v for k, v in os.environ.items() if not k.startswith('NYAYO_') }
        self.env.update(
            DJANGO_SETTINGS_MODULE='rigsettings', PYTHONPATH=os.pathsep.join([str(self.temp), str(REPO)]),
            NYAYO_DATA_DIR=str(self.temp / 'data'), NYAYO_SECRET_KEY='rig', NYAYO_INSTANCE=INSTANCE,
            RIG_WRITES=str(self.temp / 'writes.log'), PYTHONUNBUFFERED='1', PYTHONDONTWRITEBYTECODE='1')
        self.file_server = files.FileServer().__enter__()

    # nyayo

    def run(self, *argv, **kw):
        return subprocess.run([sys.executable, *argv], cwd=REPO, env=self.env, check=True, capture_output=True, text=True, **kw)

    def start_nyayo(self):
        self.run('manage.py', 'migrate', '-v0')
        os.environ.update(self.env)
        sys.path[:0] = [str(self.temp), str(REPO)]
        import django
        django.setup()
        from django.contrib.auth.models import User
        User.objects.create_user(USERNAME, password=PASSWORD)
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

    def seed(self):
        from nyayo.lab.models import Book, EngineConfig
        EngineConfig.objects.all().delete()
        EngineConfig.objects.create(
            name=ENGINE_NAME, nps=ENGINE_NPS, source=self.file_server.url + '/engines', build_path='.',
            build_compilers='g++', build_cpuflags='', build_systems='Linux', presets={ 'test_presets' : { 'default' : {} } })
        Book.objects.all().delete()
        Book.objects.create(name=self.files.BOOK_NAME, sha=self.files.book_sha(),
                            source=self.file_server.add('/books/smoke.zip', self.files.book_zip()))

    def get(self, path, timeout=10):
        try:
            with urllib.request.urlopen(self.url + path, timeout=timeout) as response:
                response.read()
                return response.status
        except urllib.error.HTTPError as error:
            return error.code
        except OSError:
            return 0

    def page(self, path):
        with urllib.request.urlopen(self.url + path, timeout=10) as response:
            return response.read().decode('utf-8', 'replace')

    # the worker

    def start_clients(self, one_at_a_time_for=None):
        # one_at_a_time_for: a test id; each worker is started once the last has passed its walk on it, as the
        # first workloads of several workers at once make the toy engine fail to execute (a race in the machine)
        for number in range(self.args.workers):
            client = self.temp / ('Client%d' % (number))
            shutil.copytree(self.ob / 'Client', client, ignore=shutil.ignore_patterns('__pycache__', 'PGNs', 'Engines', 'Networks', 'Books'))
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
            env = dict(os.environ, PYTHONUNBUFFERED='1', PYTHON=sys.executable,
                       PATH=os.path.dirname(sys.executable) + os.pathsep + os.environ['PATH'])
            log = open(self.temp / ('client%d.log' % (number)), 'w')
            self.clients.append(subprocess.Popen(
                [sys.executable, '-u', str(client / 'client.py'), '-U', USERNAME, '-P', PASSWORD, '-S', self.url,
                 '-T', '1', '-N', '1', '-I', 'rig-%d' % (number), '--no-client-downloads'],
                cwd=client, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True))
            if one_at_a_time_for:
                from nyayo.lab.models import Step
                self.wait_for(lambda: Step.objects.filter(result__test_id=one_at_a_time_for, step='running', state='passed').count() > number,
                              240, 'worker %d to pass its walk' % (number))

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

    def engine(self, label, bench=ENGINE_BENCH, broken=False, bench_mode=''):
        import hashlib
        sha = hashlib.sha1(('%s %s %s' % (self.nonce, label, time.time())).encode()).hexdigest()
        source = self.file_server.add('/engines/%s.zip' % (sha), self.files.engine_zip('smoke-%s' % (sha), broken, bench_mode))
        return { 'name' : label, 'source' : source, 'sha' : sha, 'bench' : bench }

    def create_test(self, dev=None, base=None, **overrides):
        # As the create page will: the test and its engines, reported at once, then opened to workers
        import math
        from django.contrib.auth.models import User
        from nyayo.lab.document import report_test
        from nyayo.lab.models import Engine, Test
        dev, base = dev or self.engine('dev'), base or self.engine('base')
        fields = dict(
            author=User.objects.get(username=USERNAME), book_name=self.files.BOOK_NAME,
            dev_repo=self.file_server.url + '/engines', dev_engine=ENGINE_NAME, dev_options='Threads=1 Hash=16',
            dev_time_control='N=1000', base_repo=self.file_server.url + '/engines', base_engine=ENGINE_NAME,
            base_options='Threads=1 Hash=16', base_time_control='N=1000', workload_size=2, priority=1, throughput=100,
            scale_nps=ENGINE_NPS, test_mode='GAMES', max_games=8)
        fields.update(overrides)
        if fields['test_mode'] == 'SPRT':
            fields['lowerllr'] = math.log(fields['beta'] / (1.0 - fields['alpha']))
            fields['upperllr'] = math.log((1.0 - fields['beta']) / fields['alpha'])
        test = Test.objects.create(dev=Engine.objects.create(**dev), base=Engine.objects.create(**base), approved=False, **fields)
        check(report_test(test) is not None, 'the new test could not be reported')
        Test.objects.filter(id=test.id).update(approved=True)
        return test.id

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
        # What the stop button will do: finish it with an event, so the worker is told to stop
        from nyayo.lab.document import report_test
        from nyayo.lab.models import LogEvent, Test
        Test.objects.filter(id=test_id).update(finished=True)
        LogEvent.objects.create(author=USERNAME, summary='STOP', log_file='', machine_id=0, test_id=test_id)
        report_test(test_id)
        time.sleep(RETRY + 3)

    def settled(self, test_id, state, timeout=15):
        # The match is updated in the request that changed the test, so it is there as soon as the test is
        self.wait_for(lambda: self.match(test_id).state == state, timeout, 'the match of test %d to be %s' % (test_id, state))

    def put_away(self, match, timeout=60):
        from django.db.models import Sum
        from nyayo.matches.models import Upload
        self.wait_for(lambda: Upload.objects.filter(match=match).exists() and not Upload.objects.filter(match=match, state='pending').exists(),
                      timeout, 'the games of %s to be put away' % (match.pk))
        check(not Upload.objects.filter(match=match, state='refused').exists(), 'an upload was refused: %s' % (
            list(Upload.objects.filter(match=match, state='refused').values_list('reason', flat=True))))
        return Upload.objects.filter(match=match).aggregate(total=Sum('appended'))['total'] or 0

    def close(self, keep):
        for client in self.clients:
            stop(client)
        for proc in self.procs:
            stop(proc)
        self.file_server.__exit__(None, None, None)
        if keep:
            print('Kept %s' % (self.temp))
        else:
            shutil.rmtree(self.temp, ignore_errors=True)


# Scenarios: each makes a test, waits for what a real worker does with it, and checks nyayo's rows and pages

def passing_games(r):
    test_id = r.create_test(test_mode='GAMES', max_games=8, workload_size=1)
    r.wait_for(lambda: r.test(test_id).finished, 90, 'test %d to finish' % (test_id))
    test = r.test(test_id)
    check(test.games == 8 and test.wins + test.draws + test.losses == 8, 'games: %s' % (test.games))
    check(test.LL + test.LD + test.DD + test.DW + test.WW == 4, 'the pentanomial is not half the games')
    r.settled(test_id, 'finished')
    match = r.match(test_id)
    result = match.results.get()
    check(result.games == 8, 'the match counts %d games' % (result.games))
    from nyayo.lab.models import Step
    states = {(s.step, s.side): s.state for s in Step.objects.filter(result__test_id=test_id)}
    check(len(states) == 6 and set(states.values()) == { 'passed' }, 'the walk: %s' % (states))
    kept = r.put_away(match)
    check(kept == 8, 'nyayo holds %d games, expected 8' % (kept))
    pages = [r.get(path) for path in ('/matches', '/matches/%s' % (match.pk), '/games')]
    check(pages == [200, 200, 200], 'pages answer %s' % (pages))
    check('openbench-%s-%d' % (INSTANCE, test_id) in r.page('/matches'), 'the match is not on the list')
    return 'test %d: 8 games, the walk passed, %d games put away, pages answer' % (test_id, kept)


def sprt_to_result(r):
    test_id = r.create_test(test_mode='SPRT', elolower=0.0, eloupper=200.0, alpha=0.05, beta=0.05, base_options='Threads=1 Hash=16 Strength=0')
    r.wait_for(lambda: r.test(test_id).finished, 120, 'SPRT test %d to finish' % (test_id))
    test = r.test(test_id)
    check(test.passed or test.failed, 'finished with neither a pass nor a fail')
    r.settled(test_id, 'finished')
    match = r.match(test_id)
    check(match.sprt['result'] == ('passed' if test.passed else 'failed'), 'sprt %s' % (match.sprt))
    check(abs(match.sprt['llr'] - test.currentllr) < 1e-9, 'the LLR differs')
    return 'test %d: %s, LLR %.2f, %d games' % (test_id, 'passed' if test.passed else 'failed', test.currentllr, test.games)


def engine_fails(dev, step, summary, **extra):
    def run(r):
        test_id = r.create_test(dev=dev(r), **SPRT_OPEN, **extra)
        r.wait_for(lambda: r.test(test_id).finished, 60, 'the verdict to stop test %d' % (test_id))
        row = r.step(test_id, step, 'dev')
        check(row.blame == 'engine', 'blame %r' % (row.blame))
        check(r.step(test_id, step, 'base').state == 'passed', 'base %s beside the failed dev' % (r.step(test_id, step, 'base').state))
        check(r.test(test_id).games == 0, 'games were played')
        r.settled(test_id, 'failed_prechecks')
        verdict = r.match(test_id).verdict
        check(verdict['blame'] == 'engine' and verdict['summary'] == summary, 'verdict %s' % (verdict))
        check(r.page('/matches/%s' % (r.match(test_id).pk)), 'the page does not draw')
        return 'test %d: %s' % (test_id, summary)
    return run


def judged_twice(r, test_id, step):
    # The dev step has failed twice and been judged
    row = r.step(test_id, step, 'dev')
    return bool(row and row.tries >= 2 and row.blame)


def worker_trouble(dev, base, step):
    def run(r):
        test_id = r.create_test(dev=dev(r), base=base(r), **SPRT_OPEN)
        r.wait_for(lambda: judged_twice(r, test_id, step), 60, 'the worker to try test %d twice' % (test_id))
        check(r.step(test_id, step, 'dev').blame == 'worker', 'blame %r' % (r.step(test_id, step, 'dev').blame))
        check(not r.test(test_id).finished and r.test(test_id).games == 0, 'the test stopped or played')
        r.settled(test_id, 'waiting')
        verdict = r.match(test_id).verdict
        check(verdict and verdict['blame'] == 'worker', 'verdict %s' % (verdict))
        r.stop_test(test_id)
        return 'test %d: the worker\'s failure, two tries, the test went on' % (test_id)
    return run


def unclear(dev, base, step, summary, restart=False):
    def run(r):
        from nyayo.lab import steps
        from nyayo.lab.document import report_test
        test_id = r.create_test(dev=dev(r), base=base(r), **SPRT_OPEN)
        held = lambda: judged_twice(r, test_id, step)
        r.wait_for(held, 60, 'the worker to try test %d twice' % (test_id))
        check(r.step(test_id, step, 'dev').blame == 'unclear', 'blame %r' % (r.step(test_id, step, 'dev').blame))
        check(not r.test(test_id).finished, 'a failure nobody can place stopped the test')
        r.settled(test_id, 'held')
        verdict = r.match(test_id).verdict
        check(verdict['summary'] == summary, 'verdict %s' % (verdict))
        if restart:
            steps.restart_walks(r.test(test_id))
            report_test(test_id)
            r.wait_for(held, 60, 'the worker to try test %d twice again' % (test_id))
            r.settled(test_id, 'held')
        r.stop_test(test_id)
        return 'test %d: nobody could place it, one more try, held%s' % (test_id, ', restarted and held again' if restart else '')
    return run


def later_workload(r):
    test_id = r.create_test(workload_size=1, dev=r.engine('dev', bench_mode='flip'), **SPRT_OPEN)
    r.wait_for(lambda: r.test(test_id).finished, 90, 'the verdict to stop test %d' % (test_id))
    check(r.step(test_id, 'bench', 'dev').blame == 'engine' and r.test(test_id).games > 0, 'verdict or games')
    r.settled(test_id, 'error')
    return 'test %d: bench right, then wrong on a later workload, %d games kept, error' % (test_id, r.test(test_id).games)


E = lambda **kw: (lambda r: r.engine('dev', **kw))
B = lambda **kw: (lambda r: r.engine('base', **kw))

SCENARIOS = [
    ('games', passing_games),
    ('sprt', sprt_to_result),
    ('compile-source', engine_fails(E(broken='source'), 'compile', "Dev doesn't compile")),
    ('bench-wrong', engine_fails(E(bench=ENGINE_BENCH + 1), 'bench', "Dev's bench doesn't match its commit")),
    ('bench-slow', engine_fails(E(bench_mode='slow'), 'bench', "Dev's bench runs over 60 seconds")),
    ('bench-silent', engine_fails(E(bench_mode='silent'), 'bench', 'Dev failed to run its bench')),
    ('compile-space', worker_trouble(E(broken='space'), B(), 'compile')),
    ('compile-both', worker_trouble(E(broken='source'), B(broken='source'), 'compile')),
    ('bench-slow-both', worker_trouble(E(bench_mode='slow'), B(bench_mode='slow'), 'bench')),
    ('bench-silent-both', worker_trouble(E(bench_mode='silent'), B(bench_mode='silent'), 'bench')),
    ('compile-killed', unclear(E(broken='killed'), B(), 'compile', "Dev's compile was killed, and no other worker has tried it", restart=True)),
    ('bench-wrong-both', unclear(E(bench=ENGINE_BENCH + 1), B(bench=ENGINE_BENCH + 1), 'bench', "Dev's bench doesn't match its commit, and no other worker has tried it")),
    ('bench-flip', later_workload),
]


def load(r):
    # One to a few workers on a long test while pages are read: writes a minute, and whether anything waited a second
    began = time.time()
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
    reports = collections_by_endpoint(r.temp / 'access.log', began)
    slow = [(path, round(seconds, 2)) for path, (count, longest, seconds_list) in reports.items() for seconds in [longest] if longest >= 1.0]
    page_waits = [x[0] for x in waits]
    locked = sum(1 for log in ('server.log', 'worker.log') for line in (r.temp / log).read_text().splitlines() if 'database is locked' in line.lower())
    lines = ['load: %d worker(s), test %d, %d games in %.0f s' % (r.args.workers, test_id, r.test(test_id).games, ended - began),
             'writes: %d statements, %d write transactions: %.0f and %.0f a minute' % (statements, transactions, statements / minutes, transactions / minutes),
             'requests by the workers and pages (count, slowest s, median s):']
    for path, (count, longest, seconds_list) in sorted(reports.items(), key=lambda x: -x[1][0]):
        lines.append('  %-28s %5d  %.3f  %.3f' % (path, count, longest, statistics.median(seconds_list)))
    lines.append('page reads while it ran: %d, slowest %.3f s, median %.3f s, %d not 200' % (
        len(page_waits), max(page_waits, default=0), statistics.median(page_waits or [0]), sum(1 for x in waits if x[2] != 200)))
    lines.append('requests of a second or more: %s; "database is locked" in the logs: %d' % (slow or 'none', locked))
    return '\n'.join(lines)


def collections_by_endpoint(path, since):
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
    parser.add_argument('--ob', required=True, help='openbench_eg\'s Client/ and Scripts/smoke, extracted from the commit to play with')
    parser.add_argument('--only', nargs='*', help='scenario names: ' + ', '.join(name for name, _ in SCENARIOS))
    parser.add_argument('--load', action='store_true', help='measure the load of --workers workers on a long test, instead of the scenarios')
    parser.add_argument('--load-games', type=int, default=120)
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--keep', action='store_true')
    args = parser.parse_args()

    rig = Rig(args)
    failures = 0
    try:
        rig.start_nyayo()
        if not args.load:
            rig.start_clients()
        if args.load:
            print(load(rig))
        else:
            for name, run in SCENARIOS:
                if args.only and name not in args.only:
                    continue
                began = time.time()
                try:
                    said = run(rig)
                    print('PASS %-18s %5.1fs  %s' % (name, time.time() - began, said), flush=True)
                except Exception as error:
                    failures += 1
                    print('FAIL %-18s %s: %s' % (name, type(error).__name__, error), flush=True)
    finally:
        rig.close(args.keep or failures)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
