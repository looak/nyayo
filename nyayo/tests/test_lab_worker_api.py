"""The worker's API: OpenBench's client* endpoints, as its Client calls them."""

import bz2
import json
import struct
import tempfile
from pathlib import Path

import datetime

from django.contrib.auth.models import User
from django.db import connection
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from nyayo.lab import registration
from nyayo.lab.models import EngineConfig, LogEvent, Machine, Result, ServerState, Step, Test
from nyayo.matches.models import Match, Upload

from .lab_helpers import make_test

class FakeSocket:
    """What getsockopt(IPPROTO_TCP, TCP_INFO, 104) gives: bytes with tcpi_rtt, in microseconds, at offset 68."""

    def __init__(self, micro=None, raises=None, short=False):
        self.micro, self.raises, self.short = micro, raises, short

    def getsockopt(self, level, option, size):
        if self.raises:
            raise self.raises
        data = bytearray(size)
        struct.pack_into('=I', data, 68, self.micro or 0)
        struct.pack_into('=I', data, 72, 999)  # tcpi_rttvar, which must not be read as the rtt
        return bytes(data[:40] if self.short else data)


SYSTEM = {
    'client_ver': 56, 'machine_name': 'hunt', 'concurrency': 4, 'sockets': 1, 'physical_cores': 4, 'syzygy_max': 0,
    'cpu_flags': ['POPCNT', 'AVX2'], 'tokens': {}, 'os_name': 'Linux',
    'toolchains': {'gcc': '13.3.0', 'g++': '13.3.0', 'clang': None, 'clang++': None, 'dotnet': None, 'rustc': None, 'cargo': None,
                   'python3': '3.12.3', 'make': '4.3'},
}


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class Api(TestCase):
    token = None

    def post(self, path, **form):
        # a TestCase never commits, so the reports the views leave for after the commit are run here
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(f'/{path}/', form)

    def hello(self, name='hunt', **form):
        answer = self.post('clientHello', name=name, client_ver=56, **form).json()
        self.token = answer.get('token', self.token)
        return answer

    def sign_in(self, approve=True, name='hunt', **system):
        # hello once, then a session: approved by the owner (here, in the database) unless it is to stay pending
        if self.token is None:
            self.hello(name)
        if approve:
            Machine.objects.filter(token_hash=registration.digest(self.token)).update(state='approved')
        answer = self.post('clientWorkerInfo', token=self.token, system_info=json.dumps({**SYSTEM, **system})).json()
        self.machine_id, self.secret = answer['machine_id'], answer['secret']
        return answer

    def worker(self, path, **form):
        return self.post(path, machine_id=self.machine_id, secret=self.secret, **form)

    def handed_out(self, test):
        self.sign_in()
        return self.worker('clientGetWorkload').json()['workload']


class Settings(Api):
    def test_the_client_to_fetch_is_nyayos_own(self):
        answer = self.client.get('/clientVersionRef/').json()
        self.assertEqual(sorted(answer), ['client_sha256', 'client_url', 'client_version'])
        self.assertEqual((answer['client_version'], answer['client_url']), (56, 'http://testserver/client.zip'))
        self.assertNotIn('github', json.dumps(answer))

    def test_the_fastchess_to_build(self):
        self.assertEqual(self.client.get('/clientMatchRunnerVersionRef/').json(), {
            'fastchess_min_version': '1.8.1', 'fastchess_repo_url': 'https://github.com/AndyGrant/fastchess',
            'fastchess_repo_ref': 'master'})

    def test_how_to_build_each_engine_for_an_approved_worker(self):
        self.sign_in()
        self.assertEqual(self.client.get('/clientGetBuildInfo/', {'token': self.token}).json()['Elephant'], {
            'path': 'ob_build', 'compilers': ['g', 'clang'], 'cpuflags': ['POPCNT'], 'systems': ['Linux', 'Windows'],
            'private': False})

    @override_settings(NYAYO_CLIENT_VERSION=56)
    def test_the_version_may_be_a_setting(self):
        self.assertEqual(self.client.get('/clientVersionRef/').json()['client_version'], 56)


