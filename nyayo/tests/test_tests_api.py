"""Making a test from a script: the token, `POST /api/tests` and `GET /api/tests/presets`. GitHub is faked, as the page's are."""

import io
import json
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client

from nyayo.lab import api_tokens, github, registration
from nyayo.lab.models import ApiToken, Test
from nyayo.lab.utils import TimeControl

from .test_create_test import Reports, fake_github


class Api(Reports):
    def setUp(self):
        super().setUp()
        self.token, self.row = api_tokens.make("ob_sprt", self.owner)
        self.client = Client(enforce_csrf_checks=True)

    def auth(self, token=None):
        return {"HTTP_AUTHORIZATION": "Bearer " + (token or self.token)}

    def post(self, body=None, token=None, behind=0, down=False, **changes):
        body = {"engine": "Elephant", "preset": "STC", "dev_branch": "feature-x", "base_branch": "main", **(body or {}), **changes}
        with mock.patch.object(github, "api_get", fake_github(behind=behind, down=down)):
            return self.client.post("/api/tests", json.dumps(body), content_type="application/json", **self.auth(token))


class Token(Api):
    def test_only_the_hash_is_kept(self):
        self.assertEqual(self.row.token_hash, registration.digest(self.token))
        self.assertNotIn(self.token, repr(self.row.__dict__))

    def test_a_missing_or_bad_token_is_a_401_in_json(self):
        for headers in ({}, self.auth("nope"), {"HTTP_AUTHORIZATION": "Basic " + self.token}):
            response = self.client.post("/api/tests", "{}", content_type="application/json", **headers)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(len(response.json()["errors"]), 1)
        self.assertEqual(self.client.get("/api/tests/presets?engine=Elephant").status_code, 401)
        self.assertEqual(Test.objects.count(), 0)

    def test_a_revoked_token_is_a_401(self):
        api_tokens.revoke(ApiToken.objects.all())
        self.assertEqual(self.post().status_code, 401)

    def test_a_token_of_a_user_who_cannot_sign_in_is_a_401(self):
        self.owner.is_active = False
        self.owner.save()
        self.assertEqual(self.post().status_code, 401)

    def test_a_call_marks_the_token_used(self):
        self.assertIsNone(self.row.last_used)
        self.post()
        self.row.refresh_from_db()
        self.assertIsNotNone(self.row.last_used)

    def test_a_user_who_is_not_staff_may_not(self):
        User.objects.create_user("plain", password="pw")
        token, _ = api_tokens.make("plain", User.objects.get(username="plain"))
        self.assertEqual(self.post(token=token).status_code, 403)

    def test_the_command_makes_lists_and_revokes(self):
        out = io.StringIO()
        call_command("api_token", "create", "laptop", "--user", "staff", stdout=out, stderr=io.StringIO())
        token = out.getvalue().strip()
        self.assertEqual(ApiToken.objects.get(name="laptop").token_hash, registration.digest(token))
        listing = io.StringIO()
        call_command("api_token", "list", stdout=listing)
        self.assertIn("laptop\tstaff\tactive", listing.getvalue())
        self.assertNotIn(token, listing.getvalue())
        call_command("api_token", "revoke", "laptop", stdout=io.StringIO())
        self.assertIsNotNone(ApiToken.objects.get(name="laptop").revoked)
        with self.assertRaises(CommandError):
            call_command("api_token", "create", "laptop", "--user", "staff", stdout=io.StringIO(), stderr=io.StringIO())
        with self.assertRaises(CommandError):
            call_command("api_token", "create", "x", "--user", "nobody", stdout=io.StringIO(), stderr=io.StringIO())

    def test_the_admin_revokes_and_does_not_edit(self):
        admin = Client()
        admin.force_login(self.owner)
        url = "/admin/lab/apitoken/"
        self.assertEqual(admin.get(url).status_code, 200)
        self.assertEqual(admin.get(url + "add/").status_code, 403)
        admin.post(url, {"action": "revoke", "_selected_action": [self.row.pk]})
        self.row.refresh_from_db()
        self.assertIsNotNone(self.row.revoked)


