"""The pages: the match list, a match, and a game, live."""

from datetime import timedelta
from types import SimpleNamespace

import chess
from django.conf import settings
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.template.loader import render_to_string

from django.utils import timezone

from nyayo.actions.access import may_act
from nyayo.lab.templatetags.lab_extras import producer_link
from nyayo.paging import paginate

from . import catalogue, games, pgnview, progress, sprtview, stats, stepsview, timing
from .models import KINDS, STATES, Engine, Game, Match, Tombstone

OUTCOMES = {("1-0", "white"): "win", ("0-1", "black"): "win", ("0-1", "white"): "loss", ("1-0", "black"): "loss",
            ("1/2-1/2", "white"): "draw", ("1/2-1/2", "black"): "draw"}
RESULT_VIEWS = ("cards", "list")
VIEW_COOKIE = "nyayo_results_view"
STATE_ORDER = {"playing": 0, "unfinished": 1, "scheduled": 2, "finished": 3}
# the states of a test that has no games yet, which the list shows apart from the running ones
UNSTARTED = ("waiting", "held", "failed_prechecks")
UNSTARTED_CARDS = 12
WALKS_ON_A_CARD = 3


def match_queryset(with_removed=False):
    """The matches the lists and pages show: not the removed ones (`Match.hidden`) unless asked, as a game's page does."""
    queryset = Match.objects.select_related("subject", "machine").prefetch_related("results__engine")
    return queryset if with_removed else queryset.filter(hidden=False)


def missing(request, match_id):
    """The 404 for a match that is not shown: one removed (it can be brought back), one deleted with its games, or none."""
    removed = Match.objects.filter(pk=match_id, hidden=True).first()
    tomb = None if removed else Tombstone.objects.filter(pk=match_id).first()
    return render(request, "matches/gone.html", {"match_id": match_id, "removed": removed, "tomb": tomb}, status=404)


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


def timing_start(match):
    """When a test's clock starts: its creation, or for a test with steps the first `running` step, so the time spent
    waiting for a worker and on its checks isn't in *elapsed* and *pace*. None before any worker is playing."""
    if stepsview.has_steps(match.machines):
        return stepsview.running_since(match.machines)
    return match.started_at or match.received_at


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

    # the bar runs lower to upper with 0 marked, so 0 is in range whatever the bounds
    values = [v for v in (llr, lower, upper, 0.0) if v is not None]
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

    zero, here = pct(0.0), pct(llr)
    fill = None
    if here is not None and here != zero:
        # filled from 0 towards the LLR: green on the way to passing, red on the way to failing
        fill = {"left": min(zero, here), "width": abs(here - zero), "passing": llr > 0}
    produced = match.stats if isinstance(match.stats, dict) else None
    return {
        "fill": fill, "zero_pct": zero,
        "stats": produced, "producer": match.producer_name or "the producer",
        "llr": llr, "lower": lower, "upper": upper, "result": data.get("result"),
        "alpha": number(data.get("alpha")), "beta": number(data.get("beta")),
        "elo0": number(data.get("elo0")), "elo1": number(data.get("elo1")), "model": data.get("model"),
        "range_low": low, "range_high": high,
        "llr_pct": pct(llr), "lower_pct": pct(lower), "upper_pct": pct(upper),
    }


def sprt_page(match, sprt, projection, now=None):
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
        "subject_link": sprtview.commit_link(match.subject_info, match.subject.commit),
        "base_link": sprtview.commit_link(base["info"], base["commit"]) if base else None,
        "projection": projection, "penta_rows": penta_rows(row), "faults": faults(row),
        **workers_context(match, row, now),
    }


def workers_context(match, row, now=None):
    """The Workers area: the cards with their summary when the producer sends steps, else the table; also what the
    page's poll redraws."""
    now = now or timezone.now()
    workers = sprtview.workers(match.machines, row.games if row else 0, now, match.state)
    walked = stepsview.has_steps(match.machines)
    return {
        "workers": workers, "has_steps": walked, "tally": stepsview.tally(workers) if walked else None,
        "unstarted": match.state in UNSTARTED and not (row and row.games),
        # a card with a step open is redrawn every 2 seconds; a test that can still change is looked at less often
        "steps_open": any(w["open"] for w in workers), "watch": match.state in ("waiting", "held", "running"),
        "workers_url": reverse("match_workers", args=[match.pk]),
        "show_workers": bool(workers) or match.state in UNSTARTED,
        "headline": headline(match, workers, now), "state": match.state,
    }


