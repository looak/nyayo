"""Reports by changes (docs/match-schema.md, Reports by changes): report.seq and base, the 409 on a wrong base, the
headers on the answers, the digest, and a schedule kept as rows."""

import copy
import json
import os
import time

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from nyayo.matches import digest, ingest
from nyayo.matches.models import Match, ScheduleEntry

from .test_matches_ingest import GAUNTLET, MatchTestCase

E1 = {"number": 1, "round": 1, "opponent": "barbarossa", "subject_colour": "white", "state": "finished",
      "result": "1/2-1/2", "termination": "Draw by 3-fold repetition", "fault": None}
E2 = {"number": 2, "round": 1, "opponent": "barbarossa", "subject_colour": "black", "state": "playing",
      "result": None, "termination": None, "fault": None}
E3 = {"number": 3, "round": 2, "opponent": "belette", "subject_colour": "white", "state": "finished",
      "result": "0-1", "termination": "White loses on time", "fault": {"side": "subject", "kind": "timelosses"}}
E2_DONE = dict(E2, state="finished", result="0-1", termination="Black mates")

# the contract's worked example
DIGEST_3 = "f2ffb1cc68c680de3609e169daab60a8ce76b49b068aa97f71ec5ca730f04392"
DIGEST_AFTER = "f47dc74d0112ddf62cd8c8d94b76c27606366f99e71c1fee292cad4638fa2a01"


def report(games, seq=None, base=None, **changes):
    document = copy.deepcopy(GAUNTLET)
    document["games"] = games
    if seq is not None:
        document["report"] = {"seq": seq, "base": base}
    for path, value in changes.items():
        target = document
        *parents, last = path.split("__")
        for part in parents:
            target = target[part]
        target[last] = value
    return document


class Digest(MatchTestCase):
    def test_the_contracts_worked_example(self):
        self.assertEqual(digest.digest([E1, E2, E3]), DIGEST_3)
        self.assertEqual(digest.entry_hash(E1), "c31c00e90ed62959e35278b90fcfb92de04b5f1fa78b01dfcb8e84c01a199ab0")
        self.assertEqual(digest.xor(DIGEST_3, digest.entry_hash(E2), digest.entry_hash(E2_DONE)), DIGEST_AFTER)
        self.assertEqual(digest.digest([]), "0" * 64)

    def test_only_the_eight_fields_count_and_a_missing_one_is_null(self):
        extra = dict(E2, ignored="x")
        without = {k: v for k, v in E2.items() if k != "fault"}
        self.assertEqual(digest.entry_hash(extra), digest.entry_hash(E2))
        self.assertEqual(digest.entry_hash(without), digest.entry_hash(E2))

    def test_non_ascii_is_escaped(self):
        entry = dict(E1, termination="Nulle par répétition")
        self.assertIn('"Nulle par r\\u00e9p\\u00e9tition"', digest.canonical(entry))


class WholeReports(MatchTestCase):
    def test_a_whole_report_with_a_seq_is_answered_with_what_nyayo_holds(self):
        response = self.put_match(report([E1, E2, E3], seq=1))
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual((response["Nyayo-Seq"], response["Nyayo-Schedule-Digest"]), ("1", DIGEST_3))
        match = Match.objects.get()
        self.assertEqual((match.report_seq, match.schedule_digest), (1, DIGEST_3))
        self.assertEqual(list(ScheduleEntry.objects.values_list("number", flat=True)), [1, 2, 3])
        self.assertEqual(match.schedule, [E1, E2, E3])

    def test_a_report_without_report_says_nothing_of_it(self):
        response = self.put_match(report([E1, E2, E3]))
        self.assertEqual(response.status_code, 201)
        self.assertNotIn("Nyayo-Seq", response)
        self.assertNotIn("Nyayo-Schedule-Digest", response)
        self.assertIsNone(Match.objects.get().report_seq)
        # and a document without a schedule at all (OpenBench's) as well
        document = report([])
        del document["games"]
        self.assertNotIn("Nyayo-Seq", self.put_match(document))

    def test_a_whole_report_is_accepted_whatever_nyayo_held(self):
        self.put_match(report([E1, E2, E3], seq=5))
        response = self.put_match(report([E1, E2_DONE, E3], seq=1, **{"match__updated_at": "2026-10-15T09:00:00Z"}))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response["Nyayo-Seq"], "1")
        self.assertEqual(Match.objects.get().schedule[1], E2_DONE)

    def test_a_whole_report_drops_a_scheduled_game_but_not_a_started_one(self):
        scheduled = dict(E3, state="scheduled", result=None, termination=None, fault=None)
        self.put_match(report([E1, E2, scheduled], seq=1))
        response = self.put_match(report([E1, E2], seq=2))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(ScheduleEntry.objects.count(), 2)
        self.assertEqual(response["Nyayo-Schedule-Digest"], digest.digest([E1, E2]))
        self.assertRefused(self.put_match(report([E1], seq=3)), 409, "games[2] is gone")

    def test_the_report_is_validated(self):
        for bad, reason in (({"seq": 0, "base": None}, "report.seq"), ({"seq": "1", "base": None}, "report.seq"),
                            ({"seq": 3, "base": 3}, "report.base"), ({"seq": 3, "base": 0}, "report.base"),
                            ({"seq": 3, "base": True}, "report.base"), ("one", "report must be")):
            document = report([E1])
            document["report"] = bad
            self.assertRefused(self.put_match(document), 400, reason)
        document = report([E1], seq=2, base=1)
        del document["games"]
        self.assertRefused(self.put_match(document), 400, "carries the schedule entries that changed")


