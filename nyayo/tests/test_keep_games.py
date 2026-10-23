"""manage.py keep_games: the single games already stored, migrated into batches."""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from nyayo.matches import catalogue
from nyayo.matches.models import Batch, Game, Match, MatchResult

from .test_games import pgn
from .test_index import MATE, OPENBENCH, RESIGNED
from .test_uploads import BatchCase


class KeepGames(BatchCase):
    def setUp(self):
        super().setUp()
        Match.objects.update(settings=OPENBENCH)
        MatchResult.objects.update(timelosses={}, crashes={}, illegal={})
        tags = {"Subject": "white", "Opponent": "barbarossa"}
        for number, (moves, result) in enumerate([(MATE, "1-0"), (RESIGNED, "0-1"), ("1. e4 e5", "1-0"),
                                                  (MATE, "1-0")], 1):
            self.put_game(pgn(moves, result, **tags), game=str(number))
        self.put_game(pgn("1. d4 d5"), game="9")  # still being played
        # as stored before nyayo kept batches: nothing queued for the worker
        Game.objects.update(waiting=False)

    def keep(self, *args):
        out = StringIO()
        call_command("keep_games", *args, stdout=out)
        return out.getvalue()

    def test_a_dry_run_counts_and_writes_nothing(self):
        out = self.keep("--dry-run")
        self.assertIn("4 games would be kept", out)
        self.assertIn("'rule': 2", out)
        self.assertIn("'resignation': 1", out)
        self.assertEqual(Batch.objects.count(), 0)
        self.assertEqual(Game.objects.filter(kept_in__isnull=False).count(), 0)

    @override_settings(NYAYO_BATCH_GAMES=3)
    def test_it_keeps_every_finished_game_and_checks_itself(self):
        out = self.keep()
        self.assertIn("export before and after: the same 4 games", out)
        self.assertIn("all checks passed", out)
        self.assertEqual(Game.objects.filter(kept_in__isnull=False).count(), 4)
        self.assertEqual([b.state for b in Batch.objects.order_by("number")], ["sealed"] + ["open"])
        self.assertEqual([c["game"] for c in catalogue.cards("gauntlet-test-1")], ["1", "2", "3", "4"])
        # the live game is left alone, and its files all stay
        self.assertFalse(Game.objects.get(game="9").kept_in)
        self.assertTrue(self.path("2").exists())

    def test_endings_are_recorded_with_the_inferred_flag(self):
        self.keep()
        from nyayo.matches import cards

        by_game = {}
        for c in catalogue.index().execute("SELECT game, ending, flags FROM card"):
            by_game[c[0]] = (cards.ENDINGS[c[1]], bool(c[2] & cards.INFERRED))
        self.assertEqual(by_game["1"], ("rule", False))
        self.assertEqual(by_game["2"], ("resignation", True))
        self.assertEqual(by_game["3"], ("unexplained", False))

    def test_run_again_it_does_nothing(self):
        self.keep()
        self.assertIn("0 finished games in 0 matches to keep", self.keep())
        self.assertEqual(sum(Batch.objects.values_list("games", flat=True)), 4)

    def test_purge_then_waits_for_sealed_batches(self):
        with override_settings(NYAYO_BATCH_GAMES=2):
            self.keep()
        call_command("purge_games", "--match", "gauntlet-test-1", stdout=StringIO())
        # the batches are sealed (full, and the match has ended or not: the gauntlet fixture is running)
        sealed = set(Game.objects.filter(kept_in__state="sealed").values_list("game", flat=True))
        self.assertEqual(sealed, set())
        self.assertEqual(Game.objects.exclude(result="*").count(), 0)

    def test_a_game_that_changed_under_it_is_reported(self):
        self.path("1").write_bytes(b"not a pgn")
        with self.assertRaises(CommandError):
            self.keep()
