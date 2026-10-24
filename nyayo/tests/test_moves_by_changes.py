"""Moves by changes (docs/match-schema.md, Moves by changes): PATCH on a live game, the per-ply record and the state."""

import gzip
import json
import random
from unittest import mock

import chess
import chess.pgn

from nyayo.matches import games, liveplies, pgnview

from .test_games import GameTestCase, pgn

OPENING = "1. e4 {+0.30/12 0.110s, n=1000, nps=9000, pv=\"e2e4 e7e5\"} e5 {-0.20/11 0.090s, tl=118.5s, n=900}"


def move(san, comment=None):
    return {"san": san, "comment": comment}


def random_game(plies, seed=7):
    """A legal game of that many plies as [(SAN, comment)], and the PGN of it so far."""
    rng, board, moves = random.Random(seed), chess.Board(), []
    for ply in range(plies):
        legal = list(board.legal_moves)
        chosen = rng.choice(legal)
        moves.append((board.san(chosen), f"{rng.randint(-90, 90) / 100:+.2f}/{rng.randint(8, 16)} 0.100s, n=5000"))
        board.push(chosen)
        if board.is_game_over():
            break
    return moves


def pgn_of(moves, result="*", **tags):
    board, parts = chess.Board(), []
    for san, comment in moves:
        parts.append((f"{board.fullmove_number}. " if board.turn == chess.WHITE else "") + san + " {" + comment + "}")
        board.push_san(san)
    return pgn(" ".join(parts), result, **tags)


class MovesCase(GameTestCase):
    def patch(self, start, moves, game="1", token=None, gzipped=False, match_id="gauntlet-test-1"):
        body = json.dumps({"from": start, "moves": moves}).encode()
        headers = {"Authorization": f"Bearer {token or self.token}"}
        if gzipped:
            body, headers["Content-Encoding"] = gzip.compress(body), "gzip"
        return self.client.patch(f"/api/matches/{match_id}/games/{game}", data=body, content_type="application/json",
                                 headers=headers)

    def records(self, game="1"):
        plies, _ = liveplies.side_files(self.path(game))
        return [json.loads(line) for line in plies.read_text("utf-8").splitlines()]

    def state(self, game="1"):
        return json.loads(liveplies.side_files(self.path(game))[1].read_bytes())

    def mainline(self, game="1"):
        parsed = chess.pgn.read_game(open(self.path(game), encoding="utf-8"))
        self.assertEqual(parsed.errors, [])
        return [(node.san(), node.comment) for node in parsed.mainline()]


class TheWholePgn(MovesCase):
    def test_a_whole_put_of_a_live_game_says_how_many_plies_nyayo_holds(self):
        response = self.put_game(pgn(OPENING))
        self.assertEqual((response.status_code, response["Nyayo-Plies"]), (201, "2"))
        response = self.put_game(pgn(OPENING + " 2. Nf3"))
        self.assertEqual((response.status_code, response["Nyayo-Plies"]), (200, "3"))
        self.assertEqual(len(self.records()), 3)

    def test_the_record_has_each_ply_read_once(self):
        self.put_game(pgn(OPENING, tc=None, TimeControl="120+1"))
        first, second = self.records()
        self.assertEqual((first["ply"], first["san"], first["uci"], first["colour"]), (1, "e4", "e2e4", "white"))
        self.assertEqual((first["score"], first["pawns"], first["depth"], first["seconds"], first["nodes"],
                          first["nps"], first["pv"]), ("+0.30", 0.3, 12, 0.11, 1000, 9000, "e2e4 e7e5"))
        self.assertEqual(first["fen"], chess.Board().copy().fen().replace("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
                                                                           "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"))
        self.assertEqual((second["colour"], second["pawns"], second["tl"]), ("black", 0.2, 118.5))
        state = self.state()
        self.assertEqual((state["ply"], state["last"]["san"], state["latest"]["white"]["score"]), (2, "e5", "+0.30"))
        # 120 s less 0.11 s plus the increment, and fastchess's own tl= for black
        self.assertAlmostEqual(state["left"]["white"], 120890.0)
        self.assertEqual(state["left"]["black"], 118500.0)

    def test_a_sealed_game_has_none_of_it(self):
        self.put_game(pgn(OPENING))
        self.put_game(pgn(OPENING + " 2. Nf3", "1-0"))
        plies, state = liveplies.side_files(self.path())
        self.assertFalse(plies.exists() or state.exists())
        self.assertNotIn("Nyayo-Plies", self.put_game(pgn(OPENING + " 2. Nf3", "1-0")))

    def test_a_sealed_pgn_that_differs_from_the_appended_moves_wins_and_is_logged(self):
        self.put_game(pgn(OPENING))
        with self.assertLogs("nyayo.liveplies", "WARNING") as logged:
            self.put_game(pgn("1. d4 d5 2. c4", "1-0"))
        self.assertIn("differs from the moves appended at ply 1", logged.output[0])
        self.assertEqual(self.mainline()[0][0], "d4")

    def test_a_game_restarted_on_a_resume_is_a_whole_pgn_and_shorter(self):
        self.put_game(pgn(OPENING + " 2. Nf3 Nc6"))
        response = self.put_game(pgn("1. d4"))
        self.assertEqual(response["Nyayo-Plies"], "1")
        self.assertEqual(self.patch(1, [move("d5")]).status_code, 200)


