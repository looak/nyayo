"""Uploads of finished games, PUT /api/matches/{id}/pgns/{name}, and the worker that puts them in batches."""

import bz2
import copy
import gzip
from io import StringIO
from unittest import mock

import zstandard
from django.contrib.admin.sites import site
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, override_settings
from django.utils import timezone

from nyayo.matches import catalogue, reader, worker
from nyayo.matches.models import Batch, Match, Upload

from .test_games import GameTestCase, pgn
from .test_matches_ingest import GAUNTLET


def game(result="1-0", moves="1. e4 e5 2. Nf3 Nc6", **tags):
    """A finished game as OpenBench's worker sends it, named and tagged."""
    tags = {"Subject": "white", "Opponent": "barbarossa", **tags}
    return pgn(moves, result, **{k: v for k, v in tags.items() if v is not None}).decode()


def batch_text(*games):
    return "\n".join(games).encode()


TWO = batch_text(game("1-0"), game("0-1", Subject="black"))


class BatchCase(GameTestCase):
    def put_batch(self, body, batch="1.0", match_id="gauntlet-test-1", content_type="application/x-bzip2",
                  encoding=None, token=None):
        headers = {"Authorization": f"Bearer {token or self.token}"}
        if encoding:
            headers["Content-Encoding"] = encoding
        return self.client.put(f"/api/matches/{match_id}/pgns/{batch}", data=body, content_type=content_type,
                               headers=headers)

    def work(self):
        return worker.run(once=True)

    def keep(self, body, batch="1.0", **kwargs):
        """PUTs an upload and has the worker put it away."""
        response = self.put_batch(body, batch=batch, **kwargs)
        self.assertIn(response.status_code, (200, 202), response.content)
        self.work()
        return Upload.objects.get(match_id=kwargs.get("match_id", "gauntlet-test-1"), name=batch)

    def waiting(self, batch="1.0", match_id="gauntlet-test-1"):
        return self.data / "uploads" / match_id / batch

    def kept_games(self, match_id="gauntlet-test-1"):
        return [card["game"] for card in catalogue.cards(match_id)]


class TheRequest(BatchCase):
    """The request reads nothing: it checks what needs no reading and queues the upload."""

    def test_an_upload_is_taken_as_sent_and_waits(self):
        body = bz2.compress(TWO)
        response = self.put_batch(body)
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(self.waiting().read_bytes(), body)
        row = Upload.objects.get()
        self.assertEqual((row.match_id, row.name, row.state, row.games, row.bytes),
                         ("gauntlet-test-1", "1.0", "pending", 0, len(body)))
        self.assertEqual(row.machine, self.machine)
        self.assertEqual(len(row.sha256), 64)
        self.assertEqual(Batch.objects.count(), 0)

    def test_nothing_is_read_in_the_request(self):
        self.assertEqual(self.put_batch(b"not bzip2 at all").status_code, 202)
        self.assertEqual(Upload.objects.get().state, "pending")

    def test_an_identical_resend_is_200_pending_or_done(self):
        body = bz2.compress(TWO)
        self.put_batch(body)
        self.assertEqual(self.put_batch(body).status_code, 200)
        self.work()
        self.assertEqual(self.put_batch(body).status_code, 200)
        self.assertEqual(Upload.objects.count(), 1)
        self.assertEqual(Batch.objects.get().games, 2)

    def test_different_content_under_a_stored_name_is_409(self):
        self.put_batch(bz2.compress(TWO))
        response = self.put_batch(bz2.compress(batch_text(game("1/2-1/2"))))
        self.assertEqual(response.status_code, 409)
        self.assertIn("different content", response.content.decode())

    def test_a_refused_upload_sent_again_is_read_again(self):
        bad = bz2.compress(batch_text(game("1-0", Opponent="nobody")))
        self.keep(bad)
        self.assertEqual(Upload.objects.get().state, "refused")
        self.assertEqual(self.put_batch(bad).status_code, 202)
        self.assertEqual(Upload.objects.get().state, "pending")
        self.work()
        # or fixed, under the same name
        self.assertEqual(self.put_batch(bz2.compress(TWO)).status_code, 202)
        self.work()
        self.assertEqual((Upload.objects.get().state, Upload.objects.get().games), ("done", 2))

    def test_plain_and_gzipped_pgn(self):
        self.assertEqual(self.put_batch(TWO, batch="p", content_type="application/x-chess-pgn").status_code, 202)
        self.assertEqual(self.put_batch(gzip.compress(TWO), batch="g", content_type="application/x-chess-pgn",
                                        encoding="gzip").status_code, 202)
        self.work()
        self.assertEqual(list(Upload.objects.values_list("state", "games")), [("done", 2), ("done", 2)])

    def test_no_token(self):
        response = self.client.put("/api/matches/gauntlet-test-1/pgns/1", data=b"x",
                                   content_type="application/x-bzip2")
        self.assertEqual(response.status_code, 401)

    def test_an_unknown_match(self):
        self.assertEqual(self.put_batch(bz2.compress(TWO), match_id="ghost").status_code, 404)

    def test_a_wrong_content_type(self):
        response = self.put_batch(TWO, content_type="text/plain")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Content-Type", response.content.decode())

    def test_too_large_as_sent(self):
        with override_settings(NYAYO_MAX_BATCH_UPLOAD_BYTES=10):
            self.assertEqual(self.put_batch(bz2.compress(TWO)).status_code, 413)
        self.assertEqual(Upload.objects.count(), 0)

    def test_unsafe_names(self):
        for batch in ("..", ".x", "a" * 65):
            self.assertIn(self.put_batch(bz2.compress(TWO), batch=batch).status_code, (400, 404), batch)
        self.assertEqual(Upload.objects.count(), 0)

    def test_the_reserved_match_id(self):
        document = copy.deepcopy(GAUNTLET)
        document["match"]["id"] = "batches"
        self.assertEqual(self.put_match(document).status_code, 400)

    def test_an_upload_adds_no_counts(self):
        before = list(Match.objects.get().results.values_list("games", flat=True))
        self.keep(bz2.compress(TWO))
        self.assertEqual(list(Match.objects.get().results.values_list("games", flat=True)), before)


