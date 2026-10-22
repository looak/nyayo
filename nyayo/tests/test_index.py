"""The index: every card's facts, how each game ended, and reindex."""

import bz2
import sqlite3
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from nyayo.matches import cards, catalogue, reader, reindex
from nyayo.matches.models import Batch, Match, MatchResult

from .test_games import pgn
from .test_uploads import BatchCase, batch_text, game

OPENBENCH = {"openbench": {"win_adj": "movecount=3 score=400", "draw_adj": "movenumber=40 movecount=8 score=10"}}
MATE = "1. e4 e5 2. Bc4 Nc6 3. Qh5 Nf6 4. Qxf7#"
# white resigns: its own last three scores 400 cp or more below zero
RESIGNED = ("1. e4 {-4.10 10/12 100 1000} e5 {+4.00 10/12 100 1000} 2. Nf3 {-4.50 10/12 100 1000} "
            "Nc6 {+4.20 10/12 100 1000} 3. Bb5 {-5.00 10/12 100 1000} a6 {+5.00 10/12 100 1000}")
# from move 40, both engines within 10 cp for 8 moves each
QUIET = " ".join(f"{p}3 {{+0.0{i % 10} 10/12 100 1000}} {p}6 {{-0.05 10/12 100 1000}}" for i, p in enumerate("abcdefgh"))
LATE = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 40"


def card_of(match_id, number):
    return dict(zip(catalogue.ALL_COLUMNS, catalogue.index().execute(
        f"SELECT {', '.join(catalogue.ALL_COLUMNS)} FROM card WHERE game = ?", (number,)).fetchone()))


class Facts(BatchCase):
    def test_python_chess_gives_phant_indexers_facts(self):
        text = pgn("1. e4 d5 2. exd5 c5 3. dxc6 Nf6 4. cxb7 Nbd7 5. bxa8=N e5 6. Nf3 Bd6 7. O-O O-O", "1-0").replace(
            b"\n\n", b'\n[Subject "white"]\n\n', 1)
        record = reader.PythonChess().read(text)[0]
        self.assertEqual((record["captures"], record["ep_captures"], record["promotions"], record["castled"]),
                         (3, 1, {"q": 0, "r": 0, "b": 0, "n": 1}, {"white": 13, "black": 14}))
        self.assertEqual((record["ended"], record["start"], record["min_pieces"]), (None, "463b96181691fc9c", 29))
        self.assertIsNone(record["opening"])
        fields = cards.reader_fields(record)
        self.assertEqual(fields["reader_flags"], cards.EN_PASSANT | cards.UNDERPROMOTION | cards.KNIGHT_PROMOTION)
        self.assertEqual(fields["start"], 0x463b96181691fc9c)

    def test_the_worker_writes_every_fact(self):
        self.keep(bz2.compress(batch_text(game("1-0", moves=MATE))))
        card = card_of("gauntlet-test-1", "1.0.1")
        self.assertEqual((card["ended"], cards.ENDINGS[card["ending"]], card["checks"], card["captures"]),
                         (cards.ENDED.index("checkmate"), "rule", 1, 1))
        self.assertTrue(card["flags"] & cards.MATE)
        self.assertEqual(card["material_end"], 1)

    def test_evals_from_the_comments(self):
        self.keep(bz2.compress(batch_text(game("0-1", moves=RESIGNED))))
        card = card_of("gauntlet-test-1", "1.0.1")
        # white's view: -410, -400, -450, -420, -500, -500; the largest change is 50
        self.assertEqual((card["eval_20"], card["swing"]), (None, 50))


class Endings(BatchCase):
    def setUp(self):
        super().setUp()
        Match.objects.update(settings=OPENBENCH)

    def ending(self, number="1.0.1"):
        card = card_of("gauntlet-test-1", number)
        return cards.ENDINGS[card["ending"]], bool(card["flags"] & cards.INFERRED)

    def test_a_resignation_is_inferred_from_the_scores(self):
        self.keep(bz2.compress(batch_text(game("0-1", moves=RESIGNED))))
        self.assertEqual(self.ending(), ("resignation", True))

    def test_a_draw_adjudication_is_inferred_past_move_40(self):
        self.keep(bz2.compress(batch_text(game("1/2-1/2", moves=QUIET, FEN=LATE, SetUp="1"))))
        self.assertEqual(self.ending(), ("draw_adjudication", True))

    def test_not_before_move_40(self):
        self.keep(bz2.compress(batch_text(game("1/2-1/2", moves=QUIET))))
        self.assertEqual(self.ending(), ("unexplained", False))

    def test_a_win_the_scores_dont_explain(self):
        self.keep(bz2.compress(batch_text(game("1-0", moves=RESIGNED))))
        self.assertEqual(self.ending(), ("unexplained", False))

    def test_a_termination_tag_is_taken_as_it_says(self):
        self.keep(bz2.compress(batch_text(game("0-1", moves=RESIGNED, Termination="time forfeit"))))
        self.assertEqual(self.ending(), ("time_forfeit", False))

    def test_a_rule_comes_first(self):
        self.keep(bz2.compress(batch_text(game("1-0", moves=MATE, Termination="adjudication"))))
        self.assertEqual(self.ending(), ("rule", False))

    def test_without_settings_nothing_is_inferred(self):
        Match.objects.update(settings={})
        self.keep(bz2.compress(batch_text(game("0-1", moves=RESIGNED))))
        self.assertEqual(self.ending(), ("unexplained", False))

    def test_not_when_the_match_reports_time_losses(self):
        MatchResult.objects.update(timelosses={"subject": 1, "opponent": 0})
        self.keep(bz2.compress(batch_text(game("0-1", moves=RESIGNED))))
        self.assertEqual(self.ending(), ("unexplained", False))

    def test_the_counts(self):
        self.keep(bz2.compress(batch_text(game("0-1", moves=RESIGNED), game("1-0", moves=MATE),
                                          game("1/2-1/2", moves=QUIET, FEN=LATE, SetUp="1"))))
        self.assertEqual(reindex.ending_counts(), {("resignation", True): 1, ("rule", False): 1,
                                                   ("draw_adjudication", True): 1})


