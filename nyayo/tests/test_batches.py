"""Batches of finished games: PUT /api/matches/{id}/pgns/{batch}."""

import bz2
import copy
import gzip
from io import StringIO

from django.contrib.admin.sites import site
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, override_settings

from nyayo.matches.models import GameBatch, Match

from .test_games import GameTestCase, pgn
from .test_matches_ingest import GAUNTLET


def game(result="1-0", moves="1. e4 e5 2. Nf3 Nc6", **tags):
    return pgn(moves, result, **tags).decode()


def batch_text(*games):
    return "\n".join(games).encode()


TWO = batch_text(game("1-0"), game("0-1", Subject="black", Opponent="barbarossa"))


class BatchCase(GameTestCase):
    def put_batch(self, body, batch="1.0", match_id="gauntlet-test-1", content_type="application/x-bzip2",
                  encoding=None, token=None):
        headers = {"Authorization": f"Bearer {token or self.token}"}
        if encoding:
            headers["Content-Encoding"] = encoding
        return self.client.put(f"/api/matches/{match_id}/pgns/{batch}", data=body, content_type=content_type,
                               headers=headers)

    def stored(self, batch="1.0", match_id="gauntlet-test-1"):
        return self.batches / match_id / f"{batch}.pgn.bz2"


class Storing(BatchCase):
    def test_a_bz2_batch_is_stored_as_sent(self):
        body = bz2.compress(TWO)
        response = self.put_batch(body)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.stored().read_bytes(), body)
        row = GameBatch.objects.get()
        self.assertEqual((row.match_id, row.batch, row.games, row.bytes), ("gauntlet-test-1", "1.0", 2, len(body)))
        self.assertEqual(row.machine, self.machine)
        self.assertEqual(len(row.sha256), 64)

    def test_a_plain_batch_is_recompressed(self):
        response = self.put_batch(TWO, content_type="application/x-chess-pgn")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(bz2.decompress(self.stored().read_bytes()), TWO)

    def test_a_gzipped_batch(self):
        response = self.put_batch(gzip.compress(TWO), content_type="application/x-chess-pgn", encoding="gzip")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(GameBatch.objects.get().games, 2)

    def test_concatenated_bz2_streams_are_one_batch(self):
        body = bz2.compress(game("1-0").encode()) + bz2.compress(game("0-1").encode())
        self.assertEqual(self.put_batch(body).status_code, 201)
        self.assertEqual(GameBatch.objects.get().games, 2)

    def test_an_identical_resend_is_200_and_changes_nothing(self):
        body = bz2.compress(TWO)
        self.put_batch(body)
        before = GameBatch.objects.get()
        self.assertEqual(self.put_batch(body).status_code, 200)
        self.assertEqual(GameBatch.objects.count(), 1)
        self.assertEqual(GameBatch.objects.get().received_at, before.received_at)

    def test_a_plain_resend_is_also_unchanged(self):
        self.put_batch(TWO, content_type="application/x-chess-pgn")
        self.assertEqual(self.put_batch(TWO, content_type="application/x-chess-pgn").status_code, 200)

    def test_different_content_under_a_stored_name_is_409(self):
        self.put_batch(bz2.compress(TWO))
        response = self.put_batch(bz2.compress(batch_text(game("1/2-1/2"))))
        self.assertEqual(response.status_code, 409)
        self.assertIn("sealed", response.content.decode())
        self.assertEqual(GameBatch.objects.get().games, 2)

    def test_a_batch_adds_no_counts(self):
        before = list(Match.objects.get().results.values_list("games", flat=True))
        self.put_batch(bz2.compress(TWO))
        self.assertEqual(list(Match.objects.get().results.values_list("games", flat=True)), before)

    def test_batches_are_apart_from_the_live_games(self):
        self.put_game(pgn("1. e4 e5"))
        self.put_batch(bz2.compress(TWO))
        call_command("purge_games", "--match", "gauntlet-test-1", stdout=StringIO())
        self.assertTrue(self.stored().exists())
        self.assertEqual(GameBatch.objects.count(), 1)