class Keeping(BatchCase):
    def test_the_worker_puts_an_upload_away(self):
        upload = self.keep(bz2.compress(TWO))
        self.assertEqual((upload.state, upload.games, upload.appended, upload.batches), ("done", 2, 2, [1]))
        self.assertEqual(upload.reader, reader.PythonChess.name)
        self.assertFalse(self.waiting().exists())
        batch = Batch.objects.get()
        self.assertEqual((batch.number, batch.state, batch.games), (1, "open", 2))
        self.assertEqual(batch.sources, [{"upload": "1.0", "first": 1, "count": 2}])
        self.assertEqual(self.kept_games(), ["1.0.1", "1.0.2"])
        # the games as they came, one after the other
        text = (self.batches / "gauntlet-test-1" / "1.pgn").read_bytes()
        self.assertEqual(len(text), batch.bytes)
        self.assertEqual(text.count(b"[Event "), 2)

    def test_the_cards(self):
        self.keep(bz2.compress(batch_text(game("1-0"), game("0-1", Subject="black", Termination="adjudication"))))
        first, second = catalogue.cards("gauntlet-test-1")
        opponent = Match.objects.get().results.get(opponent="barbarossa").pk
        self.assertEqual((first["colour"], first["opponent"], first["result"], first["plies"]),
                         ("white", opponent, "1-0", 4))
        self.assertEqual((second["colour"], second["result"], second["termination"]), ("black", "0-1",
                                                                                         "adjudication"))
        self.assertEqual((first["batch"], first["position"], second["position"]), (Batch.objects.get().pk, 1, 2))

    @override_settings(NYAYO_BATCH_GAMES=3)
    def test_batches_are_sealed_full_into_zstd(self):
        self.keep(bz2.compress(batch_text(*[game("1-0") for _ in range(5)])))
        sealed, open_ = Batch.objects.order_by("number")
        self.assertEqual((sealed.state, sealed.games, open_.state, open_.games), ("sealed", 3, "open", 2))
        stored = (self.batches / "gauntlet-test-1" / "1.pgn.zst").read_bytes()
        self.assertEqual(len(stored), sealed.stored_bytes)
        self.assertEqual(zstandard.ZstdDecompressor().decompress(stored).count(b"[Event "), 3)
        self.assertFalse((self.batches / "gauntlet-test-1" / "1.pgn").exists())
        self.assertEqual(Upload.objects.get().batches, [1, 2])
        self.assertEqual(self.kept_games(), [f"1.0.{n}" for n in range(1, 6)])
        # a game is read from either
        for number in ("1.0.2", "1.0.5"):
            self.assertIn(b"[Event ", catalogue.read_game("gauntlet-test-1", number))

    @override_settings(NYAYO_BATCH_GAMES=3)
    def test_uploads_share_a_batch_in_order(self):
        self.keep(bz2.compress(TWO), batch="a")
        self.keep(bz2.compress(TWO), batch="b")
        first = Batch.objects.get(number=1)
        self.assertEqual(first.sources, [{"upload": "a", "first": 1, "count": 2}, {"upload": "b", "first": 1,
                                                                                    "count": 1}])
        self.assertEqual(catalogue.game_ids(first), ["a.1", "a.2", "b.1"])
        self.assertEqual(self.kept_games(), ["a.1", "a.2", "b.1", "b.2"])

    @override_settings(NYAYO_BATCH_GAMES=3)
    def test_a_worker_stopped_half_way_carries_on(self):
        self.put_batch(bz2.compress(batch_text(*[game("1-0") for _ in range(5)])))
        with mock.patch("nyayo.matches.catalogue.seal", side_effect=OSError("the share went away")):
            with self.assertRaises(OSError):
                self.work()
        upload = Upload.objects.get()
        self.assertEqual((upload.state, upload.appended), ("pending", 3))
        # and bytes written but never committed are dropped
        with open(self.batches / "gauntlet-test-1" / "1.pgn", "ab") as file:
            file.write(b"[Event \"half a game")
        self.work()
        self.assertEqual(self.kept_games(), [f"1.0.{n}" for n in range(1, 6)])
        self.assertEqual(sum(Batch.objects.values_list("games", flat=True)), 5)
        self.assertEqual(Upload.objects.get().state, "done")

    def test_a_done_upload_is_never_appended_twice(self):
        self.keep(bz2.compress(TWO))
        Upload.objects.update(state="pending")
        self.work()
        self.assertEqual(Batch.objects.get().games, 2)

    def test_a_match_that_has_ended_has_its_batch_sealed_short(self):
        self.keep(bz2.compress(TWO))
        self.assertEqual(self.work(), 0)
        Match.objects.update(state="finished", received_at=timezone.now())
        self.assertEqual(self.work(), 0)  # not until nothing has been heard for a while
        Match.objects.update(received_at=timezone.now() - timezone.timedelta(hours=1))
        self.put_batch(bz2.compress(TWO), batch="late")
        with mock.patch.object(worker, "process_upload"):
            worker.step(reader.default())
        self.assertEqual(Batch.objects.get().state, "open")  # nor while an upload of it waits
        self.work()
        batch = Batch.objects.get()
        self.assertEqual((batch.state, batch.games), ("sealed", 4))

    def test_only_one_worker_at_a_time(self):
        with worker.only_worker():
            with self.assertRaises(RuntimeError):
                with worker.only_worker():
                    pass


