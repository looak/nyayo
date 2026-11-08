"""A tune's pages: the chart geometry, the create page's round trip, the tune's page new, running and finished, and the lists."""

import datetime
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from nyayo.lab import create_test as creating, demo, github, tunecharts, tunes, tuneview
from nyayo.lab.models import SPSARun, SPSASnapshot, Test

from .test_create_test import fake_github
from .test_tunes import INPUTS, made


class Charts(TestCase):
    def test_lately_words_are_by_the_share_of_the_range_moved(self):
        words = [tunecharts.lately_word(f)[0] for f in (0.0, 0.0099, 0.01, 0.024, 0.025, 0.3)]
        self.assertEqual(words, ["Settled", "Settled", "Slowing", "Slowing", "Moving", "Moving"])

    def test_the_step_now_shrinks_and_an_integers_stays_above_a_half(self):
        c_value = 4 * 5000 ** 0.101
        self.assertAlmostEqual(tunecharts.step_now(c_value, 0.101, False, 1), c_value)
        self.assertAlmostEqual(tunecharts.step_now(c_value, 0.101, False, 5000), 4.0)
        self.assertEqual(tunecharts.step_now(0.1, 0.101, False, 100), 0.5)
        self.assertLess(tunecharts.step_now(0.1, 0.101, True, 100), 0.5)

    def test_a_figure_shows_an_integer_whole_and_a_small_range_with_its_float(self):
        self.assertEqual(tunecharts.figure(74.6, False), "75")
        self.assertEqual(tunecharts.figure(-2.91, False, small_int=True), "\N{MINUS SIGN}3 (\N{MINUS SIGN}2.91)")
        self.assertEqual(tunecharts.figure(0.5, True), "0.5")

    def test_the_range_bar_puts_the_start_and_now_on_the_range(self):
        bar = tunecharts.range_bar(0, 200, 50, 150)
        self.assertEqual((bar["start"], bar["now"], bar["left"], bar["width"]), ("25.0", "75.0", "25.0", "50.0"))
        self.assertEqual(tunecharts.range_bar(0, 10, 5, 99)["now"], "100.0")  # out of range stays on the bar

    def test_the_elo_scale_holds_zero_and_every_whisker_on_round_gridlines(self):
        scale = tunecharts.EloScale([{"elo": 12.0, "error": 15.0}, {"elo": 3.0, "error": 14.0}])
        self.assertLessEqual(scale.low, -11)
        self.assertGreaterEqual(scale.high, 27)
        self.assertEqual(scale.low % scale.step, 0)
        self.assertTrue(any(g["zero"] for g in scale.grid(150)))
        # higher Elo is higher on the chart: a smaller y
        self.assertLess(float(scale.y(20, 150)), float(scale.y(0, 150)))
        # nothing at all is still a scale with a zero on it
        self.assertLess(tunecharts.EloScale([]).low, tunecharts.EloScale([]).high)

    def test_the_outcome_chart_is_a_line_dots_and_whiskers_for_the_finished_checks(self):
        checks = [{"iteration": 1000, "elo": 2.0, "error": 15.0}, {"iteration": 2000, "elo": 5.0, "error": 14.0}]
        chart = tunecharts.outcome_chart(checks, 2000, tunecharts.EloScale(checks))
        self.assertEqual(chart["line"].count(","), 2)
        self.assertEqual([d["x"] for d in chart["dots"]], ["600.0", "1200.0"])
        self.assertLess(float(chart["dots"][0]["y2"]), float(chart["dots"][0]["y"]))  # the upper whisker end is above the dot
        self.assertTrue(chart["area"].startswith("600.0,") and chart["area"].endswith(",%s" % chart["grid"][[g["zero"] for g in chart["grid"]].index(True)]["y"]))
        self.assertEqual(chart["now"], "+5.0 Elo ± 14.0 at 2,000")
        self.assertFalse(chart["positive"])
        self.assertIsNone(tunecharts.outcome_chart([], 100, tunecharts.EloScale([])))

    def test_a_parameter_chart_has_its_line_start_and_the_shared_shading(self):
        checks = [{"iteration": 100, "elo": 4.0, "error": 5.0}]
        scale = tunecharts.EloScale(checks)
        chart = tunecharts.parameter_chart([(0, 10), (50, 14), (100, 12)], 10, checks, 100, scale)
        points = [p.split(",") for p in chart["line"].split()]
        self.assertEqual([p[0] for p in points], ["0.0", "140.0", "280.0"])
        ys = [float(p[1]) for p in points]
        self.assertTrue(all(0 <= y <= 84 for y in ys) and ys[1] < ys[0])
        self.assertEqual(float(chart["start_y"]), ys[0])
        self.assertEqual(chart["zero_y"], scale.y(0, tunecharts.SMALL[1]))
        self.assertTrue(chart["area"])
        self.assertEqual(tunecharts.parameter_chart([(0, 1)], 1, [], 10, scale)["area"], "")

    def test_a_flat_parameter_still_draws_inside_its_box(self):
        chart = tunecharts.parameter_chart([(0, 3), (10, 3)], 3, [], 10, tunecharts.EloScale([]))
        self.assertTrue(all(0 <= float(p.split(",")[1]) <= 84 for p in chart["line"].split()))


