"""The worker's API: OpenBench's client* endpoints, as its Client calls them."""

import bz2
import json
import tempfile
from pathlib import Path

import datetime

from django.contrib.auth.models import User
from django.db import connection
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from nyayo.lab.models import EngineConfig, LogEvent, Machine, Result, ServerState, Step, Test
from nyayo.matches.models import Match, Upload

from .lab_helpers import make_test

SYSTEM = {
    'client_ver': 54, 'machine_name': 'hunt', 'concurrency': 4, 'sockets': 1, 'physical_cores': 4, 'syzygy_max': 0,
    'cpu_flags': ['POPCNT', 'AVX2'], 'compilers': {'Elephant': 'g++'}, 'tokens': {}, 'os_name': 'Linux',
}


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class Api(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('worker', password='pw')

    def post(self, path, **form):
        # a TestCase never commits, so the reports the views leave for after the commit are run here
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(f'/{path}/', form)

    def sign_in(self, **system):
        answer = self.post('clientWorkerInfo', username='worker', password='pw',
                           system_info=json.dumps({**SYSTEM, **system})).json()
        self.machine_id, self.secret = answer['machine_id'], answer['secret']
        return answer

    def worker(self, path, **form):
        return self.post(path, machine_id=self.machine_id, secret=self.secret, **form)

    def handed_out(self, test):
        self.sign_in()
        return self.worker('clientGetWorkload').json()['workload']


class Settings(Api):
    def test_the_client_to_fetch(self):
        self.assertEqual(self.client.get('/clientVersionRef/').json(), {
            'client_version': 54, 'client_repo_url': 'https://github.com/looak/openbench_eg', 'client_repo_ref': 'master'})

    def test_the_fastchess_to_build(self):
        self.assertEqual(self.client.get('/clientMatchRunnerVersionRef/').json(), {
            'fastchess_min_version': '1.8.1', 'fastchess_repo_url': 'https://github.com/AndyGrant/fastchess',
            'fastchess_repo_ref': 'master'})

    def test_how_to_build_each_engine(self):
        self.assertEqual(self.client.get('/clientGetBuildInfo/').json()['Elephant'], {
            'path': 'ob_build', 'compilers': ['g', 'clang'], 'cpuflags': ['POPCNT'], 'systems': ['Linux', 'Windows'],
            'private': False})

    @override_settings(NYAYO_CLIENT_VERSION=55, NYAYO_CLIENT_REPO_REF='next')
    def test_they_are_settings(self):
        answer = self.client.get('/clientVersionRef/').json()
        self.assertEqual((answer['client_version'], answer['client_repo_ref']), (55, 'next'))


class SigningIn(Api):
    def test_a_worker_signs_in_as_a_user_and_gets_a_machine(self):
        answer = self.sign_in()
        machine = Machine.objects.get(id=answer['machine_id'])
        self.assertEqual((machine.user, machine.secret), (self.user, answer['secret']))
        self.assertEqual(machine.info['supported'], ['Elephant'])
        self.assertEqual(machine.info['OPENBENCH_CONFIG_CHECKSUM'], ServerState.checksum())

    def test_a_bad_password_or_an_inactive_user_is_refused(self):
        form = {'system_info': json.dumps(SYSTEM)}
        self.assertEqual(self.post('clientWorkerInfo', username='worker', password='no', **form).json(),
                         {'error': 'Bad Credentials'})
        self.assertEqual(self.post('clientWorkerInfo', **form).json(), {'error': 'Bad Credentials'})
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.post('clientWorkerInfo', username='worker', password='pw', **form).json(),
                         {'error': 'Bad Credentials'})
        self.assertFalse(Machine.objects.exists())

    def test_an_old_client_is_told_to_update(self):
        answer = self.post('clientWorkerInfo', username='worker', password='pw',
                           system_info=json.dumps({**SYSTEM, 'client_ver': 53})).json()
        self.assertEqual(answer, {'error': 'Bad Client Version: Expected 54'})

    def test_it_supports_only_the_engines_it_can_build(self):
        self.sign_in(cpu_flags=['AVX2'])
        self.assertEqual(Machine.objects.get().info['supported'], [])
        self.sign_in(os_name='Windows', compilers={})
        self.assertEqual(Machine.objects.latest('id').info['supported'], [])
        self.sign_in(tokens={'Elephant': 'x'})
        EngineConfig.objects.filter(name='Elephant').update(private=True)
        self.sign_in(tokens={'Elephant': 'x'})
        self.assertEqual(Machine.objects.latest('id').info['supported'], ['Elephant'])

    def test_every_other_call_checks_the_machine(self):
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

    def test_a_network_is_served_to_a_signed_in_worker(self):
        self.assertEqual(self.client.post('/clientGetNetwork/Elephant/net/', {'username': 'worker', 'password': 'no'}).content,
                         b'Bad Credentials')
        response = self.client.post('/clientGetNetwork/Elephant/net/', {'username': 'worker', 'password': 'pw'})
        self.assertEqual(response.status_code, 404)


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
                    'spsa_inputs': 'Aspiration, int, 20, 5, 50, 4, 0.002'}

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
        self.assertEqual(len(writes), 1, writes)
        self.assertNotIn('"info"', writes[0])

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
        machine = Machine.objects.get(pk=self.machine_id)
        return machine.rtt_ms, machine.rtt_avg_ms

    def test_it_is_kept_from_a_poll_that_gets_nothing(self):
        self.sign_in()
        self.worker('clientGetWorkload', rtt_ms='12.5')
        self.assertEqual(self.latest(), (12.5, 12.5))
        self.worker('clientGetWorkload', rtt_ms='22.5')
        self.assertEqual(self.latest(), (22.5, 14.5))

    def test_it_is_kept_from_a_poll_that_is_given_work(self):
        test = make_test()
        self.sign_in()
        self.assertTrue(self.worker('clientGetWorkload', rtt_ms='30').json()['workload'])
        self.assertEqual(self.latest()[0], 30.0)
        self.assertEqual(Machine.objects.get(pk=self.machine_id).workload, test.id)

    def test_it_is_kept_from_a_heartbeat_and_from_results(self):
        test = make_test()
        self.sign_in()
        workload = self.worker('clientGetWorkload').json()['workload']
        self.worker('clientHeartbeat', test_id=test.id, rtt_ms='7')
        self.assertEqual(self.latest()[0], 7.0)
        self.worker('clientSubmitResults', test_id=test.id, result_id=workload['result']['id'], crashes=0, timelosses=0,
                    illegals=0, trinomial='0 0 0', pentanomial='0 0 0 0 0', rtt_ms='9')
        self.assertEqual(self.latest()[0], 9.0)

    def test_a_worker_that_sends_none_or_nonsense_leaves_it_alone(self):
        self.sign_in()
        self.worker('clientGetWorkload')
        self.assertEqual(self.latest(), (None, None))
        for value in ('abc', '-1', 'nan', '1e12'):
            self.worker('clientGetWorkload', rtt_ms=value)
        self.assertEqual(self.latest(), (None, None))

    def test_a_poll_is_still_one_small_write_with_it(self):
        self.sign_in()
        with CaptureQueriesContext(connection) as queries:
            self.worker('clientGetWorkload', rtt_ms='5')
        writes = [q['sql'] for q in queries if q['sql'].lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE'))]
        self.assertEqual(len(writes), 1, writes)
        self.assertNotIn('"info"', writes[0])


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
                    'spsa_inputs': 'Aspiration, int, 20, 5, 50, 4, 0.002'}

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

    def test_what_cannot_be_recorded_is_no_reason_to_fail(self):
        self.sign_in()
        self.assertEqual(self.worker('clientReportStep', test_id=999, step='compile', side='dev', state='passed').json(), {})
        self.assertEqual(self.worker('clientReportStep').json(), {})

    def test_a_bench_error_is_an_event_and_a_report(self):
        test = make_test()
        self.handed_out(test)
        self.worker('clientBenchError', test_id=test.id, error='Wrong bench: 2, expected 1')
        event = LogEvent.objects.get()
        self.assertEqual((event.summary, event.author, event.test_id), ('Wrong bench: 2, expected 1', 'worker', test.id))
        self.assertFalse(Test.objects.get().finished)

    def test_an_error_keeps_its_log(self):
        test = make_test()
        self.handed_out(test)
        with tempfile.TemporaryDirectory() as data, override_settings(DATA_DIR=Path(data)):
            self.worker('clientSubmitError', test_id=test.id, error='engine crashed', logs='the log\n')
            event = LogEvent.objects.get()
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
