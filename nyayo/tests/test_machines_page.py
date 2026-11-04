"""The Machines pages: a box per name however often its worker started, its status, what it supports, the join to the
tokens, and that nothing credential-like is ever shown, the pages being open to read."""

import datetime
import html

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from nyayo.lab.ids import match_id
from nyayo.lab.models import Machine, Result, Step
from nyayo.runs.models import Machine as Token

from .lab_helpers import make_machine, make_result, make_test


def age(machine, **delta):
    Machine.objects.filter(pk=machine.pk).update(updated=timezone.now() - datetime.timedelta(**delta))


def page(client, path="/machines"):
    reply = client.get(path)
    assert reply.status_code == 200, reply.status_code
    return html.unescape(reply.content.decode())


class List(TestCase):
    def test_it_reads_without_a_login(self):
        self.assertEqual(self.client.get("/machines").status_code, 200)
        self.assertIn("No worker has started", page(self.client))

    def test_restarts_are_one_box_with_the_totals_of_every_session(self):
        test = make_test()
        other = make_test()
        for number in range(3):
            machine = make_machine("hunt")
            result = make_result(test, machine)
            Result.objects.filter(pk=result.pk).update(games=10 * (number + 1))
        result = make_result(other, machine)
        Result.objects.filter(pk=result.pk).update(games=5)
        text = page(self.client)
        self.assertEqual(text.count('href="/machines/hunt"'), 1)
        self.assertIn("started 3 times", text)
        row = [r for r in self.client.get("/machines").context["rows"] if r["name"] == "hunt"][0]
        self.assertEqual((row["games"], row["tests"], row["sessions"], row["id"]), (65, 2, 3, machine.id))

    def test_a_worker_without_a_name_is_grouped_by_user_and_address(self):
        a = make_machine("loodinek", machine_name=None, mac_address="aa:bb")
        b = make_machine("loodinek", machine_name="None", mac_address="aa:bb")
        make_machine("loodinek", machine_name=None, mac_address="cc:dd")
        rows = self.client.get("/machines").context["rows"]
        self.assertEqual(sorted(r["name"] for r in rows), sorted(["machine %d" % b.id, "machine %d" % (b.id + 1)]))
        self.assertEqual(self.client.get("/machines/machine %d" % a.id).status_code, 200)

    def test_playing_idle_and_gone_quiet(self):
        test = make_test(name="my test")
        playing = make_machine("hunt")
        playing.workload = test.id
        playing.save()
        make_machine("tor")
        quiet = make_machine("bobby")
        age(quiet, minutes=6)
        finished = make_machine("done")
        finished.workload = make_test(finished=True).id
        finished.save()
        state = {r["name"]: r["state"] for r in self.client.get("/machines").context["rows"]}
        self.assertEqual(state, {"hunt": "playing", "tor": "idle", "bobby": "quiet", "done": "idle"})
        text = page(self.client)
        self.assertIn('<a href="/matches/%s">my test</a>' % match_id(test.id), text)
        self.assertIn("gone quiet", text)

    def test_the_five_minutes_are_the_actives(self):
        machine = make_machine("hunt")
        age(machine, minutes=4, seconds=50)
        self.assertEqual(self.client.get("/machines").context["rows"][0]["state"], "idle")
        age(machine, minutes=5, seconds=10)
        self.assertEqual(self.client.get("/machines").context["rows"][0]["state"], "quiet")

    def test_a_worker_that_plays_nothing_says_what_is_missing(self):
        make_machine("hunt", supported=[], compilers={})
        text = page(self.client)
        self.assertIn("Plays no engine.", text)
        self.assertIn("Elephant: Missing ['g', 'clang']", text)

    def test_missing_cpu_flags_and_systems_are_said_too(self):
        make_machine("hunt", supported=[], cpu_flags=[], os_name="Darwin")
        text = page(self.client)
        self.assertIn("Missing CPU flags ['POPCNT']", text)
        self.assertIn("Darwin is not one of ['Linux', 'Windows']", text)

    def test_a_worker_is_joined_to_the_token_of_its_name_and_a_token_alone_is_a_row(self):
        make_machine("Hunt")
        mine = Token(name="hunt")
        mine.new_token()
        mine.save()
        revoked = Token(name="old-laptop", active=False)
        revoked.new_token()
        revoked.save()
        text = page(self.client)
        row = [r for r in self.client.get("/machines").context["rows"] if r["name"] == "Hunt"][0]
        self.assertTrue(row["worker"] and row["token"]["name"] == "hunt")
        self.assertIn("token revoked", text)
        self.assertIn("doesn't play", text)
        self.assertEqual(text.count('class="tag worker"'), 2)  # the legend and the one worker

    def test_a_machines_link_is_there_to_follow(self):
        make_machine("hunt")
        self.assertEqual(self.client.get("/machines/hunt").status_code, 200)
        self.assertEqual(self.client.get("/machines/nobody").status_code, 404)

    def test_the_number_of_queries_does_not_grow_with_the_sessions(self):
        test = make_test()
        for _ in range(3):
            make_result(test, make_machine("hunt"))
        with CaptureQueriesContext(connection) as few:
            self.client.get("/machines")
        for _ in range(30):
            make_result(test, make_machine("hunt"))
        with CaptureQueriesContext(connection) as many:
            self.client.get("/machines")
        self.assertEqual(len(few), len(many))


