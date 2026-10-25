"""Writes the index's cards again from the batches (`manage.py reindex`).

The batches are the source of truth and the cards are derived, so the index can be dropped and rebuilt, and a new
or fixed property fills every old game without a batch being touched. A batch is visited when an extractor it was
filled by has a newer version now (cards.versions), or always with rebuild; each batch is read through the reader
and its cards written in one transaction, then marked filled, so a reindex can be stopped and started again and only
picks up where it was. Positions (a per-batch filter and positions list) will be one more extractor here.
"""

import logging

from . import cards, catalogue, games
from .models import Batch, Game, MatchResult, Upload

log = logging.getLogger("nyayo.reindex")


class Mismatch(Exception):
    """A batch whose games don't read as its row says: never written over, reported."""


def stale(batch, versions, extractors):
    have = catalogue.filled(batch)
    return [name for name in extractors if have.get(name) != versions[name]]


def who(match, game_id, tags, opponents):
    """(colour, MatchResult.id) of a game: its tags, else what the match's schedule or engines say."""
    colour, opponent = tags.get("Subject", "").lower(), tags.get("Opponent", "")
    if colour not in ("white", "black") or opponent not in opponents:
        try:
            opponent, colour = games.identify(match, game_id, tags)
        except Exception:  # an Opponent tag that isn't the match's: leave it unknown
            opponent, colour = "", ""
    return colour, opponents.get(opponent)


def arrivals(batch):
    """When nyayo received each of a batch's games, in order, as the worker dated them when it put them away: an
    upload's games when the upload arrived, a single game when it finished (else arrived). The batch's own creation
    for anything no longer there."""
    uploads = dict(Upload.objects.filter(match_id=batch.match_id).values_list("name", "received_at"))
    named = [source["game"] for source in batch.sources if "game" in source]
    singles = {game: finished or received for game, finished, received in Game.objects.filter(
        match_id=batch.match_id, game__in=named).values_list("game", "finished_at", "received_at")}
    out = []
    for source in batch.sources:
        if "upload" in source:
            out.extend([uploads.get(source["upload"]) or batch.created_at] * source["count"])
        else:
            out.append(singles.get(source["game"]) or batch.created_at)
    return out


def reindex_batch(batch, reader, extractors, rebuild=False, win=None, draw=None):
    """Writes one batch's cards again. Returns how many games."""
    data = catalogue.batch_bytes(batch)
    records = reader.read(data)
    ids = catalogue.game_ids(batch)
    if len(records) != batch.games or len(ids) != batch.games:
        raise Mismatch(f"{batch}: its row says {batch.games} games and {len(ids)} ids, the reader read "
                       f"{len(records)}")
    match = batch.match
    context = cards.Context(match, win, draw)
    versions = cards.versions(reader)
    pieces = [(record, data[record["offset"]:record["offset"] + record["length"]]) for record in records]
    received = arrivals(batch)
    if rebuild or catalogue.card_count(batch) != batch.games:
        opponents = dict(MatchResult.objects.filter(match=match).values_list("opponent", "pk"))
        rows = []
        for game_id, (record, pgn), arrived in zip(ids, pieces, received):
            tags = record.get("tags") or {}
            colour, opponent = who(match, game_id, tags, opponents)
            rows.append((record["game"], game_id, record["offset"], record["length"], {
                "colour": colour, "opponent": opponent, "result": tags.get("Result") or record.get("result"),
                "termination": (tags.get("Termination") or "")[:100], "plies": record.get("plies") or 0,
                **cards.all_fields(record, pgn, context, arrived)}))
        catalogue.write_cards(batch, rows)
        catalogue.mark_filled(batch, versions)
    else:
        rows = [(record["game"], cards.all_fields(record, pgn, context, arrived))
                for (record, pgn), arrived in zip(pieces, received)]
        catalogue.update_cards(batch, rows, extractors)
        catalogue.mark_filled(batch, {name: versions[name] for name in extractors})
    return len(records)


def run(reader, extractors=None, match=None, rebuild=False, win=None, draw=None, dry_run=False, out=None):
    """Every sealed batch that needs it, oldest first. Returns (batches, games, mismatched)."""
    extractors = list(extractors or catalogue.EXTRACTOR_COLUMNS)
    versions = cards.versions(reader)
    queryset = Batch.objects.filter(state="sealed").select_related("match").order_by("pk")
    if match:
        queryset = queryset.filter(match_id=match)
    batches = games_done = 0
    mismatched = []
    for batch in queryset.iterator():
        todo = extractors if rebuild else stale(batch, versions, extractors)
        if not todo:
            continue
        batches += 1
        if dry_run:
            games_done += batch.games
            continue
        try:
            games_done += reindex_batch(batch, reader, todo, rebuild, win, draw)
        except Mismatch as error:
            log.error("%s", error)
            mismatched.append(str(batch))
        if out and batches % 100 == 0:
            out(f"{batches} batches, {games_done} games")
    return batches, games_done, mismatched


def ending_counts(match_ids=None):
    """{(ending, inferred): games} over the cards of the matches named (every match's by default)."""
    queryset = Batch.objects.all()
    if match_ids is not None:
        queryset = queryset.filter(match_id__in=match_ids)
    ids = list(queryset.values_list("pk", flat=True))
    counts = {}
    db = catalogue.index()
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for ending, inferred, count in db.execute(
                f"SELECT ending, flags & {cards.INFERRED}, count(*) FROM card WHERE result != 0 "
                f"AND batch IN ({','.join('?' * len(chunk))}) GROUP BY 1, 2", chunk):
            key = (cards.ENDINGS[ending] if ending is not None else None, bool(inferred))
            counts[key] = counts.get(key, 0) + count
    return counts
