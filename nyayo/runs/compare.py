"""How runs are compared: baselines along the commit graph, which checks pair up, and what changed per position.

Two checks are comparable when their key matches (check id, version, limit, threads, hash, experiment and build,
see ingest.check_key). A position is compared when its id and its EPD file's content hash match. Checks that aren't
deterministic (multi threaded) are compared on totals only.
"""

from dataclasses import dataclass, field

from .models import Case, Check, Run

KEY_FIELDS = [
    ("version", "version"), ("limit_type", "limit"), ("limit_value", "limit"), ("threads", "threads"),
    ("hash_mb", "hash"), ("experiment", "experiment"), ("compiler", "compiler"), ("config", "config"),
    ("assertions", "assertions"), ("exceptions", "exceptions"), ("bits", "bits"),
]
STATE_PREFERENCE = {"published": 0, "committed": 1}


def baselines(run):
    """Per check of run (by check pk), the check it's measured against: on the nearest commit in run's ancestry
    (its own commit first) the newest comparable check, published before committed. wip runs are never baselines."""
    checks = list(run.checks.all())
    lineage = run.lineage
    position = {commit: i for i, commit in enumerate(lineage)}
    candidates = (Check.objects.filter(key__in={c.key for c in checks}, run__commit__in=lineage,
                                       run__state__in=STATE_PREFERENCE)
                  .exclude(run=run).select_related("run"))
    best = {}
    for candidate in candidates:
        rank = (position[candidate.run.commit], STATE_PREFERENCE[candidate.run.state],
                -candidate.run.recorded_at.timestamp())
        if candidate.key not in best or rank < best[candidate.key][0]:
            best[candidate.key] = (rank, candidate)
    return {c.pk: best[c.key][1] if c.key in best else None for c in checks}


def previous_on_branch(run):
    """The run recorded before this one on its branch with a comparable check, any state."""
    keys = {c.key for c in run.checks.all()}
    return (Run.objects.filter(branch=run.branch, recorded_at__lt=run.recorded_at, checks__key__in=keys)
            .exclude(pk=run.pk).distinct().order_by("-recorded_at").first())


def relation(a, b):
    """How b's commit relates to a's, in words."""
    a_revision, b_revision = a.commit or a.base_commit, b.commit or b.base_commit
    if a.commit and a.commit == b.commit:
        return "the same commit"
    if b_revision and b_revision in a.lineage:
        return "an ancestor" if b.commit else "a wip run on an ancestor"
    if a_revision and a_revision in b.lineage:
        return "a descendant" if b.commit else "a wip run on a descendant"
    return "unrelated: neither descends from the other (within 300 commits)"


def key_difference(a, b):
    """What makes two checks with the same id incomparable, e.g. 'threads 1 vs 4'."""
    differences = []
    for attribute, label in KEY_FIELDS:
        left, right = getattr(a, attribute), getattr(b, attribute)
        if left != right and label not in [d.split(" ")[0] for d in differences]:
            if label == "limit":
                left, right = f"{a.limit_type} {a.limit_value}", f"{b.limit_type} {b.limit_value}"
            differences.append(f"{label} {left} vs {right}")
    return ", ".join(differences) or "different settings"


@dataclass
class CaseChange:
    case: Case
    before: Case | None

    @property
    def points_delta(self):
        if self.case.points is None or self.before is None or self.before.points is None:
            return None
        return self.case.points - self.before.points


@dataclass
class CaseDiff:
    newly_failing: list = field(default_factory=list)
    newly_passing: list = field(default_factory=list)
    points_changed: list = field(default_factory=list)
    other_move: list = field(default_factory=list)
    only_here: int = 0
    only_there: int = 0
    compared: int = 0
    files_changed: list = field(default_factory=list)   # suite files whose content differs: nothing in them compares

    @property
    def changed(self):
        return len(self.newly_failing) + len(self.newly_passing) + len(self.points_changed) + len(self.other_move)


