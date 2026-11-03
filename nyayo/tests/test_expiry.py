"""A failed start deletes itself after NYAYO_FAILED_PRECHECKS_HOURS, and nothing else does (nyayo/matches/expiry.py)."""

from datetime import timedelta
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from nyayo.matches import expiry, stepsview, worker
from nyayo.matches.ingest import archive_dir, archive_path
from nyayo.matches.models import Batch, Game, Match, MatchResult, ScheduleEntry, Upload

from .test_matches_ingest import ID, MatchTestCase
from .test_steps_page import unstarted


class Expiry(MatchTestCase):
    def failed(self, match_id=ID, ended_hours_ago=49, state="failed_prechecks"):
        """A failed start as OpenBench reports it, ended `ended_hours_ago` hours ago."""
        document = unstarted()
        document["match"]["id"] = match_id
        self.assertEqual(self.put_match(document).status_code, 201)
        match = Match.objects.get(pk=match_id)
        match.state = state
        match.finished_at = timezone.now() - timedelta(hours=ended_hours_ago) if ended_hours_ago is not None else None
        match.save()
        return match

    def meta_path(self, match_id=ID):
        return archive_dir() / f"{match_id}.meta.json"

    def assertKept(self, match_id=ID):
        self.assertEqual(expiry.expire(), [])
        self.assertTrue(Match.objects.filter(pk=match_id).exists())
        self.assertTrue(archive_path(match_id).exists())

    def test_a_failed_start_goes_after_48_hours_with_its_archive(self):
        self.failed()
        self.assertTrue(archive_path(ID).exists() and self.meta_path().exists())
        self.assertEqual(expiry.expire(), [ID])
        self.assertFalse(Match.objects.filter(pk=ID).exists())
        self.assertFalse(MatchResult.objects.filter(match_id=ID).exists())
        self.assertFalse(archive_path(ID).exists() or self.meta_path().exists())

    def test_it_waits_for_the_time_the_page_shows(self):
        match = self.failed(ended_hours_ago=47)
        self.assertKept()
        self.assertEqual(stepsview.goes_at(match), match.finished_at + timedelta(hours=48))
        self.assertEqual(expiry.expire(now=stepsview.goes_at(match) - timedelta(seconds=1)), [])
        self.assertEqual(expiry.expire(now=stepsview.goes_at(match)), [ID])

    def test_it_reads_the_setting_the_page_reads(self):
        match = self.failed(ended_hours_ago=5)
        self.assertKept()
        with override_settings(NYAYO_FAILED_PRECHECKS_HOURS=4):
            self.assertEqual(expiry.due(match, timezone.now()), True)
            self.assertEqual(stepsview.goes_in(match, timezone.now()), "now")
            self.assertEqual(expiry.expire(), [ID])

    def test_a_failed_test_sent_again_goes_on_the_next_look(self):
        match = self.failed(ended_hours_ago=60)
        # report_to_nyayo --all sends it again with its old finished_at
        document = unstarted()
        document["match"]["finished_at"] = match.finished_at.isoformat()
        self.assertEqual(self.put_match(document).status_code, 200)
        self.assertEqual(expiry.expire(), [ID])

    def test_a_test_restarted_by_hand_is_new_again(self):
        self.failed()
        document = unstarted(state="running")
        self.assertEqual(self.put_match(document).status_code, 200)
        self.assertKept()
        # and failing anew starts the clock again
        document = unstarted()
        document["match"]["finished_at"] = timezone.now().isoformat()
        self.assertEqual(self.put_match(document).status_code, 200)
        self.assertKept()

    def test_other_states_are_never_touched_however_old(self):
        for state in ("waiting", "held", "running", "stopped", "error", "finished"):
            match = self.failed(state=state, ended_hours_ago=1000)
            self.assertKept()
            match.delete()

    def test_no_finished_at_is_not_touched(self):
        self.failed(ended_hours_ago=None)
        self.assertKept()

    def test_games_in_the_results_keep_it(self):
        self.failed()
        MatchResult.objects.filter(match_id=ID).update(games=1, wins=1)
        self.assertKept()

    def test_a_game_in_any_state_keeps_it(self):
        match = self.failed()
        ScheduleEntry.objects.create(match=match, number=1, opponent="a", subject_colour="white",
                                     state="scheduled", hash="x")
        self.assertKept()
        ScheduleEntry.objects.all().delete()
        Game.objects.create(match=match, game="1", result="*")
        self.assertKept()

    def test_an_upload_keeps_it(self):
        match = self.failed()
        Upload.objects.create(match=match, name="a", bytes=1, sha256="0" * 64)
        self.assertKept()

    def test_a_batch_keeps_it(self):
        match = self.failed()
        Batch.objects.create(match=match, number=1)
        self.assertKept()

    def test_only_the_due_one_goes(self):
        self.failed("old-one", ended_hours_ago=72)
        self.failed("young-one", ended_hours_ago=2)
        self.failed("gauntlet-ish", ended_hours_ago=72, state="held")
        self.assertEqual(expiry.expire(), ["old-one"])
        self.assertEqual(sorted(Match.objects.values_list("pk", flat=True)), ["gauntlet-ish", "young-one"])
        self.assertTrue(archive_path("young-one").exists() and archive_path("gauntlet-ish").exists())
        self.assertFalse(archive_path("old-one").exists())

    def test_a_deleted_one_is_not_rebuilt(self):
        self.failed()
        expiry.expire()
        self.assertFalse(list(archive_dir().glob(f"{ID}*")))


class WorkerLooksOnceAnHour(MatchTestCase):
    def test_the_worker_looks_at_the_start_and_then_every_interval(self):
        with mock.patch("nyayo.matches.expiry.expire") as expire, \
                mock.patch("nyayo.matches.worker.time.monotonic", side_effect=[0, 10, 3600, 3601, 7300]), \
                mock.patch("nyayo.matches.worker.step", return_value=False), \
                mock.patch("nyayo.matches.worker.time.sleep", side_effect=[None, None, StopIteration]):
            with self.assertRaises(StopIteration):
                worker.run(reader=mock.Mock(name="r"))
        self.assertGreaterEqual(expire.call_count, 2)

    def test_a_failure_in_the_look_does_not_stop_the_worker(self):
        with mock.patch("nyayo.matches.expiry.expire", side_effect=OSError("disk")), \
                mock.patch("nyayo.matches.worker.step", return_value=False):
            self.assertEqual(worker.run(once=True, reader=mock.Mock()), 0)
