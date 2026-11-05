"""Create test, and stop, restart and approve: GitHub is faked, and so are step 2's document and steps modules."""

import sys
import types
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from nyayo.lab import github
from nyayo.lab.models import Engine, LogEvent, Test
from nyayo.matches.models import Engine as MatchEngine, Match

SHA = "a" * 40
BASE_SHA = "b" * 40
REPO = "https://github.com/looak/elephant"


def fake_github(branches=None, down=False, behind=0):
    """api_get as GitHub would answer it for looak/elephant."""
    branches = branches if branches is not None else {
        "feature-x": (SHA, "Reduce losing captures\n\nBench: 2318204"),
        "main": (BASE_SHA, "Merge pull request #43\n\nBench: 2301556"),
        "no-bench": ("c" * 40, "Tidy the PV table"),
    }

    def api_get(url, timeout=10):
        if down:
            raise github.GithubDown("no route")
        tail = url.split("/repos/looak/elephant/")[-1]
        if tail.startswith("branches/"):
            name = tail.split("/", 1)[1]
            if name not in branches:
                return 404, {"message": "Branch not found"}
            sha, message = branches[name]
            return 200, {"commit": {"sha": sha, "commit": {"message": message}}}
        if tail.startswith("compare/"):
            return 200, {"ahead_by": behind}
        if tail.endswith("branches-where-head"):
            return 200, [{"name": "feature-x"}]
        if tail.startswith("commits/"):
            sha = tail.split("/")[1]
            for found, message in branches.values():
                if found == sha:
                    return 200, {"sha": sha, "commit": {"message": message}}
            return 404, {}
        return 404, {}

    return api_get


def form(**changes):
    values = {"engine": "Elephant", "dev_branch": "feature-x", "base_branch": "main", "preset": "STC"}
    values.update(changes)
    return values


class Reports(TestCase):
    """The document module is step 2's: a stand-in with the one function the pages call."""

    def setUp(self):
        self.reported = []
        document = types.ModuleType("nyayo.lab.document")
        document.report_test = lambda test: self.reported.append((test.pk, test.finished, test.approved))
        steps = types.ModuleType("nyayo.lab.steps")
        self.restarted = []
        steps.restart_walks = lambda test: self.restarted.append(test.pk)
        patcher = mock.patch.dict(sys.modules, {"nyayo.lab.document": document, "nyayo.lab.steps": steps})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.owner = User.objects.create_user("owner", password="pw", is_staff=True, is_superuser=True)
        self.staff = User.objects.create_user("staff", password="pw", is_staff=True)


