"""The pages: the match list, a match, and a game, live."""

from datetime import timedelta

import chess
import chess.svg
from django.conf import settings
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string

from django.utils import timezone

from . import games, pgnview, progress, sprtview, stats, timing
from .models import KINDS, STATES, Engine, Game, Match

OUTCOMES = {("1-0", "white"): "win", ("0-1", "black"): "win", ("0-1", "white"): "loss", ("1-0", "black"): "loss",
            ("1/2-1/2", "white"): "draw", ("1/2-1/2", "black"): "draw"}
RESULT_VIEWS = ("cards", "list")
VIEW_COOKIE = "nyayo_results_view"
STATE_ORDER = {"playing": 0, "unfinished": 1, "scheduled": 2, "finished": 3}


def match_queryset():
    return Match.objects.select_related("subject", "machine").prefetch_related("results__engine")


def engine_label(engine):
    return f"{engine.name} {engine.version}".strip()


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def format_elo(value):
    if value is None:
        return "-"
    return f"{value:+.1f}"


def format_penta(penta):
    return " ".join(str(n) for n in penta) if penta else "-"


def summary_clipboard_text(match, subject_name, summary, settings):
    date = (match.finished_at or match.updated_at or match.started_at or match.received_at)
    lines = [
        f"Match\t{match.name or match.pk}",
        f"Subject\t{subject_name}",
        f"Games\t{summary.performance_games}",
        f"Time control\t{settings.get('tc', '-')}",
        f"Date\t{date:%Y-%m-%d %H:%M UTC}",
        "",
        "Engine\tPenta\tStrength\tElo",
    ]
    for row in summary.rows:
        result = row.result
        strength = "-"
        if isinstance(result.rating, dict) and result.rating.get("value") is not None:
            strength = f"{result.rating['value']:.0f}"
        lines.append(
            f"{result.engine}\t{format_penta(result.penta)}\t{strength}\t{format_elo(row.rating.elo)}"
        )
    lines.extend([
        "",
        f"Subject Elo\t{format_elo(summary.overall.elo)}",
    ])
    return "\n".join(lines)


def silent_cutoff(now=None):
    """Running matches last reported before this are silent."""
    return (now or timezone.now()) - timedelta(minutes=settings.NYAYO_SILENT_AFTER)


def is_silent(match, cutoff):
    return match.state == "running" and match.updated_at < cutoff


def sprt_view(match):
    data = match.sprt if isinstance(match.sprt, dict) else None
    if not data:
        return None
    llr = number(data.get("llr"))
    lower = number(data.get("lower"))
    upper = number(data.get("upper"))

    values = [v for v in (llr, lower, upper) if v is not None]
    if len(values) >= 2:
        low, high = min(values), max(values)
        if low == high:
            low, high = low - 1.0, high + 1.0
    else:
        low, high = -3.0, 3.0

    def pct(value):
        if value is None:
            return None
        return max(0.0, min(100.0, (value - low) / (high - low) * 100.0))

    produced = match.stats if isinstance(match.stats, dict) else None
    return {
        "stats": produced, "producer": match.producer_name or "the producer",
        "llr": llr, "lower": lower, "upper": upper, "result": data.get("result"),
        "alpha": number(data.get("alpha")), "beta": number(data.get("beta")),
        "elo0": number(data.get("elo0")), "elo1": number(data.get("elo1")), "model": data.get("model"),
        "range_low": low, "range_high": high,
        "llr_pct": pct(llr), "lower_pct": pct(lower), "upper_pct": pct(upper),
    }


def sprt_page(match, sprt, projection):
    """What the SPRT page adds: the figures at the top, the test's settings, both engines and the block to paste."""
    row = match.results.first()
    elo, error, whose = sprtview.elo_figures(match, row)
    options = (match.subject_info or {}).get("options")
    tc = (match.settings or {}).get("tc")
    base = {"name": row.engine.name, "info": row.info, "commit": row.engine.commit} if row else None
    return {
        "row": row, "elo": elo, "elo_text": sprtview.figure(elo, sign=True), "error": error, "elo_by": whose,
        "tc_text": sprtview.tc_text(tc), "options_text": sprtview.options_text(options),
        "clipboard": sprtview.clipboard(match, row, sprt, tc, options),
        "subject_line": sprtview.engine_line(match.subject.name, match.subject_info, match.subject.commit),
        "base_line": sprtview.engine_line(base["name"], base["info"], base["commit"]) if base else "",
        "projection": projection, "penta_rows": penta_rows(row), "faults": faults(row),
    }


