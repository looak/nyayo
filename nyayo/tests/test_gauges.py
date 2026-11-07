"""The SPRT and Elo gauges: the rules in gauges.py, and the SPRT page and the match list that draw them."""

import re

from django.test import SimpleTestCase

from nyayo.matches import gauges

from .test_matches_ingest import ID, MatchTestCase, gauntlet
from .test_sprt_page import example_document

L, U = -2.9444, 2.9444


class Zones(SimpleTestCase):
    def zone(self, llr):
        return gauges.zone_of(llr, L, U)[0]

    def test_five_equal_zones_from_the_lower_bound_to_the_upper(self):
        edges = [L + (U - L) * i / 5 for i in range(1, 5)]
        self.assertEqual(self.zone(L), 0)
        self.assertEqual(self.zone(edges[0] - 0.01), 0)
        self.assertEqual(self.zone(edges[0] + 0.01), 1)
        self.assertEqual(self.zone(0), 2)
        self.assertEqual(self.zone(edges[2] + 0.01), 3)
        self.assertEqual(self.zone(edges[3] + 0.01), 4)
        self.assertEqual(self.zone(U), 4)

    def test_past_a_bound_clamps_to_the_end(self):
        self.assertEqual(gauges.zone_of(-9, L, U), (0, 0.0))
        self.assertEqual(gauges.zone_of(9, L, U), (4, 1.0))
        self.assertEqual(gauges.llr_gauge(9, L, U, "passed")["angle"], 90)
        self.assertEqual(gauges.llr_gauge(-9, L, U, "failed")["angle"], -90)
        self.assertEqual(gauges.llr_gauge(0, L, U)["angle"], 0)

    def test_the_words_and_tones_of_a_running_test(self):
        values = (-2.9, -1.5, 0, 1.5, 2.9)
        self.assertEqual([gauges.llr_gauge(v, L, U)["word"] for v in values],
                         ["Near failing", "Leaning fail", "Open", "Leaning pass", "Near passing"])
        self.assertEqual([gauges.llr_gauge(v, L, U)["tone"] for v in values], ["fail", "fail", "open", "pass", "pass"])

    def test_a_verdict_says_passed_or_failed_whatever_the_zone(self):
        passed = gauges.llr_gauge(3.3151, L, U, "passed")
        self.assertEqual((passed["word"], passed["tone"]), ("Passed", "pass"))
        failed = gauges.llr_gauge(-3.05, L, U, "failed")
        self.assertEqual((failed["word"], failed["tone"]), ("Failed", "fail"))

    def test_the_small_line(self):
        self.assertEqual(gauges.llr_gauge(1.1207, L, U, None, 18)["line"], "LLR 1.12 of 2.94 · 18 games")
        self.assertEqual(gauges.llr_gauge(3.3151, L, U, "passed", 44)["line"], "LLR 3.32, past 2.94 · 44 games")
        self.assertEqual(gauges.llr_gauge(-3.05, L, U, "failed", 1)["line"], "LLR −3.05, past −2.94 · 1 game")
        self.assertEqual(gauges.llr_gauge(-1.2, L, U, None, 15730)["line"], "LLR −1.20 of −2.94 · 15,730 games")

    def test_no_gauge_without_an_llr_or_ordered_bounds(self):
        self.assertIsNone(gauges.llr_gauge(None, L, U))
        self.assertIsNone(gauges.llr_gauge(1, None, U))
        self.assertIsNone(gauges.llr_gauge(1, U, L))

    def test_never_a_probability(self):
        g = gauges.llr_gauge(1.1, L, U, None, 10)
        self.assertNotIn("chance", (g["aria"] + g["full_aria"] + g["line"]).lower())
        self.assertNotIn("%", g["line"])


class EloScale(SimpleTestCase):
    def test_the_reach_is_the_first_round_number_with_a_tenth_to_spare(self):
        self.assertEqual(gauges.elo_reach(190.85, 152.19, 0, 50), 500)    # 343 * 1.1 = 377
        self.assertEqual(gauges.elo_reach(4.9, 3.7, 0, 5), 10)            # 8.6 * 1.1 = 9.46
        self.assertEqual(gauges.elo_reach(0, 0, 0, 5), 10)                # 5.5 is past 5
        self.assertEqual(gauges.elo_reach(0, 0, 0, 4), 5)                 # 4.4 fits
        self.assertEqual(gauges.elo_reach(0, 0, -3, 1), 5)
        self.assertEqual(gauges.elo_reach(40, 10, 0, 50), 100)            # 55 is past 50
        self.assertEqual(gauges.elo_reach(5000, 1, 0, 0), 1000)

    def test_a_negative_estimate_and_the_bounds_widen_it_too(self):
        self.assertEqual(gauges.elo_reach(-13.8, 8.5, 0, 5), 50)
        self.assertEqual(gauges.elo_reach(1, 1, 0, 300), 500)

    def test_eleven_ticks_a_zero_line_and_a_needle_at_the_estimate(self):
        g = gauges.elo_gauge(190.85, 152.19, 0, 50)
        self.assertEqual(len(g["ticks"]), 11)
        self.assertEqual([t["text"] for t in g["ticks"]][::5], ["−500", "0", "+500"])
        self.assertEqual(g["reach"], 500)
        self.assertAlmostEqual(g["angle"], (190.85 / 500) * 90, places=1)
        self.assertIsNotNone(g["band"])
        self.assertEqual([b["text"] for b in g["bounds"]], ["H₀ 0", "H₁ 50"])

    def test_the_needle_clamps_at_the_ends(self):
        self.assertEqual(gauges.elo_gauge(5000, 1, 0, 0)["angle"], 90)
        self.assertEqual(gauges.elo_gauge(-5000, 1, 0, 0)["angle"], -90)

    def test_a_band_over_half_the_scale_never_takes_the_large_arc(self):
        # 0.0 Elo, -26.8 to +26.8 on a +-50 scale spans 0.536 of the half circle
        g = gauges.elo_gauge(0, 26.8, 0, 5)
        self.assertEqual(g["reach"], 50)
        arcs = re.findall(r"A[\d.]+ [\d.]+ 0 (\d) (\d)", g["band"])
        self.assertEqual(len(arcs), 2)
        self.assertEqual([large for large, _ in arcs], ["0", "0"])
        for path in (g["ring"], g["band"]):
            self.assertTrue(all(large == "0" for large, _ in re.findall(r"A[\d.]+ [\d.]+ 0 (\d) (\d)", path)))

    def test_no_gauge_without_an_estimate_and_no_band_without_an_error(self):
        self.assertIsNone(gauges.elo_gauge(None, 1, 0, 5))
        self.assertIsNone(gauges.elo_gauge(float("inf"), 1, 0, 5))
        g = gauges.elo_gauge(10, None, 0, 5)
        self.assertIsNone(g["band"])
        self.assertEqual(g["small"], "no interval yet")


