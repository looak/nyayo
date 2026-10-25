"""The dashboard (`/`, the home page): what nyayo holds and what is being played, at a glance, in Pi-hole's way.

Its figures over the games kept come from the index database's cards, grouped in SQL, never from the batches
themselves: a few scans of one table, each well under a second for a hundred thousand cards.
"""

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.conf import settings
from django.db.models import Count, Sum
from django.db.models.functions import Coalesce
from django.shortcuts import render
from django.utils import timezone as django_timezone

from nyayo.runs.models import Run

from . import cards, catalogue, sprtview, stats
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


def file_bytes(path):
    """A SQLite file's size with its write-ahead log, 0 when it isn't there."""
    path = Path(path)
    return sum(p.stat().st_size for p in (path, path.with_name(path.name + "-wal")) if p.exists())


def size_text(count):
    return f"{count / 1e9:.1f} GB" if count >= 1e9 else f"{count / 1e6:.0f} MB"


def database():
    """The first tile: what nyayo's game database takes on disk (its database, the index and the batches), how many
    games it keeps and how many of them the index has carded."""
    kept = Batch.objects.aggregate(games=Sum("games"), stored=Sum("stored_bytes"))
    games, batches = kept["games"] or 0, kept["stored"] or 0
    index_bytes = file_bytes(settings.NYAYO_INDEX_DB)
    database_bytes = file_bytes(settings.DATABASES["default"]["NAME"])
    indexed = catalogue.index().execute("SELECT count(*) FROM card").fetchone()[0]
    return {"size": size_text(database_bytes + index_bytes + batches), "games": games,
            "parts": [("database", size_text(database_bytes)), ("index", size_text(index_bytes)),
                      ("batches", size_text(batches))],
            "indexed": 100 if not games else min(100, math.floor(100 * indexed / games)),
            "waiting": Upload.objects.filter(state="pending").count(),
            "refused": Upload.objects.filter(state="refused").count()}


def playing():
    """The second tile: the matches being played, or with none, the one played last."""
    running = Match.objects.filter(state="running")
    kinds = dict(running.values_list("kind").annotate(n=Count("pk")).order_by())
    last = None
    if not kinds:
        last = Match.objects.exclude(state="running").order_by(
            Coalesce("finished_at", "updated_at").desc()).first()
    return {"running": sum(kinds.values()), "kinds": sorted(kinds.items()),
            "in_play": Game.objects.filter(result="*", match__state="running").count(),
            "last": last, "last_at": (last.finished_at or last.updated_at) if last else None}


def latest_sprt():
    """The third tile: the SPRT that came to a verdict last, else the latest one at all, with its Elo and the faults
    of the engine under test in it (crashes, time losses, illegal moves)."""
    sprts = Match.objects.filter(kind="sprt").exclude(sprt=None).select_related("subject")
    match = (sprts.filter(sprt__result__in=["passed", "failed"]).order_by(
        Coalesce("finished_at", "updated_at").desc()).first() or sprts.order_by("-updated_at").first())
    if match is None:
        return None
    row = match.results.first()
    elo, error, _ = sprtview.elo_figures(match, row)
    faults = []
    for one, many, field in (("crash", "crashes", "crashes"), ("time loss", "time losses", "timelosses"),
                             ("illegal move", "illegal moves", "illegal")):
        n = (getattr(row, field, None) or {}).get("subject") or 0
        if n:
            faults.append(f"{n} {one if n == 1 else many}")
    return {"match": match, "result": (match.sprt or {}).get("result"), "elo": elo, "error": error,
            "llr": (match.sprt or {}).get("llr"), "faults": faults}


def github(now):
    """The fourth tile: the engine's activity on GitHub as nyayo sees it, the commits pushed in the last week that
    have a run here (the published runs), on how many branches, and when the latest push was; linked to the repository
    when NYAYO_GITHUB_REPO names it."""
    week = Run.objects.filter(state="published", published_at__gte=now - timedelta(days=7))
    found = week.aggregate(commits=Count("commit", distinct=True), branches=Count("branch", distinct=True),
                           runs=Count("pk"))
    latest = Run.objects.filter(state="published").exclude(published_at=None).order_by("-published_at").first()
    repo = settings.NYAYO_GITHUB_REPO
    links = {}
    if repo:
        links["commits"] = f"https://github.com/{repo}/commits"
        if latest and latest.commit:
            links["latest"] = f"https://github.com/{repo}/commit/{latest.commit}"
    return {**found, "latest": latest, "repo": repo, "links": links}


def figures(now):
    """The four tiles, in order: the game database, what is being played, the latest SPRT and GitHub activity."""
    return {"database": database(), "playing": playing(), "sprt": latest_sprt(), "github": github(now)}


def dashboard(request):
    now = django_timezone.now()
    cutoff = silent_cutoff(now)
    running = running_cards(cutoff, now)
    return render(request, "matches/dashboard.html", {
        "tiles": figures(now), "calendar": calendar(now), "endings": endings(), "outcomes": outcomes(),
        "running": running, "recent": recent_matches(cutoff), "openings": openings(),
    })
