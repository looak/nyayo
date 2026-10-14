from nyayo.runs import compare
from nyayo.runs.models import Run

from .helpers import NyayoTestCase, case, derived, fake_sha, real_run

WAC, WAC_4T, STS = 0, 1, 2


def run(document):
    return Run.objects.get(pk=document["run"]["id"])


class Baselines(NyayoTestCase):
    def setUp(self):
        super().setUp()
        self.real = self.ingest(real_run())                 # committed on X, the head of the ancestry
        self.x = self.real["run"]["commit"]
        self.parents = self.real["run"]["ancestors"]

    def child(self, **kwargs):
        return derived(parents=self.parents, **kwargs)

    def test_nearest_ancestor(self):
        target = self.ingest(self.child(branch="herd-x", minutes=10))
        found = compare.baselines(run(target))
        self.assertEqual({b.run_id for b in found.values()}, {run(self.real).pk})

    def test_parallel_branch_is_never_the_baseline(self):
        self.ingest(self.child(branch="herd-y", minutes=20))      # newer, but a sibling
        target = self.ingest(self.child(branch="herd-x", minutes=10))
        found = compare.baselines(run(target))
        self.assertEqual({b.run_id for b in found.values()}, {run(self.real).pk})

    def test_never_a_wip_run(self):
        self.ingest(self.child(state="wip", minutes=5, base=self.x))
        target = self.ingest(self.child(minutes=10))
        self.assertEqual({b.run_id for b in compare.baselines(run(target)).values()}, {run(self.real).pk})

    def test_published_before_committed_on_one_commit(self):
        published = self.ingest(derived(state="published", commit=self.x, parents=self.parents, minutes=-30))
        self.ingest(derived(commit=self.x, parents=self.parents, minutes=5))   # newer, only committed
        target = self.ingest(self.child(minutes=10))
        self.assertEqual({b.run_id for b in compare.baselines(run(target)).values()}, {run(published).pk})

    def test_own_commit_first(self):
        rerun = self.ingest(derived(commit=self.x, parents=self.parents, minutes=10))
        self.assertEqual({b.run_id for b in compare.baselines(run(rerun)).values()}, {run(self.real).pk})

    def test_wip_compares_with_its_base(self):
        wip = self.ingest(self.child(state="wip", minutes=10, base=self.x))
        self.assertEqual({b.run_id for b in compare.baselines(run(wip)).values()}, {run(self.real).pk})

    def test_no_baseline_outside_the_ancestry(self):
        stranger = self.ingest(derived(parents=[fake_sha("elsewhere")], minutes=10))
        self.assertEqual(set(compare.baselines(run(stranger)).values()), {None})

    def test_per_check(self):
        # a newer run on X that only has WAC: WAC compares with it, STS still with the older full run
        only_wac = derived(commit=self.x, parents=self.parents, minutes=5)
        only_wac["checks"] = only_wac["checks"][:1]
        self.ingest(only_wac)
        target = run(self.ingest(self.child(minutes=10)))
        found = {c.name: b.run_id for c, b in zip(target.checks.all(), compare.baselines(target).values())}
        self.assertEqual(found["Smoke_WinAtChess"], run(only_wac).pk)
        self.assertEqual(found["Smoke_STS"], run(self.real).pk)

    def test_previous_on_branch_includes_wip(self):
        wip = self.ingest(self.child(state="wip", minutes=5, base=self.x))
        target = self.ingest(self.child(minutes=10))
        self.assertEqual(compare.previous_on_branch(run(target)).pk, run(wip).pk)


