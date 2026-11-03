"""OpenBench's stats.py, copied unchanged: known answers, worked by hand, and properties that must hold."""

import math
from unittest import TestCase

from nyayo.lab import stats


class Elo(TestCase):
    def test_an_even_trinomial_is_zero_with_a_symmetric_interval(self):
        low, elo, high = stats.Elo((100, 100, 100))
        self.assertAlmostEqual(elo, 0.0)
        self.assertAlmostEqual(low, -high)

    def test_a_score_of_seven_twelfths(self):
        # 300 losses, 400 draws, 500 wins: mu = (400 / 2 + 500) / 1200 = 7 / 12, and -400 * log10(12 / 7 - 1) = 58.45
        low, elo, high = stats.Elo((300, 400, 500))
        self.assertAlmostEqual(elo, 58.4512, places=3)
        self.assertLess(low, elo)
        self.assertLess(elo, high)

    def test_a_pentanomial_mean(self):
        # mu = (0.25 * 20 + 0.5 * 40 + 0.75 * 30 + 10) / 110 = 0.5455
        _, elo, _ = stats.Elo((10, 20, 40, 30, 10))
        self.assertAlmostEqual(elo, 15.8034, places=3)

    def test_more_of_the_same_games_narrow_the_interval_and_keep_the_mean(self):
        low1, elo1, high1 = stats.Elo((10, 20, 40, 30, 10))
        low2, elo2, high2 = stats.Elo((20, 40, 80, 60, 20))
        self.assertAlmostEqual(elo1, elo2)
        self.assertLess(high2 - low2, high1 - low1)

    def test_no_games_or_one_game_is_nothing(self):
        self.assertEqual(stats.Elo((0, 0, 0)), (0.0, 0.0, 0.0))
        self.assertEqual(stats.Elo((0, 0, 1)), (0.0, 0.0, 0.0))

    def test_the_logistic_curve(self):
        self.assertAlmostEqual(stats.logistic_elo(0.5), 0.0)
        # -400 * log10(1 / 0.64 - 1) = -400 * log10(0.5625)
        self.assertAlmostEqual(stats.logistic_elo(0.64), 99.9510, places=3)


class Bayeselo(TestCase):
    def test_the_probabilities_of_an_even_match_are_symmetric_and_sum_to_one(self):
        loss, draw, win = stats.bayeselo_to_proba(0, 200)
        self.assertAlmostEqual(loss, win)
        self.assertAlmostEqual(loss + draw + win, 1.0)

    def test_it_round_trips(self):
        elo, draw_elo = stats.proba_to_bayeselo(*stats.bayeselo_to_proba(30, 190))
        self.assertAlmostEqual(elo, 30)
        self.assertAlmostEqual(draw_elo, 190)

    def test_a_quarter_a_half_a_quarter_is_even(self):
        elo, draw_elo = stats.proba_to_bayeselo(0.25, 0.5, 0.25)
        self.assertAlmostEqual(elo, 0.0)
        self.assertAlmostEqual(draw_elo, 190.8485, places=3)


class TrinomialSPRT(TestCase):
    def test_a_missing_outcome_gives_no_ratio(self):
        self.assertEqual(stats.TrinomialSPRT([0, 10, 10], 0, 5), 0.0)

    def test_a_win_leaning_result_favours_the_upper_hypothesis(self):
        self.assertAlmostEqual(stats.TrinomialSPRT([300, 400, 500], 0, 5), 3.7226, places=3)

    def test_an_even_result_slightly_favours_the_lower(self):
        self.assertLess(stats.TrinomialSPRT([100, 100, 100], 0, 5), 0)

    def test_mirroring_the_games_and_the_bounds_mirrors_the_hypotheses(self):
        self.assertAlmostEqual(stats.TrinomialSPRT([300, 400, 500], 0, 5),
                               -stats.TrinomialSPRT([500, 400, 300], -5, 0))


class PentanomialSPRT(TestCase):
    def test_known_ratios(self):
        self.assertAlmostEqual(stats.PentanomialSPRT([10, 20, 40, 30, 10], 0, 5), 0.16349, places=4)
        self.assertAlmostEqual(stats.PentanomialSPRT([50, 100, 200, 100, 50], 0, 5), -0.10355, places=4)

    def test_twice_the_games_make_twice_the_ratio(self):
        once = stats.PentanomialSPRT([10, 20, 40, 30, 10], 0, 5)
        self.assertAlmostEqual(stats.PentanomialSPRT([20, 40, 80, 60, 20], 0, 5), 2 * once)

    def test_no_games_is_a_ratio_of_nothing(self):
        self.assertAlmostEqual(stats.PentanomialSPRT([0, 0, 0, 0, 0], 0, 5), 0.0, places=4)

    def test_the_results_decide_the_sign(self):
        self.assertGreater(stats.PentanomialSPRT([10, 20, 40, 30, 10], 0, 5), 0)
        self.assertLess(stats.PentanomialSPRT([30, 20, 40, 20, 10], 0, 5), 0)
        self.assertTrue(math.isfinite(stats.PentanomialSPRT([0, 0, 100, 0, 0], 0, 5)))