class SigningIn(Api):
    def test_a_session_of_an_approved_worker_is_on_its_row(self):
        answer = self.sign_in()
        machine = Machine.objects.get(id=answer['machine_id'])
        self.assertEqual((machine.name, machine.secret, machine.state), ('hunt', answer['secret'], 'approved'))
        self.assertEqual(machine.info['supported'], ['Elephant'])
        self.assertEqual(machine.info['OPENBENCH_CONFIG_CHECKSUM'], ServerState.checksum())
        self.assertTrue(machine.described)
        self.assertEqual((answer['state'], answer['enabled']), ('approved', True))

    def test_a_restart_is_a_new_secret_on_the_same_row(self):
        first = self.sign_in()
        second = self.sign_in()
        self.assertEqual(first['machine_id'], second['machine_id'])
        self.assertNotEqual(first['secret'], second['secret'])
        self.assertEqual(Machine.objects.count(), 1)
        self.assertIn('Invalid Secret Token', self.post('clientHeartbeat', test_id=1, machine_id=first['machine_id'], secret=first['secret']).json()['error'])

    def test_an_unknown_token_or_none_is_refused_and_makes_nothing(self):
        form = {'system_info': json.dumps(SYSTEM)}
        for token in ({}, {'token': 'nope'}):
            reply = self.post('clientWorkerInfo', **token, **form)
            self.assertEqual(reply.status_code, 403)
            self.assertIn('say hello first', reply.json()['error'])
        self.assertFalse(Machine.objects.exists())

    def test_a_username_and_password_are_no_longer_a_way_in(self):
        User.objects.create_user('worker', password='pw')
        reply = self.post('clientWorkerInfo', username='worker', password='pw', system_info=json.dumps(SYSTEM))
        self.assertEqual(reply.status_code, 403)
        self.assertFalse(Machine.objects.exists())

    def test_an_old_client_is_told_to_update(self):
        self.hello()
        answer = self.post('clientWorkerInfo', token=self.token, system_info=json.dumps({**SYSTEM, 'client_ver': 53})).json()
        self.assertEqual(answer, {'error': 'Bad Client Version: Expected 56'})
        self.assertEqual(self.post('clientHello', name='old', client_ver=53).json(), {'error': 'Bad Client Version: Expected 56'})
        self.assertEqual(Machine.objects.count(), 1)

    def test_it_supports_only_the_engines_it_can_build(self):
        self.sign_in(cpu_flags=['AVX2'])
        self.assertEqual(Machine.objects.get().info['supported'], [])
        self.sign_in(os_name='Windows', toolchains={'make': '4.3'})
        self.assertEqual(Machine.objects.get().info['supported'], [])
        self.sign_in(toolchains={'clang': '18.1.3', 'make': '4.3'})
        self.assertEqual(Machine.objects.get().info['supported'], ['Elephant'])
        self.sign_in(toolchains={'clang': '18.1.3'})  # no make
        self.assertEqual(Machine.objects.get().info['supported'], [])
        EngineConfig.objects.filter(name='Elephant').update(private=True)
        self.sign_in(tokens={'Elephant': 'x'})
        self.assertEqual(Machine.objects.get().info['supported'], ['Elephant'])

    def test_every_other_call_checks_the_session(self):
        self.sign_in()
        for form, error in (({'machine_id': 0, 'secret': self.secret}, 'Bad Machine Id'),
                            ({'machine_id': self.machine_id, 'secret': 'wrong'}, 'Invalid Secret Token')):
            self.assertIn(error, self.post('clientHeartbeat', test_id=1, **form).json()['error'])
        self.assertEqual(self.post('clientHeartbeat', secret=self.secret).json()['error'], 'Bad Client Version: Bad Machine Id')

    def test_a_changed_build_config_sends_the_worker_off_to_restart(self):
        self.sign_in()
        config = EngineConfig.objects.get(name='Elephant')
        config.build_cpuflags = 'POPCNT BMI2'
        config.save()
        self.assertEqual(self.worker('clientGetWorkload').json(),
                         {'error': 'Bad Client Version: Server Configuration Changed'})

    def test_a_network_is_served_to_an_approved_worker_by_its_token(self):
        self.sign_in()
        self.assertEqual(self.client.post('/clientGetNetwork/Elephant/net/', {'token': 'no'}).status_code, 403)
        self.assertEqual(self.client.post('/clientGetNetwork/Elephant/net/').status_code, 403)
        self.assertEqual(self.client.post('/clientGetNetwork/Elephant/net/', {'token': self.token}).status_code, 404)


