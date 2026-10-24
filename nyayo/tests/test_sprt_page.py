"""The SPRT page: its own layout, and the block to paste."""

from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase

from nyayo.matches import sprtview
from nyayo.matches.models import Match

from .test_matches_ingest import ID, MatchTestCase, v1

EXAMPLE = """Elo   | 88.74 +- 67.44 (95%)
SPRT  | 8.0+0.00s Threads=1 Hash=8MB
LLR   | 0.31 (-2.94, 2.94) [0.00, 5.00]
Games | N: 72 W: 33 L: 15 D: 24
Penta | [1, 5, 11, 13, 6]"""


def example_document():
    document = v1(stats={"elo": 88.74, "error": 67.44, "model": "penta"})
    document["match"]["settings"]["tc"] = "8+0"
    document["subject"]["options"] = {"Hash": "8", "Threads": "1"}
    document["results"][0].update(games=72, wins=33, losses=15, draws=24, penta=[1, 5, 11, 13, 6])
    document["sprt"].update(llr=0.31, lower=-2.94, upper=2.94, elo0=0.0, elo1=5.0)
    return document


class Text(SimpleTestCase):
    def test_a_time_control(self):
        self.assertEqual(sprtview.tc_text("8+0.08"), "8.0+0.08s")
        self.assertEqual(sprtview.tc_text("10+0.1"), "10.0+0.10s")
        self.assertEqual(sprtview.tc_text("120+1"), "120.0+1.00s")
        self.assertEqual(sprtview.tc_text("40/60"), "40/60")
        self.assertEqual(sprtview.tc_text(None), "")

    def test_options(self):
        self.assertEqual(sprtview.options_text({"Hash": "8", "Threads": "1", "Contempt": "0"}),
                         "Threads=1 Hash=8MB Contempt=0")
        self.assertEqual(sprtview.options_text({"Hash": "big"}), "Hash=big")
        self.assertEqual(sprtview.options_text(None), "")

    def test_an_engine(self):
        self.assertEqual(sprtview.engine_line("Elephant", {"branch": "feature-x", "bench": 5}, "a1b2c3d4e5f6"),
                         "feature-x @ a1b2c3d4 · bench 5")
        self.assertEqual(sprtview.engine_line("Elephant", {}, ""), "Elephant")


class OnThePage(MatchTestCase):
    def page(self, document=None):
        self.assertIn(self.put_match(document or example_document()).status_code, (200, 201))
        return self.client.get(f"/matches/{ID}").content.decode()

    def test_the_block_to_paste_is_the_testers_layout(self):
        page = self.page()
        self.assertIn(f'readonly spellcheck="false">{EXAMPLE}</textarea>', page)
        self.assertIn('data-copy-target="copy-sprt"', page)

    def test_the_top_card_has_games_elo_and_llr(self):
        page = self.page()
        self.assertIn('<span class="score-value">72</span>', page)
        self.assertIn(">+88.74</span>", page)
        self.assertIn("± 67.44 Elo", page)
        self.assertIn("LLR (-2.94, 2.94)", page)

    def test_a_running_test_says_elapsed_pace_and_when_it_will_be_done(self):
        page = self.page()
        for label in ("Started", "Elapsed", "Last report", "Estimate"):
            self.assertIn(f"<dt>{label}</dt>", page)
        self.assertNotIn("<dt>Ended</dt>", page)

    def test_a_finished_test_says_ended_and_drops_the_estimate(self):
        document = example_document()
        document["match"].update(state="finished", finished_at="2026-10-16T13:00:00Z")
        document["sprt"]["result"] = "passed"
        page = self.page(document)
        self.assertIn("<dt>Ended</dt>", page)
        self.assertIn("<dt>Elapsed</dt>", page)
        self.assertIn("<dt>Pace</dt>", page)
        self.assertNotIn("<dt>Last report</dt>", page)
        self.assertNotIn("Games to go", page)
        self.assertIn('class="outcome pass card-state">passed', page)

    def test_a_stopped_test_says_stopped(self):
        document = example_document()
        document["match"].update(state="stopped", finished_at="2026-10-16T13:00:00Z")
        page = self.page(document)
        self.assertIn("<dt>Stopped</dt>", page)

    def test_the_test_settings_are_there(self):
        page = self.page()
        for text in ("<dt>Time control</dt><dd>8.0+0.00s</dd>", "<dt>UCI options</dt><dd>Threads=1 Hash=8MB</dd>",
                     "<dt>Elo bounds</dt><dd>[0.00, 5.00]</dd>", "0.05 / 0.05", "<dd>penta</dd>"):
            self.assertIn(text, page)
        self.assertIn("feature-x @ abc123", page)
        self.assertIn("main @ def456", page)

    def test_the_pentanomial_and_the_faults(self):
        page = self.page()
        self.assertIn("<th class=\"num\">LL</th>", page)
        self.assertIn('<td class="num">13</td>', page)
        self.assertIn("crashes", page)

    def test_a_gauntlet_keeps_its_own_page(self):
        from .test_matches_ingest import gauntlet
        self.put_match(gauntlet())
        page = self.client.get("/matches/gauntlet-test-1").content.decode()
        self.assertNotIn("copy-sprt", page)
        self.assertIn("Clipboard table", page)

    def test_without_stats_nyayo_calculates_and_says_so(self):
        document = example_document()
        document["stats"] = None
        page = self.page(document)
        self.assertIn("nyayo's figure", page)
        self.assertRegex(page, r"Elo   \| -?\d+\.\d\d \+- \d+\.\d\d \(95%\)")