class Appending(MovesCase):
    def setUp(self):
        super().setUp()
        self.put_game(pgn(OPENING))

    def test_the_new_moves_are_appended_to_the_pgn_and_the_record(self):
        response = self.patch(2, [move("Nf3", "+0.40/12 0.100s, n=800"), move("Nc6", "-0.30/12 0.100s, n=700")])
        self.assertEqual((response.status_code, response["Nyayo-Plies"]), (200, "4"))
        text = self.path().read_text("utf-8")
        self.assertTrue(text.rstrip().endswith("2. Nf3 {+0.40/12 0.100s, n=800} Nc6 {-0.30/12 0.100s, n=700} *"),
                        text)
        self.assertEqual([s for s, _ in self.mainline()], ["e4", "e5", "Nf3", "Nc6"])
        self.assertEqual([r["ply"] for r in self.records()], [1, 2, 3, 4])
        self.assertEqual(self.state()["ply"], 4)
        self.assertEqual(self.state()["size"], self.path().stat().st_size)

    def test_pgn_after_patches_reads_like_the_whole_pgn_of_the_same_moves(self):
        moves = random_game(120)
        self.put_game(pgn_of(moves[:10]), game="2")
        for at in range(10, 120, 7):
            chunk = [move(s, c) for s, c in moves[at:at + 7]]
            self.assertEqual(self.patch(at, chunk, game="2").status_code, 200)
        self.put_game(pgn_of(moves), game="3")
        self.assertEqual(self.mainline("2"), self.mainline("3"))
        self.assertEqual([r["fen"] for r in self.records("2")], [r["fen"] for r in self.records("3")])
        self.assertEqual({k: v for k, v in self.state("2").items() if k not in ("size", "pgn_end", "plies_size")},
                         {k: v for k, v in self.state("3").items() if k not in ("size", "pgn_end", "plies_size")})

    def test_a_game_that_starts_at_blacks_move(self):
        fen = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1"
        self.put_game(pgn("", FEN=fen, SetUp="1").replace(b"\n\n *", b"\n\n*"), game="5")
        self.assertEqual(self.patch(0, [move("e5"), move("Nf3")], game="5").status_code, 200)
        self.assertIn("1... e5 2. Nf3", self.path("5").read_text("utf-8"))
        self.assertEqual([s for s, _ in self.mainline("5")], ["e5", "Nf3"])

    def test_empty_moves_append_nothing(self):
        before = self.path().read_bytes()
        response = self.patch(2, [])
        self.assertEqual((response.status_code, response["Nyayo-Plies"]), (200, "2"))
        self.assertEqual(self.path().read_bytes(), before)

    def test_a_gzipped_request(self):
        self.assertEqual(self.patch(2, [move("Nf3")], gzipped=True).status_code, 200)

    def test_a_page_shows_the_appended_game(self):
        self.patch(2, [move("Nf3", "+0.40/12 0.100s, n=800")])
        self.assertContains(self.client.get("/matches/gauntlet-test-1/games/1"), "Nf3")


