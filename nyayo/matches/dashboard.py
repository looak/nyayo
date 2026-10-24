"""The dashboard (`/`, the home page): what nyayo holds and what is being played, at a glance, in Pi-hole's way.

Its figures over the games kept come from the index database's cards, grouped in SQL, never from the batches
themselves: a few scans of one table, each well under a second for a hundred thousand cards.
"""

import math
from datetime import datetime, timedelta, timezone

from django.db.models import Count, Sum
from django.shortcuts import render
from django.utils import timezone as django_timezone

from . import cards, catalogue, stats
from .gameslist import ENDINGS, LOST, RULES, STARTPOS, WON, hex_key, signed
from .models import Batch, Game, Match, Upload
from .pages import is_silent, match_queryset, running_cards, silent_cutoff

WEEKS = 53
CELL, GAP = 11, 3
# GitHub's greens, from no games to the most
LEVELS = ("#2a3036", "#0e4429", "#006d32", "#26a641", "#39d353")
RECENT = 8
OPENINGS = 8
# the donuts' colours: Pi-hole's, then greys
PALETTE = ("#3c8dbc", "#0b8ea8", "#12895a", "#d98a12", "#c64a3a", "#7b5ea7", "#6c757d", "#4b545c")
OUTCOME_COLOURS = {"win": "#2ecc71", "draw": "#9aa4ad", "loss": "#e5533d", "unknown": "#4b545c"}


def calendar(now):
    """Games kept by the day they were played, the last WEEKS weeks as GitHub draws a year of commits: a column a week
    from Monday, the darker green the more games. Days are UTC."""
    today = now.date()
    first = today - timedelta(days=today.weekday() + 7 * (WEEKS - 1))
    epoch = int(datetime(first.year, first.month, first.day, tzinfo=timezone.utc).timestamp())
    counts = dict(catalogue.index().execute(
        "SELECT (played_at - ?) / 86400, count(*) FROM card WHERE played_at >= ? GROUP BY 1", (epoch, epoch)))
    most = max(counts.values(), default=0)
    left, top = 28, 16
    days, months, seen = [], [], None
    for offset in range((today - first).days + 1):
        day = first + timedelta(days=offset)
        n = counts.get(offset, 0)
        level = 0 if not n else min(4, 1 + int(3 * n / most))
        week, weekday = divmod(offset, 7)
        x, y = left + week * (CELL + GAP), top + weekday * (CELL + GAP)
        days.append({"x": x, "y": y, "fill": LEVELS[level], "tip": f"{n:,} game{'s' if n != 1 else ''} on {day:%a %d %b %Y}"})
        if day.day <= 7 and weekday == 0 and day.month != seen:
            months.append({"x": x, "label": f"{day:%b}"})
            seen = day.month
    return {"days": days, "months": months, "total": sum(counts.values()), "levels": LEVELS,
            "width": left + WEEKS * (CELL + GAP), "height": top + 7 * (CELL + GAP),
            "weekdays": [{"y": top + i * (CELL + GAP) + CELL - 2, "label": label}
                         for i, label in ((0, "Mon"), (2, "Wed"), (4, "Fri"))]}


def donut(parts):
    """[(label, count, colour)] as the segments of a ring 100 long (stroke-dasharray), the biggest first."""
    total = sum(n for _, n, _ in parts)
    out, start = [], 0.0
    for label, n, colour in parts:
        if not n:
            continue
        share = 100 * n / total
        out.append({"label": label, "count": n, "colour": colour, "share": f"{share:.1f}%",
                    "dash": f"{share:.3f} {100 - share:.3f}", "offset": f"{25 - start:.3f}"})
        start += share
    return {"parts": out, "total": total}


