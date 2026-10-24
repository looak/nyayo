"""GET /api/games and manage.py export_games: every stored game matching a filter, one bz2 stream."""

import bz2
import copy
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path

import chess.pgn
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from nyayo.matches.models import Batch

from .test_uploads import TWO, BatchCase, batch_text, game
from .test_matches_ingest import GAUNTLET


def read(content):
    """The games in an export, as their (White, Result) pairs, read the way a client would: the whole stream."""
    import io

    stream = io.StringIO(bz2.decompress(content).decode())
    found = []
    while (g := chess.pgn.read_game(stream)) is not None:
        found.append((g.headers["White"], g.headers["Result"]))
    return found


class ExportCase(BatchCase):
    def second_match(self, **changes):
        document = copy.deepcopy(GAUNTLET)
        document["match"]["id"] = "sprt-1"
        document["match"]["kind"] = "sprt"
        document["match"]["settings"] = {"tc": "8+0.08", "book": "other.epd"}
        document["subject"] = {"name": "Elephant", "branch": "feature-x", "commit": "abcdef0123456789"}
        document["opponents"] = [{"id": "base", "name": "Elephant", "branch": "main"}]
        document["results"] = [{"opponent": "base", "games": 0, "wins": 0, "draws": 0, "losses": 0}]
        document["sprt"] = {"elo0": 0, "elo1": 5, "alpha": 0.05, "beta": 0.05, "llr": 0, "lower": -2.9,
                            "upper": 2.9, "result": None}
        self.assertEqual(self.put_match(document).status_code, 201)

    def export(self, query=""):
        response = self.client.get(f"/api/games{query}")
        self.assertEqual(response.status_code, 200, response.content if hasattr(response, "content") else "")
        return b"".join(response.streaming_content)


class Everything(ExportCase):
    def test_batches_and_sealed_single_games_come_together(self):
        self.keep(bz2.compress(TWO), batch="a")
        self.put_game(game("1-0", white="Elephant 1.0.0").encode(), game="7")
        self.assertEqual(read(self.export()).count(("Elephant 1.0.0", "1-0")), 2)
        self.assertEqual(len(read(self.export())), 3)

    def test_a_game_still_being_played_is_not_in_it(self):
        self.put_game(game("*").encode(), game="9")
        self.assertEqual(read(self.export()), [])

    def test_nothing_is_still_a_valid_stream(self):
        response = self.client.get("/api/games")
        self.assertEqual(bz2.decompress(b"".join(response.streaming_content)), b"")

    def test_it_is_open_like_the_pages(self):
        self.keep(bz2.compress(TWO))
        self.assertEqual(self.client.get("/api/games").status_code, 200)

    def test_it_is_a_download(self):
        response = self.client.get("/api/games")
        self.assertEqual(response["Content-Type"], "application/x-bzip2")
        self.assertIn("attachment", response["Content-Disposition"])

    def test_the_games_are_streamed_as_they_came(self):
        self.keep(bz2.compress(TWO))
        exported = bz2.decompress(self.export("?match=gauntlet-test-1"))
        self.assertEqual(exported.split(b"\n\n[")[0], TWO.split(b"\n\n[")[0])
        self.assertEqual(exported.count(b"[Event "), 2)

    def test_a_sealed_batch_is_in_it_too(self):
        from django.test import override_settings

        with override_settings(NYAYO_BATCH_GAMES=1):
            self.keep(bz2.compress(TWO))
        self.assertEqual([b.state for b in Batch.objects.order_by("number")], ["sealed", "sealed"])
        self.assertEqual(len(read(self.export())), 2)


class Filters(ExportCase):
    def setUp(self):
        super().setUp()
        self.second_match()
        self.keep(bz2.compress(batch_text(game("1-0", white="G1"))), batch="g")
        self.keep(bz2.compress(batch_text(game("0-1", white="S1", Opponent="base"),
                                          game("1-0", white="S2", Opponent="base"))), batch="s", match_id="sprt-1")

    def names(self, query):
        return sorted(white for white, _ in read(self.export(query)))

    def test_by_match(self):
        self.assertEqual(self.names("?match=sprt-1"), ["S1", "S2"])

    def test_by_kind(self):
        self.assertEqual(self.names("?kind=gauntlet"), ["G1"])
        self.assertEqual(self.names("?kind=sprt"), ["S1", "S2"])

    def test_by_engine(self):
        self.assertEqual(self.names("?engine=Elephant"), ["G1", "S1", "S2"])
        self.assertEqual(self.names("?engine=Nobody"), [])

    def test_by_branch(self):
        self.assertEqual(self.names("?branch=feature-x"), ["S1", "S2"])

    def test_by_commit_prefix(self):
        self.assertEqual(self.names("?commit=abcdef0"), ["S1", "S2"])
        self.assertEqual(self.names("?commit=abcdef1"), [])

    def test_by_time_control_and_book(self):
        self.assertEqual(self.names("?tc=8%2B0.08"), ["S1", "S2"])
        self.assertEqual(self.names("?book=8moves_v3.pgn"), ["G1"])

    def test_filters_combine_with_and(self):
        self.assertEqual(self.names("?kind=sprt&book=8moves_v3.pgn"), [])
        self.assertEqual(self.names("?kind=sprt&branch=feature-x&tc=8%2B0.08"), ["S1", "S2"])

    def test_by_date_inclusive(self):
        today = timezone.now().date()
        Batch.objects.filter(match_id="gauntlet-test-1").update(created_at=timezone.now() - timedelta(days=10))
        self.assertEqual(self.names(f"?since={today}"), ["S1", "S2"])
        self.assertEqual(self.names(f"?until={today - timedelta(days=5)}"), ["G1"])
        self.assertEqual(self.names(f"?since={today - timedelta(days=10)}&until={today}"), ["G1", "S1", "S2"])

    def test_bad_filters_are_400_with_the_reason(self):
        for query, reason in (("?kind=blitz", "kind"), ("?commit=abc", "commit"), ("?since=yesterday", "since")):
            response = self.client.get(f"/api/games{query}")
            self.assertEqual(response.status_code, 400)
            self.assertIn(reason, response.content.decode())


class Command(ExportCase):
    def test_export_games_writes_the_same_stream(self):
        self.keep(bz2.compress(TWO))
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "games.pgn.bz2"
            call_command("export_games", "-o", str(target), "--kind", "gauntlet", stdout=StringIO())
            self.assertEqual(len(read(target.read_bytes())), 2)
            call_command("export_games", "-o", str(target), "--kind", "sprt", stdout=StringIO())
            self.assertEqual(read(target.read_bytes()), [])

    def test_a_bad_filter_is_an_error(self):
        with self.assertRaises(CommandError):
            call_command("export_games", "-o", "x.bz2", "--kind", "blitz", stdout=StringIO())
