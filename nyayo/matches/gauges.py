"""Half-circle gauges for an SPRT, after election-night dials: where the LLR stands between its bounds, and where the
Elo estimate stands on a scale with its 95% interval.

Both show a position, never a probability. The geometry is worked out here, so the page and the match list draw the
same dials; the templates only print the numbers. The needles are drawn pointing straight up and turned about the hub
(`angle`, in degrees from -90 at the left to +90 at the right), so a page that is redrawn can turn one smoothly.
"""

import math

CX, CY = 160, 165
VIEW = "-22 -18 364 204"          # room round the dial for the bounds' labels
VIEW_SMALL = "0 0 320 178"

# the five zones of the SPRT gauge, from the lower bound to the upper
ZONE_LABELS = ("NEAR FAIL", "LEANS FAIL", "OPEN", "LEANS PASS", "NEAR PASS")
ZONE_WORDS = ("Near failing", "Leaning fail", "Open", "Leaning pass", "Near passing")

# the Elo scale reaches this far each way, the first that holds the interval and both bounds with a tenth to spare
ELO_REACHES = (5, 10, 20, 50, 100, 200, 500, 1000)
ELO_MARGIN = 1.1
ELO_TICKS = 11

MINUS = "\N{MINUS SIGN}"


def n(value):
    return f"{value:.1f}"


def clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def point(fraction, radius):
    """The point at `fraction` (0 left, 1 right) of the half circle, `radius` out from the hub."""
    angle = math.pi * (1 - fraction)
    return CX + radius * math.cos(angle), CY - radius * math.sin(angle)


def wedge(f0, f1, inner, outer):
    """A path for the band of the half circle between two fractions and two radii."""
    a, b, c, d = point(f0, outer), point(f1, outer), point(f1, inner), point(f0, inner)
    # Every wedge lies on one half circle, so its arcs never exceed 180 degrees: the
    # large-arc flag is always 0 (set, the browser draws the long way round).
    return (f"M{n(a[0])} {n(a[1])} A{outer} {outer} 0 0 1 {n(b[0])} {n(b[1])} "
            f"L{n(c[0])} {n(c[1])} A{inner} {inner} 0 0 0 {n(d[0])} {n(d[1])} Z")


def needle(radius, half_width):
    """The needle pointing straight up from the hub, for turning."""
    return f"{CX},{CY - radius} {CX + half_width},{CY} {CX - half_width},{CY}"


def angle_of(fraction):
    return round((fraction - 0.5) * 180, 2)


def signed(value, places=2):
    """-2.94 as −2.94 with a real minus sign, 2.94 as 2.94."""
    return f"{value:.{places}f}".replace("-", MINUS)


def elo_text(value):
    """+191 or +39.4 or −4.6: 100 and over whole, below that one decimal, always signed, a real minus."""
    shown = round(abs(value), 1)
    if shown >= 100:
        digits = f"{round(abs(value)):d}"
    else:
        digits = f"{shown:.1f}"
    if shown == 0:
        return digits
    return ("+" if value > 0 else MINUS) + digits


def bound_text(value):
    """An Elo bound as a plain number: 0, 50, −3, 2.5."""
    return (MINUS if value < 0 else "") + f"{abs(value):g}"


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def games_text(games):
    return f"{games:,} game{'' if games == 1 else 's'}"


def zone_of(llr, lower, upper):
    """(zone 0-4, fraction 0-1): where the LLR is between its bounds in fifths, clamped to the ends."""
    fraction = clamp((llr - lower) / (upper - lower))
    return min(4, int(fraction * 5)), fraction


def tone_of(zone, result):
    """pass, fail or open: a verdict says which; a running test leans by its zone."""
    if result == "passed":
        return "pass"
    if result == "failed":
        return "fail"
    return "pass" if zone >= 3 else "fail" if zone <= 1 else "open"


