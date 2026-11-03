"""A worker's walk through a test (docs/match-schema.md, *Steps*) as the page draws it.

nyayo draws the steps in the order sent and applies no rule to them: a failed step is coloured by the `blame` the
producer wrote on it, and what it says (detail, evidence, effect, log) is shown as sent. The only things worked out
here are layout (a column per step, a node per side), how long an open step has been open, and the tally of the
producer's own words for the summary.
"""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

# a worker not heard from for this long is "gone quiet" (the plan's heartbeat)
QUIET_AFTER = timedelta(minutes=5)

HEADS = {"picked_up": "Picked up", "compile": "Compile", "bench": "Bench", "running": "Running"}
# the node's own label for a step that isn't done per engine
BARE = {"picked_up": "Worker", "running": "Games"}
SIDE_ORDER = ("dev", "base")
# a failed step's colour is its blame; anything else is the plain red of a failure with no opinion
LOOKS = {"passed": "passed", "active": "active", "pending": "pending"}
BLAMES = {"engine": "engine", "worker": "worker", "unclear": "unclear"}
BLAME_WORDS = {"engine": "engine", "worker": "worker", "unclear": "can't tell", None: "failed"}
TOWARD = {"engine": "engine", "worker": "worker", "either": "either"}

# a card's kind, in the order the summary lists them: its colour, and the summary's pill
KINDS = (("running", "playing"), ("checks", "on their checks"), ("engine", "stopped by the engine"),
         ("worker", "in trouble"), ("unclear", "need a look"), ("quiet", "gone quiet"), ("elsewhere", "elsewhere"))
OPEN_STATES = ("waiting", "held", "running")


def when(value):
    return parse_datetime(value) if isinstance(value, str) else None