class Refusals(BatchCase):
    def refused(self, body, containing, **kwargs):
        upload = self.keep(body, **kwargs)
        self.assertEqual(upload.state, "refused")
        self.assertIn(containing, upload.reason)
        self.assertEqual(Batch.objects.count(), 0)
        self.assertEqual(self.kept_games(), [])
        self.assertTrue(self.waiting().exists())  # kept to be looked at, or read again
        return upload

    def test_not_bz2(self):
        self.refused(b"not bzip2", "bzip2")

    def test_a_truncated_bz2(self):
        self.refused(bz2.compress(TWO)[:-10], "bzip2")

    def test_nothing_in_it(self):
        self.refused(bz2.compress(b""), "no game")

    def test_an_unfinished_game_names_its_position(self):
        self.refused(bz2.compress(batch_text(game("1-0"), game("*"))), "game 2")

    def test_an_illegal_move_names_its_position_and_ply(self):
        bad = batch_text(game("1-0"), game("1-0"), game("1-0", moves="1. e4 e5 2. Ke3 Nc6"))
        upload = self.refused(bz2.compress(bad), "game 3")
        self.assertIn("ply 3", upload.reason)

    def test_one_bad_game_keeps_the_whole_upload_out(self):
        self.refused(bz2.compress(batch_text(game("1-0"), game("1-0"), game("*"))), "[Result]")

    def test_an_opponent_that_is_not_the_matchs(self):
        self.refused(bz2.compress(batch_text(game("1-0", Opponent="nobody"))), "isn't an opponent")

    def test_a_game_without_opponent(self):
        self.refused(bz2.compress(batch_text(game("1-0", Opponent=None))), "[Opponent]")

    def test_a_game_without_subject(self):
        self.refused(bz2.compress(batch_text(game("1-0", Subject=None))), "[Subject]")

    def test_a_subject_that_is_not_a_colour(self):
        self.refused(bz2.compress(batch_text(game("1-0", Subject="green"))), "[Subject]")

    def test_not_utf8(self):
        self.refused(bz2.compress(b"[Event \"\xff\xfe\"]\n\n1. e4 1-0\n"), "UTF-8")

    def test_an_upload_that_inflates_too_far(self):
        with override_settings(NYAYO_MAX_BATCH_BYTES=100):
            self.refused(bz2.compress(TWO), "decompressed")

    def test_read_again_from_the_admin(self):
        self.refused(bz2.compress(batch_text(game("1-0", Opponent="base"))), "isn't an opponent")
        admin = site._registry[Upload]
        with mock.patch.object(admin, "message_user"):
            admin.read_again(RequestFactory().get("/"), Upload.objects.all())
        self.assertEqual(Upload.objects.get().state, "pending")


