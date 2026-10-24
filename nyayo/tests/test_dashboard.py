"""The dashboard (/dashboard): tiles, a calendar of games kept, two donuts, matches and openings."""

import bz2
from datetime import timedelta

from django.utils import timezone

from nyayo.matches import dashboard

from .test_index import MATE, RESIGNED
from .test_uploads import BatchCase, batch_text, game

BOOK = "rnbqkb1r/pp2pppp/3p1n2/8/3NP3/8/PPP2PPP/RNBQKB1R w KQkq - 0 5"


class Dashboard(BatchCase):
    def page(self):
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_with_nothing_kept(self):
        page = self.page()
        self.assertIn(">Games kept<", page)
        self.assertIn("No games kept yet.", page)
        self.assertIn("No game has been read for its opening yet.", page)
        self.assertIn('href="/dashboard" class="active"', page)

    def test_the_games_kept(self):
        today = timezone.now().strftime("%Y.%m.%d")
        self.keep(bz2.compress(batch_text(
            game("1-0", moves=MATE, Date=today),
            game("0-1", moves=RESIGNED, Date=today),
            game("1/2-1/2", moves="1. Nb3 e6", FEN=BOOK, SetUp="1", Date=today),
            game("1-0", moves="1. Nb3 e6", FEN=BOOK, SetUp="1", Date=today))))
        page = self.page()
        # the tiles
        self.assertIn('<div class="big">4</div>', page)
        self.assertIn("4 cards", page)
        self.assertIn(">100%<", page)
        # a day with four games, the darkest green
        self.assertIn(f'fill="{dashboard.LEVELS[4]}"><title>4 games on', page)
        self.assertIn("4 in the last year", page)
        # the donuts
        self.assertIn("How the kept games ended: ", page)
        self.assertIn("Checkmate 1", page)
        self.assertIn("Won 2", page)
        self.assertIn("Drawn 1", page)
        self.assertIn("Lost 1", page)
        # the book position two games started from, with how the engine scored (a win and a draw)
        openings = dashboard.openings()
        self.assertEqual(openings[0]["games"], 2)
        self.assertEqual(openings[0]["score"], 0.75)
        self.assertIn(f'href="/games?opening={openings[0]["key"]}"', page)
        # the match they came from
        self.assertIn('href="/matches/gauntlet-test-1"', page)

    def test_the_calendar_is_a_year_of_weeks_from_monday(self):
        now = timezone.now()
        days = dashboard.calendar(now)["days"]
        self.assertEqual(len(days), 7 * (dashboard.WEEKS - 1) + now.weekday() + 1)
        first = now.date() - timedelta(days=len(days) - 1)
        self.assertEqual(first.weekday(), 0)
        self.assertIn(f"on {first:%a %d %b %Y}", days[0]["tip"])

    def test_a_donut_goes_round_once(self):
        ring = dashboard.donut([("a", 1, "#000"), ("b", 3, "#111"), ("c", 0, "#222")])
        self.assertEqual([p["label"] for p in ring["parts"]], ["a", "b"])
        self.assertEqual([p["share"] for p in ring["parts"]], ["25.0%", "75.0%"])
        self.assertEqual([p["offset"] for p in ring["parts"]], ["25.000", "0.000"])