class Bar(MatchTestCase):
    def bar(self, llr, lower=-2.94, upper=2.94):
        document = example_document()
        document["sprt"].update(llr=llr, lower=lower, upper=upper)
        self.assertIn(self.put_match(document).status_code, (200, 201))
        page = self.client.get(f"/matches/{ID}").content.decode()
        return page[page.index('class="bar sprt-track"'):page.index('class="marks muted sprt-marks"')]

    def test_zero_is_the_pillar_in_the_middle_of_symmetric_bounds(self):
        self.assertIn('class="mark zero" style="left: 50.000%"', self.bar(0.31))

    def test_a_positive_llr_fills_green_from_zero_towards_passing(self):
        bar = self.bar(1.47)  # halfway from 0 to the upper bound
        self.assertIn('class="fill pass" style="left: 50.000%; width: 25.000%"', bar)
        self.assertNotIn("fill fail", bar)

    def test_a_negative_llr_fills_red_from_the_llr_up_to_zero(self):
        bar = self.bar(-1.47)
        self.assertIn('class="fill fail" style="left: 25.000%; width: 25.000%"', bar)
        self.assertNotIn("fill pass", bar)

    def test_an_llr_of_zero_fills_nothing(self):
        bar = self.bar(0.0)
        self.assertNotIn('class="fill', bar)

    def test_zero_is_not_in_the_middle_of_uneven_bounds(self):
        # lower -1, upper 3: zero a quarter of the way along
        self.assertIn('class="mark zero" style="left: 25.000%"', self.bar(0.5, lower=-1.0, upper=3.0))


class Columns(MatchTestCase):
    def page(self, document=None):
        self.assertIn(self.put_match(document or example_document()).status_code, (200, 201))
        return self.client.get(f"/matches/{ID}").content.decode()

    def test_progress_comes_first_then_the_paste_block_beside_the_pairs(self):
        page = self.page()
        self.assertLess(page.index('<h2 id="progress">Progress</h2>'), page.index('class="match-grid sprt-columns"'))
        row = page[page.index('class="match-grid sprt-columns"'):]
        self.assertLess(row.index('id="copy-sprt"'), row.index("<h3>Score</h3>"))
        self.assertLess(row.index("<h3>Score</h3>"), row.index("<h3>Pairs</h3>"))

    def test_the_pentanomial_has_a_pillar_per_column_scaled_to_the_tallest(self):
        page = self.page()  # [1, 5, 11, 13, 6]: DW is the tallest
        for tone, height in (("ll", "7.7"), ("ld", "38.5"), ("dd", "84.6"), ("dw", "100.0"), ("ww", "46.2")):
            self.assertIn(f'<span class="pillar {tone}" style="height: {height}%">', page)
        self.assertIn('title="DW: 13 pairs, 36.1%"', page)

    def test_the_table_is_still_there(self):
        page = self.page()
        self.assertIn('<th class="num">DW</th>', page)
        self.assertIn('<td class="num muted">36.1%</td>', page)

    def test_no_pillars_before_a_pair_is_played(self):
        document = example_document()
        document["results"][0].update(games=0, wins=0, losses=0, draws=0, penta=[0, 0, 0, 0, 0])
        self.assertNotIn('class="pillars"', self.page(document))


class Workers(MatchTestCase):
    def page(self, machines):
        document = example_document()
        document["machines"] = machines
        self.assertIn(self.put_match(document).status_code, (200, 201))
        return self.client.get(f"/matches/{ID}").content.decode()

    def test_workers_are_their_own_list_after_the_results(self):
        page = self.page([{"name": "hunt", "games": 50}])
        self.assertLess(page.index('class="match-grid sprt-columns"'), page.index("<h2>Workers</h2>"))
        self.assertNotIn("<dt>Workers</dt>", page)

    def test_busiest_first_with_share_and_faults(self):
        page = self.page([{"name": "small", "games": 22, "crashes": 0, "timeloss": 0},
                          {"name": "big", "games": 50, "crashes": 2, "timeloss": 1, "user": "alex"}])
        self.assertLess(page.index("<strong>big</strong>"), page.index("<strong>small</strong>"))
        self.assertIn('<td class="num muted">69%</td>', page)  # 50 of 72
        self.assertIn('<td class="num worse">2</td>', page)

    def test_the_machines_details_show_when_they_were_reported(self):
        page = self.page([{"name": "hunt", "games": 72, "cpu": "AMD Ryzen 9 7950X", "physical_cores": 16,
                           "logical_cores": 32, "ram_mb": 65536, "os": "Linux 6.8.0", "concurrency": 14,
                           "cpu_flags": ["SSE41", "AVX2", "BMI2", "AVX512F"]}])
        self.assertIn("AMD Ryzen 9 7950X · 16 cores / 32 threads · 64 GB · Linux 6.8.0 · 14 at a time", page)
        self.assertIn('<span class="tier">AVX512F</span>', page)
        self.assertIn("all 4 CPU flags", page)

    def test_a_machine_with_only_a_name_and_games_still_lists(self):
        page = self.page([{"name": "hunt", "games": 72}])
        self.assertIn("<strong>hunt</strong>", page)
        self.assertNotIn('<tr class="spec-row">', page)

    def test_no_machines_no_section(self):
        self.assertNotIn("<h2>Workers</h2>", self.page([]))