def length(seconds):
    """41 s, 1 min 52 s, 2 h 5 min: how long a step took, or has been open."""
    seconds = max(0, round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {rest} s" if rest else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def clean(steps):
    """The steps that are objects with a name and a known state, in the order sent."""
    return [s for s in steps if isinstance(s, dict) and isinstance(s.get("step"), str)] \
        if isinstance(steps, list) else []


def head(name):
    return HEADS.get(name) or name.replace("_", " ").capitalize()


def look(step):
    state = step.get("state")
    if state == "failed":
        return BLAMES.get(step.get("blame"), "failed")
    return LOOKS.get(state, "pending")


def nps_text(nps):
    nps = number(nps)
    if nps is None:
        return ""
    return f"{nps / 1e6:.2f} Mnps" if nps >= 1e6 else f"{nps / 1e3:.0f} knps"


def sub_text(step, now, reached, machine):
    """The small line under a node's label."""
    state, at = step.get("state"), when(step.get("at"))
    took = number(step.get("seconds"))
    if state == "pending":
        return "not yet" if reached else "not reached"
    if state == "active":
        if step.get("step") == "running" and isinstance(machine.get("concurrency"), int):
            return f"{machine['concurrency']} at a time"
        return f"{length((now - at).total_seconds())} so far" if at else "now"
    if step.get("step") == "picked_up":
        return timezone.localtime(at).strftime("%H:%M:%S") if at else ""
    if state == "failed":
        return length(took) if took is not None else ""
    if step.get("cached"):
        return "cached"
    return nps_text(step.get("nps")) or (length(took) if took is not None else "")


def columns(steps, now, machine):
    """The walk left to right: a column per step, the engines one over the other where the step has a side."""
    steps = clean(steps)
    failed_before = False
    cols = []
    for step in steps:
        name, side = step["step"], step.get("side") if isinstance(step.get("side"), str) else None
        if not cols or cols[-1]["step"] != name or any(n["side"] == side for n in cols[-1]["nodes"]):
            cols.append({"step": name, "head": head(name), "nodes": []})
        label = side or BARE.get(name) or head(name)
        node_look = look(step)
        cols[-1]["nodes"].append({
            "side": side, "label": label, "look": node_look, "state": step.get("state"),
            "sub": sub_text(step, now, not failed_before, machine),
            "title": f"{head(name)}{' ' + side if side else ''}: {step.get('state')}",
            "key": f"{name}-{side or 'x'}",
        })
        if step.get("state") == "failed":
            failed_before = True
    return cols


def failures(steps):
    """The failed steps with what the producer said about each, for the block under the walk."""
    out = []
    for step in clean(steps):
        if step.get("state") != "failed":
            continue
        evidence = [{"toward": TOWARD.get(e.get("points_at"), "either"), "text": e["text"]}
                    for e in step.get("evidence") or [] if isinstance(e, dict) and isinstance(e.get("text"), str)]
        side = step.get("side")
        out.append({
            "key": f"{step['step']}-{side or 'x'}", "where": f"{side} {step['step']}" if side else head(step["step"]),
            "look": look(step), "blame": BLAME_WORDS.get(step.get("blame"), "failed"),
            "detail": step.get("detail") or "", "evidence": evidence, "effect": step.get("effect") or "",
            "log": step.get("log") or "", "tries": step.get("tries"), "retry_at": when(step.get("retry_at")),
        })
    return out


def status(steps, machine, now, state):
    """(kind, text) of a worker: what it is doing, or what went wrong, in the words of the steps."""
    steps = clean(steps)
    failed = [s for s in steps if s.get("state") == "failed"]
    if failed:
        first = failed[0]
        blame, side, name = first.get("blame"), first.get("side"), first["step"]
        tries = first.get("tries")
        if blame == "worker":
            return "worker", "Worker trouble" + (f" · {tries} tries" if isinstance(tries, int) and tries > 1 else "")
        if blame == "unclear":
            return "unclear", "Needs a look"
        who = (side or "").capitalize()
        both = len({s.get("side") for s in failed if s["step"] == name}) > 1
        who = "Dev and base" if both else who
        words = {"compile": f"{who} failed to compile", "bench": f"{who}'s bench failed"}.get(name)
        return "engine", (words or f"{head(name)} failed").strip()
    if machine.get("active") is False:
        return "elsewhere", "On something else"
    updated = when(machine.get("updated_at"))
    if state in OPEN_STATES and updated and now - updated > QUIET_AFTER:
        return "quiet", "Gone quiet"
    running = next((s for s in steps if s["step"] == "running"), None)
    if running and running.get("state") in ("active", "passed"):
        workloads = machine.get("workloads")
        return "running", "Playing" + (f" · workload {workloads}" if isinstance(workloads, int) and workloads > 1
                                       else "")
    active = next((s for s in steps if s.get("state") == "active"), None)
    if active:
        side = f" {active['side']}" if active.get("side") else ""
        verb = {"compile": "Compiling", "bench": "Benching"}.get(active["step"])
        return "checks", f"{verb}{side}" if verb else f"{head(active['step'])}{side}"
    if any(s.get("state") == "pending" for s in steps):
        return "checks", "Waiting to start"
    return "checks", "On its checks"


def card(machine, now, state):
    """What a worker's card adds to its row: its walk, its status and the failures to read."""
    steps = clean(machine.get("steps"))
    if not steps:
        return {"has_steps": False, "kind": None, "status": "", "columns": [], "failures": [], "open": False,
                "workloads": machine.get("workloads")}
    kind, text = status(steps, machine, now, state)
    return {
        "has_steps": True, "kind": kind, "status": text, "columns": columns(steps, now, machine),
        "failures": failures(steps), "open": any(s.get("state") == "active" for s in steps),
        "workloads": machine.get("workloads"), "quiet_since": when(machine.get("updated_at")) if kind == "quiet" else None,
    }


def tally(rows):
    """The summary above the cards: how each engine is doing across the workers, and how the workers are. Counted
    from the steps as the producer wrote them; nothing here decides whether a test is healthy."""
    sides = {}
    for row in rows:
        for step in clean(row.get("steps")):
            side = step.get("side")
            if not isinstance(side, str):
                continue
            counts = sides.setdefault(side, {}).setdefault(step["step"], {})
            counts[step.get("state")] = counts.get(step.get("state"), 0) + 1
            if step.get("state") == "failed":
                blame = step.get("blame") or "failed"
                counts[blame] = counts.get(blame, 0) + 1
    engines = []
    for side in sorted(sides, key=lambda s: (SIDE_ORDER.index(s) if s in SIDE_ORDER else len(SIDE_ORDER), s)):
        parts, blames = [], set()
        for name, counts in sides[side].items():
            if counts.get("passed"):
                verb = {"compile": "compiled", "bench": "benched"}.get(name, f"{name} passed")
                parts.append(f"{verb} on {counts['passed']} worker{'s' if counts['passed'] != 1 else ''}")
            if counts.get("failed"):
                failed = counts["failed"]
                what = {"compile": "failed to compile", "bench": "failed its bench"}.get(name, f"{name} failed")
                parts.append(f"{what} on {failed}")
                blames.update(b for b in ("engine", "worker", "unclear") if counts.get(b))
        engines.append({"side": side, "text": ", ".join(parts) or "nothing yet", "blames": sorted(blames)})
    blamed = {b for e in engines for b in e["blames"]}
    if "engine" in blamed:
        health = ("engine", "the engine is failing")
    elif "unclear" in blamed:
        health = ("unclear", "can't tell yet")
    elif blamed:
        health = ("worker", "only workers failed")
    elif any(e["text"] != "nothing yet" for e in engines):
        health = ("passed", "no failures")
    else:
        health = ("pending", "nothing yet")
    kinds = {}
    for row in rows:
        if row.get("kind"):
            kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
    pills = [{"kind": kind, "count": kinds[kind], "text": f"{kinds[kind]} {text}"} for kind, text in KINDS
             if kinds.get(kind)]
    return {"engines": engines, "health": {"look": health[0], "text": health[1]}, "workers": pills,
            "any": bool(engines or pills)}


def has_steps(machines):
    return any(clean(m.get("steps")) for m in machines if isinstance(m, dict))


def running_since(machines):
    """When the first worker's `running` step began: the clock a test with steps is timed from."""
    times = [when(s.get("at")) for m in machines if isinstance(m, dict) for s in clean(m.get("steps"))
             if s["step"] == "running" and s.get("state") in ("active", "passed")]
    times = [t for t in times if t]
    return min(times) if times else None


def goes_at(match):
    """When a failed start deletes itself (docs/match-schema.md, *Expiry*); step 6 does the deleting, from the same
    setting."""
    if match.state != "failed_prechecks" or match.finished_at is None:
        return None
    return match.finished_at + timedelta(hours=settings.NYAYO_FAILED_PRECHECKS_HOURS)


def goes_in(match, now):
    """`in 47 h 12 min`, or `now` when it is due; None when it doesn't expire."""
    at = goes_at(match)
    if at is None:
        return None
    left = (at - now).total_seconds()
    if left <= 0:
        return "now"
    return "in under a minute" if left < 60 else "in " + length(left // 60 * 60)