class Scheduling(Api):
    def test_a_worker_is_handed_an_approved_test_and_its_walk_begins(self):
        test = make_test()
        workload = self.handed_out(test)
        self.assertEqual(workload['test']['id'], test.id)
        self.assertEqual((workload['test']['type'], workload['test']['dev']['sha'], workload['test']['dev']['engine']),
                         ('SPRT', 'a' * 40, 'Elephant'))
        self.assertEqual(workload['test']['book']['name'], '8moves_v3.epd')
        self.assertEqual(workload['test']['book']['sha'], '1f055af431656f09ee6a09d2448e0b876125f78bb7b404fca2031c403a1541e5')
        # 4 threads at one each: 4 concurrent games, workload_size 4: 2 * 4 * 4 rounds
        self.assertEqual(workload['distribution'], {'runner-count': 1, 'concurrency-per': 4, 'rounds-per-runner': 32})
        result = Result.objects.get()
        self.assertEqual((result.id, result.workloads), (workload['result']['id'], 1))
        self.assertEqual(Machine.objects.get().workload, test.id)
        self.assertEqual(Step.objects.get().step, 'picked_up')
        # every game is kept, so FALSE becomes COMPACT; the book index moves on by what was handed out
        self.assertEqual(workload['test']['upload_pgns'], 'COMPACT')
        self.assertEqual(Test.objects.get().book_index, 1 + 16)  # 16 pairs of one runner

    def test_handing_out_reports_the_test_to_nyayo(self):
        test = make_test()
        self.handed_out(test)
        self.assertEqual(Match.objects.get().pk, test.match_id())
        self.assertEqual(Match.objects.get().machines[0]['name'], 'hunt')

    def test_nothing_is_handed_out_that_is_not_approved_finished_or_deleted(self):
        make_test(approved=False)
        make_test(finished=True)
        make_test(deleted=True)
        self.sign_in()
        self.assertEqual(self.worker('clientGetWorkload').json(), {})

    def test_the_highest_priority_wins(self):
        make_test(priority=0)
        high = make_test(priority=5)
        self.assertEqual(self.handed_out(high)['test']['id'], high.id)

    def test_a_blacklisted_test_is_skipped(self):
        test = make_test()
        self.sign_in()
        self.assertEqual(self.worker('clientGetWorkload', blacklist=[test.id]).json(), {})

    def test_a_test_needing_more_threads_than_the_worker_has_is_skipped(self):
        make_test(dev_options='Threads=8 Hash=8', base_options='Threads=8 Hash=8')
        self.sign_in()
        self.assertEqual(self.worker('clientGetWorkload').json(), {})

    def test_a_test_for_an_engine_the_worker_cannot_build_is_skipped(self):
        make_test()
        self.sign_in(cpu_flags=[])
        self.assertEqual(self.worker('clientGetWorkload').json(), {})

    def test_a_test_a_worker_has_left_is_not_handed_to_it_again(self):
        test = make_test()
        self.handed_out(test)
        result = Result.objects.get()
        for side in ('dev', 'base'):
            Step.objects.create(result=result, step='compile', side=side, state='failed', blame='unclear', tries=2)
        self.assertEqual(self.worker('clientGetWorkload').json(), {})

    def test_a_tune_is_handed_out_with_its_parameters(self):
        from nyayo.lab.spsa_utils import create_spsa_run

        class Request:
            POST = {'spsa_alpha': '0.602', 'spsa_gamma': '0.101', 'spsa_A_ratio': '0.1', 'spsa_iterations': '5000',
                    'spsa_reporting_type': 'BATCHED', 'spsa_distribution_type': 'MULTIPLE', 'spsa_pairs_per': '4',
                    'spsa_inputs': 'Aspiration, int, 20, 5, 50, 4, 0.002', 'spsa_update_rule': 'PLAIN'}

        test = make_test(test_mode='SPSA')
        create_spsa_run(test, Request())
        workload = self.handed_out(test)
        self.assertEqual(workload['reporting_type'], 'BATCHED')
        self.assertEqual(set(workload['spsa']), {'Aspiration'})
        self.assertEqual(workload['distribution']['runner-count'], 2)


