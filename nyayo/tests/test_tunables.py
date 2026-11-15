"""Reading an engine's parameters from its tune build: the job (one per commit, a failure read again on request), what a worker
reports, the JSON the create-tune page polls, and the engine's page with Refresh."""

import json
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from nyayo.lab import github, tunables
from nyayo.lab.models import EngineConfig, TunableList

from .lab_helpers import make_machine
from .test_create_test import BASE_SHA, SHA, fake_github
from .test_lab_worker_api import Api

LINES = "Skill, int, 30, 0, 100, 5, 0.002\nrfp_params__margin, int, 80, 0, 300, 15, 0.002"


def elephant():
    return EngineConfig.objects.get(name="Elephant")


class Parsing(TestCase):
    def test_the_lines_are_checked_and_kept_in_order(self):
        lines, problem = tunables.check_lines(LINES + "\n\n")
        self.assertEqual((len(lines), problem), (2, None))
        self.assertEqual(lines[0], "Skill, int, 30, 0, 100, 5, 0.002")

    def test_a_line_that_is_not_seven_fields_is_refused(self):
        for text in ("Skill, int, 30, 0, 100, 5", "Skill, long, 30, 0, 100, 5, 0.002", "Skill, int, x, 0, 100, 5, 0.002",
                     "a b, int, 1, 0, 2, 1, 0.1", "", "A, int, 1, 0, 2, 1, 0.1\nA, int, 1, 0, 2, 1, 0.1"):
            lines, problem = tunables.check_lines(text)
            self.assertIsNone(lines, text)
            self.assertTrue(problem, text)

    def test_the_ordinary_options_are_the_standard_ones_and_those_the_presets_set(self):
        config = elephant()
        config.presets = {"test_presets": {"STC": {"dev_options": "Threads=1 Hash=8 Contempt=0 Mystery=2"}}}
        names = tunables.ordinary_options(config)
        for name in ("Threads", "Hash", "Move Overhead", "Mystery", "Contempt"):
            self.assertIn(name, names)

    def test_a_done_row_reads_as_dicts(self):
        row = TunableList(lines=LINES)
        self.assertEqual(tunables.parse(row)[1], {"name": "rfp_params__margin", "type": "int", "value": "80", "min": "0", "max": "300",
                                                  "c_end": "15", "r_end": "0.002"})


class Queueing(TestCase):
    def test_a_commit_is_queued_once_however_many_ask(self):
        first, queued = tunables.request(elephant(), SHA, "feature-x")
        second, again = tunables.request(elephant(), SHA, "other-name")
        self.assertEqual((queued, again, first.pk), (True, False, second.pk))
        self.assertEqual(TunableList.objects.count(), 1)
        self.assertEqual((first.make_args, first.state), ("TUNE=1", "queued"))
        self.assertTrue(first.source.endswith("/zipball/" + SHA))

    def test_another_commit_is_another_read(self):
        tunables.request(elephant(), SHA)
        tunables.request(elephant(), BASE_SHA)
        self.assertEqual(TunableList.objects.count(), 2)

    def test_a_failure_stays_until_it_is_asked_for_again(self):
        row, _ = tunables.request(elephant(), SHA)
        TunableList.objects.filter(pk=row.pk).update(state="failed", error="boom")
        row, queued = tunables.request(elephant(), SHA)
        self.assertEqual((row.state, queued), ("failed", False))
        row, queued = tunables.request(elephant(), SHA, retry=True)
        self.assertEqual((row.state, queued, row.error), ("queued", True, "boom"))

    def test_a_done_list_is_never_read_again_even_on_retry(self):
        row, _ = tunables.request(elephant(), SHA)
        TunableList.objects.filter(pk=row.pk).update(state="done", lines=LINES, command="spsa")
        row, queued = tunables.request(elephant(), SHA, retry=True)
        self.assertEqual((row.state, queued), ("done", False))

    def test_a_read_that_is_going_on_is_left_alone(self):
        row, _ = tunables.request(elephant(), SHA)
        TunableList.objects.filter(pk=row.pk).update(state="reading")
        self.assertEqual(tunables.request(elephant(), SHA, retry=True), (TunableList.objects.get(pk=row.pk), False))


