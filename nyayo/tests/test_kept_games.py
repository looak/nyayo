"""A gauntlet's finished games, kept in the same batches as uploads, and purge_games waiting for them."""

import bz2
from io import StringIO

from django.core.management import call_command
from django.test import override_settings

from nyayo.matches import catalogue
from nyayo.matches.models import Batch, Game, Match

from .test_export import read
from .test_games import pgn
from .test_uploads import TWO, BatchCase

FINISHED = pgn("1. e4 e5 2. Nf3 Nc6 3. Bb5 a6", "1-0", Termination="adjudication")


class Keeping(BatchCase):
    def test_a_game_that_seals_is_kept_by_the_worker(self):
        self.put_game(pgn("1. e4 e5"), game="3")
        self.assertFalse(Game.objects.get().waiting)  # still being played
        self.put_game(FINISHED, game="3")
        self.assertTrue(Game.objects.get().waiting)
        self.work()
        row = Game.objects.get()
        self.assertEqual((row.waiting, row.kept_in.number), (False, 1))
        card = catalogue.cards("gauntlet-test-1")[0]
        opponent = Match.objects.get().results.get(opponent="barbarossa").pk
        self.assertEqual((card["game"], card["colour"], card["opponent"], card["result"], card["plies"],
                          card["termination"]), ("3", "white", opponent, "1-0", 6, "adjudication"))
        self.assertEqual(Batch.objects.get().sources, [{"game": "3"}])
        # as it came, and its file stays until the batch is sealed
        self.assertEqual(catalogue.read_game("gauntlet-test-1", "3").strip(), FINISHED.strip())
        self.assertTrue(self.path("3").exists())

    def test_a_resent_game_is_kept_once(self):
        self.put_game(FINISHED, game="1")
        self.work()
        self.assertEqual(self.put_game(FINISHED, game="1").status_code, 200)
        self.work()
        self.assertEqual(Batch.objects.get().games, 1)

    def test_games_and_uploads_share_the_batches(self):
        self.put_game(FINISHED, game="1")
        self.put_batch(bz2.compress(TWO))
        self.work()
        self.assertEqual([c["game"] for c in catalogue.cards("gauntlet-test-1")], ["1.0.1", "1.0.2", "1"])

    @override_settings(NYAYO_BATCH_GAMES=2)
    def test_purge_waits_for_a_sealed_batch(self):
        for number in ("1", "2", "3"):
            self.put_game(FINISHED, game=number)
        self.put_game(pgn("1. d4"), game="4")  # live
        self.work()
        self.assertEqual([b.state for b in Batch.objects.order_by("number")], ["sealed", "open"])
        out = StringIO()
        call_command("purge_games", "--match", "gauntlet-test-1", stdout=out)
        self.assertEqual(sorted(Game.objects.values_list("game", flat=True)), ["3"])
        self.assertIn("1 finished game kept back", out.getvalue())
        self.assertTrue(self.path("3").exists())
        # the purged games are still there, in their batch, on the pages and in the export
        page = self.client.get("/matches/gauntlet-test-1/games/1")
        self.assertEqual(page.status_code, 200)
        self.assertContains(self.client.get("/matches/gauntlet-test-1"), "/games/2")
        self.assertEqual(len(read(b"".join(self.client.get("/api/games").streaming_content))), 3)

    def test_a_game_finished_before_batches_waits_for_keep_games(self):
        self.put_game(FINISHED, game="1")
        Game.objects.update(waiting=False)  # as rows from before this are
        self.work()
        self.assertEqual(Batch.objects.count(), 0)
        out = StringIO()
        call_command("purge_games", "--match", "gauntlet-test-1", stdout=out)
        self.assertIn("1 finished game kept back", out.getvalue())

    def test_the_export_has_each_game_once(self):
        self.put_game(FINISHED, game="1")
        self.assertEqual(len(read(b"".join(self.client.get("/api/games").streaming_content))), 1)
        self.work()
        self.assertEqual(len(read(b"".join(self.client.get("/api/games").streaming_content))), 1)

    def test_an_ended_match_waits_for_its_games_before_sealing(self):
        from django.utils import timezone

        self.put_game(FINISHED, game="1")
        self.work()
        self.put_game(FINISHED, game="2")
        Match.objects.update(state="finished", received_at=timezone.now() - timezone.timedelta(hours=1))
        from nyayo.matches import reader, worker

        self.assertEqual(list(worker.ended_batches()), [])
        worker.step(reader.default())  # keeps game 2
        self.assertEqual(len(list(worker.ended_batches())), 1)
        self.work()
        self.assertEqual((Batch.objects.get().state, Batch.objects.get().games), ("sealed", 2))
