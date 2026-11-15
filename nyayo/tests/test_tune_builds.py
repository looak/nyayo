"""A tune is built with its engine's tune flags (`make TUNE=1`): the words are checked, copied to the checks and the SPRT after, and
sent to the worker with the workload."""

from unittest import mock

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase

from nyayo.lab import create_test as creating, github, makeargs, tunes
from nyayo.lab.models import EngineConfig, Test

from .lab_helpers import make_machine, make_result, make_test
from .test_create_test import fake_github
from .test_lab_worker_api import Api
from .test_tunes import create, made


class Words(TestCase):
    def test_name_equals_value_words_are_fine(self):
        for text in ("", "TUNE=1", "TUNE=1 ARCH=native", "_A1=b-c/d.e", "EMPTY="):
            self.assertIsNone(makeargs.problem(text), text)

    def test_anything_else_is_not(self):
        for text in ("TUNE", "=1", "1TUNE=1", "TUNE=1;rm", "A=$(x)", "A=b&c", "A=b|c", "A=`x`", "A=b>c", "A=b<c", "--version"):
            self.assertIsNotNone(makeargs.problem(text), text)

    def test_what_the_worker_sets_itself_is_not_a_make_word(self):
        for name in ("EXE", "CXX", "CC", "EVALFILE"):
            self.assertIn("set by the worker", makeargs.problem("%s=x" % name))

    def test_the_fields_validate(self):
        with self.assertRaises(ValidationError):
            EngineConfig(name="x", source="s", tune_make_args="TUNE=1 ; ls").full_clean()
        with self.assertRaises(ValidationError):
            Test._meta.get_field("make_args").run_validators("EXE=x")
        Test._meta.get_field("make_args").run_validators("TUNE=1")


class Migration(TestCase):
    def test_elephants_tune_flags_are_tune_1(self):
        self.assertEqual(EngineConfig.objects.get(name="Elephant").tune_make_args, "TUNE=1")


class Copying(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", password="pw", is_staff=True, is_superuser=True)

    def tune(self, **changes):
        test, errors, _ = create(self.owner, **changes)
        self.assertEqual(errors, {})
        return test

    def test_a_tune_gets_its_engines_flags(self):
        self.assertEqual(self.tune().make_args, "TUNE=1")

    def test_an_engine_without_flags_makes_a_tune_without(self):
        EngineConfig.objects.filter(name="Elephant").update(tune_make_args="")
        self.assertEqual(self.tune().make_args, "")

    def test_every_other_test_gets_none(self):
        with mock.patch.object(github, "api_get", fake_github()):
            values = creating.default_values(EngineConfig.objects.get(name="Elephant"))
            values.update(engine="Elephant", dev_branch="feature-x", base_branch="main")
            test, errors, _ = creating.create_test({**values, "test_mode": "SPRT"}, self.owner)
        self.assertEqual(errors, {})
        self.assertEqual(test.make_args, "")

    def test_the_checks_and_the_sprt_after_take_the_tunes(self):
        test = made(spsa_iterations="8", spsa_snapshot_every="4", spsa_check_every="4", spsa_check_games="40")
        Test.objects.filter(pk=test.pk).update(make_args="TUNE=1")
        test = Test.objects.get(pk=test.pk)
        for _ in range(4):
            test.games += 2
            tunes.advance(test, [1.0], test.games - 2)
        check = test.spsa_run.snapshots.get(iteration=4).check_test
        self.assertEqual(check.make_args, "TUNE=1")
        self.assertEqual(tunes.queue_sprt(test).make_args, "TUNE=1")

    def test_a_tune_made_without_flags_hands_none_on(self):
        test = made()
        self.assertEqual((tunes.queue_sprt(test).make_args, test.make_args), ("", ""))

    def ingested(self, test):
        # the page needs the match the report would make
        import json
        from nyayo.lab.document import build_document
        from nyayo.matches.ingest import ingest
        document = build_document(test)
        ingest(json.dumps(document).encode(), None, document["match"]["id"])

    def test_the_tune_page_and_the_match_page_say_what_it_was_built_with(self):
        test = self.tune()
        self.client.force_login(self.owner)
        self.ingested(test)
        self.assertContains(self.client.get("/matches/%s" % test.match_id()), "built with TUNE=1")
        self.assertContains(self.client.get("/create/tune"), "Built with <code>TUNE=1</code>")
        sprt = tunes.queue_sprt(test, values={"Aspiration": 33.4})
        self.ingested(sprt)
        self.assertContains(self.client.get("/matches/%s" % sprt.match_id()), "<dt>Built with</dt><dd>TUNE=1</dd>")

    def test_a_plain_test_says_nothing_of_it(self):
        test = make_test()
        self.client.force_login(self.owner)
        self.ingested(test)
        self.assertNotContains(self.client.get("/matches/%s" % test.match_id()), "Built with")


class Workload(Api):
    def test_the_words_go_to_the_worker_with_the_test(self):
        from nyayo.lab.get_workload import workload_to_dictionary
        test = make_test(make_args="TUNE=1")
        machine = make_machine()
        result = make_result(test, machine)
        self.assertEqual(workload_to_dictionary(test, result, machine)["test"]["make_args"], ["TUNE=1"])

    def test_an_ordinary_test_sends_none(self):
        from nyayo.lab.get_workload import workload_to_dictionary
        test = make_test()
        machine = make_machine()
        result = make_result(test, machine)
        self.assertEqual(workload_to_dictionary(test, result, machine)["test"]["make_args"], [])

    def test_a_client_that_is_not_the_servers_gets_no_workload(self):
        # the gate that makes workers update before they are handed a build with words they would ignore
        make_test(make_args="TUNE=1")
        self.sign_in()
        from nyayo.lab import config
        with mock.patch.object(config, "client_version", return_value=58):
            self.assertIn("Bad Client Version", self.worker("clientGetWorkload").json()["error"])