class Secrets(TestCase):
    def test_nothing_credential_like_is_shown_on_either_page(self):
        machine = make_machine("hunt", tokens={"Elephant": "ghp_THE_GIT_TOKEN"}, password="pw-in-info", mac_address="de:ad:be:ef",
                               secret="info-secret")
        Machine.objects.filter(pk=machine.pk).update(secret="THE-SESSION-SECRET")
        make_result(make_test(), machine)
        Token(name="hunt", token_hash="THE-TOKEN-HASH").save()
        for path in ("/machines", "/machines/hunt"):
            reply = self.client.get(path)
            text = html.unescape(reply.content.decode())
            for secret in ("THE-SESSION-SECRET", "ghp_THE_GIT_TOKEN", "pw-in-info", "THE-TOKEN-HASH", "info-secret", "de:ad:be:ef"):
                self.assertNotIn(secret, text, path)
        self.assertIn("Has an access token for Elephant", html.unescape(self.client.get("/machines/hunt").content.decode()))

    def test_only_listed_fields_leave_the_module(self):
        from nyayo.lab import machines
        machine = make_machine("hunt", tokens={"Elephant": "ghp_X"}, api_key="k")
        shown = repr(machines.fields(machine))
        self.assertNotIn("ghp_X", shown)
        self.assertNotIn("api_key", shown)
        self.assertNotIn("secret", shown)


class Detail(TestCase):
    def test_the_system_the_engines_and_the_recent_tests(self):
        machine = make_machine("hunt", cpu_name="Ryzen 9", logical_cores=32, os_ver="6.8", cpu_flags=["POPCNT", "AVX2"],
                               compilers={"Elephant": ["clang", "18.1.3"]}, fastchess_ver="1.8.1")
        test = make_test(name="lmr")
        result = make_result(test, machine)
        Result.objects.filter(pk=result.pk).update(games=40, crashes=2, timeloss=1)
        Step.objects.create(result=result, step="bench", side="dev", state="failed", blame="engine")
        text = page(self.client, "/machines/hunt")
        for expected in ("Ryzen 9", ">POPCNT<", ">AVX2<", "clang 18.1.3", "1.8.1", "6.8"):
            self.assertIn(expected, text)
        self.assertIn('<a href="/matches/%s">lmr</a>' % match_id(test.id), text)
        recent = self.client.get("/machines/hunt").context["recent"][0]
        self.assertEqual((recent["games"], recent["crashes"], recent["timeloss"], recent["failures"]), (40, 2, 1, 1))

    def test_trouble_now_is_a_failed_step_the_worker_is_blamed_for_on_a_running_test(self):
        machine = make_machine("hunt")
        test = make_test(name="lmr")
        result = make_result(test, machine)
        retry = timezone.now() + datetime.timedelta(minutes=5)
        Step.objects.create(result=result, step="compile", side="dev", state="failed", blame="worker", tries=2,
                            detail="No space left on device", retry_at=retry)
        Step.objects.create(result=result, step="bench", side="base", state="failed", blame="engine", detail="not this one")
        text = page(self.client, "/machines/hunt")
        self.assertIn("Trouble now", text)
        self.assertIn("No space left on device", text)
        self.assertIn("2 tries, next try", text)
        self.assertNotIn("not this one", text)
        test.finished = True
        test.save()
        self.assertNotIn("Trouble now", page(self.client, "/machines/hunt"))

    def test_a_token_alone_has_a_page_too(self):
        token = Token(name="BABYLON")
        token.new_token()
        token.save()
        text = page(self.client, "/machines/BABYLON")
        self.assertIn("Has not reported", text)
        self.assertNotIn("Trouble now", text)


class FromTheMatchPage(TestCase):
    def test_a_workers_card_links_to_its_machine_only_when_there_is_a_page(self):
        from django.template.loader import render_to_string
        from nyayo.matches import sprtview
        make_machine("hunt")
        token = Token(name="BABYLON")
        token.new_token()
        token.save()
        machines = [{"name": name, "games": 1} for name in ("hunt", "babylon", "stranger")]
        rows = sprtview.workers(machines, 3)
        self.assertEqual({r["name"]: r["page"] for r in rows}, {"hunt": True, "babylon": True, "stranger": False})
        text = render_to_string("matches/_workers.html", {"show_workers": True, "workers": rows})
        self.assertIn('<a href="/machines/hunt">hunt</a>', text)
        self.assertNotIn("/machines/stranger", text)