HEADLINE_PREFIX = {"waiting": "Waiting", "held": "Held", "failed_prechecks": "Stopped", "error": "Stopped"}
HEADLINES = {
    "waiting": ("Waiting for a worker", "The test is open and doesn't expire. It starts by itself as soon as a worker "
                                        "gets through its checks."),
    "held": ("Held", "Nobody has said whose failure it is. The test is open and doesn't expire."),
    "failed_prechecks": ("Stopped before it started", "The test failed its checks and played nothing."),
}


def headline(match, workers, now):
    """What sits under the page's title when the test has a state with no games: the producer's verdict word for
    word, else a plain line of what the state means. None for a test that is being played or is over."""
    verdict = match.verdict if isinstance(match.verdict, dict) else None
    if verdict is None and match.state not in UNSTARTED:
        return None
    verdict = verdict or {}
    plain = HEADLINES.get(match.state, ("", ""))
    summary, detail = verdict.get("summary") or plain[0], verdict.get("detail") or plain[1]
    prefix = HEADLINE_PREFIX.get(match.state)
    retries = sorted((s["retry_at"], w["name"]) for w in workers for s in w["failures"]
                     if s["retry_at"] and s["retry_at"] > now)
    stopped = match.finished_at if match.state in ("failed_prechecks", "error") else None
    return {
        "blame": verdict.get("blame"), "blame_word": stepsview.BLAME_WORDS.get(verdict["blame"]) if verdict.get("blame") else "",
        "look": verdict.get("blame") or "pending",
        "title": f"{prefix}: {summary[:1].lower() + summary[1:]}" if prefix and verdict.get("summary") else summary,
        "detail": detail, "where": verdict.get("where"),
        "seen_on": ", ".join(n for n in verdict.get("seen_on") or [] if isinstance(n, str)),
        "created": match.started_at, "stopped": stopped,
        "stopped_after": (stopped - match.started_at).total_seconds() if stopped and match.started_at else None,
        "waiting_for": (now - match.started_at).total_seconds() if match.state == "waiting" and match.started_at
        else None,
        "next_try": ", ".join(f"{name} {'in ' + stepsview.length((at - now).total_seconds())}" for at, name in retries),
        "last_report": match.updated_at,
        "goes_in": stepsview.goes_in(match, now), "goes_at": stepsview.goes_at(match),
        "stays": {"held": "No, not until it has a verdict", "waiting": "No, it stays open until a worker plays it"}.get(
            match.state),
    }


PENTA_LABELS = ("LL", "LD", "DD", "DW", "WW")


