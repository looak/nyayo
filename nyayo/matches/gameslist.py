"""The games page: every game kept as data, newest first, 64 to a page (`/games`).

It reads the index database's cards, a page of them at a time, and fills in the rest from nyayo's database for those 64
rows only: the match a game belongs to and the engines that played it. Nothing is drawn that the cards don't hold.

Filters (a query is its URL): `match` (a match's id), `result` (win, draw or loss for the engine under test), `ended`
(how it ended: a rule, cards.ENDED, or another ending, cards.ENDINGS), `moves_from` and `moves_to` (whole moves), and
`opening` (the 16 hex digits of a position, see below). Each is ANDed with the rest.
"""

from datetime import datetime, timezone

from django.http import HttpResponseBadRequest
from django.shortcuts import render

from nyayo.paging import paginate

from . import cards, catalogue
from .models import Batch, Match, MatchResult
from .pages import OUTCOMES

STARTPOS = 0x463B96181691FC9C

# how a position ended a game by the rules (cards.ENDED), and the other endings (cards.ENDINGS)
RULES = {1: "Checkmate", 2: "Stalemate", 3: "Insufficient material", 4: "Fifty-move rule", 5: "Threefold repetition"}
ENDINGS = {"rule": "Rules", "resignation": "Resignation", "draw_adjudication": "Draw adjudicated",
           "adjudication": "Adjudicated", "time_forfeit": "Lost on time", "illegal_move": "Illegal move",
           "crash": "Crash", "other": "Other", "unexplained": "Unexplained"}
# the engine under test's result as SQL over a card's colour (1 white, 2 black) and result (1 1-0, 2 0-1, 3 draw)
WON = "((colour = 1 AND result = 1) OR (colour = 2 AND result = 2))"
LOST = "((colour = 1 AND result = 2) OR (colour = 2 AND result = 1))"
RESULT_FILTERS = {"win": WON, "loss": LOST, "draw": "(colour > 0 AND result = 3)"}
COLUMNS = ", ".join(("batch", "position", "game", "colour", "opponent", "result", "plies", "ended", "ending",
                     "flags", "start", "opening", "played_at"))


def signed(value):
    return value - (1 << 64) if value >= 1 << 63 else value


def unsigned(value):
    return value + (1 << 64) if value < 0 else value


def hex_key(value):
    """The 16 hex digits of a card's signed 64-bit position hash."""
    return f"{unsigned(value):016x}" if value is not None else None


def opening_key(start, opening):
    """The position that says which opening a game is from: the book position it starts from, or for a game that
    starts from the standard position, the one after 16 plies."""
    if start is not None and unsigned(start) != STARTPOS:
        return start
    return opening


class Cards:
    """The cards matching a filter as a sequence of 64-row slices, which is all a Paginator needs of its items."""

    def __init__(self, where="", params=()):
        self.where, self.params = where, tuple(params)

    def __len__(self):
        return catalogue.index().execute(f"SELECT count(*) FROM card {self.where}", self.params).fetchone()[0]

    def __getitem__(self, window):
        start, stop = window.start or 0, window.stop
        limit = -1 if stop is None else stop - start
        rows = catalogue.index().execute(
            f"SELECT {COLUMNS} FROM card {self.where} ORDER BY played_at DESC, batch DESC, position DESC "
            f"LIMIT ? OFFSET ?", self.params + (limit, start)).fetchall()
        return decorate(rows)


def engine_name(engine, info):
    """`Elephant feature-x`: the engine and the branch it was played from, else its version."""
    return f"{engine.name} {(info or {}).get('branch') or engine.version}".strip()


def ended_by(code, ending, flags):
    """(text, inferred) for how a game ended."""
    label = ENDINGS.get(cards.ENDINGS[ending]) if ending is not None else None
    if label == "Rules":
        label = RULES.get(code, "Rules")
    return label or "", bool(flags & cards.INFERRED)