class Staff(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(self.owner)


class Creating(Staff):
    def post(self, **changes):
        form = {"engine": "Elephant", "dev_branch": "feature-x", "preset": "STC", "spsa_inputs": INPUTS, "spsa_iterations": "8",
                "spsa_pairs_per": "1", "spsa_check_every": "4", "spsa_check_games": "4", "priority": "3", "name": "my tune"}
        form.update(changes)
        with mock.patch.object(github, "api_get", fake_github()), mock.patch("nyayo.lab.document.report_test"):
            return self.client.post(reverse("create_tune"), form)

    def test_the_form_is_there_with_its_parts(self):
        page = self.client.get(reverse("create_tune")).content.decode()
        for part in ('name="spsa_inputs"', 'name="spsa_iterations"', 'name="spsa_update_rule"', 'name="spsa_then_sprt"',
                     'name="spsa_check_every"', 'name="preset"', "How it learns", "Parameters", "create_tune.js"):
            self.assertIn(part, page)

    def test_a_tune_is_made_from_the_form_with_its_preset_and_base_the_same_as_dev(self):
        response = self.post()
        test = Test.objects.get()
        self.assertRedirects(response, "/matches/%s" % test.match_id(), fetch_redirect_response=False)
        self.assertEqual((test.test_mode, test.name, test.priority), ("SPSA", "my tune", 3))
        self.assertEqual((test.dev_time_control, test.dev_options, test.base_options), ("8.0+0.08", "Threads=1 Hash=8", "Threads=1 Hash=8"))
        self.assertEqual((test.base.sha, test.base.name), (test.dev.sha, test.dev.name))
        self.assertEqual(test.spsa_run.parameters.count(), 3)

    def test_frozen_ones_sent_as_the_script_sends_them(self):
        self.post(spsa_frozen=["Contempt", "Margin"], spsa_frozen_value_Contempt="5", spsa_frozen_value_Margin="0.25")
        run = SPSARun.objects.get()
        self.assertEqual([p.name for p in run.parameters.all()], ["Aspiration"])
        self.assertEqual(Test.objects.get().dev_options, "Threads=1 Hash=8 Contempt=5 Margin=0.25")

    def test_frozen_ones_sent_without_script_as_lines(self):
        self.post(spsa_frozen_text="Contempt = 5\n\nMargin=0.25")
        self.assertEqual(SPSARun.objects.get().frozen, [{"name": "Contempt", "value": 5}, {"name": "Margin", "value": 0.25}])

    def test_a_tune_that_can_be_tuned_is_not_made_all_frozen(self):
        response = self.post(spsa_frozen=["Aspiration", "Margin", "Contempt"], spsa_frozen_value_Aspiration="20",
                             spsa_frozen_value_Margin="0.5", spsa_frozen_value_Contempt="3")
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Nothing is left to tune", status_code=400)
        self.assertEqual(Test.objects.count(), 0)

    def test_errors_are_shown_like_the_other_pages_and_the_form_keeps_what_was_typed(self):
        response = self.post(spsa_inputs="Aspiration, int, 99, 5, 50, 4, 0.002", spsa_frozen=["Nope"], name="keep me")
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "This tune can't be created yet", status_code=400)
        self.assertContains(response, "out of its range", status_code=400)
        self.assertContains(response, "Nope is frozen but not in the list", status_code=400)
        self.assertContains(response, 'value="keep me"', status_code=400)
        self.assertContains(response, "Aspiration, int, 99, 5, 50, 4, 0.002", status_code=400)

    def test_the_then_box_and_the_rule_are_kept(self):
        self.post(spsa_then_sprt="1", spsa_update_rule="PLAIN")
        run = SPSARun.objects.get()
        self.assertEqual((run.then_sprt, run.update_rule), (True, "PLAIN"))

    def test_the_then_box_left_unticked_is_off(self):
        self.post()
        self.assertFalse(SPSARun.objects.get().then_sprt)


