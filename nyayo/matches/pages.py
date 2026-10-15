"""The pages: the match list, a match, and a game, live."""

import chess
from django.conf import settings
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string

from . import games, pgnview, stats
from .models import KINDS, STATES, Engine, Game, Match

OUTCOMES = {("1-0", "white"): "win", ("0-1", "black"): "win", ("0-1", "white"): "loss", ("1-0", "black"): "loss",
            ("1/2-1/2", "white"): "draw", ("1/2-1/2", "black"): "draw"}
STATE_ORDER = {"playing": 0, "unfinished": 1, "scheduled": 2, "finished": 3}


def match_queryset():
    return Match.objects.select_related("subject", "machine").prefetch_related("results__engine")


def engine_label(engine):
    return f"{engine.name} {engine.version}".strip()


def matches_list(request):
    matches = match_queryset().order_by("-updated_at")
    kind, state, engine = request.GET.get("kind", ""), request.GET.get("state", ""), request.GET.get("engine", "")
    if kind in KINDS:
        matches = matches.filter(kind=kind)
    if state in STATES:
        matches = matches.filter(state=state)
    if engine.isdigit():
        matches = matches.filter(subject_id=int(engine))
    page = Paginator(matches, 50).get_page(request.GET.get("page"))
    for match in page.object_list:
        match.summary = stats.summarize(match.results.all())
    return render(request, "matches/matches_list.html", {
        "page": page, "kind": kind, "kinds": KINDS, "state": state, "states": STATES, "engine": engine,
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
    return render(request, "matches/match_detail.html", {
        "match": match, "boards": live_boards(match, groups["playing"]), "summary": stats.summarize(match.results.all(), excluded), "excluded": excluded,
        "groups": groups, "entries": entries, "refresh": match.state == "running", "settings": match.settings,
        "host": match.host, "subject_name": engine_label(match.subject),
        "producer_url": (match.producer or {}).get("url"),
    })


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
    return {
        "match": match, "game": game, "entry": entry, "headers": parsed.headers, "result": parsed.result,
        "live": live, "cut_off": parsed.result == "*" and not live, "plies": plies, "ply": ply,
        "board": pgnview.board_svg(parsed, ply, chess.BLACK if colour == "black" else chess.WHITE),
        "rows": pgnview.pairs(parsed.moves), "graph": pgnview.graph(parsed.moves, ply), "at_end": ply == plies,
        "move": parsed.moves[ply - 1] if ply else None, "subject_colour": colour,
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
