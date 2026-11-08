"""What a tune's page says: its progress, its parameters, the history charts, the values for the code and what comes after.

The match page of a `tune-N` match is this page (nyayo.matches.pages.match_detail asks `page()` first). The figures come from the
lab's own rows (`Test`, `SPSARun`, its parameters and snapshots), not from the match document, which has no room for them.
"""

import datetime

from django.shortcuts import render
from django.utils import timezone

from nyayo.actions.access import may_act

from . import modify_test, tunes, tunecharts
from .models import Result, SPSASnapshot, Test

SMALL_RANGE = 20  # an integer with a range this small is shown with the float SPSA keeps beside it
PACE_SNAPSHOTS = 10  # the pace is read over the newest snapshots, so a slow start or a stop doesn't set it
RECENT = datetime.timedelta(minutes=5)


def seconds_text(seconds):
    seconds = int(seconds)
    if seconds >= 86400:
        return f"{seconds // 86400} d {seconds % 86400 // 3600} h"
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    return f"{max(1, seconds // 60)} min"


def pace(run, test, now):
    """(games an hour, seconds to go) from the newest snapshots, or (None, None) when there is no pace to read."""
    snaps = list(run.snapshots.order_by("-iteration")[:PACE_SNAPSHOTS])
    if len(snaps) < 2:
        return None, None
    newest, oldest = snaps[0], snaps[-1]
    elapsed = (newest.created - oldest.created).total_seconds()
    played = newest.games - oldest.games
    if elapsed <= 0 or played <= 0:
        return None, None
    per_hour = played / elapsed * 3600
    left = max(0, 2 * run.pairs_per * run.iterations - test.games)
    return per_hour, left / played * elapsed


def checks_of(run):
    """Every snapshot that has a check, finished or not: the table of checks below the outcome chart."""
    out = []
    for s in run.snapshots.select_related("check_test"):
        if s.check_test_id is None:
            continue
        test = s.check_test
        out.append({
            "iteration": s.iteration, "snapshot": s, "test": test, "match": test.match_id(),
            "done": s.check_elo is not None, "elo": s.check_elo, "error": s.check_error,
            "games": s.check_games if s.check_games is not None else test.games, "max_games": test.max_games,
            "state": "finished" if s.check_elo is not None else "stopped" if test.finished else "playing" if test.games else "waiting",
        })
    return out


def parameter_rows(run, snapshots, so_far, scale, finished_checks):
    late = tunes.lately(run)
    rows, settled = [], 0
    for p in run.parameters.order_by("index"):
        series = [(s.iteration, s.values[p.name]) for s in snapshots if p.name in s.values]
        if series and series[-1][0] != so_far:
            series.append((so_far, p.value))
        span = p.max_value - p.min_value
        small = not p.is_float and span < SMALL_RANGE
        word, tone = tunecharts.lately_word(late.get(p.name, 0.0))
        settled += tone == "settled"
        change = p.value - p.start
        tiny = abs(change) / span < 0.01 if span else True
        rows.append({
            "name": p.name, "float": p.is_float,
            "start": tunecharts.figure(p.start, p.is_float), "now": tunecharts.figure(p.value, p.is_float, small),
            "change": ("+" if change > 0 else tunecharts.MINUS if change < 0 else "") + (f"{abs(change):.2f}" if p.is_float or small else str(int(round(abs(change))))),
            "tone": "flat" if tiny else "up" if change > 0 else "down",
            "min": tunecharts.figure(p.min_value, p.is_float), "max": tunecharts.figure(p.max_value, p.is_float),
            "bar": tunecharts.range_bar(p.min_value, p.max_value, p.start, p.value),
            "step": tunecharts.step_now(p.c_value, run.gamma, p.is_float, max(1, so_far)),
            "late": f"{late.get(p.name, 0.0) * 100:.1f}%", "word": word, "tone_late": tone,
            "chart": tunecharts.parameter_chart(series, p.start, finished_checks, so_far, scale),
            "aria": f"{p.name} from {tunecharts.figure(p.start, p.is_float)} to {tunecharts.figure(p.value, p.is_float)} over {so_far:,} iterations",
        })
    for row in rows:
        s = row["step"]
        row["step"] = f"{s:.0f}" if s >= 10 else f"{s:.2f}".rstrip("0").rstrip(".")
    return rows, settled


