"""Migrating the single games already stored into batches (`manage.py keep_games`).

Every finished single game (games/<match id>/<game>.pgn) is read through the reader and appended, as it came, to its
match's batches by the worker's own path, its id kept, its card written with its ending (cards.ending_fields: by a
rule, by its Termination tag, or deduced and marked inferred). Nothing is converted in place: games/ and archive/ are
untouched, and purge_games remains a separate step that waits for a game's batch to be sealed.

Checked before it says it is done: each match's games in batches equal its finished Game rows; the export before and
after holds the same games; the reader agrees with python-chess on the batches' games. A dry run reads everything and
counts, writing nothing.
"""

import bz2
import hashlib
import logging
from collections import Counter

from . import cards, catalogue, reader as readers, reindex, worker
from .models import Batch, Game, Match

log = logging.getLogger("nyayo.migrate")

AGREE_KEYS = ("game", "offset", "length", "ok", "result", "plies", "ended", "start", "opening", "promotions",
              "castled", "captures", "ep_captures", "checks", "min_pieces", "material")


def todo(match=None):
    """{match id: finished single games not in a batch yet}, in the order they finished."""
    rows = Game.objects.filter(kept_in=None).exclude(result="*").order_by("match_id", "finished_at", "pk")
    if match:
        rows = rows.filter(match_id=match)
    out = {}
    for row in rows:
        out.setdefault(row.match_id, []).append(row)
    return out


def game_hashes(chunks):
    """A multiset of the games in an export, each by the sha256 of its text, whatever batch or file it came from."""
    seen = Counter()
    for chunk in chunks:
        data = bz2.decompress(chunk)
        for offset, length in readers.spans(data):
            seen[hashlib.sha256(data[offset:offset + length]).hexdigest()] += 1
    return seen


def plan(reader, match_id, rows):
    """What keeping a match's games would write, without writing: (games it would keep, endings counted)."""
    context = cards.Context(Match.objects.get(pk=match_id))
    found = worker.read_singles(rows, reader)
    counts = Counter()
    for row, pgn, record in found:
        counts[cards.ENDINGS[worker.card_fields(record, pgn, context, row.colour, None)["ending"]]] += 1
    return len(found), counts


def keep(reader, match=None, dry_run=False, out=print):
    """Puts every finished single game in its match's batches. Returns {match id: (games, endings or None)}."""
    kept = {}
    for match_id, rows in todo(match).items():
        if dry_run:
            kept[match_id] = plan(reader, match_id, rows)
        else:
            worker.keep_singles(match_id, reader, rows)
            kept[match_id] = (Game.objects.filter(pk__in=[r.pk for r in rows], kept_in__isnull=False).count(), None)
        out(f"{match_id}: {kept[match_id][0]} of {len(rows)} games")
    if not dry_run:
        while (batch := worker.ended_batches().first()) is not None:
            worker.seal(batch, reader)
            out(f"sealed {batch.match_id}/{batch.number} short, its match has ended")
    return kept


def check_counts(match=None):
    """Mismatches: each match's finished Game rows against the games kept in its batches from them."""
    problems = []
    for m in (Match.objects.filter(pk=match) if match else Match.objects.all()):
        rows = Game.objects.filter(match=m).exclude(result="*").count()
        kept = Game.objects.filter(match=m, kept_in__isnull=False).count()
        singles = sum(1 for b in Batch.objects.filter(match=m) for s in b.sources if "game" in s)
        if kept != rows or singles != kept:
            problems.append(f"{m.pk}: {rows} finished games, {kept} kept, {singles} single games in batches")
    return problems


def check_reader(reader, match=None):
    """Batches whose games the configured reader and python-chess don't read the same."""
    mine = readers.PythonChess()
    bad = []
    queryset = Batch.objects.filter(state="sealed")
    if match:
        queryset = queryset.filter(match_id=match)
    for batch in queryset.order_by("pk").iterator():
        data = catalogue.batch_bytes(batch)
        a, b = mine.read(data), reader.read(data)
        if len(a) != len(b) or any({k: x.get(k) for k in AGREE_KEYS} != {k: y.get(k) for k in AGREE_KEYS}
                                   for x, y in zip(a, b)):
            bad.append(str(batch))
    return bad


def ending_report(match=None):
    """{ending: games} of the finished games of OpenBench's matches in batches, inferred and carried together."""
    ids = [m for m in Match.objects.filter(producer_name="OpenBench").values_list("pk", flat=True)
           if not match or m == match]
    counts = Counter()
    for (ending, _), n in reindex.ending_counts(ids).items():
        counts[ending] += n
    return counts
