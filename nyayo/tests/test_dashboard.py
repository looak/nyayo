"""The dashboard (/, the home page): tiles, a calendar of games kept, two donuts, matches and openings."""

import bz2
import uuid
from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from nyayo.matches import catalogue, dashboard
from nyayo.matches.models import Match
from nyayo.runs.models import Run

from .test_index import MATE, RESIGNED
from .test_matches_ingest import v1
from .test_uploads import BatchCase, batch_text, game

BOOK = "rnbqkb1r/pp2pppp/3p1n2/8/3NP3/8/PPP2PPP/RNBQKB1R w KQkq - 0 5"


class Dashboard(BatchCase):
    def page(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_with_nothing_kept(self):
        page = self.page()
        self.assertIn(">Game database<", page)
        self.assertIn("No games kept yet.", page)
        self.assertIn("No game has been read for its opening yet.", page)
        self.assertIn('href="/" class="active"', page)
        self.assertIn("0 games · 100% indexed", page)
        self.assertIn("no SPRT yet", page)
        self.assertIn("0 commits", page)
        self.assertIn("no pushed run yet", page)

    def test_the_games_kept(self):
        today = timezone.now().strftime("%Y.%m.%d")
        self.keep(bz2.compress(batch_text(
            game("1-0", moves=MATE, Date=today),
            game("0-1", moves=RESIGNED, Date=today),
            game("1/2-1/2", moves="1. Nb3 e6", FEN=BOOK, SetUp="1", Date=today),
            game("1-0", moves="1. Nb3 e6", FEN=BOOK, SetUp="1", Date=today))))
        page = self.page()
        # the tiles: the game database, and with nothing running, the match played last
        self.assertIn("4 games · 100% indexed", page)
        self.assertIn("batches ", page)
        self.assertIn(">Playing now<", page)
        Match.objects.update(state="finished")
        page = self.page()
        self.assertIn(">Last played<", page)
        self.assertIn('href="/matches/gauntlet-test-1">That match', page)
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

    def test_the_latest_sprt_with_its_faults(self):
        self.put_match(v1())  # running, the engine under test crashed once
        page = self.page()
        self.assertIn("Latest SPRT · running", page)
        self.assertIn("+21.7", page)
        self.assertIn("<b>1 crash</b> by the engine under test", page)
        self.assertIn('class="tile danger"', page[page.index("Game database"):page.index("Latest SPRT")])
        # a verdict wins over a newer test still running
        older = v1(match__id="older", match__state="finished", match__finished_at="2026-10-16T13:00:00Z",
                   sprt__result="passed")
        older["results"][0]["crashes"] = {"subject": 0, "opponent": 0}
        self.put_match(older)
        tile = dashboard.latest_sprt()
        self.assertEqual((tile["match"].pk, tile["result"], tile["faults"]), ("older", "passed", []))

    def test_github_activity_is_the_runs_pushed_this_week(self):
        now = timezone.now()

        def run(commit, branch, days):
            Run.objects.create(id=uuid.uuid4(), kind="smoke", source="ci", state="published", commit=commit,
                               branch=branch, tree=commit, recorded_at=now, published_at=now - timedelta(days=days))

        run("a" * 40, "main", 1)
        run("a" * 40, "main", 1)  # the same commit, another run
        run("b" * 40, "feature", 2)
        run("c" * 40, "main", 9)  # last week
        tile = dashboard.github(now)
        self.assertEqual((tile["commits"], tile["branches"], tile["runs"]), (2, 2, 3))
        page = self.page()
        self.assertIn("2 commits", page)
        self.assertIn("on 2 branches · ", page)
        self.assertIn(">3 runs</a>", page)
        self.assertIn("ago, main", page)
        self.assertNotIn("github.com", page)
        # with the repository configured, the commits link to it
        with override_settings(NYAYO_GITHUB_REPO="looak/elephant"):
            page = self.page()
        self.assertIn('<a href="https://github.com/looak/elephant/commits">2 commits</a>', page)
        self.assertIn(f'<a href="https://github.com/looak/elephant/commit/{"a" * 40}">main @ aaaaaaa</a>', page)
        self.assertIn("On GitHub", page)

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

    def test_the_calendars_days_are_the_sites_zone(self):
        self.keep(bz2.compress(batch_text(game("1-0", moves=MATE))))
        now = timezone.now()
        # an evening game in Vancouver: early next morning in UTC
        evening = now.replace(hour=3, minute=0, second=0, microsecond=0) - timedelta(days=2)
        catalogue.index().execute("UPDATE card SET played_at = ?", (int(evening.timestamp()),))
        utc_day = [d for d in dashboard.calendar(now)["days"] if d["tip"].startswith("1 game")]
        with override_settings(TIME_ZONE="America/Vancouver"):
            calendar = dashboard.calendar(now)
            local_day = [d for d in calendar["days"] if d["tip"].startswith("1 game")]
            self.assertIn(calendar["zone"], ("PDT", "PST"))
            self.assertIn("by the day they were played (P", self.page())
        self.assertIn(f"on {evening:%a %d %b}", utc_day[0]["tip"])
        self.assertIn(f"on {evening - timedelta(days=1):%a %d %b}", local_day[0]["tip"])
