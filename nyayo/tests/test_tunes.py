"""Tunes: the momentum update, snapshots, outcome checks, frozen values, the result, the SPRT after, creating one and
playing one to its end through the worker API."""

import json
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from nyayo.lab import create_test as creating, github, spsa_utils, tunes
from nyayo.lab.models import SPSARun, SPSASnapshot, Test

from .lab_helpers import make_machine, make_result, make_test
from .test_create_test import fake_github
from .test_lab_worker_api import Api

INPUTS = "\n".join([
    "Aspiration, int, 20, 5, 50, 4, 0.002",
    "Margin, float, 0.5, 0, 1, 0.05, 0.002",
    "Contempt, int, 3, 0, 10, 1, 0.002",
])


def form(**changes):
    values = {"engine": "Elephant", "dev_branch": "feature-x", "base_branch": "main", "preset": "STC",
              "spsa_inputs": INPUTS, "spsa_iterations": "8", "spsa_pairs_per": "1", "spsa_check_every": "4",
              "spsa_check_games": "4"}
    values.update(changes)
    return values


def tune_values(**changes):
    values = creating.default_values(creating.EngineConfig.objects.get(name="Elephant"))
    values.update(form(**changes))
    return values


class Request:
    def __init__(self, **post):
        self.POST = {"spsa_alpha": "0.602", "spsa_gamma": "0.101", "spsa_A_ratio": "0.1", "spsa_iterations": "8",
                     "spsa_reporting_type": "BULK", "spsa_distribution_type": "SINGLE", "spsa_pairs_per": "1",
                     "spsa_inputs": "Aspiration, int, 20, 5, 50, 4, 0.002", **post}


def made(**post):
    test = make_test(test_mode="SPSA")
    spsa_utils.create_spsa_run(test, Request(**post))
    return Test.objects.get(pk=test.pk)


def create(user, **changes):
    with mock.patch.object(github, "api_get", fake_github()):
        return creating.create_tune(tune_values(**changes), user)


class Update(TestCase):
    def values(self, test):
        return [p.value for p in test.spsa_run.parameters.order_by("index")]

    def report(self, test, delta, games=2):
        test.games += games
        tunes.advance(test, [delta], test.games - games)

    def test_plain_adds_the_delta_as_openbench_did(self):
        test = made(spsa_update_rule="PLAIN")
        self.report(test, 1.0)
        self.report(test, 1.0)
        self.assertEqual(self.values(test), [22.0])

    def test_momentum_keeps_a_share_of_the_step_before(self):
        test = made(spsa_update_rule="MOMENTUM")
        self.report(test, 1.0)
        self.assertAlmostEqual(self.values(test)[0], 20.1)
        self.report(test, 1.0)
        self.assertAlmostEqual(self.values(test)[0], 20.1 + 0.19)
        self.assertAlmostEqual(test.spsa_run.parameters.get().velocity, 0.19)

    def test_the_steady_step_is_the_size_of_plain_spsas(self):
        test = made()
        for _ in range(200):
            test.games += 2
            tunes.advance(test, [0.01], test.games - 2)
            test.finished = False
        self.assertAlmostEqual(test.spsa_run.parameters.get().velocity, 0.01, places=6)

    def test_momentum_is_the_default_and_values_stay_in_range(self):
        test = made()
        self.assertEqual((test.spsa_run.update_rule, test.spsa_run.momentum), ("MOMENTUM", 0.9))
        self.report(test, 1000.0)
        self.assertEqual(self.values(test), [50.0])
        self.report(test, -1000.0)
        self.assertGreaterEqual(self.values(test)[0], 5)

    def test_noise_cancels_under_momentum(self):
        plain, smooth = made(spsa_update_rule="PLAIN", spsa_iterations="100"), made(spsa_iterations="100")
        for test in (plain, smooth):
            for delta in [1.0, -1.0] * 5:
                self.report(test, delta)
        self.assertEqual(self.values(plain), [20.0])
        self.assertLess(abs(self.values(smooth)[0] - 20.0), 0.5)