class Refusals(BatchCase):
    def refused(self, body, status=400, containing="", **kwargs):
        response = self.put_batch(body, **kwargs)
        self.assertEqual(response.status_code, status, response.content)
        self.assertIn(containing, response.content.decode())
        self.assertEqual(GameBatch.objects.count(), 0)
        self.assertFalse(self.stored().exists())

    def test_no_token(self):
        response = self.client.put("/api/matches/gauntlet-test-1/pgns/1", data=b"x",
                                   content_type="application/x-bzip2")
        self.assertEqual(response.status_code, 401)

    def test_an_unknown_match(self):
        self.refused(bz2.compress(TWO), 404, "unknown match", match_id="ghost")

    def test_not_bz2(self):
        self.refused(b"not bzip2", 400, "bzip2")

    def test_a_truncated_bz2(self):
        self.refused(bz2.compress(TWO)[:-10], 400, "bzip2")

    def test_nothing_in_it(self):
        self.refused(bz2.compress(b""), 400, "no game")

    def test_a_wrong_content_type(self):
        self.refused(TWO, 400, "Content-Type", content_type="text/plain")

    def test_an_unfinished_game_names_its_position(self):
        self.refused(bz2.compress(batch_text(game("1-0"), game("*"))), 400, "game 2")

    def test_an_illegal_move_names_its_position(self):
        bad = batch_text(game("1-0"), game("1-0"), game("1-0", moves="1. e4 e5 2. Ke3 Nc6"))
        self.refused(bz2.compress(bad), 400, "game 3")

    def test_one_bad_game_keeps_the_whole_batch_out(self):
        self.refused(bz2.compress(batch_text(game("1-0"), game("*"))), 400, "[Result]")

    def test_an_opponent_that_is_not_the_matchs(self):
        self.refused(bz2.compress(batch_text(game("1-0", Opponent="nobody"))), 400, "isn't an opponent")

    def test_a_subject_that_is_not_a_colour(self):
        self.refused(bz2.compress(batch_text(game("1-0", Subject="green"))), 400, "[Subject]")

    def test_not_utf8(self):
        self.refused(bz2.compress(b"\xff\xfe"), 400, "UTF-8")

    def test_a_batch_that_inflates_too_far(self):
        with override_settings(NYAYO_MAX_BATCH_BYTES=100):
            self.refused(bz2.compress(TWO), 400, "decompressed")

    def test_too_large_as_sent(self):
        with override_settings(NYAYO_MAX_BATCH_UPLOAD_BYTES=10):
            self.refused(bz2.compress(TWO), 413, "larger")

    def test_unsafe_names(self):
        for batch in ("..", ".x", "a" * 65):
            response = self.put_batch(bz2.compress(TWO), batch=batch)
            self.assertIn(response.status_code, (400, 404), batch)
        self.assertEqual(GameBatch.objects.count(), 0)

    def test_the_reserved_match_id(self):
        document = copy.deepcopy(GAUNTLET)
        document["match"]["id"] = "batches"
        self.assertEqual(self.put_match(document).status_code, 400)


class Purging(BatchCase):
    def test_purge_batches_removes_files_and_rows_by_hand(self):
        self.put_batch(bz2.compress(TWO), batch="a")
        self.put_batch(bz2.compress(batch_text(game("0-1"))), batch="b")
        out = StringIO()
        call_command("purge_batches", "--match", "gauntlet-test-1", "--dry-run", stdout=out)
        self.assertEqual(GameBatch.objects.count(), 2)
        self.assertIn("2 batches would be deleted", out.getvalue())
        call_command("purge_batches", "--match", "gauntlet-test-1", stdout=StringIO())
        self.assertEqual(GameBatch.objects.count(), 0)
        self.assertFalse((self.batches / "gauntlet-test-1").exists())

    def test_it_needs_a_match_with_batches(self):
        with self.assertRaises(CommandError):
            call_command("purge_batches", "--match", "gauntlet-test-1", stdout=StringIO())

    def test_a_match_with_batches_is_not_deleted_from_under_them(self):
        self.put_batch(bz2.compress(TWO))
        admin = site._registry[Match]
        match = Match.objects.get()
        self.assertFalse(admin.has_delete_permission(RequestFactory().get("/"), match))
        call_command("purge_batches", "--match", "gauntlet-test-1", stdout=StringIO())
        self.assertTrue(admin.has_delete_permission(RequestFactory().get("/"), match))


class OnThePage(BatchCase):
    def test_the_match_page_counts_stored_games(self):
        self.assertNotContains(self.client.get("/matches/gauntlet-test-1"), "Stored games")
        self.put_batch(bz2.compress(TWO), batch="a")
        self.put_batch(bz2.compress(batch_text(game("0-1"))), batch="b")
        page = self.client.get("/matches/gauntlet-test-1").content.decode()
        self.assertIn("Stored games", page)
        self.assertIn("kept as data\">3 ", page)
        self.assertIn("/api/games?match=gauntlet-test-1", page)
