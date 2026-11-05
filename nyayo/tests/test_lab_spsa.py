"""OpenBench's spsa_utils.py (one import line pointed at nyayo.lab.models), against a tune made the way a form makes it."""

import numpy as np
from django.contrib.auth.models import User
from django.test import TestCase

from nyayo.lab import spsa_utils
from nyayo.lab.models import Engine, SPSAParameter, Test

INPUTS = "\n".join([
    "Aspiration, int, 20, 5, 50, 4, 0.002",
    "Margin, float, 0.5, 0, 1, 0.05, 0.002",
])


class Request:
    def __init__(self, **post):
        self.POST = {"spsa_alpha": "0.602", "spsa_gamma": "0.101", "spsa_A_ratio": "0.1", "spsa_iterations": "5000",
                     "spsa_reporting_type": "BATCHED", "spsa_distribution_type": "MULTIPLE", "spsa_pairs_per": "4",
                     "spsa_inputs": INPUTS, **post}


class Spsa(TestCase):
    def tune(self, **post):
        user = User.objects.create_user("owner")
        engine = Engine.objects.create(name="Elephant", source="https://github.com/looak/elephant", sha="a" * 40)
        test = Test.objects.create(author=user, dev=engine, base=engine, book_name="8moves_v3.epd", test_mode="SPSA",
                                   dev_repo="r", base_repo="r", dev_engine="Elephant", base_engine="Elephant",
                                   dev_options="Threads=1", base_options="Threads=1", dev_time_control="8.0+0.08",
                                   base_time_control="8.0+0.08")
        spsa_utils.create_spsa_run(test, Request(**post))
        return Test.objects.get(pk=test.pk)

    def test_the_parameters_are_made_with_their_constants(self):
        tune = self.tune()
        first, second = tune.spsa_run.parameters.order_by("index")
        self.assertEqual((first.name, first.is_float, first.value, first.start), ("Aspiration", False, 20, 20))
        self.assertEqual((second.name, second.is_float), ("Margin", True))
        # c_value = c_end * iterations ** gamma, a_value = r_end * c_end ** 2 * (A_ratio * iterations + iterations) ** alpha
        self.assertAlmostEqual(first.c_value, 4 * 5000 ** 0.101)
        self.assertAlmostEqual(first.a_value, 0.002 * 16 * (0.1 * 5000 + 5000) ** 0.602)
        self.assertEqual(SPSAParameter.objects.count(), 2)

    def test_the_input_and_the_optimal_values_read_back(self):
        tune = self.tune()
        self.assertEqual(spsa_utils.spsa_original_input(tune),
                         "Aspiration, int, 20.0, 5.0, 50.0, 4.0, 0.002\nMargin, float, 0.5, 0.0, 1.0, 0.05, 0.002")
        self.assertEqual(spsa_utils.spsa_optimal_values(tune), "Aspiration, 20\nMargin, 0.5")

    def test_a_workload_is_assigned_from_the_start(self):
        tune = self.tune()
        np.random.seed(1)
        assignment = spsa_utils.spsa_workload_assignment_dict(tune, 3)
        aspiration = assignment["Aspiration"]
        # no games yet: iteration 1, so c is the whole c_value, and one pair of values per runner
        self.assertAlmostEqual(aspiration["c"], 4 * 5000 ** 0.101)
        self.assertEqual((aspiration["index"], len(aspiration["dev"]), len(aspiration["base"])), (0, 3, 3))
        for dev, base, flip in zip(aspiration["dev"], aspiration["base"], aspiration["flip"]):
            self.assertIn(flip, (-1, 1))
            self.assertIsInstance(dev, int)
            self.assertTrue(5 <= dev <= 50 and 5 <= base <= 50)
            # the two sides sit either side of the value, at most two c apart once rounded
            self.assertLessEqual(abs(dev - base), 2 * aspiration["c"] + 2)
        self.assertTrue(all(isinstance(x, float) for x in assignment["Margin"]["dev"]))

    def test_a_single_distribution_repeats_one_draw_for_every_runner(self):
        tune = self.tune(spsa_distribution_type="SINGLE")
        assignment = spsa_utils.spsa_workload_assignment_dict(tune, 4)
        for name in ("Aspiration", "Margin"):
            self.assertEqual(len(set(assignment[name]["dev"])), 1)
            self.assertEqual(len(assignment[name]["dev"]), 4)

    def test_only_a_tune_is_assigned(self):
        tune = self.tune()
        tune.test_mode = "SPRT"
        self.assertIsNone(spsa_utils.spsa_workload_assignment_dict(tune, 2))

    def test_the_digest_starts_with_its_header_and_has_a_line_per_parameter(self):
        lines = spsa_utils.spsa_param_digest(self.tune()).split("\n")
        self.assertEqual(lines[0], "Name,Curr,Start,Min,Max,C,C_end,R,R_end")
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[1].startswith("Aspiration,20.0000,20,5,50,"))