class TunePage(Staff):
    def page(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response

    def test_a_new_tune_says_what_it_has_not_got_yet(self):
        test = made(spsa_iterations="100", spsa_check_every="10")
        from nyayo.lab.document import build_document  # the page needs the match the report would make
        from nyayo.matches.ingest import ingest
        import json
        document = build_document(test)
        ingest(json.dumps(document).encode(), None, document["match"]["id"])
        response = self.page("/matches/%s" % test.match_id())
        self.assertTemplateUsed(response, "lab/tune_detail.html")
        self.assertContains(response, "iteration 0 of 100")
        self.assertContains(response, "The first outcome check comes at iteration 10")
        self.assertContains(response, "Nothing is queued when this tune ends")
        self.assertContains(response, "SPRT these values now")

    def seeded(self, **kwargs):
        return self.client.get(demo.seed_tune(**kwargs))

    def test_a_running_tune_has_its_progress_parameters_charts_and_code(self):
        response = self.seeded(name="t-run", iterations=2000, now_iteration=1100)
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn("iteration 1,100 of 2,000", page)
        self.assertIn("55.0%", page)
        self.assertEqual(page.count('class="tune-small"'), 1)
        self.assertEqual(page.count("<figure><figcaption><span class=\"mono\">"), 12)
        for part in ("LmrBase", "constexpr int LmrBase", "Outcome", "The checks (", "CSV", "JSON", "settled", "Step now", "Lately"):
            self.assertIn(part, page)
        # frozen values are in the header, not the code
        self.assertIn("2 frozen: SingularDepth 6, IirDepth 4", page)
        self.assertNotIn("constexpr int SingularDepth", page)
        self.assertEqual(response.context["settled"] + response.context["moving"], 12)
        self.assertTrue(response.context["refresh"])

    def test_the_page_is_the_tunes_numbers_not_the_pairs(self):
        response = self.seeded(name="t-numbers", iterations=2000, now_iteration=1100)
        run = SPSARun.objects.get(tune__name="t-numbers")
        row = response.context["rows"][0]
        self.assertEqual(row["name"], "LmrBase")
        parameter = run.parameters.get(name="LmrBase")
        self.assertEqual(row["now"], tunes_figure(parameter))
        self.assertEqual([c["iteration"] for c in response.context["checks"]], [1000])
        self.assertEqual(response.context["code"]["options"].split()[0].split("=")[0], "LmrBase")

    def test_a_finished_tune_links_the_sprt_that_followed(self):
        response = self.seeded(name="t-done", iterations=2000, now_iteration=2000, finished=True)
        follow = SPSARun.objects.get(tune__name="t-done").then_test
        self.assertEqual(follow.test_mode, "SPRT")
        page = response.content.decode()
        self.assertIn('href="/matches/%s"' % follow.match_id(), page)
        self.assertIn("queued", page)
        self.assertIn("finished", page)
        self.assertFalse(response.context["refresh"])

    def test_a_tune_that_asks_for_an_sprt_says_it_waits(self):
        test = made(spsa_iterations="100", spsa_then_sprt=True)
        context = tuneview.then_block(test.spsa_run)
        self.assertEqual((context["test"], context["waits"]), (None, True))

    def test_the_pace_and_the_end_come_from_the_newest_snapshots(self):
        test = made(spsa_iterations="1000", spsa_pairs_per="2")
        run = test.spsa_run
        run.snapshots.all().delete()
        now = timezone.now()
        for i in range(5):
            SPSASnapshot.objects.create(spsa_run=run, iteration=i * 10, games=i * 40, values={"Aspiration": 20.0})
            SPSASnapshot.objects.filter(iteration=i * 10).update(created=now - datetime.timedelta(minutes=(4 - i) * 6))
        test.games = 160
        per_hour, left = tuneview.pace(run, test, now)
        self.assertAlmostEqual(per_hour, 400.0)                 # 160 games in 24 minutes
        self.assertAlmostEqual(left, (4000 - 160) / 160 * 24 * 60)
        fresh = made()
        self.assertEqual(tuneview.pace(fresh.spsa_run, fresh, now), (None, None))

    def test_the_checks_table_says_where_each_stands(self):
        self.seeded(name="t-checks", iterations=2000, now_iteration=1100)
        run = SPSARun.objects.get(tune__name="t-checks")
        snapshot = run.snapshots.get(iteration=1000)
        Test.objects.filter(pk=snapshot.check_test_id).update(finished=False, games=120)
        SPSASnapshot.objects.filter(pk=snapshot.pk).update(check_elo=None, check_error=None, check_games=None)
        rows = {c["iteration"]: c for c in tuneview.checks_of(run)}
        self.assertEqual(rows[1000]["state"], "playing")
        self.assertEqual((rows[1000]["games"], rows[1000]["max_games"]), (120, 500))
        Test.objects.filter(pk=snapshot.check_test_id).update(games=0)
        self.assertEqual({c["iteration"]: c for c in tuneview.checks_of(run)}[1000]["state"], "waiting")

    def test_a_match_that_is_not_a_labs_tune_is_the_ordinary_page(self):
        import json
        from nyayo.lab.document import build_document
        from nyayo.matches.ingest import ingest
        from .lab_helpers import make_test
        sprt = make_test()
        document = build_document(sprt)
        ingest(json.dumps(document).encode(), None, document["match"]["id"])
        response = self.client.get("/matches/%s" % sprt.match_id())
        self.assertNotIn("lab/tune_detail.html", [t.name for t in response.templates])
        self.assertTemplateUsed(response, "matches/sprt_detail.html")


def tunes_figure(parameter):
    return tunecharts.figure(parameter.value, parameter.is_float, not parameter.is_float and parameter.max_value - parameter.min_value < tuneview.SMALL_RANGE)


class Lists(Staff):
    def test_checks_are_left_out_of_the_tests_list_unless_asked_for(self):
        demo.seed_tune(name="t-list", iterations=2000, now_iteration=1100)
        run = SPSARun.objects.get(tune__name="t-list")
        check = run.snapshots.exclude(check_test=None).first().check_test
        page = self.client.get(reverse("tests")).content.decode()
        self.assertIn("t-list", page)
        self.assertNotIn("/matches/%s" % check.match_id(), page)
        shown = self.client.get(reverse("tests") + "?checks=1").content.decode()
        self.assertIn("/matches/%s" % check.match_id(), shown)

    def test_a_tune_row_says_its_iteration_and_its_newest_check_not_an_elo_from_its_pairs(self):
        demo.seed_tune(name="t-row", iterations=2000, now_iteration=1100)
        response = self.client.get(reverse("tests"))
        page = response.content.decode()
        self.assertIn("iteration 1100/2000", page)
        figures = tunes.list_figures([Test.objects.get(name="t-row").match_id()])
        (only,) = figures.values()
        self.assertEqual((only["iteration"], only["iterations"], only["at"]), (1100, 2000, 1000))
        self.assertIsNotNone(only["elo"])

    def test_the_tune_the_checks_belong_to_is_found_by_the_check_ids(self):
        demo.seed_tune(name="t-ids", iterations=2000, now_iteration=1100)
        ids = tunes.check_match_ids()
        self.assertEqual(len(ids), 1)
        self.assertTrue(all(i.startswith("games-") for i in ids))
        self.assertEqual(tunes.list_figures(["sprt-1", "games-2", "tune-99999"]), {})
