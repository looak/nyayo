import gzip
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from nyayo.matches import games
from nyayo.matches.models import Game, Match, MatchSnapshot

from .test_matches_ingest import MatchTestCase, gauntlet

MOVES = "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6"


def pgn(moves=MOVES, result="*", white="Elephant 1.0.0", black="Barbarossa 0.6.0", **tags):
    headers = {"Event": "gauntlet", "White": white, "Black": black, "Result": result, **tags}
    lines = [f'[{name} "{value}"]' for name, value in headers.items()]
    return ("\n".join(lines) + f"\n\n{moves} {result}\n").encode()


class GameTestCase(MatchTestCase):
    def setUp(self):
        super().setUp()
        self.put_match(gauntlet())

    def put_game(self, raw, game="1", match_id="gauntlet-test-1", token=None, gzipped=False):
        headers = {"Authorization": f"Bearer {token or self.token}"}
        if gzipped:
            raw = gzip.compress(raw)
            headers["Content-Encoding"] = "gzip"
        return self.client.put(f"/api/matches/{match_id}/games/{game}", data=raw,
                               content_type="application/x-chess-pgn", headers=headers)

    def path(self, game="1"):
        return self.games / "gauntlet-test-1" / f"{game}.pgn"


class Streaming(GameTestCase):
    def test_first_put_creates_the_game(self):
        raw = pgn()
        response = self.put_game(raw)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(self.path().read_bytes(), raw)
        game = Game.objects.get()
        self.assertEqual((game.game, game.result, game.white, game.black),
                         ("1", "*", "Elephant 1.0.0", "Barbarossa 0.6.0"))
        self.assertTrue(game.live)
        self.assertIsNone(game.finished_at)

    def test_each_move_replaces_the_file(self):
        self.put_game(pgn("1. e4 e5"))
        later = pgn(MOVES)
        self.assertEqual(self.put_game(later).status_code, 200)
        self.assertEqual(self.path().read_bytes(), later)
        self.assertEqual(Game.objects.count(), 1)

    def test_moves_do_not_write_the_index(self):
        self.put_game(pgn("1. e4 e5"))
        game = Game.objects.get()
        with self.assertNumQueries(3):  # the token, the match, the game row; no write, no transaction
            self.put_game(pgn(MOVES))
        self.assertEqual(Game.objects.get().received_at, game.received_at)

    def test_gzip(self):
        self.assertEqual(self.put_game(pgn(), gzipped=True).status_code, 201)

    def test_get_returns_the_pgn_without_a_token(self):
        raw = pgn()
        self.put_game(raw)
        response = self.client.get("/api/matches/gauntlet-test-1/games/1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, raw)
        self.assertEqual(self.client.get("/api/matches/gauntlet-test-1/games/9").status_code, 404)

    def test_get_cant_climb_out(self):
        self.assertEqual(self.client.get("/api/matches/gauntlet-test-1/games/..").status_code, 404)
        self.assertEqual(self.client.get("/api/matches/../games/x").status_code, 404)

    def test_match_counts_are_untouched(self):
        before = MatchSnapshot.objects.count()
        self.put_game(pgn(result="1-0"))
        self.assertEqual(MatchSnapshot.objects.count(), before)
        self.assertEqual(Match.objects.get().results.get(opponent="barbarossa").games, 10)


class Sealing(GameTestCase):
    def test_a_result_seals_the_game(self):
        self.put_game(pgn())
        final = pgn(result="1-0", Termination="adjudication")
        self.assertEqual(self.put_game(final).status_code, 200)
        game = Game.objects.get()
        self.assertEqual((game.result, game.termination, game.live), ("1-0", "adjudication", False))
        self.assertIsNotNone(game.finished_at)
        refused = self.put_game(pgn(moves=MOVES + " 4. Ba4", result="1-0"))
        self.assertEqual(refused.status_code, 409)
        self.assertIn("sealed", refused.content.decode())
        self.assertEqual(self.path().read_bytes(), final)

    def test_a_game_cant_be_reopened(self):
        self.put_game(pgn(result="0-1"))
        self.assertEqual(self.put_game(pgn()).status_code, 409)

    def test_resending_the_final_pgn_is_fine(self):
        final = pgn(result="1/2-1/2")
        self.put_game(final)
        self.assertEqual(self.put_game(final).status_code, 200)

    def test_a_finished_game_can_be_the_first_report(self):
        self.assertEqual(self.put_game(pgn(result="1-0")).status_code, 201)
        self.assertIsNotNone(Game.objects.get().finished_at)

    def test_a_resumed_game_may_start_over_while_live(self):
        self.put_game(pgn(MOVES))
        self.assertEqual(self.put_game(pgn("1. d4")).status_code, 200)


class Refusals(GameTestCase):
    def test_bad_token(self):
        self.assertEqual(self.put_game(pgn(), token="nope").status_code, 401)
        self.assertFalse(self.path().exists())

    def test_unknown_match(self):
        response = self.put_game(pgn(), match_id="ghost")
        self.assertEqual(response.status_code, 404)
        self.assertIn("PUT the match", response.content.decode())

    def test_game_id(self):
        self.assertEqual(self.put_game(pgn(), game="..").status_code, 400)
        self.assertEqual(self.put_game(pgn(), game="a" * 65).status_code, 400)

    def test_not_a_pgn(self):
        self.assertEqual(self.put_game(b"").status_code, 400)
        self.assertEqual(self.put_game(b"\xff\xfe").status_code, 400)

    def test_illegal_move(self):
        response = self.put_game(pgn("1. e4 e5 2. Ke3"))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.path().exists())

    def test_result_tag(self):
        self.assertEqual(self.put_game(pgn().replace(b'"*"', b'"2-0"')).status_code, 400)