class Creating(Reports):
    def create(self, user=None, down=False, behind=0, **changes):
        self.client.force_login(user or self.owner)
        with mock.patch.object(github, "api_get", fake_github(down=down, behind=behind)):
            return self.client.post(reverse("create_test"), self.page_values(**changes))

    def page_values(self, **changes):
        response = self.client.get(reverse("create_test"))
        values = response.context["values"]
        values = {key: value for key, value in values.items()}
        values.update(form(**changes))
        return values

    def test_a_signed_out_visitor_is_sent_to_sign_in(self):
        self.assertEqual(self.client.get(reverse("create_test")).status_code, 302)
        self.assertEqual(self.client.post(reverse("create_test"), form()).status_code, 302)
        self.assertEqual(self.client.get(reverse("create_test_lookup")).status_code, 302)
        self.assertEqual(Test.objects.count(), 0)

    def test_the_page_has_the_engines_presets(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("create_test")).content.decode()
        for preset in ("STC", "LTC", "SMP STC", "SMP LTC"):
            self.assertIn(f'value="{preset}"', page)
        self.assertIn("8.0+0.08", page)
        self.assertIn("Advanced", page)

    def test_a_branch_makes_a_waiting_test_and_is_reported(self):
        response = self.create()
        test = Test.objects.get()
        self.assertRedirects(response, f"/matches/{test.match_id()}", fetch_redirect_response=False)
        self.assertEqual(self.reported, [(test.pk, False, True)])
        self.assertEqual((test.dev.name, test.dev.sha, test.dev.bench), ("feature-x", SHA, 2318204))
        self.assertEqual((test.base.name, test.base.sha, test.base.bench), ("main", BASE_SHA, 2301556))
        self.assertEqual(test.dev.source, f"https://api.github.com/repos/looak/elephant/zipball/{SHA}")
        self.assertEqual((test.dev_time_control, test.base_time_control), ("8.0+0.08", "8.0+0.08"))
        self.assertEqual((test.dev_options, test.workload_size), ("Threads=1 Hash=8", 32))
        self.assertEqual((test.test_mode, test.elolower, test.eloupper, test.alpha, test.beta), ("SPRT", 0.0, 5.0, 0.05, 0.05))
        self.assertEqual(test.author, self.owner)
        self.assertEqual(test.book_name, "8moves_v3.epd")
        self.assertTrue(test.approved)
        self.assertFalse(test.finished)
        self.assertEqual(LogEvent.objects.get().summary, "CREATE P=0 TP=1000")

    def test_a_preset_is_what_is_stored(self):
        self.create(preset="LTC", dev_tc="40.0+0.4", base_tc="40.0+0.4", dev_options="Threads=1 Hash=64",
                    base_options="Threads=1 Hash=64", workload_size="8")
        test = Test.objects.get()
        self.assertEqual((test.dev_time_control, test.workload_size), ("40.0+0.40", 8))

    def test_a_sha_is_named_by_its_branch(self):
        self.create(dev_branch=SHA)
        test = Test.objects.get()
        self.assertEqual((test.dev.name, test.dev.sha, test.name), ("feature-x", SHA, ""))

    def test_a_sha_with_no_branch_is_named_by_its_first_line(self):
        def api_get(url, timeout=10):
            if url.endswith("branches-where-head") or url.endswith("pulls"):
                return 200, []
            return fake_github()(url, timeout)
        self.client.force_login(self.owner)
        with mock.patch.object(github, "api_get", api_get):
            self.client.post(reverse("create_test"), self.page_values(dev_branch=SHA))
        self.assertEqual(Test.objects.get().name, "Reduce losing captures")

    def test_a_name_typed_is_kept(self):
        self.create(name="  my idea ")
        self.assertEqual(Test.objects.get().name, "my idea")

    def test_a_bench_typed_stands_in_for_the_message(self):
        self.create(dev_branch="no-bench", dev_bench="2000000")
        self.assertEqual(Test.objects.get().dev.bench, 2000000)

    def test_with_no_bench_it_is_refused(self):
        response = self.create(dev_branch="no-bench")
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Unable to parse a Bench for no-bench", status_code=400)
        self.assertEqual(Test.objects.count(), 0)
        self.assertEqual(self.reported, [])

    def test_an_unknown_branch_is_refused(self):
        response = self.create(dev_branch="feature-y")
        self.assertContains(response, "feature-y could not be found in looak/elephant", status_code=400)
        self.assertEqual(Engine.objects.count(), 0)

    def test_a_repository_that_is_no_engines_is_refused(self):
        response = self.create(dev_repo="https://github.com/someone/else")
        self.assertContains(response, "someone/else isn&#x27;t the source of an engine", status_code=400)
        self.assertEqual(Test.objects.count(), 0)

    def test_bad_settings_are_refused_by_field(self):
        response = self.create(dev_options="Threads=0 Hash=8", bounds="[5, 1]", win_adj="what", book="nope")
        self.assertEqual(response.status_code, 400)
        errors = response.context["errors"]
        self.assertEqual(set(errors), {"dev_options", "bounds", "win_adj", "book"})
        self.assertEqual(Test.objects.count(), 0)

    def test_a_branch_behind_its_base_stops_the_form(self):
        response = self.create(behind=14)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "feature-x is 14 commits behind main")
        self.assertContains(response, "Create it anyway")
        self.assertEqual(Test.objects.count(), 0)

    def test_create_it_anyway_creates_it(self):
        self.client.force_login(self.owner)
        with mock.patch.object(github, "api_get", fake_github(behind=14)):
            self.client.post(reverse("create_test"), {**self.page_values(), "anyway": "1"})
        self.assertEqual(Test.objects.count(), 1)

    def test_github_down_with_a_typed_sha_and_bench_still_creates(self):
        response = self.create(down=True, dev_branch=SHA, base_branch=BASE_SHA, dev_bench="2318204", base_bench="2301556",
                               name="by hand")
        test = Test.objects.get()
        self.assertRedirects(response, f"/matches/{test.match_id()}", fetch_redirect_response=False)
        self.assertEqual((test.dev.sha, test.dev.bench, test.base.sha, test.name), (SHA, 2318204, BASE_SHA, "by hand"))

    def test_github_down_with_a_branch_name_says_what_to_type(self):
        response = self.create(down=True)
        self.assertContains(response, "GitHub did not answer", status_code=400)
        self.assertContains(response, "type the full 40 character sha", status_code=400)
        self.assertEqual(Test.objects.count(), 0)

    def test_a_user_who_is_not_the_owner_makes_a_test_that_waits_for_approval(self):
        self.create(user=self.staff)
        self.assertFalse(Test.objects.get().approved)

    def test_the_lookup(self):
        self.client.force_login(self.owner)
        with mock.patch.object(github, "api_get", fake_github(behind=3)):
            data = self.client.get(reverse("create_test_lookup"), {"repo": REPO, "dev": "feature-x", "base": "main"}).json()
            missing = self.client.get(reverse("create_test_lookup"), {"repo": REPO, "dev": "nope", "base": "main"}).json()
            elsewhere = self.client.get(reverse("create_test_lookup"), {"repo": "https://github.com/a/b", "dev": "x"}).json()
        self.assertEqual((data["dev"]["short"], data["dev"]["bench"], data["base"]["bench"], data["behind"]), (SHA[:12], 2318204, 2301556, 3))
        self.assertTrue(missing["dev"]["missing"])
        self.assertIn("error", elsewhere)
        with mock.patch.object(github, "api_get", fake_github(down=True)):
            self.assertTrue(self.client.get(reverse("create_test_lookup"), {"repo": REPO, "dev": "main"}).json()["dev"]["down"])


