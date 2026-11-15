"""A selection of the games kept as data: the matches' filters, then the cards' (docs/match-schema.md, Storage).

The games page (`/games`) and `manage.py export_training` share it, so a filter means the same games wherever it is
given. A selection is two parts: which matches (their kind, producer, engine under test, book and time control), and
which of their games, by the cards in the index (result, how the game ended, length). The cards' part is SQL over the
index's `card` table: `where()` gives the clause and its parameters.
"""

import re
from datetime import date, datetime, timezone

from django.db.models import Q

from . import cards, catalogue
from .models import Batch, Match, RenamedMatch

# how a position ended a game by the rules (cards.ENDED), and the other endings (cards.ENDINGS)
RULES = {1: "Checkmate", 2: "Stalemate", 3: "Insufficient material", 4: "Fifty-move rule", 5: "Threefold repetition"}
ENDINGS = {"rule": "Rules", "resignation": "Resignation", "draw_adjudication": "Draw adjudicated",
           "adjudication": "Adjudicated", "time_forfeit": "Lost on time", "illegal_move": "Illegal move",
           "crash": "Crash", "other": "Other", "unexplained": "Unexplained"}
# the engine under test's result as SQL over a card's colour (1 white, 2 black) and result (1 1-0, 2 0-1, 3 draw)
WON = "((colour = 1 AND result = 1) OR (colour = 2 AND result = 2))"
LOST = "((colour = 1 AND result = 2) OR (colour = 2 AND result = 1))"
RESULT_FILTERS = {"win": WON, "loss": LOST, "draw": "(colour > 0 AND result = 3)"}
# a game with a result: 1-0, 0-1 or a draw, not * (a game cut off, or one whose card doesn't know)
DECIDED = "result IN (1, 2, 3)"

# Endings for training (owner, 2026-10-26): kept, a game whose result says who stood better (by a rule, a
# resignation, an adjudication, inferred ones included); left out, one whose result says nothing about the position
# (lost on time, a crash, an illegal move, or an ending nyayo can't explain). The short names are the command's.
ENDING_NAMES = {"rule": ("rule",), "resignation": ("resignation",),
                "adjudication": ("adjudication", "draw_adjudication"), "time": ("time_forfeit",),
                "crash": ("crash",), "illegal": ("illegal_move",), "unexplained": ("unexplained",),
                "other": ("other",)}
GOOD_ENDINGS = ("rule", "resignation", "adjudication")


class BadSelection(ValueError):
    pass


def ending_choices():
    """(value, label) for the games page's `ended` filter: the rules first, then the other endings."""
    rules = [(name, RULES[code]) for code, name in enumerate(cards.ENDED) if code]
    return rules + [(name, ENDINGS[name]) for name in cards.ENDINGS if name != "rule"]


def ending_filter(value):
    """(SQL, params) for one `ended` value of the games page; ValueError for one that isn't."""
    if value in cards.ENDED[1:]:
        return "(ending = ? AND ended = ?)", [cards.ENDINGS.index("rule"), cards.ENDED.index(value)]
    if value in cards.ENDINGS and value != "rule":
        return "ending = ?", [cards.ENDINGS.index(value)]
    raise ValueError(value)


def endings_kept(keep=None, exclude=()):
    """The cards.ENDINGS codes kept: the short names in keep (GOOD_ENDINGS when None), less those in exclude.
    BadSelection naming one that isn't a short name."""
    keep = GOOD_ENDINGS if keep is None else keep
    for name in (*keep, *exclude):
        if name not in ENDING_NAMES:
            raise BadSelection(f"{name!r} is not an ending; one of {', '.join(ENDING_NAMES)}")
    names = [name for name in keep if name not in exclude]
    return sorted({cards.ENDINGS.index(e) for name in names for e in ENDING_NAMES[name]})


def version_key(text):
    """1.3.0 as (1, 3, 0) to compare versions by; None for one without a number up front (a branch name)."""
    found = re.match(r"v?(\d+(?:\.\d+)*)", (text or "").strip())
    return tuple(int(part) for part in found.group(1).split(".")) if found else None


def parse_versions(text):
    """`1.3.0-` (that version or later), `1.2.0-1.3.0` (between, both included) or `1.3.0` (that one) as (low,
    high), either None for no bound. BadSelection for anything else."""
    low, dash, high = text.strip().partition("-")
    bounds = (version_key(low) if low else None, (version_key(high) if high else None) if dash else version_key(low))
    if (low and bounds[0] is None) or (high and bounds[1] is None) or bounds == (None, None):
        raise BadSelection(f"engine version {text!r} must be like 1.3.0, 1.3.0- or 1.2.0-1.3.0")
    return bounds