class Changes(MatchTestCase):
    def setUp(self):
        super().setUp()
        self.put_match(report([E1, E2, E3], seq=1))

    def change(self, games, seq=2, base=1, **kwargs):
        return self.put_match(report(games, seq=seq, base=base, **kwargs))

    def test_a_change_goes_on_its_base_and_updates_the_digest_by_the_entries(self):
        response = self.change([E2_DONE])
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((response["Nyayo-Seq"], response["Nyayo-Schedule-Digest"]), ("2", DIGEST_AFTER))
        match = Match.objects.get()
        self.assertEqual(match.schedule, [E1, E2_DONE, E3])
        self.assertEqual(match.schedule_digest, digest.digest(match.schedule))

    def test_an_entry_left_out_is_unchanged_and_a_new_one_is_added(self):
        new = {"number": 4, "round": 2, "opponent": "belette", "subject_colour": "black", "state": "scheduled",
               "result": None, "termination": None, "fault": None}
        response = self.change([new])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Match.objects.get().schedule, [E1, E2, E3, new])
        self.assertEqual(response["Nyayo-Schedule-Digest"], digest.digest([E1, E2, E3, new]))

    def test_resending_an_unchanged_entry_changes_nothing(self):
        response = self.change([E1, E3])
        self.assertEqual(response["Nyayo-Schedule-Digest"], DIGEST_3)

    def test_the_other_parts_of_the_document_stay_whole(self):
        row = dict(GAUNTLET["results"][0], games=12, wins=6, draws=4, losses=2, penta=[0, 1, 2, 3, 0])
        response = self.change([E2_DONE], results=[row], match__updated_at="2026-10-15T09:00:00Z")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Match.objects.get().results.get(opponent="barbarossa").games, 12)

    def test_a_wrong_base_is_409_naming_the_seq_and_applies_nothing(self):
        self.change([E2_DONE])
        response = self.change([E1], seq=4, base=1)
        self.assertRefused(response, 409, "nyayo's seq for gauntlet-test-1 is 2")
        self.assertEqual((response["Nyayo-Seq"], response["Nyayo-Schedule-Digest"]), ("2", DIGEST_AFTER))
        self.assertEqual(Match.objects.get().report_seq, 2)
        # the same change twice (an answer was lost) is a wrong base the second time
        self.assertRefused(self.change([E2_DONE]), 409, "is 2")
        self.assertEqual(Match.objects.get().schedule_digest, DIGEST_AFTER)

    def test_a_base_nyayo_holds_no_seq_for_is_409_without_headers(self):
        self.put_match(report([E1], match__id="other-1", seq=None))
        document = report([E1], seq=2, base=1, match__id="other-1")
        response = self.put_match(document)
        self.assertRefused(response, 409, "holds no seq")
        self.assertNotIn("Nyayo-Seq", response)
        ghost = self.put_match(report([E1], seq=2, base=1, match__id="ghost-1"))
        self.assertRefused(ghost, 409, "holds no seq")

    def test_a_started_games_identity_is_fixed_in_a_change_too(self):
        response = self.change([dict(E2, subject_colour="white")])
        self.assertRefused(response, 409, "games[2].subject_colour changed")
        self.assertEqual(response["Nyayo-Seq"], "1")
        self.assertRefused(self.change([dict(E1, result="1-0")]), 409, "games[1] finished with 1/2-1/2")
        self.assertRefused(self.change([dict(E1, opponent="belette")]), 409, "games[1].opponent")
        self.assertEqual(Match.objects.get().report_seq, 1)

    def test_a_scheduled_game_may_be_corrected_in_a_change(self):
        scheduled = dict(E3, state="scheduled", result=None, termination=None, fault=None)
        self.put_match(report([E1, E2, scheduled], seq=7, match__id="sched-1"))
        response = self.put_match(report([dict(scheduled, subject_colour="black")], seq=8, base=7,
                                         match__id="sched-1"))
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Match.objects.get(pk="sched-1").schedule[2]["subject_colour"], "black")

    def test_what_may_not_change_is_still_refused(self):
        response = self.change([E2_DONE], settings={"tc": "60+1"}, **{"match__settings": {"tc": "60+1"}})
        self.assertRefused(response, 409, "settings")
        shrunk = {"opponent": "barbarossa", "games": 8, "wins": 4, "draws": 2, "losses": 2}
        self.assertRefused(self.change([E2_DONE], results=[shrunk]), 409, "counts only grow")

    def test_an_entry_that_is_not_valid_is_400(self):
        self.assertRefused(self.change([dict(E2, opponent="nobody")]), 400, "isn't in opponents")
        self.assertRefused(self.change([E2_DONE, E2_DONE]), 400, "used twice")

    def test_a_change_costs_the_same_however_long_the_schedule(self):
        def queries(size):
            match_id = f"long-{size}"
            entries = [dict(E2, number=n) for n in range(1, size + 1)]
            self.put_match(report(entries, seq=1, match__id=match_id))
            with CaptureQueriesContext(connection) as captured:
                response = self.put_match(report([dict(E2, number=size, state="finished", result="1-0",
                                                       termination="x")], seq=2, base=1, match__id=match_id))
            self.assertEqual(response.status_code, 200, response.content)
            return len(captured)

        self.assertEqual(queries(5), queries(400))