class Waiting(Api):
    def old(self, **delta):
        import datetime
        from django.utils import timezone
        Machine.objects.filter(pk=self.machine_id).update(updated=timezone.now() - datetime.timedelta(**delta))

    def test_asking_for_work_and_getting_none_is_being_heard_from(self):
        self.sign_in()
        self.old(minutes=20)
        Machine.objects.filter(pk=self.machine_id).update(mnps=1.5)
        self.assertEqual(self.worker('clientGetWorkload').json(), {})
        machine = Machine.objects.get(pk=self.machine_id)
        self.assertGreater(machine.updated, timezone.now() - datetime.timedelta(seconds=30))
        self.assertEqual(machine.mnps, 1.5)
        self.assertEqual(machine.info['machine_name'], 'hunt')

    def test_a_poll_costs_one_small_write(self):
        self.sign_in()
        with CaptureQueriesContext(connection) as queries:
            self.worker('clientGetWorkload')
        writes = [q['sql'] for q in queries if q['sql'].lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE'))]
        # the machine's own write, and the one small upsert of its hour for the graph (lab/hours.py)
        self.assertEqual(len(writes), 2, writes)
        self.assertEqual(sum('lab_machinehour' in w for w in writes), 1, writes)
        self.assertTrue(all('"info"' not in w for w in writes))

    def test_the_workload_it_has_left_is_let_go_and_that_test_told(self):
        test = make_test(name='first')
        self.handed_out(test)
        self.assertEqual(Machine.objects.get(pk=self.machine_id).workload, test.id)
        self.assertTrue(Match.objects.get(pk=test.match_id()).machines[0]['active'])
        Test.objects.filter(pk=test.id).update(finished=True)
        self.assertEqual(self.worker('clientGetWorkload').json(), {})
        self.assertEqual(Machine.objects.get(pk=self.machine_id).workload, 0)

    def test_a_machine_that_still_has_a_test_is_not_made_idle_by_asking_while_it_gets_work(self):
        test = make_test()
        self.handed_out(test)
        self.assertEqual(self.worker('clientGetWorkload').json()['workload']['test']['id'], test.id)
        self.assertEqual(Machine.objects.get(pk=self.machine_id).workload, test.id)

    def test_the_page_says_idle_then_gone_quiet(self):
        self.sign_in()
        self.old(minutes=20)
        self.assertEqual(self.client.get('/machines').context['rows'][0]['state'], 'quiet')
        self.worker('clientGetWorkload')
        self.assertEqual(self.client.get('/machines').context['rows'][0]['state'], 'idle')
        self.old(minutes=6)
        self.assertEqual(self.client.get('/machines').context['rows'][0]['state'], 'quiet')


class ResponseTime(Api):
    def latest(self):
        return Machine.objects.get(pk=self.machine_id).rtt_ms

    def poll(self, path='clientGetWorkload', socket=None, **form):
        extra = {} if socket is None else {'gunicorn.socket': socket}
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(f'/{path}/', {'machine_id': self.machine_id, 'secret': self.secret, **form}, **extra)

    def test_it_is_the_kernels_round_trip_in_ms_from_a_poll_that_gets_nothing(self):
        self.sign_in()
        self.poll(socket=FakeSocket(12500))
        self.assertEqual(self.latest(), 12.5)
        self.poll(socket=FakeSocket(800))
        self.assertEqual(self.latest(), 0.8)

    def test_it_is_read_from_a_poll_that_is_given_work(self):
        test = make_test()
        self.sign_in()
        self.assertTrue(self.poll(socket=FakeSocket(3000)).json()['workload'])
        self.assertEqual(self.latest(), 3.0)
        self.assertEqual(Machine.objects.get(pk=self.machine_id).workload, test.id)

    def test_it_is_read_from_a_heartbeat(self):
        test = make_test()
        self.sign_in()
        self.poll('clientHeartbeat', socket=FakeSocket(7000), test_id=test.id)
        self.assertEqual(self.latest(), 7.0)

    def test_what_the_worker_says_of_it_is_not_believed(self):
        self.sign_in()
        self.poll(rtt_ms='1')
        self.assertIsNone(self.latest())

    def test_a_request_that_cannot_be_measured_is_answered_all_the_same(self):
        self.sign_in()
        for sock in (None, FakeSocket(raises=OSError('closed')), FakeSocket(raises=AttributeError('TCP_INFO')), FakeSocket(0), FakeSocket(short=True)):
            self.assertEqual(self.poll(socket=sock).json(), {})
        self.assertIsNone(self.latest())

    def test_one_that_cannot_be_measured_leaves_the_last_value(self):
        self.sign_in()
        self.poll(socket=FakeSocket(4000))
        self.poll(socket=FakeSocket(raises=OSError('closed')))
        self.assertEqual(self.latest(), 4.0)

    def test_a_poll_is_still_one_small_write_with_it(self):
        self.sign_in()
        with CaptureQueriesContext(connection) as queries:
            self.poll(socket=FakeSocket(5000))
        writes = [q['sql'] for q in queries if q['sql'].lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE'))]
        # the machine's own write, and the one small upsert of its hour for the graph (lab/hours.py)
        self.assertEqual(len(writes), 2, writes)
        self.assertEqual(sum('lab_machinehour' in w for w in writes), 1, writes)
        self.assertTrue(all('"info"' not in w for w in writes))


class Results(Api):
    def submit(self, workload, penta='0 0 0 0 0', trinomial='0 0 0', **extra):
        return self.worker('clientSubmitResults', test_id=workload['test']['id'], result_id=workload['result']['id'],
                           crashes=0, timelosses=0, illegals=0, trinomial=trinomial, pentanomial=penta, **extra).json()

    def test_a_workloads_results_are_added_to_the_test_the_result_and_the_match(self):
        test = make_test()
        workload = self.handed_out(test)
        self.assertEqual(self.submit(workload, penta='0 1 2 1 0', trinomial='2 4 2'), {})
        test = Test.objects.get()
        self.assertEqual((test.games, test.wins, test.draws, test.losses), (8, 2, 4, 2))
        self.assertEqual(test.as_penta(), (0, 1, 2, 1, 0))
        result = Result.objects.get()
        self.assertEqual((result.games, result.wins, result.LD), (8, 2, 1))
        match = Match.objects.get()
        self.assertEqual(match.results.get().games, 8)
        self.assertEqual(match.machines[0]['games'], 8)

    def test_an_sprt_moves_its_llr_and_ends_on_a_bound(self):
        test = make_test(alpha=0.05, beta=0.05, lowerllr=-2.94, upperllr=2.94)
        workload = self.handed_out(test)
        self.assertEqual(self.submit(workload, penta='0 5 20 15 0', trinomial='10 20 10'), {})
        first = Test.objects.get()
        self.assertNotEqual(first.currentllr, 0)
        self.assertFalse(first.finished)
        self.assertEqual(self.submit(workload, penta='0 0 0 0 400', trinomial='0 0 400'), {'stop': True})
        last = Test.objects.get()
        self.assertTrue(last.finished and last.passed and not last.failed)
        self.assertGreater(last.currentllr, 2.94)
        match = Match.objects.get()
        self.assertEqual((match.state, match.sprt['result']), ('finished', 'passed'))

    def test_a_finished_test_refuses_more(self):
        test = make_test()
        workload = self.handed_out(test)
        Test.objects.filter(id=test.id).update(finished=True)
        self.assertEqual(self.submit(workload, penta='0 0 1 0 0', trinomial='0 2 0'), {'stop': True})
        self.assertEqual(Test.objects.get().games, 0)

    def test_a_games_test_ends_at_its_game_count(self):
        test = make_test(test_mode='GAMES', max_games=8)
        workload = self.handed_out(test)
        self.assertEqual(self.submit(workload, penta='0 0 4 0 0', trinomial='0 8 0'), {'stop': True})
        match = Match.objects.get()
        self.assertEqual((match.state, match.kind), ('finished', 'games'))

    def test_a_tune_moves_its_parameters_within_bounds(self):
        from nyayo.lab.spsa_utils import create_spsa_run

        class Request:
            POST = {'spsa_alpha': '0.602', 'spsa_gamma': '0.101', 'spsa_A_ratio': '0.1', 'spsa_iterations': '5000',
                    'spsa_reporting_type': 'BATCHED', 'spsa_distribution_type': 'SINGLE', 'spsa_pairs_per': '4',
                    'spsa_inputs': 'Aspiration, int, 20, 5, 50, 4, 0.002', 'spsa_update_rule': 'PLAIN'}

        test = make_test(test_mode='SPSA')
        create_spsa_run(test, Request())
        workload = self.handed_out(test)
        self.submit(workload, spsa_delta=json.dumps([100]))
        self.assertEqual(Test.objects.get().spsa_run.parameters.get().value, 50)

    def test_crashes_make_a_test_an_error_and_are_counted(self):
        test = make_test()
        workload = self.handed_out(test)
        self.worker('clientSubmitResults', test_id=test.id, result_id=workload['result']['id'], crashes=2, timelosses=1,
                    illegals=0, trinomial='0 2 0', pentanomial='0 0 1 0 0')
        self.assertTrue(Test.objects.get().error)
        result = Result.objects.get()
        self.assertEqual((result.crashes, result.timeloss), (2, 1))

    def test_nps_figures(self):
        test = make_test()
        workload = self.handed_out(test)
        self.worker('clientSubmitNPS', nps=3000000, dev_nps=2000000, base_nps=1000000)
        machine = Machine.objects.get()
        self.assertEqual((machine.mnps, machine.dev_mnps, machine.base_mnps), (3.0, 2.0, 1.0))
        for _ in range(2):
            self.worker('clientSubmitNPSStats', result_id=workload['result']['id'], dev_nodes=10, dev_time=2,
                        dev_time_scaled=3, base_nodes=20, base_time=4, base_time_scaled=5)
        result = Result.objects.get()
        self.assertEqual((result.dev_nodes, result.base_time_scaled), (20, 10))

    def test_the_heartbeat_says_when_to_stop(self):
        test = make_test()
        self.handed_out(test)
        self.assertEqual(self.worker('clientHeartbeat', test_id=test.id).json(), {})
        Test.objects.filter(id=test.id).update(finished=True)
        self.assertEqual(self.worker('clientHeartbeat', test_id=test.id).json(), {'stop': True})


class Steps(Api):
    def test_a_step_is_recorded_and_reported(self):
        test = make_test()
        workload = self.handed_out(test)
        answer = self.worker('clientReportStep', test_id=test.id, step='compile', side='dev', state='passed',
                             seconds='12.5', cached='false').json()
        self.assertEqual(answer, {})
        self.assertEqual(Step.objects.get(step='compile', side='dev').seconds, 12.5)
        walk = Match.objects.get().machines[0]['steps']
        self.assertEqual([x['state'] for x in walk][:2], ['passed', 'passed'])
        del workload

    def test_an_engines_failure_stops_the_worker_and_the_test(self):
        test = make_test()
        self.handed_out(test)
        log = "../src/eval/mobility.cpp:212:18: error: 'attacks_bb' was not declared in this scope"
        self.worker('clientReportStep', test_id=test.id, step='compile', side='dev', state='failed', detail='x', log=log)
        answer = self.worker('clientReportStep', test_id=test.id, step='compile', side='base', state='passed').json()
        self.assertEqual(answer, {'stop': True})
        self.assertTrue(Test.objects.get().finished)
        match = Match.objects.get()
        self.assertEqual(match.state, 'failed_prechecks')
        self.assertEqual(match.verdict['summary'], "Dev doesn't compile")

    def test_a_workers_failure_is_to_be_retried(self):
        test = make_test()
        self.handed_out(test)
        self.worker('clientReportStep', test_id=test.id, step='compile', side='dev', state='failed',
                    log='cc1plus: fatal error: No space left on device')
        answer = self.worker('clientReportStep', test_id=test.id, step='compile', side='base', state='passed').json()
        self.assertEqual(answer, {'retry_after': 300})
        self.assertFalse(Test.objects.get().finished)

    def test_a_match_runner_that_played_nothing_is_a_failed_running_step_to_retry(self):
        test = make_test()
        self.handed_out(test)
        answer = self.worker('clientReportStep', test_id=test.id, step='running', side='', state='failed',
                             detail='Match runner #0 ended with no finished games', log='fastchess: not found').json()
        self.assertEqual(answer, {'retry_after': 300})
        self.assertFalse(Test.objects.get().finished)
        row = Step.objects.get(step='running')
        self.assertEqual((row.state, row.blame, row.tries), ('failed', 'worker', 1))
        walk = Match.objects.get().machines[0]['steps']
        running = [x for x in walk if x['step'] == 'running'][0]
        self.assertEqual((running['state'], running['blame'], running['detail']),
                         ('failed', 'worker', 'Match runner #0 ended with no finished games'))
        self.assertEqual(running['log'], 'fastchess: not found')
        self.assertIn('tries again', running['effect'])

    def test_what_cannot_be_recorded_is_no_reason_to_fail(self):
        self.sign_in()
        self.assertEqual(self.worker('clientReportStep', test_id=999, step='compile', side='dev', state='passed').json(), {})
        self.assertEqual(self.worker('clientReportStep').json(), {})

    def test_a_bench_error_is_an_event_and_a_report(self):
        test = make_test()
        self.handed_out(test)
        self.worker('clientBenchError', test_id=test.id, error='Wrong bench: 2, expected 1')
        event = LogEvent.objects.exclude(test_id=0).get()
        self.assertEqual((event.summary, event.author, event.test_id), ('Wrong bench: 2, expected 1', 'hunt', test.id))
        self.assertFalse(Test.objects.get().finished)

    def test_an_error_keeps_its_log(self):
        test = make_test()
        self.handed_out(test)
        with tempfile.TemporaryDirectory() as data, override_settings(DATA_DIR=Path(data)):
            self.worker('clientSubmitError', test_id=test.id, error='engine crashed', logs='the log\n')
            event = LogEvent.objects.exclude(test_id=0).get()
            self.assertEqual(event.log_file, f'event{event.id}.log')
            self.assertEqual((Path(data) / 'logs' / event.log_file).read_text(), 'the log\n')


class Uploads(Api):
    def games(self, count=2):
        pgn = ''.join(f'[Event "x"]\n[Site "y"]\n[Round "{n}"]\n[White "Elephant feature"]\n[Black "Elephant main"]\n'
                      f'[Result "1-0"]\n[Subject "white"]\n[Opponent "base"]\n\n1. e4 e5 1-0\n\n' for n in range(count))
        return bz2.compress(pgn.encode())

    def upload(self, workload, data, book_index=1):
        return self.worker('clientSubmitPGN', test_id=workload['test']['id'], result_id=workload['result']['id'],
                           book_index=book_index, file=SimpleUploadedFile('x.pgn.bz2', data))

    def test_an_upload_becomes_one_of_nyayos_uploads_and_the_match_is_there_first(self):
        test = make_test()
        workload = self.handed_out(test)
        Match.objects.all().delete()
        with tempfile.TemporaryDirectory() as uploads, override_settings(NYAYO_UPLOADS_DIR=Path(uploads)):
            self.assertEqual(self.upload(workload, self.games()).json(), {})
            upload = Upload.objects.get()
            self.assertEqual((upload.match_id, upload.name, upload.state), (test.match_id(), f"{workload['result']['id']}.1", 'pending'))
            self.assertTrue((Path(uploads) / test.match_id() / upload.name).is_file())
            # the same bytes again are recognised, not a second upload
            self.upload(workload, self.games())
            self.assertEqual(Upload.objects.count(), 1)

    def test_a_workload_that_played_nothing_sends_nothing_worth_keeping(self):
        test = make_test()
        workload = self.handed_out(test)
        self.assertEqual(self.upload(workload, bz2.compress(b'')).json(), {})
        self.assertFalse(Upload.objects.exists())

    def test_a_refusal_is_logged_and_the_worker_goes_on(self):
        test = make_test()
        workload = self.handed_out(test)
        with tempfile.TemporaryDirectory() as uploads, override_settings(NYAYO_UPLOADS_DIR=Path(uploads)):
            self.upload(workload, self.games(1))
            with self.assertLogs('nyayo.lab.worker_api', 'ERROR'):
                self.assertEqual(self.upload(workload, self.games(2)).json(), {})


class Unapproved(Results):
    def test_a_worker_on_a_test_that_is_unapproved_is_told_to_stop_by_a_heartbeat_and_by_its_results(self):
        test = make_test()
        workload = self.handed_out(test)
        self.assertEqual(self.worker('clientHeartbeat', test_id=test.id).json(), {})
        Test.objects.filter(pk=test.id).update(approved=False)
        self.assertEqual(self.worker('clientHeartbeat', test_id=test.id).json(), {'stop': True})
        self.assertEqual(self.submit(workload, penta='0 1 2 1 0', trinomial='2 4 2'), {'stop': True})
        self.assertEqual(Test.objects.get().games, 0)