def endings():
    """How the kept games ended, by the cards' ended (rules) and ending columns, the six commonest and the rest."""
    counted = {}
    for ended, ending, n in catalogue.index().execute(
            "SELECT ended, ending, count(*) FROM card GROUP BY ended, ending"):
        label = ENDINGS.get(cards.ENDINGS[ending]) if ending is not None else None
        if label == "Rules":
            label = RULES.get(ended, "Rules")
        label = label or "Not read yet"
        counted[label] = counted.get(label, 0) + n
    ranked = sorted(counted.items(), key=lambda item: -item[1])
    parts = [(label, n, PALETTE[i]) for i, (label, n) in enumerate(ranked[:6])]
    rest = sum(n for _, n in ranked[6:])
    if rest:
        parts.append(("Other", rest, PALETTE[6]))
    return donut(parts)


def outcomes():
    """The kept games from the engine under test's side: won, drawn, lost, and those whose side isn't known."""
    won, drawn, lost, unknown = catalogue.index().execute(
        f"SELECT sum({WON}), sum(colour > 0 AND result = 3), sum({LOST}), sum(colour = 0 OR result = 0) "
        f"FROM card").fetchone()
    return donut([("Won", won or 0, OUTCOME_COLOURS["win"]), ("Drawn", drawn or 0, OUTCOME_COLOURS["draw"]),
                  ("Lost", lost or 0, OUTCOME_COLOURS["loss"]), ("Side not known", unknown or 0,
                                                                  OUTCOME_COLOURS["unknown"])])


def openings():
    """The book positions most games started from (a game from the standard position: the one after 16 plies), with
    how the engine under test scored from them."""
    rows = catalogue.index().execute(
        "SELECT CASE WHEN start IS NOT NULL AND start != ? THEN start ELSE opening END AS key, count(*), "
        f"sum(colour > 0 AND result > 0), sum({WON}), sum(colour > 0 AND result = 3) "
        "FROM card GROUP BY key HAVING key IS NOT NULL ORDER BY 2 DESC LIMIT ?",
        (signed(STARTPOS), OPENINGS)).fetchall()
    most = rows[0][1] if rows else 0
    out = []
    for key, n, known, won, drawn in rows:
        score = (won + drawn / 2) / known if known else None
        out.append({"key": hex_key(key), "games": n, "score": score, "width": f"{100 * n / most:.1f}",
                    "won": f"{100 * won / known:.1f}" if known else "0",
                    "drawn": f"{100 * drawn / known:.1f}" if known else "0"})
    return out


def recent_matches(cutoff):
    matches = list(match_queryset().order_by("-updated_at")[:RECENT])
    for match in matches:
        match.summary = stats.summarize(match.results.all())
        match.silent = is_silent(match, cutoff)
        produced = match.stats if isinstance(match.stats, dict) else None
        match.elo = produced.get("elo") if produced else match.summary.overall.elo
    return matches


def figures(now):
    """The four tiles: games kept, matches running, games played in the last day, and the index."""
    kept = Batch.objects.aggregate(games=Sum("games"), stored=Sum("stored_bytes"), batches=Count("pk"))
    db = catalogue.index()
    indexed = db.execute("SELECT count(*) FROM card").fetchone()[0]
    day = db.execute("SELECT count(*) FROM card WHERE played_at >= ?",
                     (int((now - timedelta(days=1)).timestamp()),)).fetchone()[0]
    running = Match.objects.filter(state="running")
    kinds = dict(running.values_list("kind").annotate(n=Count("pk")).order_by())
    games = kept["games"] or 0
    return {
        "kept": games, "batches": kept["batches"], "stored_mb": round((kept["stored"] or 0) / 1e6),
        "running": sum(kinds.values()), "kinds": sorted(kinds.items()),
        "in_play": Game.objects.filter(result="*", match__state="running").count(),
        "day": day, "per_hour": round(day / 24),
        "indexed": indexed, "index_share": 100 if not games else min(100, math.floor(100 * indexed / games)),
        "waiting": Upload.objects.filter(state="pending").count(),
        "refused": Upload.objects.filter(state="refused").count(),
    }


def dashboard(request):
    now = django_timezone.now()
    cutoff = silent_cutoff(now)
    running = running_cards(cutoff, now)
    return render(request, "matches/dashboard.html", {
        "tiles": figures(now), "calendar": calendar(now), "endings": endings(), "outcomes": outcomes(),
        "running": running, "recent": recent_matches(cutoff), "openings": openings(),
    })