class PositionDiff(NyayoTestCase):
    def setUp(self):
        super().setUp()
        self.real = self.ingest(real_run())

    def changed_child(self, edit):
        document = derived(parents=self.real["run"]["ancestors"], minutes=10)
        edit(document)
        return run(self.ingest(document))

    def pair(self, target, name):
        return next(p for p in compare.compare_with_baselines(target).pairs if p.check.name == name)

    def test_categories(self):
        cases = self.real["checks"][WAC]["cases"]
        failing = next(c["id"] for c in cases if c["status"] == "fail")
        moved = next(c["id"] for c in cases if c["status"] == "pass" and c["id"] != "WAC.001")

        def edit(document):
            case(document, WAC, "WAC.001").update(status="fail", got="a2a3")
            case(document, WAC, failing).update(status="pass")
            case(document, WAC, moved)["got"] = "h1h2"
            case(document, STS, "Undermine.001").update(points=0)
        target = self.changed_child(edit)

        wac = self.pair(target, "Smoke_WinAtChess").diff
        self.assertEqual([c.case.case_id for c in wac.newly_failing], ["WAC.001"])
        self.assertEqual([c.case.case_id for c in wac.newly_passing], [failing])
        self.assertEqual([c.case.case_id for c in wac.other_move], [moved])
        self.assertEqual(wac.compared, 300)

        sts = self.pair(target, "Smoke_STS").diff
        self.assertEqual([(c.case.case_id, c.points_delta) for c in sts.points_changed], [("Undermine.001", -10)])

    def test_multi_threaded_is_totals_only(self):
        target = self.changed_child(lambda d: case(d, WAC_4T, "WAC.001").update(status="fail"))
        pair = self.pair(target, "Smoke_WinAtChess_4Threads")
        self.assertIsNotNone(pair.base)
        self.assertIsNone(pair.diff)

    def test_edited_epd_file_breaks_the_comparison(self):
        def edit(document):
            document["checks"][WAC]["params"]["file_sha256"]["wac_new.epd"] = "0" * 64
        diff = self.pair(self.changed_child(edit), "Smoke_WinAtChess").diff
        self.assertEqual((diff.compared, diff.only_here, diff.only_there), (0, 300, 300))


class RunAgainstRun(NyayoTestCase):
    def test_other_settings_are_not_comparable(self):
        real = self.ingest(real_run())
        other = derived(parents=real["run"]["ancestors"], minutes=10)
        other["checks"][STS]["params"]["hash_mb"] = 16
        other["checks"] = other["checks"][1:]
        other = self.ingest(other)

        comparison = compare.compare_runs(run(other), run(real))
        self.assertEqual([p.check.name for p in comparison.pairs], ["Smoke_WinAtChess_4Threads"])
        self.assertEqual([(c.name, why) for c, why in comparison.only_a], [("Smoke_STS", "not comparable: hash 16 vs 64")])
        reasons = dict((c.name, why) for c, why in comparison.only_b)
        self.assertEqual(reasons["Smoke_STS"], "not comparable: hash 64 vs 16")
        self.assertIn("threads 1 vs 4", reasons["Smoke_WinAtChess"])

    def test_relation(self):
        real = self.ingest(real_run())
        a = self.ingest(derived(parents=real["run"]["ancestors"], branch="herd-x", minutes=10))
        b = self.ingest(derived(parents=real["run"]["ancestors"], branch="herd-y", minutes=10))
        self.assertEqual(compare.relation(run(a), run(real)), "an ancestor")
        self.assertTrue(compare.relation(run(a), run(b)).startswith("unrelated"))


class History(NyayoTestCase):
    def test_failing_since(self):
        real = self.ingest(real_run())                       # WAC.001 passes on X
        x = real["run"]["commit"]
        line = [x] + real["run"]["ancestors"][1:]
        first = derived(parents=line, branch="main", minutes=10)
        case(first, WAC, "WAC.001").update(status="fail", got="a2a3")
        self.ingest(first)
        second = derived(parents=first["run"]["ancestors"], branch="main", minutes=20)
        case(second, WAC, "WAC.001").update(status="fail", got="a2a3")
        self.ingest(second)
        wip = derived(state="wip", parents=second["run"]["ancestors"], branch="main", minutes=30)
        self.ingest(wip)

        head, series = compare.case_history("epd.wac_new", "WAC.001", "main")
        self.assertEqual(head.pk, run(wip).pk)
        single = series[0]
        self.assertTrue(single.check.deterministic)
        self.assertEqual([r.run.pk for r in single.rows], [run(d).pk for d in (wip, second, first, real)])
        summary = single.summary()
        self.assertFalse(summary["passing"])
        self.assertEqual(summary["since"].pk, run(first).pk)
        self.assertEqual(summary["other"].pk, run(real).pk)
        self.assertEqual(len(series), 2)     # 1 thread and 4 threads