def diff_cases(check, base):
    """Position by position, check against base. None when either isn't deterministic: totals only then."""
    if not (check.deterministic and base.deterministic):
        return None
    before = {c.identity: c for c in base.cases.all()}
    diff = CaseDiff()
    seen = set()
    for case in check.cases.all():
        previous = before.get(case.identity)
        if previous is None:
            diff.only_here += 1
            continue
        seen.add(case.identity)
        diff.compared += 1
        change = CaseChange(case, previous)
        passed, passed_before = case.status == "pass", previous.status == "pass"
        if passed_before and not passed:
            diff.newly_failing.append(change)
        elif passed and not passed_before:
            diff.newly_passing.append(change)
        elif change.points_delta:
            diff.points_changed.append(change)
        elif case.got != previous.got:
            diff.other_move.append(change)
    diff.only_there = len(before) - len(seen)
    now, then = check.params.get("file_sha256") or {}, base.params.get("file_sha256") or {}
    diff.files_changed = sorted(f for f, sha in now.items() if then.get(f) not in (None, sha))
    return diff


@dataclass
class Bar:
    """One score drawn as a bar: segments are (state, count, percent, title) side by side, markers (kind, label,
    value, percent) are levels drawn across it."""
    label: str
    value: float
    total: float
    delta: float | None = None
    segments: list = field(default_factory=list)
    markers: list = field(default_factory=list)


def percent(value, total):
    return max(0.0, min(100.0, 100.0 * value / total))


@dataclass
class Pair:
    check: Check
    base: Check | None
    diff: CaseDiff | None = None
    note: str = ""

    @property
    def not_deterministic(self):
        """The same tree and build searched differently: a determinism failure."""
        return bool(self.diff and self.diff.changed and self.check.run.tree
                    and self.check.run.tree == self.base.run.tree)

    def case_groups(self):
        """The check's positions by EPD file, in the order they ran: (file, [(case, state)], how many passed). state is
        pass or fail, or newly-passing / newly-failing against the baseline when positions were compared."""
        newly = {}
        if self.diff:
            newly.update((change.case.pk, "newly-failing") for change in self.diff.newly_failing)
            newly.update((change.case.pk, "newly-passing") for change in self.diff.newly_passing)
        groups = {}
        for case in self.check.cases.all():
            state = newly.get(case.pk) or ("pass" if case.status == "pass" else "fail")
            groups.setdefault(case.file, []).append((case, state))
        return [(file, cells, sum(case.status == "pass" for case, _ in cells)) for file, cells in groups.items()]

    def bars(self):
        """Positions solved, then points for a scored suite. Solved splits into the positions that pass in both runs,
        newly pass, newly fail and fail in both, when positions were compared. The suite's own minimum and expected
        level mark the score it's judged on (points when there are any), the baseline run marks both."""
        verdict = self.check.engine_verdict or {}
        judged = "points" if self.check.metric("points") is not None else "solved"
        bars = []
        for name, total_name in (("solved", "total"), ("points", "max_points")):
            value, total = self.check.metric(name), self.check.metric(total_name)
            if value is None or not total:
                continue
            bar = Bar(name, value, total, self.delta(name))
            if name == "points":
                counts = [("pass", value, "points earned"), ("unearned", total - value, "points not earned")]
            elif self.diff and self.diff.compared:
                gained, lost = len(self.diff.newly_passing), len(self.diff.newly_failing)
                counts = [("pass", value - gained, "passed in both"), ("newly-passing", gained, "passed, failed before"),
                          ("newly-failing", lost, "failed, passed before"), ("fail", total - value - lost, "failed in both")]
            else:
                counts = [("pass", value, "passed"), ("fail", total - value, "failed")]
            bar.segments = [(state, count, percent(count, total), f"{count:g} {title}")
                            for state, count, title in counts if count > 0]
            if name == judged:
                for kind, label in (("minimum", "minimum"), ("baseline", "expected")):
                    if verdict.get(kind):
                        bar.markers.append((kind, label, verdict[kind], percent(verdict[kind], total)))
            then = self.base.metric(name) if self.base else None
            if then is not None:
                bar.markers.append(("base", "baseline run", then, percent(then, total)))
            bars.append(bar)
        return bars

    def delta(self, name):
        if self.base is None:
            return None
        now, then = self.check.metric(name), self.base.metric(name)
        return None if now is None or then is None else now - then


@dataclass
class Comparison:
    pairs: list
    only_a: list = field(default_factory=list)       # (check, reason)
    only_b: list = field(default_factory=list)
    a_label: str = "A"
    b_label: str = "B"


