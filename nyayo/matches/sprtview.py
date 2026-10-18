"""What an SPRT's page says about itself: the figures a tester reads off, and the same in a block to paste.

The block is the layout testers share (fishtest, OpenBench's own pages):

    Elo   | 88.74 +- 67.44 (95%)
    SPRT  | 8.0+0.00s Threads=1 Hash=8MB
    LLR   | 0.31 (-2.94, 2.94) [0.00, 5.00]
    Games | N: 72 W: 33 L: 15 D: 24
    Penta | [1, 5, 11, 13, 6]

Elo and its error are the producer's (`stats`) when it sent them, nyayo's own from the counts otherwise.
"""

from . import pgnview, stats

# the options a tester wants first, in this order
FIRST_OPTIONS = ("Threads", "Hash")


def tc_text(tc):
    """`8+0.08` as `8.0+0.08s`; anything that isn't base+increment (moves per period, a word) as it came."""
    control = pgnview.parse_time_control(tc)
    if control is None or not tc or "/" in str(tc):
        return str(tc) if tc else ""
    base, increment = control
    return f"{base:.1f}+{increment:.2f}s"


def options_text(options):
    """`Threads=1 Hash=8MB`: the usual two first, a hash size in megabytes, the rest after in name order."""
    if not isinstance(options, dict):
        return ""
    names = [n for n in FIRST_OPTIONS if n in options] + sorted(n for n in options if n not in FIRST_OPTIONS)
    parts = []
    for name in names:
        value = str(options[name])
        parts.append(f"{name}={value}MB" if name == "Hash" and value.isdigit() else f"{name}={value}")
    return " ".join(parts)


def figure(value, places=2, sign=False):
    if value is None:
        return "-"
    return f"{value:+.{places}f}" if sign else f"{value:.{places}f}"


def elo_figures(match, row):
    """(elo, error, whose) for the match: the producer's when it sent them, else nyayo's from the counts."""
    produced = match.stats if isinstance(match.stats, dict) else None
    if produced:
        return produced.get("elo"), produced.get("error"), match.producer_name or "the producer"
    if row is None:
        return None, None, "nyayo"
    rating = stats.rating(row.wins, row.draws, row.losses, row.penta)
    return rating.elo, rating.error, "nyayo"


def clipboard(match, row, sprt, tc, options):
    """The five lines, aligned on the bar. Whatever is missing is a dash."""
    elo, error, _ = elo_figures(match, row)
    llr = f"{figure(sprt.get('llr'))} ({figure(sprt.get('lower'))}, {figure(sprt.get('upper'))})" \
          f" [{figure(sprt.get('elo0'))}, {figure(sprt.get('elo1'))}]"
    games = (row.games, row.wins, row.losses, row.draws) if row else (0, 0, 0, 0)
    penta = ", ".join(str(n) for n in row.penta) if row and row.penta else "-"
    setup = " ".join(part for part in (tc_text(tc), options_text(options)) if part) or "-"
    lines = [
        ("Elo", f"{figure(elo)} +- {figure(error)} (95%)"),
        ("SPRT", setup),
        ("LLR", llr),
        ("Games", "N: {} W: {} L: {} D: {}".format(*games)),
        ("Penta", f"[{penta}]"),
    ]
    return "\n".join(f"{name:<5} | {text}" for name, text in lines)


def engine_line(name, info, commit):
    """`feature-x @ a1b2c3d4 · bench 2301556` for an engine of the test."""
    info = info or {}
    parts = [info.get("branch") or name]
    if commit:
        parts[0] += f" @ {commit[:8]}"
    if info.get("bench") is not None:
        parts.append(f"bench {info['bench']}")
    return " · ".join(parts)
