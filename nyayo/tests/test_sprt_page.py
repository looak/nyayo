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
