import json
from types import SimpleNamespace

import chess
from django.test import SimpleTestCase

from nyayo.matches import pgnview, stats
from nyayo.matches.models import Match

from .test_games import GameTestCase, pgn
from .test_matches_ingest import gauntlet, scheduled

COMMENTED = (
    '[White "Elephant 1.0.0"]\n[Black "Barbarossa 0.6.0"]\n[Result "*"]\n\n'
    "1. e4 {+0.30/18 1.204s} e5 {-0.25/16 0.9s} 2. Nf3 {+M5/22 0.4s} Nc6 {book} *\n"
).encode()


class Stats(SimpleTestCase):
    def test_elo_of_a_score(self):
        self.assertAlmostEqual(stats.elo(0.5), 0.0)
        self.assertAlmostEqual(stats.elo(0.64), 100.0, delta=1.0)
        self.assertIsNone(stats.elo(1.0))

    def test_error_shrinks_with_games(self):
        small = stats.rating(30, 40, 30)
        large = stats.rating(300, 400, 300)
        self.assertEqual(round(small.score, 3), 0.5)
        self.assertGreater(small.error, large.error)

    def test_pentanomial_is_used_when_it_covers_every_game(self):
        # the same pairs' results, fully correlated within a pair: the trinomial would be too sure
        penta = [0, 0, 20, 0, 0]
        covered = stats.rating(20, 0, 20, penta)
        self.assertTrue(covered.paired)
        partial = stats.rating(20, 0, 20, [0, 0, 5, 0, 0])
        self.assertFalse(partial.paired)

    def test_no_games(self):
        self.assertIsNone(stats.rating(0, 0, 0).elo)

    def test_performance_needs_every_rating(self):
        def result(opponent, wins, rating):
            return SimpleNamespace(opponent=opponent, games=wins + 10, wins=wins, draws=0, losses=10, penta=None,
                                   rating={"value": rating} if rating else None)
        rated = stats.summarize([result("a", 10, 2400), result("b", 10, 2500)])
        self.assertAlmostEqual(rated.performance, 2450.0)
        unrated = stats.summarize([result("a", 10, 2400), result("b", 10, None)])
        self.assertIsNone(unrated.performance)
        # excluding the unrated opponent makes it computable again
        self.assertAlmostEqual(stats.summarize([result("a", 10, 2400), result("b", 10, None)], {"b"}).performance, 2400)


class Comments(SimpleTestCase):
    def test_scores_depth_and_time(self):
        self.assertEqual(pgnview.parse_comment("+0.30/18 1.204s"), ("+0.30", 0.30, 18, 1.204))
        self.assertEqual(pgnview.parse_comment("-1.50/9 0.1s")[1], -1.5)
        score, pawns, *_ = pgnview.parse_comment("-M3/30 0.2s")
        self.assertEqual((score, pawns < 0), ("-M3", True))

    def test_other_comments_are_ignored(self):
        self.assertEqual(pgnview.parse_comment("book"), ("", None, None, None))

    def test_white_side_scores_for_the_graph(self):
        parsed = pgnview.load(COMMENTED)
        self.assertEqual([m.pawns is not None for m in parsed.moves], [True, True, True, False])
        self.assertEqual(parsed.moves[0].pawns, 0.30)    # white's own +0.30
        self.assertEqual(parsed.moves[1].pawns, 0.25)    # black's -0.25 is white's +0.25
        self.assertTrue(parsed.moves[3].book)
        self.assertEqual([g["key"] for g in pgnview.graphs(parsed.moves, 2)], ["eval", "depth", "time"])

    def test_board_at_a_ply(self):
        parsed = pgnview.load(COMMENTED)
        board, last = pgnview.position(parsed, 1)
        self.assertEqual(last.uci(), "e2e4")
        self.assertIsNotNone(board.piece_at(chess.E4))
        self.assertTrue(pgnview.board_svg(parsed, 0).startswith("<svg"))