class Snapshots(TestCase):
    def iterations(self, test):
        return list(test.spsa_run.snapshots.values_list("iteration", flat=True))

    def test_one_is_written_at_creation_with_the_start_values(self):
        test = made()
        snapshot = test.spsa_run.snapshots.get()
        self.assertEqual((snapshot.iteration, snapshot.games, snapshot.values), (0, 0, {"Aspiration": 20.0}))

    def test_defaults_are_a_snapshot_a_hundredth_and_a_check_a_twentieth(self):
        test = made(spsa_iterations="5000", spsa_snapshot_every="", spsa_check_every="")
        self.assertEqual((test.spsa_run.snapshot_every, test.spsa_run.check_every), (50, 250))
        self.assertEqual((tunes.default_snapshot_every(5000), tunes.default_snapshot_every(50)), (50, 1))

    def test_one_is_written_each_time_an_iteration_multiple_is_crossed(self):
        test = made(spsa_iterations="10", spsa_snapshot_every="3", spsa_check_every="0")
        for _ in range(7):
            test.games += 2 # one iteration (pairs_per 1)
            tunes.advance(test, [0.0], test.games - 2)
        self.assertEqual(self.iterations(test), [0, 3, 6])

    def test_a_report_that_crosses_several_makes_one_at_the_iteration_it_reached(self):
        test = made(spsa_iterations="100", spsa_snapshot_every="3", spsa_check_every="0")
        test.games += 20
        tunes.advance(test, [1.0], 0)
        self.assertEqual(self.iterations(test), [0, 10])

    def test_the_end_writes_one_and_not_a_second_for_the_same_iteration(self):
        test = made(spsa_iterations="4", spsa_snapshot_every="2", spsa_check_every="0")
        for _ in range(4):
            test.games += 2
            tunes.advance(test, [0.0], test.games - 2)
        self.assertTrue(test.finished)
        self.assertEqual(self.iterations(test), [0, 2, 4])

    def test_a_check_is_due_on_its_multiple_and_is_a_games_test(self):
        test = made(spsa_iterations="8", spsa_snapshot_every="4", spsa_check_every="4", spsa_check_games="40")
        for _ in range(4):
            test.games += 2
            tunes.advance(test, [1.0], test.games - 2)
        snapshot = test.spsa_run.snapshots.get(iteration=4)
        check = snapshot.check_test
        self.assertEqual((check.test_mode, check.max_games, check.priority, check.approved), ("GAMES", 40, test.priority + 1, True))
        self.assertEqual((check.dev_id, check.base_id, check.dev_time_control, check.book_name),
                         (test.dev_id, test.base_id, test.dev_time_control, test.book_name))
        self.assertIsNone(test.spsa_run.snapshots.get(iteration=0).check_test)
        self.assertEqual(check.name, "Tune %d check @4" % test.id)

    def test_no_checks_when_check_every_is_zero(self):
        test = made(spsa_iterations="8", spsa_check_every="0")
        for _ in range(8):
            test.games += 2
            tunes.advance(test, [1.0], test.games - 2)
        self.assertEqual(Test.objects.filter(test_mode="GAMES").count(), 0)