class Acting(Reports):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        with mock.patch.object(github, "api_get", fake_github()):
            self.client.post(reverse("create_test"), Creating.page_values(self, ))
        self.test = Test.objects.get()
        self.reported.clear()
        self.client.logout()

    def act(self, action, user=None):
        if user:
            self.client.force_login(user)
        return self.client.post(reverse("test_action", args=[self.test.pk, action]))

    def test_signed_out_none_of_them_works(self):
        for action in ("stop", "restart", "approve"):
            self.assertEqual(self.act(action).status_code, 302)
        self.test.refresh_from_db()
        self.assertFalse(self.test.finished)
        self.assertEqual(self.reported, [])

    def test_stop_and_restart(self):
        self.assertRedirects(self.act("stop", self.owner), f"/matches/{self.test.match_id()}", fetch_redirect_response=False)
        self.test.refresh_from_db()
        self.assertTrue(self.test.finished)
        self.act("restart")
        self.test.refresh_from_db()
        self.assertFalse(self.test.finished)
        self.assertEqual(self.restarted, [self.test.pk])
        self.assertEqual([r[1] for r in self.reported], [True, False])
        self.assertEqual([e.summary for e in LogEvent.objects.order_by("id")][1:], ["STOP", "RESTART"])

    def test_approve_is_for_the_owner(self):
        Test.objects.update(approved=False)
        self.assertEqual(self.act("approve", self.staff).status_code, 403)
        self.assertFalse(Test.objects.get().approved)
        self.act("approve", self.owner)
        self.assertTrue(Test.objects.get().approved)

    def test_an_action_needs_a_post_and_a_known_name(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("test_action", args=[self.test.pk, "stop"])).status_code, 405)
        self.assertEqual(self.act("delete").status_code, 404)
        self.assertEqual(self.client.post(reverse("test_action", args=[999999, "stop"])).status_code, 404)

    def test_the_match_page_has_the_buttons_for_the_people_who_may_use_them(self):
        Match.objects.create(id=self.test.match_id(), subject=MatchEngine.objects.create(name="Elephant"), kind="sprt", state="waiting", name="x", producer={"name": "OpenBench", "url": "https://old/test/1"})
        url = f"/matches/{self.test.match_id()}"
        self.assertNotContains(self.client.get(url), "test-actions")
        self.client.force_login(self.owner)
        page = self.client.get(url).content.decode()
        self.assertIn(reverse("test_action", args=[self.test.pk, "stop"]), page)
        self.assertNotIn("/approve", page)
        self.assertIn("https://old/test/1", page)

    def test_an_old_match_has_no_openbench_link(self):
        Match.objects.create(id="sprt-12", subject=MatchEngine.objects.create(name="Elephant", commit="x"), kind="sprt", state="finished", name="old",
                             producer={"name": "OpenBench", "url": "https://old/test/12"})
        self.client.force_login(self.owner)
        page = self.client.get("/matches/sprt-12").content.decode()
        self.assertNotIn("https://old/test/12", page)
        self.assertNotIn("test-actions", page)


