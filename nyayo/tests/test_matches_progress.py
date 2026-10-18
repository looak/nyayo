"""An SPRT's progress graphs and the projection drawn from its snapshots."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from django.test import SimpleTestCase

from nyayo.matches import progress
from nyayo.matches.models import Match, MatchSnapshot

from .test_matches_ingest import MatchTestCase
from .test_matches_stats import ID, v1

T0 = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
SPRT = {"lower": -2.94, "upper": 2.94, "result": None}


def snap(games, llr, minutes, elo=None, error=None):
    return SimpleNamespace(games=games, wins=games // 2, draws=games // 4, losses=games - games // 2 - games // 4,
                           llr=llr, received_at=T0 + timedelta(minutes=minutes),
                           stats={"elo": elo, "error": error, "model": "penta"} if elo is not None else None)


class Projection(SimpleTestCase):
    def series(self, rows):
        return progress.points(snap(*row) for row in rows)

    def test_a_rising_llr_heads_for_the_upper_bound(self):
        series = self.series([(100, 0.1, 0), (200, 0.5, 10), (300, 0.9, 20), (400, 1.3, 30)])
        result = progress.projection(series, SPRT, T0 + timedelta(minutes=30))
        self.assertEqual(result["bound"], "upper")
        self.assertAlmostEqual(result["games"], (2.94 - 1.3) / 0.004, delta=1)
        # 10 games a minute
        self.assertAlmostEqual(result["seconds"], result["games"] * 6, delta=1)
        self.assertEqual(result["at"], T0 + timedelta(minutes=30) + timedelta(seconds=result["seconds"]))

    def test_a_falling_llr_heads_for_the_lower_bound(self):
        series = self.series([(100, -0.1, 0), (200, -0.5, 10), (300, -0.9, 20)])
        self.assertEqual(progress.projection(series, SPRT, T0)["bound"], "lower")

    def test_only_the_later_half_counts(self):
        # flat at first, then climbing: the early games would flatten the slope
        series = self.series([(100, 0.0, 0), (200, 0.0, 10), (300, 0.0, 20), (400, 0.6, 30), (500, 1.2, 40),
                              (600, 1.8, 50)])
        result = progress.projection(series, SPRT, T0)
        self.assertAlmostEqual(result["games"], (2.94 - 1.8) / 0.006, delta=1)

    def test_too_few_reports(self):
        self.assertIn("too few", progress.projection(self.series([(100, 0.1, 0), (200, 0.2, 5)]), SPRT, T0)["reason"])

    def test_a_flat_llr(self):
        series = self.series([(100, 0.2, 0), (200, 0.2, 5), (300, 0.2, 10)])
        self.assertIn("isn't moving", progress.projection(series, SPRT, T0)["reason"])

    def test_past_a_bound_waits_for_the_producer(self):
        series = self.series([(100, 1.0, 0), (200, 2.0, 5), (300, 3.0, 10)])
        self.assertIn("producer", progress.projection(series, SPRT, T0)["reason"])

    def test_a_decided_test_has_no_projection(self):
        series = self.series([(100, 1.0, 0), (200, 2.0, 5), (300, 3.0, 10)])
        self.assertIsNone(progress.projection(series, {**SPRT, "result": "passed"}, T0))

    def test_games_without_time_between_them(self):
        series = self.series([(100, 0.1, 0), (200, 0.5, 0), (300, 0.9, 0)])
        self.assertIsNone(progress.projection(series, SPRT, T0)["seconds"])


class Graphs(SimpleTestCase):
    def test_elo_comes_from_the_producer_when_it_sent_it(self):
        rows = progress.points([snap(100, 0.1, 0, elo=12.0, error=40.0), snap(200, 0.2, 5, elo=15.0, error=30.0)])
        self.assertEqual([(r["elo"], r["error"]) for r in rows], [(12.0, 40.0), (15.0, 30.0)])

    def test_elo_is_calculated_otherwise(self):
        rows = progress.points([snap(100, 0.1, 0)])
        self.assertIsNotNone(rows[0]["elo"])

    def test_a_graph_needs_two_points(self):
        self.assertEqual(progress.graphs(progress.points([snap(100, 0.1, 0)]), SPRT), [])

    def test_the_llr_graph_marks_the_bounds_and_elo_has_a_band(self):
        rows = progress.points([snap(g, g / 100 / 5, g, elo=10.0 + g / 100, error=50.0 / (g / 100))
                                for g in range(100, 600, 100)])
        elo, llr = progress.graphs(rows, SPRT)
        self.assertEqual((elo["key"], llr["key"]), ("elo", "llr"))
        self.assertTrue(elo["band"])
        self.assertEqual([m["label"] for m in llr["marks"]], ["lower", "upper"])
        self.assertIn(",", llr["line"])

    def test_many_snapshots_are_thinned(self):
        rows = [{"games": n, "time": T0, "llr": n / 1000, "elo": 1.0, "error": None} for n in range(1, 2001)]
        chart = progress.line_chart("llr", "LLR", rows, lambda r: r["llr"])
        self.assertLessEqual(len(chart["line"].split()), progress.MAX_POINTS)
        self.assertTrue(chart["line"].endswith(chart["line"].split()[-1]))


class OnThePage(MatchTestCase):
    def report(self, games, llr, minute):
        document = v1()
        document["match"]["updated_at"] = (T0 + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")
        wins = games // 2
        document["results"][0].update(games=games, wins=wins, draws=games - wins, losses=0, penta=None,
                                      crashes={"subject": 0, "opponent": 0})
        document["sprt"]["llr"] = llr
        document["stats"] = {"elo": games / 10, "error": 200 / games, "model": "penta"}
        self.assertIn(self.put_match(document).status_code, (200, 201))

    def test_a_running_sprt_shows_both_graphs_and_a_projection(self):
        for i, games in enumerate((100, 200, 300, 400)):
            self.report(games, 0.3 * (i + 1), i)
        page = self.client.get(f"/matches/{ID}").content.decode()
        self.assertIn('aria-label="Elo over games"', page)
        self.assertIn('aria-label="LLR over games"', page)
        self.assertIn("Heading for", page)
        self.assertIn("the upper bound", page)
        self.assertIn("Games to go", page)

    def test_a_finished_sprt_keeps_the_graphs_and_drops_the_projection(self):
        for i, games in enumerate((100, 200, 300)):
            self.report(games, 0.3 * (i + 1), i)
        Match.objects.update(state="finished")
        page = self.client.get(f"/matches/{ID}").content.decode()
        self.assertIn('aria-label="LLR over games"', page)
        self.assertNotIn("Heading for", page)

    def test_a_gauntlet_has_no_sprt_graphs(self):
        from .test_matches_ingest import gauntlet
        self.put_match(gauntlet())
        page = self.client.get("/matches/gauntlet-test-1").content.decode()
        self.assertNotIn("over games", page)