class Result(TestCase):
    def tune(self):
        test = made(spsa_iterations="100", spsa_inputs="Aspiration, int, 20, 0, 100, 4, 0.002\nMargin, float, 0.5, 0, 1, 0.05, 0.002")
        run = test.spsa_run
        run.snapshots.all().delete()
        return test, run

    def put(self, run, iteration, a, m=0.5):
        SPSASnapshot.objects.create(spsa_run=run, iteration=iteration, games=2 * iteration, values={"Aspiration": a, "Margin": m})

    def test_the_average_is_over_the_last_fifth_and_the_current_value(self):
        test, run = self.tune()
        for iteration, value in [(10, 30), (50, 40), (80, 50), (90, 60), (100, 70)]:
            self.put(run, iteration, value, 0.5)
        run.parameters.filter(name="Aspiration").update(value=80)
        # iterations 80, 90 and 100 are in the last 20% of the 100 so far; with the current value, 50, 60, 70, 80
        self.assertEqual(tunes.averaged_values(run)["Aspiration"], 65.0)
        self.assertEqual(tunes.averaged_values(run)["Margin"], 0.5)

    def test_with_no_snapshots_it_is_the_current_values(self):
        test, run = self.tune()
        self.assertEqual(tunes.averaged_values(run), {"Aspiration": 20.0, "Margin": 0.5})

    def test_lately_is_the_share_of_the_range_moved_in_the_last_tenth_of_the_snapshots(self):
        test, run = self.tune()
        for i in range(21):
            self.put(run, i, 20 + i, 0.5)
        run.parameters.filter(name="Aspiration").update(value=40)
        late = tunes.lately(run)
        # 21 snapshots: the last 10% is 3 of them, so it is read against the snapshot at index 17, value 37
        self.assertAlmostEqual(late["Aspiration"], 3 / 100)
        self.assertEqual(late["Margin"], 0)

    def test_the_history_has_a_row_a_snapshot_with_the_check_columns(self):
        test, run = self.tune()
        self.put(run, 0, 20)
        self.put(run, 5, 25)
        SPSASnapshot.objects.filter(iteration=5).update(check_elo=3.5, check_error=4.0, check_games=500)
        rows = tunes.history(run)
        self.assertEqual([r["iteration"] for r in rows], [0, 5])
        self.assertEqual((rows[1]["check_elo"], rows[1]["check_error"], rows[1]["check_games"], rows[0]["check_elo"]), (3.5, 4.0, 500, None))
        lines = tunes.history_csv(run).splitlines()
        self.assertEqual(lines[0], "iteration,games,created,Aspiration,Margin,check,check_elo,check_error,check_games")
        self.assertTrue(lines[2].startswith("5,10,") and lines[2].endswith(",25,0.5,,3.5,4.0,500"))

    def test_an_sprt_of_given_values_against_the_start(self):
        test, run = self.tune()
        sprt = tunes.queue_sprt(test, values={"Aspiration": 33.4, "Margin": 0.25})
        self.assertEqual((sprt.test_mode, sprt.dev_id, sprt.base_id, sprt.approved), ("SPRT", test.dev_id, test.base_id, True))
        self.assertEqual(sprt.dev_options, "Threads=1 Hash=8 Aspiration=33 Margin=0.25")
        self.assertEqual(sprt.base_options, "Threads=1 Hash=8 Aspiration=20 Margin=0.5")
        self.assertEqual((sprt.dev_time_control, sprt.base_time_control), (test.dev_time_control, test.base_time_control))
        self.assertEqual((sprt.elolower, sprt.eloupper, sprt.alpha, sprt.beta), (0.0, 5.0, 0.05, 0.05))
        self.assertGreater(sprt.upperllr, 0)

    def test_the_sprt_is_of_the_averaged_values_by_default(self):
        test, run = self.tune()
        self.put(run, 100, 60)
        run.parameters.filter(name="Aspiration").update(value=40)
        self.assertEqual(tunes.queue_sprt(test).dev_options, "Threads=1 Hash=8 Aspiration=50 Margin=0.5")