class Jobs(TestCase):
    def test_a_worker_that_supports_the_engine_is_handed_the_job_once(self):
        row, _ = tunables.request(elephant(), SHA, "feature-x")
        one, two = make_machine("one"), make_machine("two")
        job = tunables.next_job(one)
        self.assertEqual((job["id"], job["engine"], job["sha"], job["make_args"]), (row.pk, "Elephant", SHA, ["TUNE=1"]))
        self.assertIn("Threads", job["ordinary"])
        self.assertEqual(job["build"]["path"], "ob_build")
        self.assertIsNone(tunables.next_job(two))
        row.refresh_from_db()
        self.assertEqual((row.state, row.machine_id, row.worker), ("reading", one.id, "one"))

    def test_nobody_is_handed_it_who_cannot_build_the_engine_or_may_not_work(self):
        tunables.request(elephant(), SHA)
        self.assertIsNone(tunables.next_job(make_machine("other", supported=["Another"])))
        self.assertIsNone(tunables.next_job(make_machine("off", enabled=False)))
        self.assertIsNone(tunables.next_job(make_machine("new", state="pending")))

    def test_a_read_a_worker_never_finished_is_queued_again(self):
        row, _ = tunables.request(elephant(), SHA)
        tunables.next_job(make_machine("one"))
        TunableList.objects.filter(pk=row.pk).update(started=timezone.now() - timezone.timedelta(minutes=tunables.STALE_MINUTES + 1))
        self.assertIsNotNone(tunables.next_job(make_machine("two")))
        self.assertEqual(TunableList.objects.get(pk=row.pk).worker, "two")


class Reports(TestCase):
    def setUp(self):
        self.row, _ = tunables.request(elephant(), SHA)
        self.machine = make_machine("one")
        tunables.next_job(self.machine)

    def test_a_list_is_kept_with_the_command_that_answered(self):
        row, problem = tunables.report(self.machine, self.row.pk, "done", lines=LINES, command="uci")
        self.assertEqual((problem, row.state, row.command, row.lines.count("\n")), (None, "done", "uci", 1))
        self.assertIsNotNone(row.read)

    def test_a_list_that_is_not_one_is_a_failure_with_the_reason(self):
        row, problem = tunables.report(self.machine, self.row.pk, "done", lines="nonsense", command="spsa")
        self.assertTrue(problem)
        self.assertEqual(row.state, "failed")
        self.assertIn("line 1", row.error)

    def test_a_failure_keeps_the_workers_last_lines(self):
        row, _ = tunables.report(self.machine, self.row.pk, "failed", error="make: *** [all] Error 1")
        self.assertEqual((row.state, row.error), ("failed", "make: *** [all] Error 1"))

    def test_another_worker_cannot_report_it(self):
        _, problem = tunables.report(make_machine("other"), self.row.pk, "done", lines=LINES, command="spsa")
        self.assertEqual(problem, "This read is not yours")
        self.assertEqual(TunableList.objects.get(pk=self.row.pk).state, "reading")

    def test_progress_is_a_detail_and_a_done_row_is_not_changed(self):
        tunables.report(self.machine, self.row.pk, "active", detail="building")
        self.assertEqual(TunableList.objects.get(pk=self.row.pk).detail, "building")
        tunables.report(self.machine, self.row.pk, "done", lines=LINES, command="spsa")
        row, problem = tunables.report(self.machine, self.row.pk, "failed", error="late")
        self.assertEqual((row.state, problem), ("done", None))


