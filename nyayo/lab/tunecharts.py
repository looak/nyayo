"""The geometry of a tune's charts, worked out here so the page only prints numbers (as nyayo/matches/gauges.py does for the
SPRT's dials): the outcome chart, the small chart per parameter with the outcome shaded behind it, and a parameter's range bar.

All the charts draw the outcome on one Elo scale (`EloScale`), so a shaded patch means the same in every one. The x axis of
every chart is the iterations so far, from 0 at the left edge to the newest at the right.
"""

import math

MINUS = "\N{MINUS SIGN}"

# how far a parameter moved over the last tenth of the snapshots, as a share of its range
SETTLED_BELOW = 0.01   # less than 1%: settled
SLOWING_BELOW = 0.025  # less than 2.5%: slowing, else moving

BIG = (1200, 150)
SMALL = (280, 84)
PAD = 4  # room above and below a plot, in viewBox units


def n(value):
    return f"{value:.1f}"


def signed(value, places=1):
    text = f"{abs(value):.{places}f}"
    return ("+" if value > 0 else MINUS if value < 0 else "") + text


def lately_word(fraction):
    """(word, tone) for how far a parameter moved lately: settled, slowing or moving."""
    if fraction < SETTLED_BELOW:
        return "Settled", "settled"
    if fraction < SLOWING_BELOW:
        return "Slowing", "slowing"
    return "Moving", "moving"


def step_now(c_value, gamma, is_float, iteration):
    """The size of the nudge a pair plays now (spsa_utils: c_value / iteration ** gamma, never below 0.5 for an integer)."""
    c = c_value / max(1, iteration) ** gamma
    return max(c, 0.0 if is_float else 0.5)


def figure(value, is_float, small_int=False):
    """A value as the page shows it: an integer whole, a float to 3 places, and for an integer with a small range the
    float SPSA keeps beside it (the option is the rounded one)."""
    if is_float:
        return f"{value:.3f}".rstrip("0").rstrip(".").replace("-", MINUS) or "0"
    whole = str(int(round(value))).replace("-", MINUS)
    return f"{whole} ({value:.2f})".replace("-", MINUS) if small_int else whole


def range_bar(low, high, start, now):
    """Where the start tick, the current dot and the moved span sit on a parameter's range, as percentages."""
    span = (high - low) or 1.0

    def at(x):
        return max(0.0, min(100.0, (x - low) / span * 100))

    return {"start": n(at(start)), "now": n(at(now)), "left": n(min(at(start), at(now))), "width": n(abs(at(now) - at(start)))}


class EloScale:
    """The Elo axis the outcome is drawn on: from just under the lowest whisker to just over the highest, with zero inside it,
    and gridlines at a round step."""

    def __init__(self, checks):
        lows = [0.0] + [c["elo"] - (c["error"] or 0) for c in checks]
        highs = [0.0] + [c["elo"] + (c["error"] or 0) for c in checks]
        low, high = min(lows), max(highs)
        step = self.step_for(high - low)
        self.step = step
        self.low = math.floor(low / step) * step
        self.high = math.ceil(high / step) * step
        if self.high == self.low:
            self.high += step

    @staticmethod
    def step_for(span):
        for step in (1, 2, 5, 10, 20, 50, 100, 200, 500):
            if span / step <= 6:
                return step
        return 1000

    def y(self, elo, height):
        return n(height - PAD - (elo - self.low) / (self.high - self.low) * (height - 2 * PAD))

    def grid(self, height):
        out, value = [], self.low
        while value <= self.high + 1e-9:
            out.append({"y": self.y(value, height), "zero": value == 0, "text": signed(value, 0) if value else "0"})
            value += self.step
        return out


def x_of(iteration, so_far, width):
    return n(iteration / so_far * width) if so_far else "0.0"


def outcome_chart(checks, so_far, scale):
    """The big chart: the finished checks as a line with its area to zero, a dot and a 95% whisker each. `checks` are dicts
    with iteration, elo and error. Returns None while there is no finished check."""
    if not checks:
        return None
    width, height = BIG
    points = [f"{x_of(c['iteration'], so_far, width)},{scale.y(c['elo'], height)}" for c in checks]
    zero = scale.y(0, height)
    last = checks[-1]
    return {
        "width": width, "height": height,
        "line": " ".join(points),
        "area": f"{x_of(checks[0]['iteration'], so_far, width)},{zero} " + " ".join(points) + f" {x_of(last['iteration'], so_far, width)},{zero}",
        "dots": [{"x": x_of(c["iteration"], so_far, width), "y": scale.y(c["elo"], height),
                  "y1": scale.y(c["elo"] - c["error"], height) if c["error"] else None,
                  "y2": scale.y(c["elo"] + c["error"], height) if c["error"] else None,
                  "text": f"{c['iteration']:,}: {signed(c['elo'])} " + (f"± {c['error']:.1f}" if c["error"] else "(no interval: every game the same)")}
                 for c in checks],
        "grid": scale.grid(height),
        "labels": [{"at": n(c["iteration"] / so_far * 100) if so_far else "0.0", "text": f"{c['iteration']:,}"} for c in checks],
        "now": f"{signed(last['elo'])} Elo " + (f"± {last['error']:.1f}" if last["error"] else "(no interval)") + f" at {last['iteration']:,}",
        "positive": last["elo"] - (last["error"] or 0) > 0,
        "aria": f"Outcome against the start: {signed(last['elo'])} Elo at iteration {last['iteration']:,}, after {len(checks)} checks",
    }


def parameter_chart(rows, start, checks, so_far, scale):
    """A small chart of one parameter: its value line over the snapshots (rows of (iteration, value)) on a scale of its own, the
    start dashed, and the outcome shaded behind it on the shared Elo scale."""
    width, height = SMALL
    values = [v for _, v in rows] + [start]
    low, high = min(values), max(values)
    pad = (high - low) * 0.15 or abs(high or 1) * 0.02 or 0.5

    def y(value):
        return n(height - PAD - (value - (low - pad)) / (high - low + 2 * pad) * (height - 2 * PAD))

    out = {"width": width, "height": height, "start_y": y(start),
           "line": " ".join(f"{x_of(i, so_far, width)},{y(v)}" for i, v in rows),
           "zero_y": scale.y(0, height), "area": ""}
    if checks:
        zero = scale.y(0, height)
        points = [f"{x_of(c['iteration'], so_far, width)},{scale.y(c['elo'], height)}" for c in checks]
        out["area"] = f"{x_of(checks[0]['iteration'], so_far, width)},{zero} " + " ".join(points) + f" {x_of(checks[-1]['iteration'], so_far, width)},{zero}"
    return out