def penta_rows(row):
    """The pentanomial as columns: each one's count, its share of the pairs and the height of its pillar as a
    percentage of the tallest, None before it has any."""
    if not row or not row.penta or not sum(row.penta):
        return None
    pairs, tallest = sum(row.penta), max(row.penta)
    return [{"label": label, "count": count, "share": f"{count / pairs:.1%}", "tone": label.lower(),
             "height": f"{count / tallest * 100:.1f}"}
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


def upload_counts(match):
    """The match's uploads by state, the ones it has."""
    from django.db.models import Count

    return dict(match.uploads.values_list("state").annotate(n=Count("pk")).order_by())


def kept(card, opponents):
    """A card as game_entries() takes a game's row: finished, the subject's colour, the opponent's id."""
    return SimpleNamespace(live=False, result=card["result"], colour=card["colour"],
                           opponent=opponents.get(card["opponent"], ""), termination=card["termination"])


def opponent_ids(match):
    return dict(match.results.values_list("pk", "opponent"))


def sprt_progress(match):
    """The SPRT card's graphs and projection, from the snapshots; empty for a match that isn't an SPRT."""
    if not isinstance(match.sprt, dict):
        return {}
    series = progress.points(match.snapshots.all())
    running = match.state == "running"
    return {"graphs": progress.graphs(series, match.sprt),
            "projection": progress.projection(series, match.sprt, timezone.now()) if running else None}


# most running matches the list shows a card for; one or two is usual
RUNNING_CARDS = 6
# most boards in play a card shows, the latest started
CARD_BOARDS = 6


def playing_entries(match, limit):
    """The games of a running match that are being played, as live_boards() takes them, without building every
    entry of the match (an SPRT's can be tens of thousands)."""
    names = opponent_names(match)
    rows = sorted(match.games.filter(result="*").order_by("-started_at")[:limit], key=lambda g: g.started_at)
    return [{"number": g.game, "colour": g.colour, "opponent": g.opponent,
             "opponent_name": names.get(g.opponent, g.opponent)} for g in rows]


def running_cards(cutoff, now):
    """The matches being played right now, newest first, for the top of the list: running, and reported lately (a
    silent match is likely dead). Each as what a glance needs: games, Elo, how it is going and when it will be done."""
    cards = []
    for match in match_queryset().filter(state="running", updated_at__gte=cutoff).order_by("-updated_at")[
            :RUNNING_CARDS]:
        summary = stats.summarize(match.results.all())
        progress_info = match.progress or {}
        total = progress_info.get("total") or len(match.schedule) or None
        played = progress_info.get("played") if total else None
        if played is None:
            played = summary.overall.games
        playing = progress_info.get("playing") or 0
        run = timing.summarize(match.state, timing_start(match), match.finished_at,
                               match.updated_at, total, played, playing, [], (match.settings or {}).get("concurrency"),
                               now)
        card = {"match": match, "total": total, "played": played, "playing": playing, "timing": run,
                "score": summary.overall.score, "tc": (match.settings or {}).get("tc"), "sprt": sprt_view(match),
                "boards": live_boards(match, playing_entries(match, CARD_BOARDS))}
        if card["sprt"]:
            card.update(sprt_card(match, card["sprt"], now))
        else:
            card.update(elo=summary.overall.elo, error=summary.overall.error, projection=None)
        cards.append(card)
    return cards


def unstarted_cards(now):
    """The tests that haven't played a game, for the top of the list and the dashboard: waiting, held (apart) and
    failed starts, each with a compact walk of its first workers, its reason and, for a failed start, when it goes."""
    found = {}
    for state in UNSTARTED:
        cards = []
        for match in match_queryset().filter(state=state).order_by("-updated_at")[:UNSTARTED_CARDS]:
            workers = sprtview.workers(match.machines, 0, now, match.state)
            walking = [w for w in workers if w["has_steps"]]
            cards.append({"match": match, "workers": walking[:WALKS_ON_A_CARD],
                          "more": max(0, len(walking) - WALKS_ON_A_CARD), "idle": not workers,
                          "headline": headline(match, workers, now), "tc": (match.settings or {}).get("tc")})
        found[state] = cards
    titles = (("waiting", "Waiting for a worker"), ("failed_prechecks", "Failed their start"),
              ("held", "Held, they need a look"))
    return {"groups": [({"cards": found[state]}, title) for state, title in titles], "any": any(found.values()),
            "refresh": bool(found["waiting"] or found["held"])}


def sprt_card(match, sprt, now):
    """What an SPRT adds to its card: its own Elo and error, and where the LLR is heading."""
    elo, error, _ = sprtview.elo_figures(match, match.results.first())
    series = progress.points(match.snapshots.all())
    return {"elo": elo, "error": error, "projection": progress.projection(series, match.sprt, now)}


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
    page = paginate(matches, request)
    now = timezone.now()
    running = running_cards(cutoff, now)
    unstarted = unstarted_cards(now)
    for match in page.object_list:
        match.summary = stats.summarize(match.results.all())
        match.silent = is_silent(match, cutoff)
        # an SPRT's Elo is its producer's; the rest are nyayo's from the counts
        produced = match.stats if isinstance(match.stats, dict) else None
        match.elo = produced.get("elo") if produced else match.summary.overall.elo
        match.elo_error = produced.get("error") if produced else match.summary.overall.error
    return render(request, "matches/matches_list.html", {
        "page": page, "kind": kind, "kinds": KINDS, "state": state, "states": STATES + ["silent"], "engine": engine,
        "running": running, "unstarted": unstarted, "can_act": may_act(request.user),
        "removed_count": Match.objects.filter(hidden=True).count() if may_act(request.user) else 0,
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
    # games kept in batches have cards, not rows; a game in both is the same game
    opponents = opponent_ids(match)
    for card in catalogue.cards(match.pk):
        stored.setdefault(card["game"], kept(card, opponents))
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
    if row is None:
        found = catalogue.find(match.pk, number)
        row = kept(found[1], opponent_ids(match)) if found else None
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


def match_workers(request, match_id):
    """The Workers area alone, for the match page to poll while a step is open and redraw."""
    match = match_queryset().filter(pk=match_id).first()
    if match is None:
        raise Http404("no such match")
    context = workers_context(match, match.results.first())
    context.update(match=match, polled=True)
    html = render_to_string("matches/_workers.html", context, request=request)
    return JsonResponse({"html": html, "open": context["steps_open"], "live": context["watch"],
                         "state": match.state})


def match_detail(request, match_id):
    match = match_queryset().filter(pk=match_id).first()
    if match is None:
        return missing(request, match_id)
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
    kept = len(groups["finished"])
    games_note = None
    if not match.schedule and total is None and match.results.exists():
        # no schedule to count (an SPRT): the games played are the results', which is what the test was decided on, and what
        # the tile and the pace are made of. The games kept may be more (a worker uploads its whole file, with the games it
        # played after its last report) or fewer; they are shown as they are, with a note
        played = summary.overall.games
        games_note = {"kept": kept, "counted": played} if kept != played else None
    elif not match.schedule and total is None:
        played = kept
    else:
        played = kept
    subject_name = engine_label(match.subject)
    context = {
        "view": view, "match": match, "silent": is_silent(match, silent_cutoff()), "silent_minutes": settings.NYAYO_SILENT_AFTER, "boards": live_boards(match, groups["playing"]),
        "summary": summary, "excluded": excluded, "games_note": games_note,
        "summary_clipboard": summary_clipboard_text(match, subject_name, summary, match.settings or {}),
        "sprt": sprt_view(match), **sprt_progress(match),
        "timing": timing.summarize(
            match.state, timing_start(match), match.finished_at, match.updated_at, total,
            played, len(groups["playing"]), durations, (match.settings or {}).get("concurrency"),
            timezone.now()),
        "steps_start": stepsview.running_since(match.machines) if stepsview.has_steps(match.machines) else None,
        "groups": groups, "entries": entries, "refresh": match.state == "running", "settings": match.settings,
        "host": match.host, "subject_name": subject_name, "stored_games": stored_games(match),
        "uploads": upload_counts(match),
        "producer_url": producer_link(match),
    }
    # the games table shows one page of them, in play order (playing, cut off, scheduled, finished); what was counted
    # above, the totals and the boards in play, is of all of them
    games_page = paginate(entries, request)
    context.update({
        "games_page": games_page, "totals": {name: len(rows) for name, rows in groups.items()},
        "groups": {name: [e for e in games_page.object_list if e["state"] == name] for name in groups},
    })
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
    """The boards of the games being played, as they stand, for the match page to show at a glance. Each is drawn in
    the browser by chessground from its position, the side the engine under test plays at the bottom."""
    boards = []
    for entry in playing:
        raw = games.read_pgn(match.pk, entry["number"])
        parsed = pgnview.load(raw) if raw else None
        if parsed is None:
            continue
        shown = pgnview.positions(parsed)[-1]
        boards.append({"entry": entry, "plies": len(parsed.moves),
                       "last": parsed.moves[-1] if parsed.moves else None,
                       "white": parsed.headers.get("White", ""), "black": parsed.headers.get("Black", ""),
                       "fen": shown["fen"], "last_move": " ".join(shown["last"] or []),
                       "orientation": "black" if entry["colour"] == "black" else "white"})
    return boards


def game_context(match, game, ply=None):
    """What the game page and its live fragment show, or None when nothing is known of the game."""
    raw = games.read_pgn(match.pk, game) or catalogue.read_game(match.pk, game)
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

    board, _ = pgnview.position(parsed, ply)
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
        # the board is drawn in the browser (chessground) from these, so stepping through the game needs no request
        "orientation": bottom, "positions": (every := pgnview.positions(parsed)), "shown": every[ply],
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
    match = get_object_or_404(match_queryset(with_removed=True), pk=match_id)
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
    match = get_object_or_404(match_queryset(with_removed=True), pk=match_id)
    context = game_context(match, game, ply)
    if context is None:
        raise Http404("no such game")
    html = render_to_string("matches/_game_body.html", context, request=request)
    return JsonResponse({"html": html, "live": bool(context.get("live")), "plies": context.get("plies", 0),
                         "waiting": bool(context.get("waiting")), "v": token})
