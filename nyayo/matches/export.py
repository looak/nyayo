"""Every stored game matching a filter, as one bz2 stream (docs/match-schema.md, Export).

Each of nyayo's batches, sealed or open, is compressed as a bz2 stream of its own and the streams concatenated (a
multi-stream bz2 file is valid, Python's `bz2` and `bzip2 -d` read it whole), then the finished single games of
gauntlets not in a batch yet, so the export is every game nyayo has for the filter, each once. Matches are filtered
on what is indexed already; there is no index of positions.
"""

import bz2
from datetime import date, datetime, time, timedelta, timezone

from . import catalogue
from .games import read_pgn
from .models import KINDS, Batch, Game, Match

# after each batch: a batch needn't end with a blank line, and the next one's first tag would run into its last move
SEPARATOR = bz2.compress(b"\n\n")
FILTERS = ("kind", "engine", "branch", "commit", "tc", "book", "since", "until", "match")


class BadFilter(ValueError):
    pass


def parse(params):
    """The filters in a query (or any mapping with .get) as a dict of the ones given; BadFilter naming the first
    that's wrong."""
    given = {name: params.get(name) for name in FILTERS if params.get(name)}
    if "kind" in given and given["kind"] not in KINDS:
        raise BadFilter(f"kind must be one of {', '.join(KINDS)}")
    if "commit" in given and len(given["commit"]) < 7:
        raise BadFilter("commit must be at least 7 characters")
    for name in ("since", "until"):
        if name in given:
            try:
                given[name] = date.fromisoformat(given[name])
            except ValueError:
                raise BadFilter(f"{name} must be an ISO 8601 date, like 2026-10-17")
    return given


def matches(filters):
    queryset = Match.objects.all()
    if "kind" in filters:
        queryset = queryset.filter(kind=filters["kind"])
    if "engine" in filters:
        queryset = queryset.filter(subject__name=filters["engine"])
    if "branch" in filters:
        queryset = queryset.filter(subject_info__branch=filters["branch"])
    if "commit" in filters:
        queryset = queryset.filter(subject__commit__startswith=filters["commit"])
    if "tc" in filters:
        queryset = queryset.filter(settings__tc=filters["tc"])
    if "book" in filters:
        queryset = queryset.filter(settings__book=filters["book"])
    if "match" in filters:
        queryset = queryset.filter(pk=filters["match"])
    return queryset


def window(filters):
    """The received_at bounds as aware datetimes: since from the start of its day, until to the end of its day."""
    low = high = None
    if "since" in filters:
        low = datetime.combine(filters["since"], time.min, timezone.utc)
    if "until" in filters:
        high = datetime.combine(filters["until"] + timedelta(days=1), time.min, timezone.utc)
    return low, high


def chunks(filters):
    """The export as bytes, piece by piece. Always a valid bz2 stream, an empty one when nothing matches."""
    ids = matches(filters).values("pk")
    low, high = window(filters)
    batches = Batch.objects.filter(match_id__in=ids, games__gt=0)
    # a finished game already in a batch is exported from there, once
    singles = Game.objects.filter(match_id__in=ids, kept_in=None).exclude(result="*")
    if low:
        batches, singles = batches.filter(created_at__gte=low), singles.filter(received_at__gte=low)
    if high:
        batches, singles = batches.filter(created_at__lt=high), singles.filter(received_at__lt=high)
    sent = False
    for row in batches.order_by("match_id", "number"):
        try:
            data = catalogue.batch_bytes(row)
        except OSError:
            continue
        sent = True
        yield bz2.compress(data)
    for row in singles.order_by("match_id", "game"):
        pgn = read_pgn(row.match_id, row.game)
        if pgn:
            sent = True
            yield bz2.compress(pgn.rstrip(b"\n") + b"\n\n")
    if not sent:
        yield bz2.compress(b"")