class TheReader(BatchCase):
    def test_it_splits_games_where_python_chess_does(self):
        text = (b"junk before\n[Event \"a\"]\n[Result \"1-0\"]\n\n1. e4 {a comment\n\n[with a tag inside]} e5 1-0\n"
                b"\n\n[Event \"b\"]\n\n[Result \"0-1\"]\n\n1. d4 0-1\n")
        records = reader.default().read(text)
        self.assertEqual([r["tags"]["Event"] for r in records], ["a", "b"])
        self.assertTrue(all(r["ok"] for r in records))
        self.assertEqual([r["plies"] for r in records], [2, 1])
        for r in records:
            self.assertTrue(text[r["offset"]:r["offset"] + r["length"]].startswith(b"[Event"))
            self.assertTrue(text[r["offset"]:r["offset"] + r["length"]].endswith((b"1-0", b"0-1")))

    def test_openbench_layout(self):
        text = (b"[Event \"?\"]\n[White \"Weiss-dev\"]\n[Result \"1-0\"]\n\ne4 {+1.31 14/40 159 377044} e5 "
                b"{-1.21 14/34 136 319833} 1-0\n\n") * 3
        self.assertEqual(len(reader.default().read(text)), 3)


class Purging(BatchCase):
    def test_purge_batches_removes_batches_cards_and_uploads(self):
        self.keep(bz2.compress(TWO), batch="a")
        self.put_batch(bz2.compress(TWO), batch="b")
        out = StringIO()
        call_command("purge_batches", "--match", "gauntlet-test-1", "--dry-run", stdout=out)
        self.assertIn("1 batch and 2 uploads would be deleted", out.getvalue())
        call_command("purge_batches", "--match", "gauntlet-test-1", stdout=StringIO())
        self.assertEqual((Batch.objects.count(), Upload.objects.count()), (0, 0))
        self.assertEqual(self.kept_games(), [])
        self.assertFalse((self.batches / "gauntlet-test-1").exists())
        self.assertFalse(self.waiting("b").exists())

    def test_it_needs_a_match_with_batches(self):
        with self.assertRaises(CommandError):
            call_command("purge_batches", "--match", "gauntlet-test-1", stdout=StringIO())

    def test_batches_are_apart_from_the_live_games(self):
        self.put_game(pgn("1. e4 e5"))
        self.keep(bz2.compress(TWO))
        call_command("purge_games", "--match", "gauntlet-test-1", stdout=StringIO())
        self.assertEqual(self.kept_games(), ["1.0.1", "1.0.2"])

    def test_a_match_with_kept_games_is_not_deleted_from_under_them(self):
        self.put_batch(bz2.compress(TWO))
        admin = site._registry[Match]
        match = Match.objects.get()
        self.assertFalse(admin.has_delete_permission(RequestFactory().get("/"), match))
        call_command("purge_batches", "--match", "gauntlet-test-1", stdout=StringIO())
        self.assertTrue(admin.has_delete_permission(RequestFactory().get("/"), match))


class OnThePages(BatchCase):
    def test_the_match_page_counts_stored_games_and_uploads(self):
        self.assertNotContains(self.client.get("/matches/gauntlet-test-1"), "Stored games")
        self.keep(bz2.compress(TWO), batch="a")
        self.keep(bz2.compress(batch_text(game("0-1"))), batch="b")
        self.put_batch(bz2.compress(TWO), batch="c")
        page = self.client.get("/matches/gauntlet-test-1").content.decode()
        self.assertIn("Stored games", page)
        self.assertIn("kept as data, in batches\">3", page)
        self.assertIn("1 upload waiting", page)
        self.assertIn("/api/games?match=gauntlet-test-1", page)

    def test_kept_games_are_listed_and_open(self):
        self.keep(bz2.compress(batch_text(game("1-0", moves="1. e4 {+0.35 12/18 104 1532876} e5 "
                                                            "{-0.20 11/20 98 1200000}"))))
        self.assertContains(self.client.get("/matches/gauntlet-test-1"), "/games/1.0.1")
        page = self.client.get("/matches/gauntlet-test-1/games/1.0.1")
        self.assertEqual(page.status_code, 200)
        # cutechess-ob's comments: the score, depth and time are read
        self.assertContains(page, "+0.35")
        self.assertEqual(self.client.get("/matches/gauntlet-test-1/games/1.0.9").status_code, 404)