class Identity(GameTestCase):
    def identified(self, **kwargs):
        self.put_game(pgn(**kwargs))
        game = Game.objects.get()
        return game.opponent, game.colour

    def test_subject_as_white(self):
        self.assertEqual(self.identified(), ("barbarossa", "white"))

    def test_subject_as_black(self):
        self.assertEqual(self.identified(white="Barbarossa 0.6.0", black="Elephant 1.0.0"), ("barbarossa", "black"))

    def test_bare_names(self):
        self.assertEqual(self.identified(white="Belette", black="Elephant"), ("belette", "black"))

    def test_tags_win(self):
        self.assertEqual(self.identified(white="x", black="y", Subject="Black", Opponent="belette"),
                         ("belette", "black"))

    def test_unknown_names_are_left_blank(self):
        self.assertEqual(self.identified(white="x", black="y"), ("", ""))

    def test_unknown_opponent_tag(self):
        self.assertEqual(self.put_game(pgn(Opponent="ghost")).status_code, 400)


class Purging(GameTestCase):
    def setUp(self):
        super().setUp()
        self.put_game(pgn(result="1-0"), game="1")
        self.put_game(pgn(), game="2")

    def purge(self, *args):
        out = StringIO()
        call_command("purge_games", *args, stdout=out)
        return out.getvalue()

    def test_needs_a_selection(self):
        with self.assertRaises(CommandError):
            self.purge()

    def test_by_match_removes_files_and_rows_only(self):
        self.purge("--match", "gauntlet-test-1")
        self.assertEqual(Game.objects.count(), 0)
        self.assertFalse((self.games / "gauntlet-test-1").exists())
        self.assertEqual(Match.objects.count(), 1)
        self.assertEqual(MatchSnapshot.objects.count(), 1)
        self.assertTrue((self.archive / "matches" / "gauntlet-test-1.json").exists())

    def test_by_age(self):
        Game.objects.filter(game="1").update(finished_at=timezone.now() - timedelta(days=40))
        Game.objects.filter(game="2").update(received_at=timezone.now() - timedelta(days=1))
        self.purge("--older-than", "30")
        self.assertEqual(list(Game.objects.values_list("game", flat=True)), ["2"])
        self.assertFalse(self.path("1").exists())
        self.assertTrue(self.path("2").exists())

    def test_dry_run(self):
        self.purge("--match", "gauntlet-test-1", "--dry-run")
        self.assertEqual(Game.objects.count(), 2)
        self.assertTrue(self.path("1").exists())

    def test_deleting_a_match_removes_its_games(self):
        games.delete_files("gauntlet-test-1")
        self.assertFalse((self.games / "gauntlet-test-1").exists())