class Endpoint(Api):
    def test_the_job_comes_ahead_of_a_workload_and_the_report_is_taken(self):
        row, _ = tunables.request(elephant(), SHA, "feature-x")
        self.sign_in()
        job = self.worker("clientGetWorkload").json()["tunables"]
        self.assertEqual((job["id"], job["sha"], job["make_args"]), (row.pk, SHA, ["TUNE=1"]))
        # nobody else is given it, and nothing more is left for this worker
        self.assertEqual(self.worker("clientGetWorkload").json(), {})
        self.assertEqual(self.worker("clientReportTunables", id=row.pk, state="active", detail="building").status_code, 200)
        reply = self.worker("clientReportTunables", id=row.pk, state="done", lines=LINES, command="spsa")
        self.assertEqual(reply.status_code, 200)
        row.refresh_from_db()
        self.assertEqual((row.state, row.command), ("done", "spsa"))

    def test_a_report_of_a_bad_list_is_refused_and_fails_the_read(self):
        row, _ = tunables.request(elephant(), SHA)
        self.sign_in()
        self.worker("clientGetWorkload")
        reply = self.worker("clientReportTunables", id=row.pk, state="done", lines="x", command="spsa")
        self.assertEqual(reply.status_code, 409)
        self.assertEqual(TunableList.objects.get(pk=row.pk).state, "failed")

    def test_a_report_of_nothing_is_a_400_and_of_a_stranger_is_a_404(self):
        self.sign_in()
        self.assertEqual(self.worker("clientReportTunables", state="done").status_code, 400)
        self.assertEqual(self.worker("clientReportTunables", id=9999, state="done").status_code, 404)

    def test_a_worker_that_is_not_approved_is_not_listened_to(self):
        row, _ = tunables.request(elephant(), SHA)
        self.sign_in(approve=False)
        reply = self.worker("clientReportTunables", id=row.pk, state="done", lines=LINES, command="spsa")
        self.assertEqual(reply.status_code, 403)

    def test_an_old_client_is_told_to_update(self):
        self.sign_in()
        Machine = __import__("nyayo.lab.models", fromlist=["Machine"]).Machine
        row = Machine.objects.get(id=self.machine_id)
        row.info = {**row.info, "client_ver": 57}
        row.save()
        answer = self.worker("clientGetWorkload").json()
        self.assertIn("Bad Client Version", answer["error"])


class Pages(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(self.owner)
        patch = mock.patch.object(github, "api_get", fake_github())
        patch.start()
        self.addCleanup(patch.stop)

    def state(self, **query):
        return self.client.get(reverse("tunables_state"), {"engine": "Elephant", **query}).json()

    def read(self, **form):
        return self.client.post(reverse("tunables_read"), {"engine": "Elephant", **form}).json()

    def test_the_pages_need_a_signed_in_owner(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("tunables_state"), {"engine": "Elephant", "ref": "main"}).status_code, 302)
        self.assertEqual(self.client.post(reverse("tunables_read"), {"engine": "Elephant", "ref": "main"}).status_code, 302)
        self.assertEqual(TunableList.objects.count(), 0)

    def test_a_branch_nobody_read_is_none_and_looking_it_up_queues_nothing(self):
        answer = self.state(ref="feature-x")
        self.assertEqual((answer["state"], answer["sha"], answer["short"]), ("none", SHA, SHA[:7]))
        self.assertEqual(answer["subject"], "Reduce losing captures")
        self.assertEqual(TunableList.objects.count(), 0)

    def test_a_branch_that_is_not_there_says_so(self):
        self.assertIn("no branch or commit nowhere", self.state(ref="nowhere")["error"].lower())

    def test_github_down_says_so(self):
        with mock.patch.object(github, "api_get", fake_github(down=True)):
            self.assertIn("did not answer", self.state(ref="main")["error"])

    def test_reading_queues_once_and_then_follows_the_row(self):
        first = self.read(ref="feature-x")
        self.assertEqual((first["state"], first["queued"]), ("queued", True))
        self.assertFalse(self.read(ref="feature-x")["queued"])
        self.assertEqual(TunableList.objects.count(), 1)
        self.assertEqual(self.state(sha=SHA)["state"], "queued")
        TunableList.objects.update(state="reading", worker="hunt", detail="building")
        answer = self.state(ref="feature-x")
        self.assertEqual((answer["state"], answer["worker"], answer["detail"]), ("reading", "hunt", "building"))

    def test_a_done_list_fills_at_once_and_says_which_command_answered(self):
        row = TunableList.objects.create(engine="Elephant", sha=SHA, make_args="TUNE=1", state="done", lines=LINES, command="uci", read=timezone.now())
        answer = self.state(ref="feature-x")
        self.assertEqual((answer["state"], answer["lines"], answer["command"], answer["guessed"]), ("done", LINES, "uci", True))
        TunableList.objects.filter(pk=row.pk).update(command="spsa")
        self.assertFalse(self.state(ref="feature-x")["guessed"])

    def test_a_failure_is_shown_with_its_log_and_read_again_on_retry(self):
        TunableList.objects.create(engine="Elephant", sha=SHA, make_args="TUNE=1", state="failed", error="g++: error: nope")
        self.assertEqual(self.state(ref="feature-x")["error"], "g++: error: nope")
        self.assertEqual(self.read(ref="feature-x")["state"], "failed")
        again = self.read(ref="feature-x", retry="1")
        self.assertEqual((again["state"], again["queued"]), ("queued", True))

    def test_an_engine_that_is_not_there_is_a_404(self):
        self.assertEqual(self.client.get(reverse("tunables_state"), {"engine": "Nothing", "ref": "main"}).status_code, 404)

    def test_the_create_page_carries_the_urls_and_the_box_for_the_status(self):
        page = self.client.get(reverse("create_tune")).content.decode()
        self.assertIn('id="read-status"', page)
        self.assertIn(reverse("tunables_state"), page)
        self.assertIn(reverse("tunables_read"), page)