PENTA_LABELS = ("LL", "LD", "DD", "DW", "WW")


def penta_rows(row):
    """The pentanomial as columns with each one's share of the pairs, None before it has any."""
    if not row or not row.penta or not sum(row.penta):
        return None
    pairs = sum(row.penta)
    return [{"label": label, "count": count, "share": f"{count / pairs:.1%}"}
            for label, count in zip(PENTA_LABELS, row.penta)]


def faults(row):
    """Time losses, crashes and illegal moves per side, only those that happened."""
    if not row:
        return []
    out = []
    for label, sides in (("time losses", row.timelosses), ("crashes", row.crashes), ("illegal moves", row.illegal)):
        sides = sides or {}
        if sides.get("subject") or sides.get("opponent"):
            out.append({"label": label, "subject": sides.get("subject", 0), "opponent": sides.get("opponent", 0)})
    return out


def stored_games(match):
    """Games kept as data for this match, in batches."""
    from django.db.models import Sum

    return match.batches.aggregate(total=Sum("games"))["total"] or 0


def sprt_progress(match):
    """The SPRT card's graphs and projection, from the snapshots; empty for a match that isn't an SPRT."""
    if not isinstance(match.sprt, dict):
        return {}
    series = progress.points(match.snapshots.all())
    running = match.state == "running"
    return {"graphs": progress.graphs(series, match.sprt),
            "projection": progress.projection(series, match.sprt, timezone.now()) if running else None}


def matches_list(request):
    matches = match_queryset().order_by("-updated_at")
    kind, state, engine = request.GET.get("kind", ""), request.GET.get("state", ""), request.GET.get("engine", "")
    if kind in KINDS:
        matches = matches.filter(kind=kind)
    cutoff = silent_cutoff()
    if state == "silent":
        matches = matches.filter(state="running", updated_at__lt=cutoff)
    elif state in STATES:
        matches = matches.filter(state=state)
    if engine.isdigit():
        matches = matches.filter(subject_id=int(engine))
    page = Paginator(matches, 50).get_page(request.GET.get("page"))
    for match in page.object_list:
        match.summary = stats.summarize(match.results.all())
        match.silent = is_silent(match, cutoff)
    return render(request, "matches/matches_list.html", {
        "page": page, "kind": kind, "kinds": KINDS, "state": state, "states": STATES + ["silent"], "engine": engine,
        "engines": Engine.objects.filter(matches_as_subject__isnull=False).distinct(),
    })


def sort_key(entry):
    """Playing, cut off and scheduled games in play order; finished ones newest first."""
    number = entry["number"]
    order = (0, int(number)) if number.isdigit() else (1, number)
    if entry["state"] == "finished" and number.isdigit():
        order = (0, -int(number))
    return STATE_ORDER[entry["state"]], order


def game_entries(match):
    """Every game of a match as the page shows it.

    The runner's schedule and nyayo's own index of PGNs each know part of this. A game's PGN, once it has a result, is
    the last word; a game of a match that isn't running is never live, it was cut off."""
    schedule = {str(entry.get("number")): entry for entry in match.schedule}
    stored = {game.game: game for game in match.games.all()}
    names = opponent_names(match)
    entries = [build_entry(match, number, schedule.get(number, {}), stored.get(number), names)
               for number in schedule.keys() | stored.keys()]
    entries.sort(key=sort_key)
    return entries


def opponent_names(match):
    return {r.opponent: engine_label(r.engine) for r in match.results.all()}


def game_entry(match, number):
    """One game as game_entries() shows it, without building the rest; None when nothing is known of it."""
    entry = next((e for e in match.schedule if str(e.get("number")) == number), None)
    row = Game.objects.filter(match=match, game=number).first()
    if entry is None and row is None:
        return None
    return build_entry(match, number, entry or {}, row, opponent_names(match))