@override_settings(NYAYO_BATCH_GAMES=2)
class Reindex(BatchCase):
    def setUp(self):
        super().setUp()
        self.keep(bz2.compress(batch_text(game("0-1", moves=RESIGNED), game("1-0", moves=MATE),
                                          game("1-0"))))
        self.put_game(pgn("1. d4 d5", "1/2-1/2"), game="7")
        self.work()
        self.sealed = Batch.objects.get(number=1)

    def reindex(self, *args):
        out = StringIO()
        call_command("reindex", *args, stdout=out)
        return out.getvalue()

    def test_a_batch_sealed_by_one_reader_is_filled_already(self):
        self.assertEqual(catalogue.filled(self.sealed), cards.versions(reader.default()))
        self.assertIn("0 batches, 0 games indexed", self.reindex())

    def test_a_new_version_of_one_extractor_visits_its_batches_only_for_it(self):
        catalogue.index().execute("UPDATE card SET ending = NULL, captures = 99")
        with mock.patch.object(cards, "ENDING_VERSION", "2"):
            self.assertIn("1 batch, 2 games indexed", self.reindex())
            self.assertEqual(catalogue.filled(self.sealed)["ending"], "2")
        self.assertEqual(cards.ENDINGS[card_of("gauntlet-test-1", "1.0.2")["ending"]], "rule")
        self.assertEqual(card_of("gauntlet-test-1", "1.0.2")["captures"], 99)  # the reader's columns untouched

    def test_only_one_property(self):
        catalogue.index().execute("UPDATE card SET swing = NULL")
        with mock.patch.object(cards, "EVAL_VERSION", "2"), mock.patch.object(cards, "ENDING_VERSION", "2"):
            self.reindex("--only", "eval")
            self.assertEqual(catalogue.filled(self.sealed)["ending"], "1")
        self.assertEqual(card_of("gauntlet-test-1", "1.0.1")["swing"], 50)

    def test_the_index_is_rebuilt_from_the_batches_alone(self):
        before = {row[0]: row for row in catalogue.index().execute(
            f"SELECT game, {', '.join(catalogue.ALL_COLUMNS)} FROM card")}
        sealed = list(Batch.objects.filter(state="sealed").order_by("number"))
        self.assertEqual(len(sealed), 2)
        catalogue.index().execute("DELETE FROM card")
        catalogue.index().execute("DELETE FROM filled")
        self.assertIn("2 batches, 4 games indexed", self.reindex())
        after = {row[0]: row for row in catalogue.index().execute(
            f"SELECT game, {', '.join(catalogue.ALL_COLUMNS)} FROM card")}
        self.assertEqual(after, before)

    def test_dry_run(self):
        catalogue.index().execute("DELETE FROM filled")
        self.assertIn("2 batches, 4 games would be indexed", self.reindex("--dry-run"))
        self.assertEqual(catalogue.filled(self.sealed), {})

    def test_an_unknown_extractor(self):
        with self.assertRaises(CommandError):
            self.reindex("--only", "colour")

    def test_a_batch_that_doesnt_read_as_its_row_says_is_reported(self):
        Batch.objects.filter(pk=self.sealed.pk).update(games=5)
        catalogue.index().execute("DELETE FROM filled")
        with self.assertRaises(CommandError):
            self.reindex()


class Upgrade(BatchCase):
    def test_an_index_of_version_1_gains_the_new_columns(self):
        path = self.data / "old.sqlite3"
        db = sqlite3.connect(path)
        db.executescript(catalogue.SCHEMA)
        db.execute("PRAGMA user_version=1")
        db.execute("INSERT INTO card (batch, position, game, offset, length, colour, result, plies) "
                   "VALUES (1, 1, 'x', 0, 10, 1, 1, 4)")
        db.commit()
        db.close()
        with override_settings(NYAYO_INDEX_DB=path):
            catalogue.close_index()
            columns = {row[1] for row in catalogue.index().execute("PRAGMA table_info(card)")}
            self.assertTrue({"ending", "flags", "start", "swing"} <= columns)
            self.assertEqual(catalogue.index().execute("SELECT game, flags FROM card").fetchall(), [("x", 0)])
            catalogue.close_index()