def llr_gauge(llr, lower, upper, result=None, games=0, small=False):
    """The SPRT gauge as numbers for a template, or None without an LLR and two ordered bounds."""
    if not (finite(llr) and finite(lower) and finite(upper)) or not lower < upper:
        return None
    outer, inner = (150, 52) if small else (145, 46)
    zone, fraction = zone_of(llr, lower, upper)
    result = result if result in ("passed", "failed") else None
    tone = tone_of(zone, result)
    word = "Passed" if result == "passed" else "Failed" if result == "failed" else ZONE_WORDS[zone]
    if result:
        bound = upper if result == "passed" else lower
        line = f"LLR {signed(llr)}, past {signed(bound)} · {games_text(games)}"
    else:
        # it is heading for the bound on its side of zero
        line = f"LLR {signed(llr)} of {signed(upper if llr >= 0 else lower)} · {games_text(games)}"
    zones = []
    for i in range(5):
        mx, my = point((i + 0.5) / 5, inner + (outer - inner) * 0.6)
        zones.append({"index": i, "d": wedge(i / 5, (i + 1) / 5, inner, outer), "label": ZONE_LABELS[i],
                      "lx": n(mx), "ly": n(my + 3)})
    return {
        "zones": zones, "zone": zone, "tone": tone, "word": word, "line": line, "llr": signed(llr),
        "lower_text": signed(lower), "upper_text": signed(upper), "result": result,
        "hub": wedge(0, 1, 0, inner - (2 if small else 1)), "hub_radius": 16 if small else 7,
        "needle": needle(outer - (6 if small else 4), 13 if small else 5), "angle": angle_of(fraction),
        "view": VIEW_SMALL if small else VIEW,
        "aria": f"SPRT: {word}, {line}" if not small else f"SPRT {word}, LLR {signed(llr)}",
        "full_aria": f"SPRT: {word}, {line}, between failing at {signed(lower)} and passing at {signed(upper)}",
    }


def elo_reach(elo, error, elo0, elo1):
    """How far the Elo scale goes each way: the first round number that holds the estimate with its interval and both
    bounds, with a tenth to spare; the last when nothing does."""
    need = max(abs(elo) + (error or 0), abs(elo0 or 0), abs(elo1 or 0)) * ELO_MARGIN
    return next((r for r in ELO_REACHES if r >= need), ELO_REACHES[-1])


def elo_gauge(elo, error, elo0, elo1):
    """The Elo gauge as numbers for a template, or None without an estimate."""
    if not finite(elo):
        return None
    error = error if finite(error) and error >= 0 else None
    elo0 = elo0 if finite(elo0) else None
    elo1 = elo1 if finite(elo1) else None
    reach = elo_reach(elo, error, elo0, elo1)
    outer, inner = 145, 46

    def at(value):
        return clamp((value + reach) / (2 * reach))

    ticks = []
    for i in range(-5, 6):
        value = reach * i // 5
        fraction = at(value)
        o, s, label = point(fraction, outer), point(fraction, outer - 7), point(fraction, outer - 19)
        ticks.append({"x1": n(o[0]), "y1": n(o[1]), "x2": n(s[0]), "y2": n(s[1]), "lx": n(label[0]),
                      "ly": n(label[1] + 3), "text": "0" if value == 0 else ("+" if value > 0 else MINUS) + str(abs(value)),
                      "tone": "pass" if value > 0 else "fail" if value < 0 else "zero"})
    z0, z1 = point(0.5, inner), point(0.5, outer)
    bounds = []
    for name, value, anchor, tone in (("H₀", elo0, "end", "base"), ("H₁", elo1, "start", "pass")):
        if value is None:
            continue
        f = at(value)
        a, o, label = point(f, inner), point(f, outer + 3), point(f, outer + 9)
        bounds.append({"x1": n(a[0]), "y1": n(a[1]), "x2": n(o[0]), "y2": n(o[1]),
                       "lx": n(label[0] + (-2 if anchor == "end" else 2)), "ly": n(label[1]),
                       "text": f"{name} {bound_text(value)}", "anchor": anchor, "tone": tone})
    low = high = band = None
    if error is not None:
        low, high = elo - error, elo + error
        if at(high) - at(low) > 0.002:
            band = wedge(at(low), at(high), inner, outer)
    big = f"{elo_text(elo)} Elo"
    small = f"{elo_text(low)} to {elo_text(high)} · 95%" if error is not None else "no interval yet"
    tone = "pass" if elo > 0 else "fail" if elo < 0 else "zero"
    aria = f"Elo {elo_text(elo)}" + (f", 95% from {elo_text(low)} to {elo_text(high)}" if error is not None else "")
    if elo0 is not None and elo1 is not None:
        aria += f", bounds {bound_text(elo0)} and {bound_text(elo1)}"
    return {
        "reach": reach, "ring": wedge(0, 1, inner, outer), "band": band, "ticks": ticks, "bounds": bounds,
        "zero": {"x1": n(z0[0]), "y1": n(z0[1]), "x2": n(z1[0]), "y2": n(z1[1])},
        "hub": wedge(0, 1, 0, inner - 1), "hub_radius": 7, "needle": needle(outer - 4, 5), "angle": angle_of(at(elo)),
        "big": big, "small": small, "tone": tone, "aria": aria, "view": VIEW,
    }