def code_blocks(run, values):
    """The averaged values as C++ constants, as UCI options and as SPSA input (the values as the new start)."""
    params = list(run.parameters.order_by("index"))
    width = max((len(p.name) for p in params), default=0)
    cpp, options, spsa = [], [], []
    for p in params:
        value = values.get(p.name, p.value)
        text = tunes.number(value, p.is_float)
        cpp.append(f"constexpr {'double' if p.is_float else 'int'} {p.name.ljust(width)} = {text};")
        options.append(f"{p.name}={text}")
        spsa.append(", ".join([p.name, "float" if p.is_float else "int", text, tunes.number(p.min_value, p.is_float),
                               tunes.number(p.max_value, p.is_float), f"{p.c_end:g}", f"{p.r_end:g}"]))
    return {"cpp": "\n".join(cpp), "options": " ".join(options), "spsa": "\n".join(spsa)}


def then_block(run):
    follow = run.then_test
    if follow is None:
        return {"test": None, "waits": run.then_sprt}
    state = "finished" if follow.finished else "running" if follow.games else "queued"
    if follow.finished:
        state = "passed" if follow.passed else "failed" if follow.failed else "stopped"
    return {"test": follow, "match": follow.match_id(), "state": state, "waits": False}


def context(request, match, test):
    run = test.spsa_run
    now = timezone.now()
    snapshots = list(run.snapshots.all())
    so_far = tunes.iteration_of(run, test.games)
    checks = checks_of(run)
    done = [c for c in checks if c["done"]]
    scale = tunecharts.EloScale(done)
    outcome = tunecharts.outcome_chart(done, max(so_far, 1), scale)
    rows, settled = parameter_rows(run, snapshots, max(so_far, 1), scale, done)

    per_hour, left = pace(run, test, now)
    total_games = 2 * run.pairs_per * run.iterations
    check_count = run.iterations // run.check_every if run.check_every else 0
    recent = [r for r in Result.objects.filter(test=test).select_related("machine") if r.machine.updated >= now - RECENT]
    running = not test.finished and test.approved
    values = tunes.averaged_values(run)

    frozen = ", ".join(f"{f['name']} {tunes.number(float(f['value']))}" for f in run.frozen or [])
    facts = [("Games", f"{test.games:,}"),
             ("Pairs per hour", f"{per_hour / 2:,.0f}" if per_hour and running else None),
             ("Ends", ("in about " + seconds_text(left) + ", " + (now + datetime.timedelta(seconds=left)).strftime("%a %H:%M")) if left is not None and running else None),
             ("Workers", ", ".join(sorted({r.machine.name or f"machine {r.machine_id}" for r in recent})) if running else None),
             ("Started", timezone.localtime(test.creation).strftime("%a %H:%M")),
             ("Snapshots", f"{len(snapshots):,}")]
    return {
        "match": match, "test": test, "run": run, "silent": False, "refresh": match.state in ("running", "waiting"),
        "iteration": so_far, "iterations": run.iterations, "pct": f"{so_far / run.iterations * 100:.1f}%" if run.iterations else "0%",
        "pct_width": f"{min(100.0, so_far / run.iterations * 100):.1f}%" if run.iterations else "0%",
        "facts": [(label, value) for label, value in facts if value], "settled": settled, "tuned": len(rows), "moving": len(rows) - settled, "snapshots": snapshots,
        "rows": rows, "outcome": outcome, "scale": scale, "checks": checks,
        "setup": " · ".join(part for part in (
            "SPSA", test.dev_time_control, test.dev_options, test.book_name,
            "SPSA with momentum" if run.update_rule == "MOMENTUM" else "plain SPSA",
            f"{run.pairs_per} pair{'s' if run.pairs_per != 1 else ''} per iteration",
            f"{len(run.frozen or [])} frozen: {frozen}" if run.frozen else "") if part),
        "commit": test.dev.sha[:7], "branch": test.dev.name,
        "snapshot_every": run.snapshot_every, "check_every": run.check_every, "check_games": run.check_games,
        "code": code_blocks(run, values), "average_tail": f"{run.average_tail * 100:g}%",
        "then": then_block(run), "planned_games": total_games, "check_count": check_count,
        "can_act": may_act(request.user), "history_json": f"/tests/{test.id}/history.json", "history_csv": f"/tests/{test.id}/history.csv",
        "buttons": modify_test.offers(test) if may_act(request.user) else [],
    }


def page(request, match):
    """The tune's page, or None when the match is not a tune of the lab's."""
    test = modify_test.test_of(match)
    if test is None or test.test_mode != "SPSA" or not hasattr(test, "spsa_run"):
        return None
    return render(request, "lab/tune_detail.html", context(request, match, test))