class Creating(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", password="pw", is_staff=True, is_superuser=True)

    def test_a_tune_is_made_with_its_parameters_and_the_defaults(self):
        test, errors, _ = create(self.owner, spsa_iterations="200", spsa_check_every="")
        self.assertEqual(errors, {})
        run = test.spsa_run
        self.assertEqual((test.test_mode, test.approved), ("SPSA", True))
        self.assertEqual([p.name for p in run.parameters.order_by("index")], ["Aspiration", "Margin", "Contempt"])
        self.assertEqual((run.reporting_type, run.distribution_type, run.update_rule, run.momentum), ("BULK", "SINGLE", "MOMENTUM", 0.9))
        self.assertEqual((run.snapshot_every, run.check_every, run.check_games, run.then_sprt, run.frozen), (2, 10, 4, False, []))
        self.assertEqual(run.snapshots.get().iteration, 0)

    def test_the_form_fields_are_kept(self):
        test, errors, _ = create(self.owner, spsa_update_rule="PLAIN", spsa_check_every="0", spsa_check_games="250", spsa_then_sprt="on")
        run = test.spsa_run
        self.assertEqual((run.update_rule, run.check_every, run.check_games, run.then_sprt), ("PLAIN", 0, 250, True))

    def test_frozen_parameters_are_not_tuned_and_ride_on_both_sides(self):
        test, errors, _ = create(self.owner, spsa_frozen=["Contempt", "Margin"], spsa_frozen_value_Contempt="5", spsa_frozen_value_Margin="0.25")
        self.assertEqual(errors, {})
        run = test.spsa_run
        self.assertEqual([p.name for p in run.parameters.all()], ["Aspiration"])
        self.assertEqual(run.frozen, [{"name": "Contempt", "value": 5}, {"name": "Margin", "value": 0.25}])
        self.assertEqual(test.dev_options, "Threads=1 Hash=8 Contempt=5 Margin=0.25")
        self.assertEqual(test.base_options, "Threads=1 Hash=8 Contempt=5 Margin=0.25")

    def test_errors_are_by_field(self):
        cases = [
            (dict(spsa_inputs="Aspiration, int, 20, 5, 50"), "spsa_inputs", "seven fields"),
            (dict(spsa_inputs="Aspiration, int, 99, 5, 50, 4, 0.002"), "spsa_inputs", "out of its range"),
            (dict(spsa_inputs="Aspiration, long, 20, 5, 50, 4, 0.002"), "spsa_inputs", "int or float"),
            (dict(spsa_inputs="Aspiration, int, x, 5, 50, 4, 0.002"), "spsa_inputs", "not a number"),
            (dict(spsa_inputs="A, int, 1, 0, 2, 1, 1\nA, int, 1, 0, 2, 1, 1"), "spsa_inputs", "twice"),
            (dict(spsa_inputs=""), "spsa_inputs", "Nothing is left"),
            (dict(spsa_frozen=["Nope"]), "spsa_frozen", "not in the list"),
            (dict(spsa_frozen=["Contempt"], spsa_frozen_value_Contempt="x"), "spsa_frozen", "not a number"),
            (dict(spsa_inputs="A, int, 1, 0, 2, 1, 1", spsa_frozen=["A"], spsa_frozen_value_A="1"), "spsa_inputs", "Nothing is left"),
            (dict(spsa_iterations="0"), "spsa_iterations", "Iterations"),
            (dict(spsa_update_rule="NEWTON"), "spsa_update_rule", "one of"),
            (dict(spsa_check_every="9"), "spsa_check_every", "never happens"),
            (dict(spsa_check_games="0"), "spsa_check_games", "Check games"),
        ]
        for changes, field, message in cases:
            with self.subTest(changes=changes):
                test, errors, _ = create(self.owner, **changes)
                self.assertIsNone(test)
                self.assertIn(message, errors.get(field, ""))
        self.assertEqual(Test.objects.count(), 0)

    def test_the_tests_own_errors_are_still_found(self):
        test, errors, _ = create(self.owner, dev_tc="nope")
        self.assertIsNone(test)
        self.assertIn("dev_tc", errors)

    def test_the_create_page_makes_a_tune_when_the_mode_is_spsa(self):
        self.client.force_login(self.owner)
        values = {k: v for k, v in tune_values(test_mode="SPSA", spsa_frozen=["Contempt"], spsa_frozen_value_Contempt="5").items() if k != "spsa_frozen"}
        values["spsa_frozen"] = ["Contempt"]
        with mock.patch.object(github, "api_get", fake_github()), mock.patch("nyayo.lab.document.report_test"):
            response = self.client.post(reverse("create_test"), values)
        test = Test.objects.get()
        self.assertRedirects(response, "/matches/%s" % test.match_id(), fetch_redirect_response=False)
        self.assertEqual((test.test_mode, test.spsa_run.frozen), ("SPSA", [{"name": "Contempt", "value": 5}]))


class Endpoints(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", password="pw", is_staff=True, is_superuser=True)
        self.test = made(spsa_iterations="100")
        self.test.spsa_run.parameters.update(value=30)

    def test_queuing_an_sprt_is_gated_like_stop_and_restart(self):
        url = reverse("tune_sprt", args=[self.test.id])
        self.assertEqual(self.client.post(url).status_code, 302)
        self.assertEqual(Test.objects.filter(test_mode="SPRT").count(), 0)
        User.objects.create_user("plain", password="pw")
        self.client.login(username="plain", password="pw")
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code, 405)
        with mock.patch("nyayo.lab.document.report_test"):
            response = self.client.post(url)
        sprt = Test.objects.get(test_mode="SPRT")
        self.assertRedirects(response, "/matches/%s" % sprt.match_id(), fetch_redirect_response=False)
        self.assertEqual((sprt.author, sprt.dev_options), (self.owner, "Threads=1 Hash=8 Aspiration=30"))

    def test_only_a_tune_has_one(self):
        self.client.force_login(self.owner)
        other = make_test()
        self.assertEqual(self.client.post(reverse("tune_sprt", args=[other.id])).status_code, 404)
        self.assertEqual(self.client.get(reverse("tune_history_json", args=[other.id])).status_code, 404)

    def test_the_history_is_open_json_and_csv(self):
        data = self.client.get(reverse("tune_history_json", args=[self.test.id])).json()
        self.assertEqual(data["tune"], self.test.match_id())
        self.assertEqual(data["history"][0]["values"], {"Aspiration": 20.0})
        response = self.client.get(reverse("tune_history_csv", args=[self.test.id]))
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertTrue(response.content.decode().startswith("iteration,games,created,Aspiration,check,"))


class PlayedToItsEnd(Api):
    def submit(self, test, result, games, penta, trinomial, **extra):
        return self.worker("clientSubmitResults", test_id=test.id, result_id=result.id, crashes=0, timelosses=0, illegals=0,
                           trinomial=trinomial, pentanomial=penta, **extra).json()

    def test_a_tune_with_checks_frozen_values_and_an_sprt_after(self):
        owner = User.objects.create_user("owner", password="pw", is_staff=True, is_superuser=True)
        test, errors, _ = create(owner, spsa_frozen=["Contempt"], spsa_frozen_value_Contempt="5", spsa_then_sprt="1")
        self.assertEqual(errors, {})
        SPSARun.objects.filter(tune=test).update(snapshot_every=2) # check_every 4 of 8, set by the form
        workload = self.handed_out(test)
        self.assertEqual(workload["test"]["id"], test.id)
        self.assertEqual(sorted(workload["spsa"]), ["Aspiration", "Margin"])
        machine = make_machine("second")
        result = make_result(test, machine)

        for step in range(8):
            reply = self.submit(test, result, 2, "0 0 1 0 0", "0 2 0", spsa_delta=json.dumps([1.0, 0.0]))
            self.assertEqual(reply, {"stop": True} if step == 7 else {})
            if step in (3, 7):
                check = SPSARun.objects.get(tune=test).snapshots.get(iteration=step + 1).check_test
                self.assertEqual((check.test_mode, check.max_games), ("GAMES", 4))
                # the check plays out: three wins and a draw
                self.submit(check, make_result(check, machine), 4, "0 0 0 1 1", "0 1 3")
                self.assertTrue(Test.objects.get(pk=check.pk).finished)

        test = Test.objects.get(pk=test.pk)
        run = test.spsa_run
        self.assertTrue(test.finished)

        # snapshots at the creation, every 2 iterations and the end
        snapshots = list(run.snapshots.all())
        self.assertEqual([s.iteration for s in snapshots], [0, 2, 4, 6, 8])
        self.assertEqual([s.games for s in snapshots], [0, 4, 8, 12, 16])
        self.assertEqual(snapshots[0].values, {"Aspiration": 20.0, "Margin": 0.5})
        self.assertAlmostEqual(snapshots[1].values["Aspiration"], 20.29)
        self.assertEqual(snapshots[1].values["Margin"], 0.5)
        self.assertLess(snapshots[2].values["Aspiration"], snapshots[4].values["Aspiration"])

        # a check at 4 and 8, with the Elo the games gave
        self.assertEqual([s.check_test_id is not None for s in snapshots], [False, False, True, False, True])
        for snapshot in (snapshots[2], snapshots[4]):
            self.assertGreater(snapshot.check_elo, 0)
            self.assertGreater(snapshot.check_error, 0)
            self.assertEqual(snapshot.check_games, 4)
        first = snapshots[2].check_test
        self.assertEqual(first.name, "%s check @4" % (test.name or "Tune %d" % test.id))
        self.assertEqual(first.dev_options, "Threads=1 Hash=8 Contempt=5 Aspiration=%d Margin=0.5" % round(snapshots[2].values["Aspiration"]))
        self.assertEqual(first.base_options, "Threads=1 Hash=8 Contempt=5 Aspiration=20 Margin=0.5")
        self.assertEqual((first.test_mode, first.priority, test.priority), ("GAMES", 1, 0))

        # the tune's own sides carry the frozen value, and the SPRT queued at the end the same
        self.assertEqual((test.dev_options, test.base_options), ("Threads=1 Hash=8 Contempt=5", "Threads=1 Hash=8 Contempt=5"))
        sprt = run.then_test
        self.assertEqual((sprt.test_mode, sprt.approved, sprt.finished), ("SPRT", True, False))
        self.assertTrue(sprt.dev_options.startswith("Threads=1 Hash=8 Contempt=5 Aspiration="))
        self.assertEqual(sprt.base_options, "Threads=1 Hash=8 Contempt=5 Aspiration=20 Margin=0.5")

        # both rules move values: the tune moved, and a plain one is tried in Update
        self.assertGreater(run.parameters.get(name="Aspiration").value, 20)
        self.assertEqual(Test.objects.filter(test_mode="SPRT").count(), 1)