class Creating(Api):
    def test_it_makes_a_test_as_the_token_user(self):
        response = self.post()
        self.assertEqual(response.status_code, 201)
        test = Test.objects.get()
        body = response.json()
        self.assertEqual((body["id"], body["match"], body["warnings"]), (test.id, test.match_id(), []))
        self.assertEqual(body["url"], "http://testserver/matches/" + test.match_id())
        self.assertEqual((test.author, test.approved), (self.owner, True))
        self.assertEqual((test.dev.name, test.base.name), ("feature-x", "main"))
        self.assertEqual(self.reported, [(test.pk, False, True)])

    def test_a_staff_users_test_waits_for_approval(self):
        token, _ = api_tokens.make("staff", self.staff)
        self.assertEqual(self.post(token=token).status_code, 201)
        self.assertFalse(Test.objects.get().approved)

    def test_the_preset_then_what_is_sent(self):
        self.post(preset="LTC", bounds="[0.00, 3.00]", priority=5, dev_options="Threads=1 Hash=64")
        test = Test.objects.get()
        self.assertEqual((test.elolower, test.eloupper, test.priority), (0.0, 3.0, 5))
        self.assertEqual(test.dev_options, "Threads=1 Hash=64")
        from nyayo.lab.models import EngineConfig
        from nyayo.lab.create_test import preset_values
        ltc = preset_values(EngineConfig.objects.get(name="Elephant"), "LTC")
        self.assertNotEqual(ltc["base_tc"], "8.0+0.08")
        self.assertEqual(test.base_time_control, TimeControl.parse(ltc["base_tc"]))
        self.assertEqual(test.base_options, ltc["base_options"])

    def test_a_page_error_comes_back_as_json(self):
        response = self.post(dev_branch="no-bench")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(any("Unable to parse a Bench for no-bench" in error for error in response.json()["errors"]))
        self.assertEqual(response.json()["warnings"], [])
        self.assertEqual(Test.objects.count(), 0)

    def test_a_field_the_page_checks_is_checked(self):
        response = self.post(bounds="nonsense")
        self.assertEqual(response.status_code, 400)
        self.assertIn("SPRT Bounds must be formatted as [float1, float2]", response.json()["errors"])

    def test_behind_base_is_a_warning_and_a_400_until_anyway(self):
        response = self.post(behind=14)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["warnings"], ["feature-x is 14 commits behind main."])
        self.assertEqual(len(response.json()["errors"]), 1)
        self.assertEqual(Test.objects.count(), 0)
        response = self.post(behind=14, anyway=True)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Test.objects.count(), 1)

    def test_an_unknown_engine_or_preset_lists_the_valid_names(self):
        response = self.post(engine="Stockfish")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Elephant", response.json()["errors"][0])
        response = self.post(preset="Bullet")
        self.assertEqual(response.status_code, 400)
        for name in ("STC", "LTC", "SMP STC", "SMP LTC"):
            self.assertIn(name, response.json()["errors"][0])
        self.assertEqual(Test.objects.count(), 0)

    def test_an_unknown_field_or_a_bad_body_is_a_400(self):
        self.assertIn("Unknown field bounds_typo", self.post(bounds_typo="x").json()["errors"][0])
        self.assertEqual(self.post(test_mode="SPSA").status_code, 400)
        response = self.client.post("/api/tests", "[1]", content_type="application/json", **self.auth())
        self.assertEqual(response.status_code, 400)

    def test_only_post_creates(self):
        self.assertEqual(self.client.get("/api/tests", **self.auth()).status_code, 405)


class Presets(Api):
    def get(self, query="engine=Elephant"):
        return self.client.get("/api/tests/presets?" + query, **self.auth())

    def test_it_returns_what_the_page_starts_from(self):
        from nyayo.lab.create_test import engine_table
        body = self.get().json()
        table = engine_table()["Elephant"]
        self.assertEqual((body["engine"], body["default"], body["presets"]), ("Elephant", table["default"], table["presets"]))
        self.assertIn("STC", body["presets"])

    def test_an_unknown_engine_lists_the_valid_ones(self):
        response = self.get("engine=Nope")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Elephant", response.json()["errors"][0])