class Landing(TestCase):
    """With step 2's real document and steps: a test made on the page is a match, waiting, with the page's buttons."""

    def test_a_test_from_a_branch_and_from_a_sha_land_as_waiting_matches(self):
        from nyayo.matches.models import Match
        owner = User.objects.create_user("owner", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(owner)
        for ref in ("feature-x", SHA):
            with mock.patch.object(github, "api_get", fake_github()):
                values = self.client.get(reverse("create_test")).context["values"]
                response = self.client.post(reverse("create_test"), {**values, **form(dev_branch=ref)})
            test = Test.objects.latest("pk")
            self.assertRedirects(response, f"/matches/{test.match_id()}", fetch_redirect_response=False)
            match = Match.objects.get(pk=test.match_id())
            self.assertEqual(match.state, "waiting")
            page = self.client.get(f"/matches/{test.match_id()}").content.decode()
            self.assertIn(reverse("test_action", args=[test.pk, "stop"]), page)
        self.assertEqual(Match.objects.count(), 2)
        self.client.post(reverse("test_action", args=[test.pk, "stop"]))
        self.assertEqual(Match.objects.get(pk=test.match_id()).state, "stopped")
        self.client.post(reverse("test_action", args=[test.pk, "restart"]))
        self.assertEqual(Match.objects.get(pk=test.match_id()).state, "waiting")


class BranchList(Reports):
    """The branch names the page suggests: paged, kept a minute, refused for a repository that is no engine's."""

    def setUp(self):
        super().setUp()
        github._branch_cache.clear()
        self.addCleanup(github._branch_cache.clear)
        self.client.force_login(self.owner)

    def listing(self, pages, calls=None):
        def api_get(url, timeout=10):
            if calls is not None:
                calls.append(url)
            page = int(url.rsplit("page=", 1)[1])
            return 200, [{"name": name} for name in pages[page - 1]] if page <= len(pages) else []
        return api_get

    def ask(self, repo=REPO):
        return self.client.get(reverse("create_test_branches"), {"repo": repo}).json()

    def test_pages_are_followed_until_a_short_one(self):
        full = ["b%03d" % n for n in range(100)]
        calls = []
        with mock.patch.object(github, "api_get", self.listing([full, full[:0] + ["zeta", "main"]], calls)):
            data = self.ask()
        self.assertEqual(len(calls), 2)
        self.assertIn("page=2", calls[1])
        self.assertEqual(len(data["branches"]), 102)
        self.assertNotIn("error", data)

    def test_the_default_base_comes_first_then_alphabetical(self):
        with mock.patch.object(github, "api_get", self.listing([["zeta", "alpha", "main", "beta"]])):
            self.assertEqual(self.ask()["branches"], ["main", "alpha", "beta", "zeta"])

    def test_the_answer_is_kept_for_a_minute(self):
        calls = []
        with mock.patch.object(github, "api_get", self.listing([["main"]], calls)):
            self.ask()
            self.ask()
            self.assertEqual(len(calls), 1)
            with mock.patch.object(github.time, "monotonic", return_value=github.time.monotonic() + 61):
                self.ask()
        self.assertEqual(len(calls), 2)

    def test_a_repository_that_is_no_engines_is_refused_without_asking(self):
        calls = []
        with mock.patch.object(github, "api_get", self.listing([["main"]], calls)):
            data = self.ask("https://github.com/a/b")
        self.assertEqual(data["branches"], [])
        self.assertIn("error", data)
        self.assertEqual(calls, [])

    def test_github_being_down_is_an_empty_list_and_the_reason(self):
        with mock.patch.object(github, "api_get", fake_github(down=True)):
            response = self.client.get(reverse("create_test_branches"), {"repo": REPO})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["branches"], [])
        self.assertEqual(response.json()["error"], "GitHub did not answer.")

    def test_a_failure_is_not_kept(self):
        with mock.patch.object(github, "api_get", fake_github(down=True)):
            self.ask()
        with mock.patch.object(github, "api_get", self.listing([["main"]])):
            self.assertEqual(self.ask()["branches"], ["main"])

    def test_signed_out_goes_to_sign_in(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("create_test_branches"), {"repo": REPO}).status_code, 302)


class GithubAddress(TestCase):
    def test_the_api_is_githubs_unless_the_environment_says_otherwise(self):
        self.assertEqual(github.api_base(REPO), "https://api.github.com/repos/looak/elephant")
        with mock.patch.dict("os.environ", {"NYAYO_GITHUB_API": "http://127.0.0.1:9/repos/"}):
            self.assertEqual(github.api_base(REPO + "/"), "http://127.0.0.1:9/repos/looak/elephant/")