def compare_with_baselines(run):
    """Each of run's checks against its own baseline."""
    found = baselines(run)
    checks = list(run.checks.all())
    pairs = []
    for check in checks:
        base = found[check.pk]
        pair = Pair(check, base)
        if base is None:
            same_suite = (Check.objects.filter(check_id=check.check_id, run__commit__in=run.lineage,
                                               run__state__in=STATE_PREFERENCE).exclude(run=run).exists())
            pair.note = "no comparable run: " + ("the same suite ran with other settings or another build"
                                                 if same_suite else "a new check")
        else:
            pair.diff = diff_cases(check, base)
        pairs.append(pair)
    comparison = Comparison(pairs, a_label="this run", b_label="baseline")
    # what the baseline runs had and this one didn't
    keys = {c.key for c in checks}
    seen = set()
    for base_run in {b.run for b in found.values() if b is not None}:
        for check in base_run.checks.all():
            if check.key not in keys and check.key not in seen:
                seen.add(check.key)
                comparison.only_b.append((check, f"missing, the baseline {base_run.short_name} ran it"))
    return comparison


def compare_runs(a, b):
    """a's checks paired with b's by key. Checks without a partner are listed with why, never counted as passing."""
    a_checks, b_checks = list(a.checks.all()), list(b.checks.all())
    b_by_key = {c.key: c for c in b_checks}
    comparison = Comparison([])
    for check in a_checks:
        base = b_by_key.get(check.key)
        if base is None:
            comparison.only_a.append((check, unmatched_reason(check, b_checks)))
        else:
            comparison.pairs.append(Pair(check, base, diff_cases(check, base)))
    a_keys = {c.key for c in a_checks}
    for check in b_checks:
        if check.key not in a_keys:
            comparison.only_b.append((check, unmatched_reason(check, a_checks)))
    return comparison


def unmatched_reason(check, others):
    same_id = [o for o in others if o.check_id == check.check_id]
    if not same_id:
        return "not in the other run"
    return "not comparable: " + "; ".join(key_difference(check, o) for o in same_id)


# --- one position through history ---

@dataclass
class HistoryRow:
    case: Case
    run: Run
    file_changed: bool = False      # the EPD file's content differs from the next older row


@dataclass
class Series:
    key: str
    check: Check
    file: str
    rows: list = field(default_factory=list)

    @property
    def label(self):
        return f"{self.check.name or self.check.check_id}: {self.check.settings}" + \
            ("" if self.check.deterministic else ", not deterministic")

    def summary(self):
        """Whether the position passes now, and since when, along committed and published runs with the newest
        row's EPD file content."""
        settled = [row for row in self.rows if row.run.state != "wip"]
        if not settled:
            return None
        newest = settled[0]
        current = [row for row in settled if row.case.file_sha256 == newest.case.file_sha256]
        passing = newest.case.status == "pass"
        streak_end, other = None, None
        for row in current:
            if (row.case.status == "pass") == passing:
                streak_end = row
            else:
                other = row
                break
        return {"passing": passing, "since": streak_end.run, "other": other.run if other else None,
                "newest": newest.run}


def case_history(check_id, case_id, branch):
    """The position's results along branch's history: the newest run on branch gives the line of commits, every
    committed or published run on one of them, plus wip runs made on the branch. Newest first, one series per check
    key and file."""
    head = Run.objects.filter(branch=branch).order_by("-recorded_at").first()
    if head is None:
        return None, []
    lineage = head.lineage
    position = {commit: i for i, commit in enumerate(lineage)}
    cases = (Case.objects.filter(case_id=case_id, check_result__check_id=check_id)
             .select_related("check_result", "check_result__run"))
    series = {}
    for case in cases:
        run = case.check_result.run
        if run.state == "wip":
            if run.branch != branch or run.base_commit not in position:
                continue
            # a wip run sits ahead of the commit it was made on
            rank = (position[run.base_commit], 0, -run.recorded_at.timestamp())
        elif run.commit in position:
            rank = (position[run.commit], 1, -run.recorded_at.timestamp())
        else:
            continue
        key = f"{case.check_result.key}:{case.file}"
        if key not in series:
            series[key] = Series(key, case.check_result, case.file)
        series[key].rows.append((rank, HistoryRow(case, run)))

    result = []
    for s in series.values():
        s.rows = [row for _, row in sorted(s.rows, key=lambda item: item[0])]
        for newer, older in zip(s.rows, s.rows[1:]):
            newer.file_changed = newer.case.file_sha256 != older.case.file_sha256
        result.append(s)
    # deterministic series first, then the longest
    result.sort(key=lambda s: (not s.check.deterministic, -len(s.rows)))
    return head, result