class WhenItDoesntFollow(MovesCase):
    def setUp(self):
        super().setUp()
        self.put_game(pgn(OPENING))

    def unchanged(self):
        self.assertEqual(self.state()["ply"], 2)
        self.assertEqual(len(self.records()), 2)
        self.assertEqual([s for s, _ in self.mainline()], ["e4", "e5"])

    def test_a_from_that_is_not_the_end_is_409_with_the_plies_held(self):
        for start in (1, 3):
            response = self.patch(start, [move("Nf3")])
            self.assertEqual((response.status_code, response["Nyayo-Plies"]), (409, "2"))
            self.assertIn("holds 2 plies", response.content.decode())
        self.unchanged()

    def test_an_illegal_move_is_400_at_once_naming_it_and_none_are_appended(self):
        response = self.patch(2, [move("Nf3"), move("Ke3"), move("Nc6")])
        self.assertEqual(response.status_code, 400)
        self.assertIn("moves[1] (Ke3) at ply 4", response.content.decode())
        self.unchanged()
        self.assertEqual(self.patch(2, [move("Nf3")]).status_code, 200)

    def test_an_ambiguous_or_unreadable_move_is_400(self):
        for san in ("Nf4", "e9", "zz", "N"):
            self.assertEqual(self.patch(2, [move(san)]).status_code, 400, san)
        self.unchanged()

    def test_a_body_that_is_not_a_request_is_400(self):
        for body in (b"not json", b"[]", b'{"from": "2", "moves": []}', b'{"from": 2}',
                     b'{"from": 2, "moves": [{"san": ""}]}', b'{"from": 2, "moves": [{"san": "Nf3", "comment": "}"}]}',
                     b'{"from": -1, "moves": []}'):
            response = self.client.patch("/api/matches/gauntlet-test-1/games/1", data=body,
                                         content_type="application/json",
                                         headers={"Authorization": f"Bearer {self.token}"})
            self.assertEqual(response.status_code, 400, body)
        self.unchanged()

    def test_a_game_nyayo_does_not_hold_is_409_with_zero_plies(self):
        response = self.patch(0, [move("e4")], game="99")
        self.assertEqual((response.status_code, response["Nyayo-Plies"]), (409, "0"))

    def test_an_unknown_match_is_404_and_no_token_is_401(self):
        self.assertEqual(self.patch(2, [move("Nf3")], match_id="ghost").status_code, 404)
        self.assertEqual(self.patch(2, [move("Nf3")], token="nope").status_code, 401)

    def test_a_sealed_game_is_409(self):
        self.put_game(pgn(OPENING + " 2. Nf3", "1-0"))
        response = self.patch(3, [move("Nc6")])
        self.assertEqual(response.status_code, 409)
        self.assertIn("sealed", response.content.decode())
        self.assertNotIn("Nyayo-Plies", response)

    def test_a_pgn_changed_under_the_record_is_not_appended_to(self):
        with open(self.path(), "ab") as file:
            file.write(b"\n")
        response = self.patch(2, [move("Nf3")])
        self.assertEqual(response.status_code, 409)
        self.assertIn("send the whole PGN", response.content.decode())
        # the whole PGN repairs it
        self.assertEqual(self.put_game(pgn(OPENING))["Nyayo-Plies"], "2")
        self.assertEqual(self.patch(2, [move("Nf3")]).status_code, 200)

    def test_a_game_from_before_there_was_a_record_is_resynced(self):
        liveplies.forget(self.path())
        response = self.patch(2, [move("Nf3")])
        self.assertEqual((response.status_code, response["Nyayo-Plies"]), (409, "0"))
        self.assertEqual(self.put_game(pgn(OPENING))["Nyayo-Plies"], "2")

    def test_a_crash_between_the_pgn_and_the_record_is_repaired_by_a_resync(self):
        with mock.patch.object(liveplies, "atomic_write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.patch(2, [move("Nf3")])
        response = self.patch(2, [move("Nf3")])
        self.assertEqual(response.status_code, 409)  # the PGN got ahead of the state
        self.assertEqual(self.put_game(pgn(OPENING + " 2. Nf3"))["Nyayo-Plies"], "3")
        self.assertEqual(self.patch(3, [move("Nc6")]).status_code, 200)


class WhatItCosts(MovesCase):
    def test_a_move_at_ply_300_parses_no_game_and_reads_no_pgn(self):
        moves = random_game(300)
        self.assertGreaterEqual(len(moves), 200)
        self.put_game(pgn_of(moves[:-2]), game="long")
        with mock.patch.object(chess.pgn, "read_game", side_effect=AssertionError("parsed the game")), \
                mock.patch.object(games, "read_pgn", side_effect=AssertionError("read the pgn")), \
                mock.patch.object(pgnview, "load", side_effect=AssertionError("loaded the game")):
            response = self.patch(len(moves) - 2, [move(s, c) for s, c in moves[-2:]], game="long")
        self.assertEqual((response.status_code, response["Nyayo-Plies"]), (200, str(len(moves))))
        self.assertEqual(len(self.records("long")), len(moves))

    def test_the_work_does_not_grow_with_the_game(self):
        def calls(plies):
            moves = random_game(plies, seed=plies)
            self.put_game(pgn_of(moves[:-1]), game=f"g{plies}")
            with mock.patch.object(chess.Board, "push", autospec=True, side_effect=chess.Board.push) as push, \
                    mock.patch.object(chess.Board, "parse_san", autospec=True,
                                      side_effect=chess.Board.parse_san) as parse:
                self.patch(len(moves) - 1, [move(*moves[-1])], game=f"g{plies}")
            return push.call_count, parse.call_count

        self.assertEqual(calls(10), calls(250))

    def test_no_row_is_written(self):
        self.put_game(pgn(OPENING))
        with self.assertNumQueries(3) as queries:  # the token, the match's settings, the game's row
            self.patch(2, [move("Nf3")])
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries.captured_queries))