class Formatting(SimpleTestCase):
    def test_a_hundred_and_over_is_whole_below_that_one_decimal(self):
        self.assertEqual(gauges.elo_text(190.85), "+191")
        self.assertEqual(gauges.elo_text(99.94), "+99.9")
        self.assertEqual(gauges.elo_text(99.96), "+100")
        self.assertEqual(gauges.elo_text(4.9), "+4.9")
        self.assertEqual(gauges.elo_text(343.0), "+343")

    def test_a_real_minus_sign(self):
        self.assertEqual(gauges.elo_text(-13.8), "−13.8")
        self.assertEqual(gauges.elo_text(-304.45), "−304")
        self.assertEqual(gauges.signed(-2.9444), "−2.94")
        self.assertEqual(gauges.bound_text(-3), "−3")
        self.assertEqual(gauges.bound_text(2.5), "2.5")
        self.assertNotIn("-", gauges.elo_gauge(-13.8, 8.5, -3, 1)["big"])

    def test_zero_has_no_sign(self):
        self.assertEqual(gauges.elo_text(0), "0.0")
        self.assertEqual(gauges.elo_text(-0.02), "0.0")

    def test_the_big_and_small_lines(self):
        g = gauges.elo_gauge(190.85, 152.19, 0, 50)
        self.assertEqual(g["big"], "+191 Elo")
        self.assertEqual(g["small"], "+38.7 to +343 · 95%")
        self.assertEqual(gauges.elo_gauge(-4.6, 3.0, -3, 1)["small"], "−7.6 to −1.6 · 95%")


class OnThePage(MatchTestCase):
    def put(self, state=None, **sprt):
        document = example_document()
        document["sprt"].update(sprt)
        if state:
            document["match"].update(state=state, finished_at="2026-10-16T13:00:00Z")
        self.assertIn(self.put_match(document).status_code, (200, 201))
        return self.client.get(f"/matches/{ID}").content.decode()

    def test_a_running_test_has_both_gauges(self):
        page = self.put(llr=1.1207)
        self.assertEqual(page.count('class="gneedle"'), 2)
        self.assertIn('aria-label="SPRT: Leaning pass, LLR 1.12 of 2.94 · 72 games', page)
        self.assertIn("fails at −2.94", page)
        self.assertIn("passes at 2.94", page)
        self.assertIn("base is stronger", page)
        self.assertIn("dev is stronger", page)
        self.assertIn('aria-label="Elo +88.7, 95% from +21.3 to +156, bounds 0 and 5"', page)
        self.assertIn('class="gauges"', page)
        self.assertNotIn("chance of pass", page.lower())

    def test_a_finished_test_says_passed_past_the_bound(self):
        page = self.put("finished", result="passed", llr=3.3151)
        self.assertIn('<div class="gbig tone-pass">Passed</div>', page)
        self.assertIn("LLR 3.32, past 2.94 · 72 games", page)

    def test_a_failed_test_is_red(self):
        page = self.put("finished", result="failed", llr=-3.05)
        self.assertIn('<div class="gbig tone-fail">Failed</div>', page)


class InTheList(MatchTestCase):
    def test_an_sprt_row_has_a_small_gauge_and_the_result_word(self):
        document = example_document()
        document["sprt"]["llr"] = 1.5
        self.assertIn(self.put_match(document).status_code, (200, 201))
        page = self.client.get("/tests").content.decode()
        self.assertIn('class="gauge-small"', page)
        self.assertIn('aria-label="SPRT Leaning pass, LLR 1.50"', page)
        self.assertIn("LLR 1.50", page)

    def test_a_gauntlet_row_has_none(self):
        self.put_match(gauntlet())
        self.assertNotIn('class="gauge-small"', self.client.get("/tests").content.decode())
