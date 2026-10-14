"""Elephant's mock smoke runs (logs/results-mock/SCENARIOS.md in the Elephant repo): seven runs on real commits with
positions flipped, added, removed and broken on purpose. The expected numbers are the reference answers given there
by Elephant's own client, except where nyayo's baseline rule differs (see M2)."""

import gzip
import json
from pathlib import Path

from nyayo.runs import compare
from nyayo.runs.models import Run

from .helpers import NyayoTestCase

MOCK = Path(__file__).parent / "fixtures" / "mock"
TAGS = {"m0": "9f4c7df8d170-smoke-m0", "m1": "0be5cdac39da-smoke-m1", "m2": "148709c3c998-smoke-m2-published",
        "m2-rerun": "148709c3c998-smoke-m2-rerun", "m3": "d6ad75b51f01-smoke-m3", "m4": "d695c0979c67-smoke-m4",
        "m5": "1a5f3e2db2cc-smoke-m5"}


def mock(tag):
    path = next(MOCK.glob(f"{TAGS[tag]}*.json.gz"))
    return json.loads(gzip.decompress(path.read_bytes()))


def ids(changes):
    return [c.case.case_id for c in changes]


class Scenarios(NyayoTestCase):
    def setUp(self):
        super().setUp()
        self.runs = {}
        for tag in TAGS:
            document = self.ingest(mock(tag))
            self.runs[tag] = Run.objects.get(pk=document["run"]["id"])

    def pairs(self, comparison):
        return {p.check.name: p for p in comparison.pairs}

    def counts(self, pair):
        d = pair.diff
        return (d.compared, len(d.newly_failing), len(d.newly_passing), len(d.points_changed), len(d.other_move))

    def test_m1_against_m0(self):
        pairs = self.pairs(compare.compare_with_baselines(self.runs["m1"]))
        wac, sts, wac4 = pairs["Smoke_WinAtChess"], pairs["Smoke_STS"], pairs["Smoke_WinAtChess_4Threads"]
        self.assertEqual({wac.base.run, sts.base.run, wac4.base.run}, {self.runs["m0"]})
        self.assertEqual(ids(wac.diff.newly_failing), ["WAC.097", "WAC.160"])
        self.assertEqual(ids(wac.diff.newly_passing), ["WAC.116", "WAC.237", "WAC.256"])
        self.assertEqual(self.counts(wac), (300, 2, 3, 0, 4))
        self.assertEqual(self.counts(sts), (325, 3, 2, 8, 5))
        self.assertEqual((wac.delta("solved"), sts.delta("points")), (1, 0))
        self.assertIsNone(wac4.diff)
        self.assertEqual(wac4.delta("solved"), 2)

    def test_m2_against_m1(self):
        # Elephant's client compares a committed run with the commit before it, so its reference answer for m2 is
        # against m1. nyayo looks at the run's own commit first, so here m1 is picked explicitly.
        pairs = self.pairs(compare.compare_runs(self.runs["m2"], self.runs["m1"]))
        wac, sts = pairs["Smoke_WinAtChess"], pairs["Smoke_STS"]
        self.assertEqual(ids(wac.diff.newly_failing), ["WAC.028", "WAC.128", "WAC.149", "WAC.191", "WAC.235", "WAC.237"])
        self.assertEqual(ids(wac.diff.newly_passing), ["WAC.092", "WAC.152", "WAC.160", "WAC.252"])
        self.assertEqual(self.counts(wac), (300, 6, 4, 0, 3))
        self.assertEqual(self.counts(sts), (325, 4, 5, 12, 6))
        self.assertEqual(sts.delta("points"), 36)

    def test_m2_rerun_is_not_deterministic(self):
        pairs = self.pairs(compare.compare_with_baselines(self.runs["m2"]))
        wac, sts = pairs["Smoke_WinAtChess"], pairs["Smoke_STS"]
        self.assertEqual(wac.base.run, self.runs["m2-rerun"])
        self.assertEqual(ids(wac.diff.newly_passing), ["WAC.009"])
        self.assertEqual(self.counts(wac), (300, 0, 1, 0, 0))
        self.assertTrue(wac.not_deterministic)
        self.assertEqual(self.counts(sts), (325, 0, 0, 0, 0))
        self.assertFalse(sts.not_deterministic)

    def test_m3_compares_with_m0(self):
        pairs = self.pairs(compare.compare_with_baselines(self.runs["m3"]))
        wac, sts = pairs["Smoke_WinAtChess"], pairs["Smoke_STS"]
        self.assertEqual(wac.base.run, self.runs["m0"])
        self.assertEqual(ids(wac.diff.newly_failing), ["WAC.151"])
        self.assertEqual(self.counts(wac), (300, 1, 7, 0, 2))
        self.assertEqual(self.counts(sts), (325, 2, 6, 4, 0))

    def test_m4_new_and_missing_checks(self):
        comparison = compare.compare_with_baselines(self.runs["m4"])
        pairs = self.pairs(comparison)
        wac, sts, arasan = pairs["Smoke_WinAtChess"], pairs["Smoke_STS"], pairs["Smoke_Arasan2024"]
        self.assertEqual({wac.base.run, sts.base.run}, {self.runs["m2"]})      # published on 148709c, the newer
        self.assertEqual(ids(wac.diff.newly_failing), ["WAC.018", "WAC.023", "WAC.100", "WAC.281"])
        self.assertEqual(wac.diff.newly_failing[0].case.status, "error")
        self.assertEqual(self.counts(wac), (300, 4, 5, 0, 2))
        self.assertEqual(self.counts(sts), (325, 2, 2, 5, 0))
        self.assertEqual((sts.diff.only_here, sts.diff.only_there), (65, 0))
        self.assertIsNone(arasan.base)
        self.assertEqual(arasan.note, "no comparable run: a new check")
        self.assertEqual([(c.name, why.split(",")[0]) for c, why in comparison.only_b],
                         [("Smoke_WinAtChess_4Threads", "missing")])

    def test_m5_wip(self):
        pairs = self.pairs(compare.compare_with_baselines(self.runs["m5"]))
        wac, sts = pairs["Smoke_WinAtChess"], pairs["Smoke_STS"]
        wac200, arasan = pairs["Smoke_WinAtChess_200k"], pairs["Smoke_Arasan2024"]
        self.assertEqual({wac.base.run, sts.base.run, arasan.base.run}, {self.runs["m4"]})
        self.assertEqual(wac.diff.compared, 0)
        self.assertEqual(wac.diff.files_changed, ["wac_new.epd"])
        self.assertEqual(self.counts(sts), (260, 1, 2, 3, 2))
        self.assertEqual(sts.diff.only_there, 130)
        self.assertEqual(sts.delta("points"), 1418 - 2103)
        self.assertIsNone(wac200.base)
        self.assertEqual(wac200.note, "no comparable run: the same suite ran with other settings or another build")
        self.assertEqual(arasan.check.status, "error")
        self.assertEqual(self.counts(arasan), (12, 2, 2, 0, 8))
        self.assertEqual(arasan.diff.only_there, 8)
        self.assertEqual(compare.previous_on_branch(self.runs["m5"]), self.runs["m4"])

    def test_herd_docs_is_never_a_herd_baseline(self):
        for tag in ("m1", "m2", "m2-rerun", "m4", "m5"):
            used = {b.run for b in compare.baselines(self.runs[tag]).values() if b}
            self.assertNotIn(self.runs["m3"], used, tag)

    def test_wac_297_along_herd_smoke_report(self):
        head, series = compare.case_history("epd.wac_new", "WAC.297", "herd-smoke-report")
        self.assertEqual(head, self.runs["m5"])
        labels = [(s.check.name, len(s.rows)) for s in series]
        self.assertEqual(labels, [("Smoke_WinAtChess", 6), ("Smoke_WinAtChess_200k", 1),
                                  ("Smoke_WinAtChess_4Threads", 4)])
        single = series[0]
        self.assertEqual([r.run for r in single.rows],
                         [self.runs[t] for t in ("m5", "m4", "m2", "m2-rerun", "m1", "m0")])
        self.assertEqual([r.case.got for r in single.rows], ["e7c3", "e7c3", "e7c3", "e7c3", "e7h4", "e7h4"])
        summary = single.summary()
        self.assertFalse(summary["passing"])
        self.assertEqual((summary["since"], summary["other"]), (self.runs["m0"], None))
        four = series[2]
        self.assertEqual([r.case.status for r in four.rows], ["pass", "pass", "fail", "fail"])

    def test_pages_render_every_scenario(self):
        for tag, run in self.runs.items():
            for url in (f"/runs/{run.pk}", f"/compare?a={run.pk}&b=previous", f"/compare?a={run.pk}&b=baseline"):
                self.assertEqual(self.client.get(url).status_code, 200, (tag, url))
        page = self.client.get(f"/runs/{self.runs['m5'].pk}").content.decode()
        self.assertIn("the suite file changed (wac_new.epd)", page)
        self.assertIn("130 positions missing", page)
        self.assertIn("harness: error", page)
        page = self.client.get(f"/runs/{self.runs['m4'].pk}").content.decode()
        self.assertIn("a new check", page)
        self.assertIn("missing, the baseline", page)
        self.assertIn("Not deterministic: the same tree", self.client.get(f"/runs/{self.runs['m2'].pk}").content.decode())