class Archive(MatchTestCase):
    def archived(self):
        return json.loads(ingest.archive_path("gauntlet-test-1").read_bytes())

    def test_a_whole_report_is_archived_as_sent_a_change_only_now_and_then(self):
        self.put_match(report([E1, E2, E3], seq=1))
        self.assertEqual(self.archived()["games"], [E1, E2, E3])
        self.put_match(report([E2_DONE], seq=2, base=1))
        self.assertEqual(self.archived()["games"], [E1, E2, E3])  # within the minute: not written again

    def test_the_archive_is_the_whole_truth_when_it_is_written(self):
        with override_settings(NYAYO_ARCHIVE_EVERY=0):
            self.put_match(report([E1, E2, E3], seq=1))
            self.put_match(report([E2_DONE], seq=2, base=1))
        archived = self.archived()
        self.assertEqual(archived["games"], [E1, E2_DONE, E3])
        self.assertEqual(archived["report"], {"seq": 2, "base": None})
        self.assertEqual(digest.digest(archived["games"]), Match.objects.get().schedule_digest)

    def test_it_is_written_when_the_match_ends_and_when_the_minute_is_up(self):
        self.put_match(report([E1, E2, E3], seq=1))
        self.put_match(report([E2_DONE], seq=2, base=1, match__state="finished",
                              match__finished_at="2026-10-15T09:00:00Z", match__updated_at="2026-10-15T09:00:00Z"))
        self.assertEqual(self.archived()["games"], [E1, E2_DONE, E3])
        self.assertEqual(self.archived()["match"]["state"], "finished")

    def test_an_old_archive_is_written_again(self):
        self.put_match(report([E1, E2, E3], seq=1))
        old = time.time() - 120
        os.utime(ingest.archive_path("gauntlet-test-1"), (old, old))
        self.put_match(report([E2_DONE], seq=2, base=1))
        self.assertEqual(self.archived()["games"], [E1, E2_DONE, E3])

    def test_a_change_after_an_older_archive_is_still_checked_against_the_rules(self):
        self.put_match(report([E1, E2, E3], seq=1))
        self.put_match(report([E2_DONE], seq=2, base=1))
        # the archive still has game 2 playing; the rows have it finished
        self.assertRefused(self.put_match(report([dict(E2_DONE, result="1-0")], seq=3, base=2)), 409,
                           "games[2] finished")
