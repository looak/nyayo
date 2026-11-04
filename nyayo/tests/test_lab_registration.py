"""Worker registration: hello without credentials, a token kept as a hash, the states, the describe job, what a pending
worker is refused, the owner's switches, join codes, the limits, and the migration of sessions into one row."""

import datetime
import json

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from nyayo.lab import registration, toolchains
from nyayo.lab.models import JoinCode, LogEvent, Machine, Result

from .lab_helpers import make_machine, make_test
from .test_lab_worker_api import SYSTEM, Api


class Hello(Api):
    def test_a_worker_says_hello_with_a_name_and_is_given_a_token_once_and_is_pending(self):
        answer = self.hello('hunt')
        self.assertEqual((answer['state'], answer['enabled'], answer['name']), ('pending', True, 'hunt'))
        machine = Machine.objects.get()
        self.assertEqual(answer['worker_id'], machine.id)
        self.assertTrue(len(answer['token']) >= 40)
        self.assertEqual(machine.token_hash, registration.digest(answer['token']))
        self.assertNotIn(answer['token'], repr(machine.__dict__))
        self.assertIsNone(machine.user)
        self.assertTrue(LogEvent.objects.filter(machine_id=machine.id, summary__contains='waiting for approval').exists())

    def test_a_known_token_says_hello_again_without_a_row(self):
        first = self.hello('hunt')
        again = self.hello('hunt', token=first['token'])
        self.assertEqual(again['worker_id'], first['worker_id'])
        self.assertEqual(again['token'], first['token'])  # echoed: what the client reads every answer for, and nothing new
        self.assertTrue(again['approved'] is False and first['approved'] is False)
        self.assertEqual(Machine.objects.count(), 1)

    def test_an_unknown_token_is_a_new_worker(self):
        self.hello('hunt')
        self.hello('hunt', token='made-up')
        self.assertEqual(Machine.objects.count(), 2)

    def test_a_name_that_is_taken_gets_a_number(self):
        self.hello('hunt')
        self.token = None
        self.assertEqual(self.hello('Hunt')['name'], 'Hunt (2)')
        self.token = None
        self.assertEqual(self.hello('')['name'], 'worker')
        self.token = None
        self.assertEqual(self.hello('  a   b  ')['name'], 'a b')

    def test_a_turned_away_name_is_refused_a_new_token(self):
        self.hello('hunt')
        Machine.objects.update(state='turned_away')
        self.token = None
        reply = self.client.post('/clientHello/', {'name': 'HUNT', 'client_ver': 54})
        self.assertEqual((reply.status_code, reply.json()['state']), (403, 'turned_away'))
        self.assertEqual(Machine.objects.count(), 1)

    def test_a_turned_away_worker_with_its_token_still_hears_wait(self):
        self.sign_in()
        Machine.objects.update(state='turned_away')
        self.sign_in(approve=False)
        self.assertEqual(self.worker('clientGetWorkload').json(), {'wait': 'turned away'})

    def test_no_more_than_twenty_may_be_pending_and_a_join_code_gets_past_it(self):
        for number in range(registration.PENDING_LIMIT):
            self.token = None
            self.hello('w%d' % number)
        self.token = None
        reply = self.client.post('/clientHello/', {'name': 'one more', 'client_ver': 54})
        self.assertEqual(reply.status_code, 429)
        self.assertEqual(Machine.objects.count(), registration.PENDING_LIMIT)
        code, _ = registration.make_code('owner')
        answer = self.client.post('/clientHello/', {'name': 'one more', 'client_ver': 54, 'join_code': code}).json()
        self.assertEqual(answer['state'], 'approved')

    def test_a_pending_worker_with_no_approval_goes_after_seven_days(self):
        self.hello('old')
        self.hello('new') if False else None
        Machine.objects.update(first_seen=timezone.now() - datetime.timedelta(days=8))
        self.token = None
        self.hello('fresh')
        self.assertEqual(list(Machine.objects.values_list('name', flat=True)), ['fresh'])

    def test_an_old_pending_worker_that_has_played_stays(self):
        machine = make_machine('played', state='pending')
        Result.objects.create(test=make_test(), machine=machine)
        Machine.objects.update(first_seen=timezone.now() - datetime.timedelta(days=30))
        self.assertEqual(registration.purge_pending(), 0)