class Pages(GameTestCase):
    def test_matches_list(self):
        response = self.client.get("/matches")
        self.assertContains(response, "gauntlet-test-1")
        self.assertContains(response, "elephant-1.0.0-ccrl-blitz")
        self.assertContains(self.client.get("/matches?kind=gauntlet"), "gauntlet-test-1")
        self.assertNotContains(self.client.get("/matches?kind=sprt"), "gauntlet-test-1")
        self.assertNotContains(self.client.get("/matches?state=finished"), "gauntlet-test-1")

    def test_match_page_shows_counts_and_rating(self):
        response = self.client.get("/matches/gauntlet-test-1")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Barbarossa 0.6.0")
        self.assertContains(response, "2465")
        self.assertContains(response, "65.0%")  # 5 wins and 3 draws in 10
        self.assertContains(response, 'http-equiv="refresh"')

    def test_a_finished_match_does_not_refresh(self):
        document = gauntlet()
        document["match"].update(state="finished", finished_at="2026-10-15T10:00:00Z")
        self.put_match(document)
        self.assertNotContains(self.client.get("/matches/gauntlet-test-1"), 'http-equiv="refresh"')

    def test_exclude_an_opponent(self):
        response = self.client.get("/matches/gauntlet-test-1?exclude=barbarossa")
        self.assertContains(response, "without barbarossa")

    def test_unknown_match(self):
        self.assertEqual(self.client.get("/matches/ghost").status_code, 404)

    def test_the_schedule_is_listed_in_groups(self):
        self.put_match(scheduled())
        self.put_game(pgn("1. e4 e5", result="1-0"), game="1")
        self.put_game(pgn("1. d4"), game="2")
        response = self.client.get("/matches/gauntlet-test-1")
        self.assertContains(response, "2 scheduled")
        self.assertContains(response, ">Playing <")
        self.assertContains(response, ">Finished <")
        self.assertContains(response, 'href="/matches/gauntlet-test-1/games/2"')
        self.assertContains(response, "outcome-win")

    def test_a_stopped_match_shows_nothing_as_playing(self):
        document = scheduled()
        document["match"]["state"] = "stopped"
        self.put_match(document)
        self.put_game(pgn("1. d4"), game="2")
        response = self.client.get("/matches/gauntlet-test-1")
        self.assertNotContains(response, ">Playing <")
        self.assertContains(response, ">Cut off <")

    def test_game_page(self):
        self.put_game(COMMENTED)
        response = self.client.get("/matches/gauntlet-test-1/games/1")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<svg")
        self.assertContains(response, "Nf3")
        self.assertContains(response, "+M5")
        self.assertContains(response, 'data-plies="4"')
        self.assertContains(response, "live")

    def test_game_page_at_a_ply(self):
        self.put_game(COMMENTED)
        self.assertContains(self.client.get("/matches/gauntlet-test-1/games/1?ply=2"), 'data-ply="2"')
        self.assertContains(self.client.get("/matches/gauntlet-test-1/games/1?ply=99"), 'data-ply="4"')

    def test_live_fragment(self):
        self.put_game(COMMENTED)
        data = json.loads(self.client.get("/matches/gauntlet-test-1/games/1/live").content)
        self.assertTrue(data["live"])
        self.assertEqual(data["plies"], 4)
        self.assertIn("<svg", data["html"])
        self.put_game(COMMENTED.replace(b'"*"', b'"1-0"').replace(b"Nc6 {book} *", b"Nc6 {book} 1-0"))
        data = json.loads(self.client.get("/matches/gauntlet-test-1/games/1/live").content)
        self.assertFalse(data["live"])

    def test_a_game_that_was_cut_off_is_not_live(self):
        document = gauntlet()
        document["match"]["state"] = "stopped"
        self.put_match(document)
        self.put_game(COMMENTED)
        response = self.client.get("/matches/gauntlet-test-1/games/1")
        self.assertContains(response, "cut off")
        data = json.loads(self.client.get("/matches/gauntlet-test-1/games/1/live").content)
        self.assertFalse(data["live"])

    def test_a_scheduled_game_waits(self):
        self.put_match(scheduled())
        response = self.client.get("/matches/gauntlet-test-1/games/3")
        self.assertContains(response, "has not started yet")
        data = json.loads(self.client.get("/matches/gauntlet-test-1/games/3/live").content)
        self.assertTrue(data["waiting"])

    def test_unknown_game(self):
        self.assertEqual(self.client.get("/matches/gauntlet-test-1/games/9").status_code, 404)
        self.assertEqual(self.client.get("/matches/gauntlet-test-1/games/9/live").status_code, 404)

    def test_navigation_has_matches(self):
        self.assertContains(self.client.get("/"), 'href="/matches"')

    def test_the_subject_is_at_the_bottom(self):
        self.put_game(pgn(white="Barbarossa 0.6.0", black="Elephant 1.0.0"))
        self.assertEqual(Match.objects.count(), 1)
        self.assertContains(self.client.get("/matches/gauntlet-test-1/games/1"), "<svg")