class EnginePage(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(self.owner)
        patch = mock.patch.object(github, "api_get", fake_github())
        patch.start()
        self.addCleanup(patch.stop)
        self.url = reverse("engine", args=["Elephant"])

    def test_the_index_lists_the_engines(self):
        page = self.client.get(reverse("engines")).content.decode()
        self.assertIn("Elephant", page)
        self.assertIn(self.url, page)

    def test_the_page_needs_a_signed_in_owner(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.client.post(reverse("engine_refresh", args=["Elephant"]), {"branch": "main"}).status_code, 302)

    def test_a_branch_nobody_read_offers_refresh_and_shows_what_was_read_last(self):
        TunableList.objects.create(engine="Elephant", sha=BASE_SHA, make_args="TUNE=1", state="done", lines=LINES, command="spsa", read=timezone.now())
        page = self.client.get(self.url, {"branch": "feature-x"}).content.decode()
        self.assertIn("have not been read", page)
        self.assertIn("Refresh", page)
        self.assertIn("rfp_params__margin", page)  # the newest list, said to be of another commit
        self.assertIn(BASE_SHA[:7], page)

    def test_the_default_branch_is_the_engines_main(self):
        page = self.client.get(self.url).content.decode()
        self.assertIn('value="main"', page)
        self.assertIn(BASE_SHA[:7], page)

    def test_refresh_reads_the_newest_commit_once(self):
        refresh = reverse("engine_refresh", args=["Elephant"])
        reply = self.client.post(refresh, {"branch": "feature-x"})
        self.assertRedirects(reply, self.url + "?branch=feature-x", fetch_redirect_response=False)
        self.assertEqual(TunableList.objects.get().sha, SHA)
        self.client.post(refresh, {"branch": "feature-x"})
        self.assertEqual(TunableList.objects.count(), 1)
        self.assertIn("Reading the parameters from", self.client.get(self.url, {"branch": "feature-x"}).content.decode())

    def test_a_commit_already_read_is_shown_with_when_and_is_not_queued_again(self):
        TunableList.objects.create(engine="Elephant", sha=SHA, make_args="TUNE=1", state="done", lines=LINES, command="uci",
                                   read=timezone.now(), worker="hunt")
        self.client.post(reverse("engine_refresh", args=["Elephant"]), {"branch": "feature-x"})
        self.assertEqual(TunableList.objects.get().state, "done")
        page = self.client.get(self.url, {"branch": "feature-x"}).content.decode()
        self.assertIn("by hunt", page)
        self.assertIn("a guess", page)
        self.assertIn("Skill", page)

    def test_a_failure_is_shown_and_refresh_reads_it_again(self):
        TunableList.objects.create(engine="Elephant", sha=SHA, make_args="TUNE=1", state="failed", error="make: *** Error 2", worker="hunt")
        page = self.client.get(self.url, {"branch": "feature-x"}).content.decode()
        self.assertIn("make: *** Error 2", page)
        self.client.post(reverse("engine_refresh", args=["Elephant"]), {"branch": "feature-x"})
        self.assertEqual(TunableList.objects.get().state, "queued")

    def test_github_down_still_shows_the_page(self):
        with mock.patch.object(github, "api_get", fake_github(down=True)):
            page = self.client.get(self.url, {"branch": "feature-x"})
        self.assertEqual(page.status_code, 200)
        self.assertIn("did not answer", page.content.decode())