def build_entry(match, number, entry, row, names):
    result = None
    if row is not None and not row.live:
        state, result = "finished", row.result
    elif entry.get("state") == "finished":
        state, result = "finished", entry.get("result")
    elif row is not None or entry.get("state") == "playing":
        state = "playing" if match.state == "running" else "unfinished"
    else:
        state = entry.get("state") or "scheduled"
    colour = entry.get("subject_colour") or (row.colour if row else "")
    opponent = entry.get("opponent") or (row.opponent if row else "")
    return {
        "number": number, "round": entry.get("round"), "opponent": opponent,
        "opponent_name": names.get(opponent, opponent), "colour": colour, "state": state, "result": result,
        "outcome": OUTCOMES.get((result, colour)),
        "termination": entry.get("termination") or (row.termination if row else ""),
        "fault": entry.get("fault"), "has_pgn": row is not None,
    }


def match_detail(request, match_id):
    match = get_object_or_404(match_queryset(), pk=match_id)
    excluded = {o for o in request.GET.getlist("exclude") if o}
    entries = game_entries(match)
    groups = {name: [e for e in entries if e["state"] == name] for name in STATE_ORDER}
    total = (match.progress or {}).get("total") or len(match.schedule) or None
    durations = timing.game_durations(
        match.games.exclude(result="*").values_list("started_at", "finished_at"))
    # how the results are shown: the choice in the url, else the one made last time, kept in a cookie
    chosen = request.GET.get("view")
    view = chosen if chosen in RESULT_VIEWS else request.COOKIES.get(VIEW_COOKIE)
    view = view if view in RESULT_VIEWS else RESULT_VIEWS[0]
    summary = stats.summarize(match.results.all(), excluded)
    if not match.schedule and total is None:
        # no schedule to count (an SPRT): the games played are the results'
        played = max(len(groups["finished"]), stats.summarize(match.results.all()).overall.games)
    else:
        played = len(groups["finished"])
    subject_name = engine_label(match.subject)
    context = {
        "view": view, "match": match, "silent": is_silent(match, silent_cutoff()), "silent_minutes": settings.NYAYO_SILENT_AFTER, "boards": live_boards(match, groups["playing"]),
        "summary": summary, "excluded": excluded,
        "summary_clipboard": summary_clipboard_text(match, subject_name, summary, match.settings or {}),
        "sprt": sprt_view(match), **sprt_progress(match),
        "timing": timing.summarize(
            match.state, match.started_at or match.received_at, match.finished_at, match.updated_at, total,
            played, len(groups["playing"]), durations, (match.settings or {}).get("concurrency"),
            timezone.now()),
        "groups": groups, "entries": entries, "refresh": match.state == "running", "settings": match.settings,
        "host": match.host, "subject_name": subject_name, "stored_games": stored_games(match),
        "producer_url": (match.producer or {}).get("url"),
    }
    template = "matches/match_detail.html"
    if context["sprt"]:
        # an SPRT is a test of one change, not a field of opponents: its own page
        context.update(sprt_page(match, context["sprt"], context["projection"]))
        template = "matches/sprt_detail.html"
    response = render(request, template, context)
    if chosen in RESULT_VIEWS:
        response.set_cookie(VIEW_COOKIE, chosen, max_age=365 * 24 * 3600, samesite="Lax")
    return response


def live_boards(match, playing):
    """The boards of the games being played, as they stand, for the match page to show at a glance."""
    boards = []
    for entry in playing:
        raw = games.read_pgn(match.pk, entry["number"])
        parsed = pgnview.load(raw) if raw else None
        if parsed is None:
            continue
        plies = len(parsed.moves)
        orientation = chess.BLACK if entry["colour"] == "black" else chess.WHITE
        boards.append({"entry": entry, "plies": plies, "last": parsed.moves[-1] if parsed.moves else None,
                       "white": parsed.headers.get("White", ""), "black": parsed.headers.get("Black", ""),
                       "svg": pgnview.board_svg(parsed, plies, orientation, size=260)})
    return boards