FROM_A_POSITION = "\n".join([
    '[White "Elephant 1.0.0"]',
    '[Black "Barbarossa 0.6.0"]',
    '[Result "*"]',
    '[SetUp "1"]',
    '[FEN "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2"]',
    "",
    "2. Nf3 Nc6 3. Bb5 *",
    "",
]).encode()


class FromAPosition(SimpleTestCase):
    def test_move_numbers_follow_the_position(self):
        parsed = pgnview.load(FROM_A_POSITION)
        self.assertEqual([m.number for m in parsed.moves], [2, 2, 3])
        self.assertEqual([row[0] for row in pgnview.pairs(parsed.moves)], [2, 3])


class LiveBoards(GameTestCase):
    def test_the_match_page_shows_the_boards_being_played(self):
        self.put_match(scheduled())
        self.put_game(COMMENTED, game="2")
        response = self.client.get("/matches/gauntlet-test-1")
        self.assertContains(response, "Playing now")
        self.assertContains(response, 'class="live-board"')
        self.assertContains(response, "<svg")

    def test_no_boards_when_nothing_is_playing(self):
        self.assertNotContains(self.client.get("/matches/gauntlet-test-1"), "Playing now")


class FastPolling(GameTestCase):
    URL = "/matches/gauntlet-test-1/games/1/live"

    def fragment(self, **query):
        return self.client.get(self.URL, query)

    def test_the_fragment_carries_a_version(self):
        self.put_game(COMMENTED)
        data = json.loads(self.fragment().content)
        self.assertTrue(data["v"])

    def test_nothing_new_is_an_empty_204_without_parsing(self):
        self.put_game(COMMENTED)
        version = json.loads(self.fragment().content)["v"]
        with self.assertNumQueries(1):  # the match's state, nothing else
            response = self.fragment(v=version)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")

    def test_a_new_move_changes_the_version(self):
        self.put_game(COMMENTED)
        version = json.loads(self.fragment().content)["v"]
        self.put_game(COMMENTED.replace(b"Nc6 {book} *", b"Nc6 {book} 3. Bb5 *"))
        response = self.fragment(v=version)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertNotEqual(data["v"], version)
        self.assertEqual(data["plies"], 5)

    def test_a_stopped_match_changes_the_version(self):
        self.put_game(COMMENTED)
        version = json.loads(self.fragment().content)["v"]
        document = gauntlet()
        document["match"]["state"] = "stopped"
        self.put_match(document)
        data = json.loads(self.fragment(v=version).content)
        self.assertFalse(data["live"])

    def test_the_version_is_per_ply(self):
        self.put_game(COMMENTED)
        end = json.loads(self.fragment().content)["v"]
        self.assertEqual(self.fragment(v=end, ply="2").status_code, 200)

    def test_the_page_starts_from_the_same_version(self):
        self.put_game(COMMENTED)
        page = self.client.get("/matches/gauntlet-test-1/games/1").content.decode()
        version = json.loads(self.fragment().content)["v"]
        self.assertIn(f'data-version="{version}"', page)
        self.assertEqual(self.fragment(v=version).status_code, 204)

    def test_the_interval_is_configurable(self):
        self.put_game(COMMENTED)
        self.assertContains(self.client.get("/matches/gauntlet-test-1/games/1"), 'data-poll="100"')
        with self.settings(NYAYO_GAME_POLL_MS=250):
            self.assertContains(self.client.get("/matches/gauntlet-test-1/games/1"), 'data-poll="250"')

    def test_ids_that_climb_are_not_found(self):
        self.assertEqual(self.client.get("/matches/gauntlet-test-1/games/../live").status_code, 404)
        self.assertEqual(self.client.get("/matches/ghost/games/1/live").status_code, 404)



EXTRAS = "\n".join([
    '[White "Elephant 1.0.0"]', '[Black "Barbarossa 0.6.0"]', '[Result "*"]', '[TimeControl "60+1"]', "",
    "1. e4 {+0.30/18 2.000s, n=1200000, nps=600000, pv=e4 e5 Nf3} e5 {-0.25/16 3.500s} 2. Nf3 {+0.40/19 1.000s} *",
    "",
]).encode()