class JoinCodes(Api):
    def test_a_code_approves_on_hello_once(self):
        code, row = registration.make_code('owner')
        self.assertEqual(row.code_hash, registration.digest(code))
        self.assertNotIn(code, row.code_hash)
        answer = self.hello('hunt', join_code=code)
        self.assertEqual(answer['state'], 'approved')
        self.assertEqual(JoinCode.objects.get().used_by_id, answer['worker_id'])
        self.token = None
        reply = self.client.post('/clientHello/', {'name': 'other', 'client_ver': 54, 'join_code': code})
        self.assertEqual((reply.status_code, reply.json()['error']), (403, 'Bad join code'))
        self.assertEqual(Machine.objects.count(), 1)

    def test_the_client_sends_it_as_join(self):
        code, _ = registration.make_code('owner')
        answer = self.client.post('/clientHello/', {'name': 'hunt', 'client_ver': 54, 'join': code}).json()
        self.assertEqual((answer['state'], answer['approved']), ('approved', True))

    def test_an_expired_or_unknown_code_is_refused(self):
        code, row = registration.make_code('owner')
        JoinCode.objects.update(expires=timezone.now() - datetime.timedelta(seconds=1))
        for bad in (code, 'nope'):
            reply = self.client.post('/clientHello/', {'name': 'x', 'client_ver': 54, 'join_code': bad})
            self.assertEqual(reply.status_code, 403)
        self.assertFalse(Machine.objects.exists())

    def test_a_code_is_kept_as_a_hash_and_lasts_what_it_was_made_for(self):
        code, row = registration.make_code('owner', hours=2)
        self.assertAlmostEqual((row.expires - timezone.now()).total_seconds(), 7200, delta=5)
        self.assertEqual(registration.make_code('owner', hours=100000)[1].expires.date() <= (timezone.now() + datetime.timedelta(hours=registration.CODE_HOURS_MAX, minutes=1)).date(), True)

    def test_a_known_token_with_a_code_does_not_use_it(self):
        first = self.hello('hunt')
        code, _ = registration.make_code('owner')
        self.hello('hunt', token=first['token'], join_code=code)
        self.assertIsNone(JoinCode.objects.get().used_by)


class Describe(Api):
    def test_the_first_answer_names_the_tools_until_the_worker_has_described_itself(self):
        self.sign_in(toolchains=None)
        self.assertFalse(Machine.objects.get().described)
        self.assertEqual(self.worker('clientGetWorkload').json(), {'describe': toolchains.TOOLS})
        self.assertEqual(toolchains.TOOLS, ['gcc', 'g++', 'clang', 'clang++', 'dotnet', 'rustc', 'cargo', 'python3', 'make'])
        self.sign_in()  # the same call of clientWorkerInfo, now with toolchains
        self.assertTrue(Machine.objects.get().described)
        self.assertEqual(self.worker('clientGetWorkload').json(), {})

    def test_a_first_registration_with_an_empty_toolchains_is_not_described(self):
        self.sign_in(toolchains={})
        self.assertFalse(Machine.objects.get().described)
        self.assertEqual(self.worker('clientGetWorkload').json(), {'describe': toolchains.TOOLS})

    def test_every_session_is_described_again(self):
        self.sign_in()
        self.assertTrue(Machine.objects.get().described)
        self.sign_in(toolchains=None)
        self.assertFalse(Machine.objects.get().described)
        self.assertIn('describe', self.worker('clientGetWorkload').json())

    def test_a_pending_worker_is_told_to_describe_then_to_wait_and_is_given_no_test(self):
        make_test()
        self.sign_in(approve=False, toolchains=None)
        self.assertIn('describe', self.worker('clientGetWorkload').json())
        self.sign_in(approve=False)
        self.assertEqual(self.worker('clientGetWorkload').json(), {'wait': 'waiting for approval'})
        self.assertFalse(Result.objects.exists())

    def test_what_it_says_decides_what_it_can_build(self):
        self.sign_in(approve=False, toolchains={'clang++': '18.1.3', 'make': '4.3'})
        self.assertEqual(Machine.objects.get().info['supported'], ['Elephant'])
        self.assertEqual(Machine.objects.get().info['toolchains']['clang++'], '18.1.3')


