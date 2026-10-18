"""An SPRT's progress: Elo and LLR over games drawn from the snapshots, and how many more games and how much more time
the test needs to reach a bound.

The figures are the producer's where it sends them (the snapshot's LLR and `stats`); a snapshot without stats gets
nyayo's own Elo from its counts. The projection is an estimate: it takes the LLR's slope over the later half of the
games so far and runs it to the nearer bound it is heading for. LLR is not linear in games, so the farther the bound,
the rougher the answer.
"""

import math
from datetime import timedelta

from . import pgnview, stats

WIDTH, HEIGHT = 640, 200
PAD_LEFT, PAD_RIGHT, PAD_TOP, PAD_BOTTOM = 46, 12, 12, 24
# more points than this make no difference to the eye and a heavy page
MAX_POINTS = 300
# the projection needs this many snapshots in its window to call a slope
MIN_POINTS = 3


def points(snapshots):
    """Snapshots as {games, time, llr, elo, error}, in order; games where nothing changed are dropped."""
    out, last = [], None
    for snap in snapshots:
        if snap.games == last:
            continue
        last = snap.games
        elo, error = None, None
        if isinstance(snap.stats, dict):
            elo, error = snap.stats.get("elo"), snap.stats.get("error")
        else:
            rating = stats.rating(snap.wins, snap.draws, snap.losses)
            elo, error = rating.elo, rating.error
        out.append({"games": snap.games, "time": snap.received_at, "llr": snap.llr, "elo": elo, "error": error})
    return out


def projection(series, sprt, now):
    """{bound, games, seconds, at, basis} for a running test heading for a bound, or {reason} when it can't say."""
    if not isinstance(sprt, dict) or sprt.get("result"):
        return None
    lower, upper = sprt.get("lower"), sprt.get("upper")
    have = [p for p in series if p["llr"] is not None]
    if lower is None or upper is None or len(have) < MIN_POINTS:
        return {"reason": "too few reports to tell"}
    latest = have[-1]
    if latest["llr"] <= lower or latest["llr"] >= upper:
        return {"reason": "past a bound; waiting for the producer's verdict"}
    start = latest["games"] / 2
    window = [p for p in have if p["games"] >= start]
    if len(window) < MIN_POINTS:
        window = have[-MIN_POINTS:]
    slope = fit_slope([(p["games"], p["llr"]) for p in window])
    if slope is None or abs(slope) < 1e-9:
        return {"reason": "the LLR isn't moving"}
    bound, name = (upper, "upper") if slope > 0 else (lower, "lower")
    games = (bound - latest["llr"]) / slope
    span = (window[-1]["time"] - window[0]["time"]).total_seconds()
    played = window[-1]["games"] - window[0]["games"]
    seconds = games / (played / span) if span > 0 and played > 0 else None
    return {"bound": name, "bound_value": bound, "games": games, "seconds": seconds,
            "at": now + timedelta(seconds=seconds) if seconds is not None else None,
            "basis": f"the LLR's slope over the last {played} games"}


def fit_slope(pairs):
    """Least squares slope of y on x, None when x doesn't vary."""
    n = len(pairs)
    mean_x = sum(x for x, _ in pairs) / n
    mean_y = sum(y for _, y in pairs) / n
    spread = sum((x - mean_x) ** 2 for x, _ in pairs)
    if spread == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in pairs) / spread


def thin(rows):
    if len(rows) <= MAX_POINTS:
        return rows
    step = len(rows) / MAX_POINTS
    kept = [rows[int(i * step)] for i in range(MAX_POINTS - 1)]
    return kept + [rows[-1]]


def line_chart(key, label, rows, value, lines=(), band=None):
    """One metric over games as SVG-ready geometry; None before there are two points to draw.

    rows are points(); value(row) the figure or None; lines [(value, label)] horizontal marks (the bounds); band(row)
    an optional (low, high) drawn behind the line."""
    drawn = [(r["games"], value(r), band(r) if band else None) for r in thin(rows) if value(r) is not None]
    if len(drawn) < 2:
        return None
    low_x, high_x = drawn[0][0], drawn[-1][0]
    if high_x == low_x:
        high_x = low_x + 1
    ys = [v for _, v, _ in drawn] + [v for v, _ in lines]
    if band:
        # the band is wide early on; only the later half decides the scale, the rest is clipped to it
        for _, _, b in drawn[len(drawn) // 2:]:
            if b:
                ys += list(b)
    low_y, high_y = min(ys), max(ys)
    pad = (high_y - low_y) * 0.08 or 1.0
    low_y, high_y = low_y - pad, high_y + pad
    step = pgnview.nice_step(high_y - low_y)
    low_y = math.floor(low_y / step) * step
    high_y = math.ceil(high_y / step) * step

    def x(games):
        return PAD_LEFT + (games - low_x) / (high_x - low_x) * (WIDTH - PAD_LEFT - PAD_RIGHT)

    def y(v):
        v = max(low_y, min(high_y, v))
        return PAD_TOP + (high_y - v) / (high_y - low_y) * (HEIGHT - PAD_TOP - PAD_BOTTOM)

    polygon = None
    if band:
        upper = [f"{x(g):.1f},{y(b[1]):.1f}" for g, _, b in drawn if b]
        lower = [f"{x(g):.1f},{y(b[0]):.1f}" for g, _, b in reversed(drawn) if b]
        if len(upper) >= 2:
            polygon = " ".join(upper + lower)
    yticks, tick = [], low_y
    while tick <= high_y + 1e-9:
        yticks.append({"y": round(y(tick), 1), "label": pgnview.human(round(tick, 6))})
        tick += step
    count = max(1, min(8, high_x - low_x))
    xstep = max(1, math.ceil((high_x - low_x) / count))
    xticks = [{"x": round(x(n), 1), "label": pgnview.human(n)} for n in range(int(low_x), int(high_x) + 1, xstep)]
    return {
        "key": key, "label": label, "width": WIDTH, "height": HEIGHT, "left": PAD_LEFT, "right": WIDTH - PAD_RIGHT,
        "top": PAD_TOP, "bottom": HEIGHT - PAD_BOTTOM,
        "line": " ".join(f"{x(g):.1f},{y(v):.1f}" for g, v, _ in drawn), "band": polygon,
        "marks": [{"y": round(y(v), 1), "label": name} for v, name in lines if low_y <= v <= high_y],
        "yticks": yticks, "xticks": xticks,
        "last": {"x": round(x(drawn[-1][0]), 1), "y": round(y(drawn[-1][1]), 1)},
    }


def graphs(series, sprt):
    sprt = sprt if isinstance(sprt, dict) else {}
    bounds = [(sprt[k], k) for k in ("lower", "upper") if isinstance(sprt.get(k), (int, float))]
    llr = line_chart("llr", "LLR", series, lambda r: r["llr"], bounds)
    elo = line_chart("elo", "Elo", series, lambda r: r["elo"],
                     band=lambda r: (r["elo"] - r["error"], r["elo"] + r["error"])
                     if r["elo"] is not None and r["error"] is not None else None)
    return [g for g in (elo, llr) if g]