def decorate(rows):
    batches = Batch.objects.in_bulk({row[0] for row in rows})
    matches = Match.objects.select_related("subject").in_bulk({b.match_id for b in batches.values()})
    results = {r.pk: r for r in MatchResult.objects.filter(match_id__in=matches).select_related("engine")}
    out = []
    for (batch, position, game, colour, opponent, result, plies, ended, ending, flags, start, opening,
         played_at) in rows:
        match = matches[batches[batch].match_id]
        colour = catalogue.COLOURS.get(colour, "")
        subject = engine_name(match.subject, match.subject_info)
        rival = results.get(opponent)
        rival = engine_name(rival.engine, rival.info) if rival else ""
        names = {"white": subject if colour == "white" else rival, "black": rival if colour == "white" else subject}
        if not colour:
            names = {"white": "", "black": ""}
        result = catalogue.RESULTS.get(result, "*")
        text, inferred = ended_by(ended, ending, flags)
        key = opening_key(start, opening)
        out.append({
            "game": game, "match": match, "white": names["white"], "black": names["black"], "colour": colour,
            "moves": (plies + 1) // 2, "plies": plies, "result": result, "outcome": OUTCOMES.get((result, colour)),
            "ended": text, "inferred": inferred,
            "played": None if played_at is None else datetime.fromtimestamp(played_at, timezone.utc),
            "exact_time": bool(flags & cards.PLAYED_TIME),
            "opening": hex_key(key),
        })
    return out


def ending_choices():
    """(value, label) for the `ended` filter: the rules first, then the other endings."""
    rules = [(name, RULES[code]) for code, name in enumerate(cards.ENDED) if code]
    return rules + [(name, ENDINGS[name]) for name in cards.ENDINGS if name != "rule"]


def ending_filter(value):
    """(SQL, params) for an `ended` value; ValueError for one that isn't."""
    if value in cards.ENDED[1:]:
        return "(ending = ? AND ended = ?)", [cards.ENDINGS.index("rule"), cards.ENDED.index(value)]
    if value in cards.ENDINGS and value != "rule":
        return "ending = ?", [cards.ENDINGS.index(value)]
    raise ValueError(value)


def whole(text):
    """A whole number from a filter's field, None for an empty one; ValueError for anything else."""
    text = text.strip()
    if not text:
        return None
    if not text.isdigit():
        raise ValueError(text)
    return int(text)


def parse_position(text):
    """The signed 64-bit hash of a position given as 16 hex digits; ValueError for anything else."""
    if len(text) != 16:
        raise ValueError(text)
    return signed(int(text, 16))


def games_list(request):
    where, params, filters = [], [], {}
    match_id = request.GET.get("match", "").strip()
    if match_id:
        match = Match.objects.filter(pk=match_id).first()
        filters["match"] = {"id": match_id, "name": (match.name or match.pk) if match else match_id,
                            "known": match is not None}
        ids = list(Batch.objects.filter(match_id=match_id).values_list("pk", flat=True))
        where.append(f"batch IN ({','.join('?' * len(ids))})" if ids else "0")
        params += ids
    result = request.GET.get("result", "")
    if result in RESULT_FILTERS:
        filters["result"] = result
        where.append(RESULT_FILTERS[result])
    ended = request.GET.get("ended", "")
    if ended:
        try:
            sql, values = ending_filter(ended)
        except ValueError:
            return HttpResponseBadRequest("ended must be one of " + ", ".join(v for v, _ in ending_choices()))
        filters["ended"] = dict(ending_choices())[ended]
        where.append(sql)
        params += values
    try:
        least, most = whole(request.GET.get("moves_from", "")), whole(request.GET.get("moves_to", ""))
    except ValueError:
        return HttpResponseBadRequest("moves must be whole numbers")
    # a game of n moves has 2n - 1 or 2n plies
    if least is not None:
        filters["moves_from"] = least
        where.append("plies >= ?")
        params.append(2 * least - 1)
    if most is not None:
        filters["moves_to"] = most
        where.append("plies <= ?")
        params.append(2 * most)
    opening = request.GET.get("opening", "").strip().lower()
    if opening:
        try:
            key = parse_position(opening)
        except ValueError:
            return HttpResponseBadRequest("opening must be a position, 16 hex digits")
        filters["opening"] = opening
        # a game from the standard position is found by the position after 16 plies, any other by where it started
        where.append("((start = ? AND start != ?) OR (start = ? AND opening = ?))")
        params += [key, signed(STARTPOS), signed(STARTPOS), key]
    page = paginate(Cards("WHERE " + " AND ".join(where) if where else "", params), request)
    kept = Match.objects.filter(batches__isnull=False).distinct().order_by("-updated_at")
    return render(request, "matches/games_list.html", {
        "games_page": page, "total": page.paginator.count, "filters": filters,
        "choices": {"matches": kept, "endings": ending_choices(), "results": ("win", "draw", "loss")},
        "asked": {name: request.GET.get(name, "") for name in ("match", "result", "ended", "moves_from", "moves_to",
                                                               "opening")}})