class Clocks(SimpleTestCase):
    def test_time_controls(self):
        self.assertEqual(pgnview.parse_time_control("120+1"), (120.0, 1.0))
        self.assertEqual(pgnview.parse_time_control("300"), (300.0, 0.0))
        self.assertEqual(pgnview.parse_time_control("0.5+0.05"), (0.5, 0.05))
        self.assertIsNone(pgnview.parse_time_control("tournament"))
        self.assertIsNone(pgnview.parse_time_control(None))

    def test_remaining_is_base_plus_increments_minus_time_spent(self):
        parsed = pgnview.load(EXTRAS)
        left = pgnview.clocks(parsed.moves, (60.0, 1.0), 3)
        self.assertEqual(left["white"], (60 + 1 - 2.0 + 1 - 1.0) * 1000)
        self.assertEqual(left["black"], (60 + 1 - 3.5) * 1000)

    def test_a_book_move_costs_nothing(self):
        parsed = pgnview.load(COMMENTED)
        before = pgnview.clocks(parsed.moves, (60.0, 1.0), 3)["black"]
        after = pgnview.clocks(parsed.moves, (60.0, 1.0), 4)["black"]  # the fourth ply is a book move
        self.assertEqual(before, after)
        self.assertAlmostEqual(after, (60 + 1 - 0.9) * 1000)

    def test_unknown_time_control(self):
        self.assertIsNone(pgnview.clocks([], None, 0))

    def test_never_below_zero(self):
        parsed = pgnview.load(EXTRAS)
        self.assertEqual(pgnview.clocks(parsed.moves, (1.0, 0.0), 3)["white"], 0.0)


class Detail(SimpleTestCase):
    def test_nodes_nps_and_pv_are_read_when_present(self):
        parsed = pgnview.load(EXTRAS)
        first = parsed.moves[0]
        self.assertEqual((first.nodes, first.nps, first.pv), (1200000, 600000, "e4 e5 Nf3"))
        self.assertEqual((parsed.moves[1].nodes, parsed.moves[1].pv), (None, ""))

    def test_graphs_only_for_what_the_game_has(self):
        keys = [g["key"] for g in pgnview.graphs(pgnview.load(EXTRAS).moves)]
        self.assertEqual(keys, ["eval", "depth", "time"])  # nodes and nps have one value each, not a line

    def test_a_line_per_engine(self):
        graph = pgnview.graphs(pgnview.load(EXTRAS).moves)[0]
        self.assertEqual([line["colour"] for line in graph["lines"]], ["white", "black"])
        self.assertEqual(len(graph["lines"][0]["dots"]), 2)

    def test_eval_is_each_engines_own_view(self):
        moves = pgnview.load(EXTRAS).moves
        self.assertEqual([m.own_pawns for m in moves], [0.30, -0.25, 0.40])
        self.assertEqual(moves[1].pawns, 0.25)  # white's side, as before


class GamePageLayout(GameTestCase):
    def test_clocks_and_engine_figures(self):
        self.put_game(EXTRAS)
        page = self.client.get("/matches/gauntlet-test-1/games/1").content.decode()
        self.assertIn('data-clock-white="', page)
        self.assertIn('class="clock"', page)
        self.assertIn("Reconstructed", page)
        self.assertIn('data-running="black"', page)  # three plies played, black is on the move
        self.assertIn("thinking", page)

    def test_tabs_and_graph_buttons(self):
        self.put_game(EXTRAS)
        page = self.client.get("/matches/gauntlet-test-1/games/1").content.decode()
        self.assertIn('data-tab="graphs"', page)
        self.assertIn('data-metric="eval"', page)
        self.assertIn('data-metric="time"', page)
        self.assertNotIn('data-metric="nps"', page)

    def test_nodes_column_only_with_detail(self):
        self.put_game(EXTRAS)
        self.assertContains(self.client.get("/matches/gauntlet-test-1/games/1"), "<dt>Nodes</dt>")
        self.put_game(pgn(moves="1. e4 e5 2. Nf3"), game="2")
        self.assertNotContains(self.client.get("/matches/gauntlet-test-1/games/2"), "<dt>Nodes</dt>")

    def test_a_game_without_a_known_time_control_has_no_clocks(self):
        self.put_game(pgn("1. e4 e5", TimeControl="tournament"))
        self.assertNotContains(self.client.get("/matches/gauntlet-test-1/games/1"), 'class="clock"')

    def test_nobody_is_thinking_in_a_finished_game(self):
        self.put_game(EXTRAS.replace(b'"*"', b'"1-0"').replace(b"2. Nf3 {+0.40/19 1.000s} *", b"2. Nf3 {+0.40/19 1.000s} 1-0"))
        page = self.client.get("/matches/gauntlet-test-1/games/1").content.decode()
        self.assertNotIn('class="badge running thinking-badge"', page)
        self.assertIn('data-running=""', page)