def game_context(match, game, ply=None):
    """What the game page and its live fragment show, or None when nothing is known of the game."""
    raw = games.read_pgn(match.pk, game)
    entry = game_entry(match, game)
    if raw is None:
        return None if entry is None else {"match": match, "game": game, "entry": entry, "waiting": True}
    parsed = pgnview.load(raw)
    if parsed is None:
        raise Http404("no game in the PGN")
    plies = len(parsed.moves)
    ply = plies if ply is None else max(0, min(ply, plies))
    colour = (entry or {}).get("colour") or ""
    live = parsed.result == "*" and match.state == "running"
    bottom = "black" if colour == "black" else "white"

    board, last = pgnview.position(parsed, ply)
    control = pgnview.parse_time_control(parsed.headers.get("TimeControl") or (match.settings or {}).get("tc"))
    left = pgnview.clocks(parsed.moves, control, ply)
    latest = pgnview.last_moves(parsed.moves, ply)
    # only the end of a live game has someone thinking; a move being looked back at has nobody
    to_move = "white" if board.turn == chess.WHITE else "black"
    running = to_move if live and ply == plies else None
    sides = {}
    for side in ("white", "black"):
        sides[side] = {"colour": side, "name": parsed.headers.get(side.capitalize(), ""), "subject": colour == side,
                       "clock": left[side] if left else None, "last": latest[side],
                       "to_move": to_move == side and ply == plies and parsed.result == "*"}
    top = "white" if bottom == "black" else "black"
    return {
        "match": match, "game": game, "entry": entry, "headers": parsed.headers, "result": parsed.result,
        "live": live, "cut_off": parsed.result == "*" and not live, "plies": plies, "ply": ply,
        "fen": board.fen(), "pgn_text": pgnview.plain_pgn(raw),
        "board": chess.svg.board(board, orientation=chess.BLACK if bottom == "black" else chess.WHITE, lastmove=last,
                                 check=board.king(board.turn) if board.is_check() else None, size=720),
        "rows": pgnview.pairs(parsed.moves), "graphs": pgnview.graphs(parsed.moves, ply, control), "at_end": ply == plies,
        "move": parsed.moves[ply - 1] if ply else None, "subject_colour": colour,
        "sides": [sides["white"], sides["black"]], "top": sides[top], "bottom": sides[bottom],
        "control": control, "running": running or "", "age_ms": games.age_ms(match.pk, game) if running else 0,
        "has_clocks": left is not None,
        "has_detail": any(m.nodes is not None or m.nps is not None or m.pv for m in parsed.moves),
    }


def version_token(match_id, game, state, ply):
    """What a live page has seen of a game: the PGN's fingerprint, the match's state (a stopped match turns a live
    game into a cut off one without the PGN changing) and the move being looked at."""
    return f"{games.stamp(match_id, game)}.{state}.{'' if ply is None else ply}"


def requested_ply(request):
    value = request.GET.get("ply", "")
    return int(value) if value.lstrip("-").isdigit() else None


def game_page(request, match_id, game):
    match = get_object_or_404(match_queryset(), pk=match_id)
    ply = requested_ply(request)
    context = game_context(match, game, ply)
    if context is None:
        raise Http404("no such game")
    context.update(version=version_token(match_id, game, match.state, ply), pinned=ply is not None,
                   poll_ms=settings.NYAYO_GAME_POLL_MS)
    return render(request, "matches/game.html", context)


def game_fragment(request, match_id, game):
    """The part of the game page that changes as it is played, for the page to poll. Polled many times a second, so
    when the client says it already has this version (v) the answer is an empty 204 before anything is parsed."""
    state = Match.objects.filter(pk=match_id).values_list("state", flat=True).first()
    if state is None:
        raise Http404("no such match")
    ply = requested_ply(request)
    token = version_token(match_id, game, state, ply)
    if request.GET.get("v") == token:
        return HttpResponse(status=204)
    match = get_object_or_404(match_queryset(), pk=match_id)
    context = game_context(match, game, ply)
    if context is None:
        raise Http404("no such game")
    html = render_to_string("matches/_game_body.html", context, request=request)
    return JsonResponse({"html": html, "live": bool(context.get("live")), "plies": context.get("plies", 0),
                         "waiting": bool(context.get("waiting")), "v": token})