class WhatAPendingWorkerIsRefused(Api):
    ENDPOINTS = ['clientBenchError', 'clientReportStep', 'clientSubmitNPS', 'clientSubmitError', 'clientSubmitResults',
                 'clientSubmitNPSStats', 'clientHeartbeat', 'clientSubmitPGN']

    def test_every_call_of_a_session_but_the_poll_is_refused_with_no_test_source_or_setting(self):
        test = make_test()
        for state in ('pending', 'turned_away'):
            self.token = None
            self.sign_in(approve=False, name=state)
            Machine.objects.filter(token_hash=registration.digest(self.token)).update(state=state)
            for path in self.ENDPOINTS:
                reply = self.worker(path, test_id=test.id, result_id=1, error='x', step='compile', state='passed')
                self.assertEqual(reply.status_code, 403, (state, path))
                body = reply.json()
                self.assertEqual(body['state'], state)
                self.assertNotIn(str(test.id), json.dumps(body))
                self.assertNotIn('sha', json.dumps(body))
        self.assertFalse(Result.objects.exists())

    def test_the_networks_and_the_settings_are_refused_to_it_by_its_token(self):
        self.sign_in(approve=False)
        for path, call in (('/clientGetBuildInfo/', self.client.get), ('/clientGetNetwork/Elephant/net/', self.client.post)):
            reply = call(path, {'token': self.token})
            self.assertEqual(reply.status_code, 403, path)
        self.assertEqual(self.client.get('/clientGetNetwork/Elephant/net/').status_code, 403)

    def test_how_to_build_an_engine_is_open_to_a_worker_that_sends_no_token_as_it_always_was(self):
        # the client asks before it knows whether it is approved; the answer names no test and no source
        answer = self.client.get('/clientGetBuildInfo/').json()
        self.assertEqual(sorted(answer['Elephant']), ['compilers', 'cpuflags', 'path', 'private', 'systems'])

    def test_it_may_ask_for_a_version_and_for_fastchess(self):
        self.sign_in(approve=False)
        self.assertEqual(self.client.get('/clientVersionRef/').status_code, 200)
        self.assertEqual(self.client.get('/clientMatchRunnerVersionRef/').status_code, 200)

    def test_approving_lets_the_same_worker_in(self):
        test = make_test()
        self.sign_in(approve=False)
        self.assertEqual(self.worker('clientGetWorkload').json(), {'wait': 'waiting for approval'})
        Machine.objects.update(state='approved')
        self.assertEqual(self.worker('clientGetWorkload').json()['workload']['test']['id'], test.id)


class Switching(Api):
    def test_a_disabled_worker_is_told_to_wait_and_finishes_what_it_has(self):
        test = make_test()
        workload = self.handed_out(test)
        Machine.objects.update(enabled=False)
        # a workload it has finishes
        reply = self.worker('clientSubmitResults', test_id=test.id, result_id=workload['result']['id'], crashes=0, timelosses=0,
                            illegals=0, trinomial='1 0 1', pentanomial='0 0 1 0 0')
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(Result.objects.get().games, 2)
        self.assertEqual(self.worker('clientHeartbeat', test_id=test.id).status_code, 200)
        # and gets no new one
        self.assertEqual(self.worker('clientGetWorkload').json(), {'wait': 'disabled'})
        Machine.objects.update(enabled=True)
        self.assertIn('workload', self.worker('clientGetWorkload').json())

    def test_a_disabled_worker_still_has_the_settings(self):
        self.sign_in()
        Machine.objects.update(enabled=False)
        self.assertEqual(self.client.get('/clientGetBuildInfo/', {'token': self.token}).status_code, 200)


