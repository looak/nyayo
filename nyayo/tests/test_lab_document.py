"""A test's match document, built and ingested in-process."""

import json
from unittest import mock

from django.test import TestCase, override_settings

from nyayo.lab import document, steps
from nyayo.lab.models import LogEvent
from nyayo.matches.ingest import validate
from nyayo.matches.models import Match

from .lab_helpers import make_machine, make_result, make_test


@override_settings(NYAYO_INSTANCE="lab")
class Document(TestCase):
    def test_the_first_report_creates_the_match_under_the_tests_id(self):
        test = make_test()
        ingested = document.report_test(test)
        self.assertTrue(ingested.created)
        match = Match.objects.get()
        self.assertEqual(match.pk, f"openbench-lab-{test.id}")
        self.assertEqual((match.kind, match.state), ("sprt", "waiting"))
        self.assertEqual(match.name, "Elephant feature vs main")
        self.assertEqual(match.producer, {"name": "OpenBench", "version": "lab", "url": None})

    def test_a_test_may_be_given_by_its_id(self):
        test = make_test()
        self.assertIsNotNone(document.report_test(test.id))
        self.assertFalse(document.report_test(test.id).created)

    def test_the_document_is_valid_and_carries_counts_sprt_and_machines(self):
        test = make_test(games=20, wins=8, draws=6, losses=6, LL=1, LD=2, DD=3, DW=2, WW=2, currentllr=0.4)
        result = make_result(test, make_machine())
        result.games, result.wins, result.draws, result.losses = 20, 8, 6, 6
        result.LL, result.LD, result.DD, result.DW, result.WW = 1, 2, 3, 2, 2
        result.save()
        built = document.build_document(test)
        validate(built)
        self.assertEqual(built["results"][0]["penta"], [1, 2, 3, 2, 2])
        self.assertEqual(built["results"][0]["wins"], 8)
        self.assertEqual(built["sprt"]["llr"], 0.4)
        self.assertEqual(built["machines"][0]["name"], "hunt")
        self.assertEqual(built["subject"]["options"], {"Threads": 1, "Hash": 8})
        self.assertEqual(built["subject"]["branch"], "feature")
        self.assertEqual(built["match"]["state"], "running")

    def test_the_state_follows_the_test(self):
        test = make_test()
        self.assertEqual(document.match_state(test), ("waiting", None))
        result = make_result(test, make_machine())
        steps.start_walk(result)
        steps.record_step(result, {"step": "running", "side": "", "state": "passed"})
        self.assertEqual(document.match_state(test), ("running", None))
        test.finished, test.passed = True, True
        self.assertEqual(document.match_state(test), ("finished", "passed"))
        test.passed, test.failed = False, True
        self.assertEqual(document.match_state(test), ("finished", "failed"))
        test.finished = test.failed = False
        test.deleted = True
        self.assertEqual(document.match_state(test), ("stopped", None))

    def test_a_tune_or_a_games_test_is_a_games_match(self):
        test = make_test(test_mode="GAMES", max_games=100)
        built = document.build_document(test)
        self.assertEqual((built["match"]["kind"], built["sprt"], built["stats"]), ("games", None, None))
        validate(built)

    def test_a_stopped_test_says_when(self):
        test = make_test(finished=True)
        LogEvent.objects.create(author="owner", summary="STOP", log_file="", machine_id=0, test_id=test.id)
        self.assertEqual(document.match_state(test), ("stopped", None))
        self.assertIsNotNone(document.build_document(test)["match"]["finished_at"])

    def test_counts_only_grow_so_a_later_report_updates(self):
        test = make_test()
        document.report_test(test)
        test.games, test.wins = 2, 2
        test.save()
        result = make_result(test, make_machine())
        result.games, result.wins = 2, 2
        result.save()
        self.assertFalse(document.report_test(test).created)
        self.assertEqual(Match.objects.get().results.get().games, 2)

    def test_a_failed_report_is_logged_and_not_raised(self):
        test = make_test()
        with mock.patch.object(document, "build_document", side_effect=RuntimeError("boom")):
            with self.assertLogs("nyayo.lab.document", "ERROR"):
                self.assertIsNone(document.report_test(test))
        self.assertFalse(Match.objects.exists())

    def test_it_is_json(self):
        json.dumps(document.build_document(make_test()))