def parse_since(text):
    """The date a selection starts from, `2026-10-01`, as a date; None for blank. BadSelection for anything else."""
    text = (text or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
    raise BadSelection(f"from must be a date, like 2026-10-01, not {text!r}")


def players(where="1"):
    """[(name, games, usable)] for every player in the cards, by the name the engine reports (the PGN's White and
    Black tags), the most games first: the games it played, and those `where` (SQL over the cards) lets through, a
    dataset's by default. A game between two of one name counts once."""
    both = ("SELECT white_name AS name, result, ending, ended FROM card WHERE white_name IS NOT NULL "
            "UNION ALL SELECT black_name, result, ending, ended FROM card WHERE black_name IS NOT NULL "
            "AND black_name IS NOT white_name")
    rows = catalogue.index().execute(
        f"SELECT name, count(*), sum(CASE WHEN {where} THEN 1 ELSE 0 END) FROM ({both}) GROUP BY name "
        f"ORDER BY count(*) DESC, name").fetchall()
    return [(name, games, usable or 0) for name, games, usable in rows]


def players_filter(names):
    """(SQL, params) for the cards of games either side of which is one of names; None for none."""
    if not names:
        return None
    marks = ",".join("?" * len(names))
    return f"(white_name IN ({marks}) OR black_name IN ({marks}))", list(names) * 2


def since_filter(since):
    """(SQL, params) for the cards of games that began on or after a date (UTC): by the game's own start, else its
    finish (`begun_at`), else, for a game with neither, by when its match began. None for no date."""
    if not since:
        return None
    start = datetime(since.year, since.month, since.day, tzinfo=timezone.utc)
    late = Batch.objects.filter(match__started_at__gte=start).values_list("pk", flat=True)
    ids = ",".join(str(pk) for pk in late)  # integers, so inlined: there may be more than a query's parameters
    return f"(begun_at >= ?{f' OR (begun_at IS NULL AND batch IN ({ids}))' if ids else ''})", [int(start.timestamp())]


def known_players():
    return {name for name, _, _ in players()}


class Selection:
    """The games a set of filters chooses. Every filter is optional; none is every game with a result.

    matches: match ids; openbench: test numbers (the N of `sprt-N`, or of the old `openbench-<host>-<N>`, or OpenBench's test URL); kind: a match kind;
    versions: (low, high) from parse_versions(), the engine under test's version; book and tc: as the match's settings
    say them; endings: the cards.ENDINGS codes kept (endings_kept()), None for any; players: names as the engines
    report them, a game is kept when either side is one of them, none for every game; since: a date (parse_since()),
    no games began before it, None for no limit. A game's start is its own `GameStartTime`, else its `GameEndTime`,
    else its match's start; a game none of them dates is left out when a date is asked."""

    def __init__(self, matches=(), openbench=(), kind=None, versions=None, book=None, tc=None, endings=None,
                 players=(), since=None):
        self.match_ids, self.openbench, self.kind = list(matches), [str(n) for n in openbench], kind
        self.versions, self.book, self.tc, self.endings = versions, book, tc, endings
        self.players, self.since = list(players), since

    def described(self):
        """The filters as given, for a manifest: plain JSON."""
        return {"matches": self.match_ids, "openbench": self.openbench, "kind": self.kind,
                "versions": [".".join(map(str, v)) if v else None for v in self.versions] if self.versions else None,
                "book": self.book, "tc": self.tc,
                "endings": [cards.ENDINGS[code] for code in self.endings] if self.endings is not None else None,
                "players": self.players, "since": self.since.isoformat() if self.since else None}

    def label(self):
        """The filters that narrow a dataset, as a line for the pages: `Elephant 1.4.0, Stash 36 · from 2026-10-01`;
        empty for none."""
        parts = [", ".join(self.players)] if self.players else []
        return " · ".join(parts + ([f"from {self.since.isoformat()}"] if self.since else []))

    def matches(self):
        """The matches chosen, as a queryset when no version is asked for, else a list."""
        queryset = Match.objects.select_related("subject")
        chosen = Q()
        if self.match_ids:
            # an old id (openbench-openbench-12) in a saved link or script means the match it was renamed to
            renamed = RenamedMatch.objects.filter(old_id__in=self.match_ids).values_list("new_id", flat=True)
            chosen |= Q(pk__in=self.match_ids) | Q(pk__in=list(renamed))
        for number in self.openbench:
            chosen |= Q(pk__regex=rf"^(openbench-.+|sprt|games|tune|datagen)-{re.escape(number)}$") | Q(
                producer__url__endswith=f"/test/{number}/")
        if self.match_ids or self.openbench:
            queryset = queryset.filter(chosen)
        if self.kind:
            queryset = queryset.filter(kind=self.kind)
        if self.book:
            queryset = queryset.filter(settings__book=self.book)
        if self.tc:
            queryset = queryset.filter(settings__tc=self.tc)
        if not self.versions:
            return queryset
        low, high = self.versions
        found = []
        for match in queryset:
            version = version_key(match.subject.version or (match.subject_info or {}).get("version"))
            if version is not None and (low is None or version >= low) and (high is None or version <= high):
                found.append(match)
        return found

    def where(self):
        """(SQL, params) choosing the cards of the chosen matches' games: decided, and ended as asked."""
        clauses, params = [DECIDED], []
        for found in (players_filter(self.players), since_filter(self.since)):
            if found:
                clauses.append(found[0])
                params += found[1]
        if self.endings is not None:
            clauses.append(f"ending IN ({','.join('?' * len(self.endings))})" if self.endings else "0")
            params += self.endings
        return " AND ".join(clauses), params