class Acting(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', password='pw', is_staff=True, is_superuser=True)

    def test_approve_turn_away_enable_and_disable_follow_the_state_and_are_logged(self):
        machine = make_machine('hunt', state='pending')
        steps = [('approve', ('approved', True)), ('disable', ('approved', False)), ('enable', ('approved', True)),
                 ('turn_away', ('turned_away', True)), ('approve', ('approved', True))]
        for action, expected in steps:
            registration.act(machine, action, 'owner')
            machine.refresh_from_db()
            self.assertEqual((machine.state, machine.enabled), expected, action)
        events = list(LogEvent.objects.filter(machine_id=machine.id).order_by('id').values_list('author', 'summary'))
        self.assertEqual([e[1].split(': ')[1] for e in events], ['APPROVE', 'DISABLE', 'ENABLE', 'TURN_AWAY', 'APPROVE'])
        self.assertEqual({e[0] for e in events}, {'owner'})

    def test_what_cannot_be_done_is_said(self):
        machine = make_machine('hunt', state='pending')
        for action in ('enable', 'disable'):
            with self.assertRaises(ValueError):
                registration.act(machine, action, 'owner')
        with self.assertRaises(ValueError):
            registration.act(machine, 'delete', 'owner')
        Machine.objects.update(state='approved')
        machine.refresh_from_db()
        with self.assertRaises(ValueError):
            registration.act(machine, 'approve', 'owner')


class OnThePage(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', password='pw', is_staff=True, is_superuser=True)

    def sign_in(self):
        self.client.login(username='owner', password='pw')

    def act(self, machine, action, **extra):
        return self.client.post('/machine-action', {'id': machine.id, 'action': action, **extra})

    def test_a_pending_worker_is_on_the_list_with_its_state_and_buttons_only_when_signed_in(self):
        make_machine('newbie', state='pending')
        page = self.client.get('/machines').content.decode()
        self.assertIn('Waiting for approval', page)
        self.assertIn('1 worker waiting for approval', page)
        self.assertNotIn('name="action"', page)
        self.sign_in()
        page = self.client.get('/machines').content.decode()
        for button in ('value="approve"', 'value="turn_away"'):
            self.assertIn(button, page)
        self.assertNotIn('value="disable"', page)

    def test_the_actions_are_behind_the_gate(self):
        machine = make_machine('newbie', state='pending')
        reply = self.act(machine, 'approve')
        self.assertEqual(reply.status_code, 302)
        self.assertIn('sign-in', reply['Location'])
        User.objects.create_user('helper', password='pw')
        self.client.login(username='helper', password='pw')
        self.assertEqual(self.act(machine, 'approve').status_code, 403)
        self.assertEqual(self.client.post('/machine-join-code').status_code, 403)
        machine.refresh_from_db()
        self.assertEqual(machine.state, 'pending')

    def test_the_owner_approves_turns_away_and_switches_from_the_list_and_the_page(self):
        self.sign_in()
        machine = make_machine('newbie', state='pending')
        self.assertRedirects(self.act(machine, 'approve'), '/machines', fetch_redirect_response=False)
        self.assertRedirects(self.act(machine, 'disable', back='machine'), '/machines/newbie', fetch_redirect_response=False)
        machine.refresh_from_db()
        self.assertEqual((machine.state, machine.enabled), ('approved', False))
        page = self.client.get('/machines/newbie').content.decode()
        self.assertIn('value="enable"', page)
        self.assertIn('Approval', page)
        self.assertEqual(self.act(machine, 'approve').status_code, 400)
        self.assertEqual(self.client.post('/machine-action', {'id': 9999, 'action': 'approve'}).status_code, 404)
        self.assertEqual(self.client.post('/machine-action', {'id': 'x', 'action': 'approve'}).status_code, 404)

    def test_a_disabled_and_a_turned_away_worker_say_so_and_are_not_playing(self):
        make_machine('off', enabled=False)
        make_machine('away', state='turned_away')
        rows = {r['name']: r for r in self.client.get('/machines').context['rows']}
        self.assertEqual((rows['off']['state'], rows['off']['health']['label']), ('disabled', 'Disabled'))
        self.assertEqual((rows['away']['state'], rows['away']['health']['label']), ('turned_away', 'Turned away'))

    def test_a_join_code_is_made_shown_once_and_works(self):
        self.sign_in()
        page = self.client.post('/machine-join-code', {'hours': '2'}).content.decode()
        self.assertIn('A join code', page)
        code = page.split('class="join-code">')[1].split('<')[0]
        self.assertEqual(JoinCode.objects.get().code_hash, registration.digest(code))
        self.assertNotIn(code, self.client.get('/machines').content.decode())
        self.assertIn('A code until', self.client.get('/machines').content.decode())
        answer = self.client.post('/clientHello/', {'name': 'joiner', 'client_ver': 54, 'join_code': code}).json()
        self.assertEqual(answer['state'], 'approved')
        self.assertNotIn('A code until', self.client.get('/machines').content.decode())
        self.assertEqual(self.client.post('/machine-join-code', {'hours': 'x'}).status_code, 400)

    def test_an_unused_code_can_be_revoked(self):
        self.sign_in()
        code, row = registration.make_code('owner')
        self.client.post('/machine-code-revoke', {'id': row.id})
        self.assertFalse(JoinCode.objects.exists())
        self.client.post('/machine-code-revoke', {'id': 'x'})


class Migrating(TestCase):
    def test_sessions_of_a_box_become_one_row_with_every_result(self):
        import importlib
        from django.apps import apps
        migration = importlib.import_module('nyayo.lab.migrations.0005_one_row_per_worker')
        user = User.objects.create_user('worker')
        test, other = make_test(), make_test()
        old = Machine.objects.create(user=user, info={'machine_name': 'hunt'})
        new = Machine.objects.create(user=user, info={'machine_name': 'Hunt'})
        lone = Machine.objects.create(user=user, info={'machine_name': 'tor'})
        anonymous = Machine.objects.create(user=user, info={'machine_name': None, 'mac_address': 'aa'})
        for machine, played in ((old, test), (old, other), (new, test), (lone, test)):
            Result.objects.create(test=played, machine=machine, games=10, wins=4, workloads=1)
        Machine.objects.update(state='pending', enabled=False)
        migration.merge(apps, type('Editor', (), {})())
        self.assertEqual(sorted(Machine.objects.values_list('name', flat=True)), sorted(['Hunt', 'tor', 'worker-%d' % anonymous.id]))
        hunt = Machine.objects.get(name='Hunt')
        self.assertEqual(hunt.id, new.id)
        self.assertEqual((hunt.state, hunt.enabled), ('approved', True))
        by_test = {r.test_id: (r.games, r.wins, r.workloads) for r in Result.objects.filter(machine=hunt)}
        self.assertEqual(by_test, {test.id: (20, 8, 2), other.id: (10, 4, 1)})
        self.assertEqual(Result.objects.count(), 3)
