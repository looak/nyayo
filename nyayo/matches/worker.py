"""nyayo's worker: the games kept as data, put away in the background (`manage.py worker`, nyayo-worker.service).

It looks for work every second or so, oldest first, one thing at a time; the queue is the database. Nothing a
producer sends waits for it.

- **A pending upload** is read whole first, every game through the reader and checked as the endpoint used to check
  them (every move legal, a final result, [Subject] white or black, [Opponent] one of the match's). The first game
  that fails refuses the upload with the reason, and none of it is kept. Then its games go, as they came, into the
  match's open batch, sealed at 64 games, each with its card; then the upload is done and its file deleted. Its row
  counts the games appended, and each batch's row which uploads' games it holds, so a worker stopped half way carries
  on where it was and appends nothing twice.
- **A finished single game** (a gauntlet's, sent move by move and sealed by its final PGN) is read the same way and
  appended to its match's open batch, as it came. Its file and row stay until that batch is sealed; purge_games
  leaves it until then. Its row says which batch it is in, so it is kept once however often it is sent.
- **A match that has ended** (finished, stopped or broken, nothing heard for NYAYO_SEAL_AFTER seconds, no upload
  pending and no game waiting) has its open batch sealed short.
"""

import contextlib
import logging
import os
import time
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from nyayo.runs.ingest import IngestError

from . import cards, catalogue, games as single_games, reader as readers, uploads
from .models import Batch, Game, Match, MatchResult, Upload

log = logging.getLogger("nyayo.worker")


def describe(record):
    tags = record.get("tags") or {}
    return f"game {record['game']} ({tags.get('White', '?')} v {tags.get('Black', '?')})"


def problem(records, opponents):
    """Why an upload's games can't be kept, naming the first game that fails, or None."""
    if not records:
        return "the batch holds no game"
    for record in records:
        where, tags = describe(record), record.get("tags") or {}
        if not record["ok"]:
            error = record.get("error") or {}
            return f"{where}: illegal or unreadable move at ply {error.get('ply', '?')}: {error.get('reason', '?')}"
        result = tags.get("Result", "*")
        if result not in uploads.FINAL:
            return f"{where}: [Result] must be one of {', '.join(uploads.FINAL)}, got {result!r}"
        colour = tags.get("Subject", "")
        if colour.lower() not in ("white", "black"):
            return f"{where}: [Subject] must be white or black, got {colour!r}"
        opponent = tags.get("Opponent", "")
        if opponent not in opponents:
            return f"{where}: [Opponent] {opponent!r} isn't an opponent of the match"
    return None


def card_fields(record, pgn, context, colour, opponent, received=None):
    """A game's card: who played it, its result and plies, and every extractor's facts."""
    tags = record["tags"]
    return {"colour": colour, "opponent": opponent, "result": tags.get("Result", "*"),
            "termination": tags.get("Termination", "")[:100], "plies": record["plies"],
            **cards.all_fields(record, pgn, context, received)}


def seal(batch, reader):
    """Seals a batch; one whose games one reader read all of, as the worker writes them, is marked filled by the
    extractors' current versions, so reindex passes it by."""
    catalogue.seal(batch)
    if batch.reader == reader.name:
        catalogue.mark_filled(batch, cards.versions(reader))


def refuse(upload, reason):
    upload.state, upload.reason, upload.done_at = "refused", reason, timezone.now()
    upload.save(update_fields=["state", "reason", "done_at"])
    log.warning("refused upload %s/%s: %s", upload.match_id, upload.name, reason)


def process_upload(upload, reader):
    """Reads, checks and puts away one pending upload."""
    opponents = dict(upload.match.results.values_list("opponent", "pk"))
    try:
        raw = uploads.read(upload)
    except IngestError as error:
        return refuse(upload, error.reason)
    except OSError as error:
        return refuse(upload, f"its file can't be read: {error}")
    try:
        records = reader.read(raw)
    except readers.ReaderError as error:
        return refuse(upload, f"the reader couldn't read it: {error}")
    reason = problem(records, opponents)
    if reason:
        return refuse(upload, reason)
    if (upload.games, upload.reader) != (len(records), reader.name):
        upload.games, upload.reader = len(records), reader.name
        upload.save(update_fields=["games", "reader"])

    context = cards.Context(upload.match)
    games = []
    for r in records:
        pgn = raw[r["offset"]:r["offset"] + r["length"]]
        tags = r["tags"]
        games.append((f"{upload.name}.{r['game']}", pgn, card_fields(
            r, pgn, context, tags.get("Subject", "").lower(), opponents.get(tags.get("Opponent", "")),
            upload.received_at)))
    while upload.appended < len(games):
        batch = catalogue.open_batch(upload.match_id)
        start = upload.appended
        group = games[start:start + settings.NYAYO_BATCH_GAMES - batch.games]

        def progress(batch, count=len(group)):
            upload.appended += count
            if batch.number not in upload.batches:
                upload.batches = upload.batches + [batch.number]
            upload.save(update_fields=["appended", "batches"])

        catalogue.append(batch, group, reader.name, {"upload": upload.name, "first": start + 1}, progress)
        if catalogue.full(batch):
            seal(batch, reader)
    upload.state, upload.done_at = "done", timezone.now()
    upload.save(update_fields=["state", "done_at"])
    uploads.upload_path(upload.match_id, upload.name).unlink(missing_ok=True)
    log.info("upload %s/%s: %d games kept", upload.match_id, upload.name, len(games))


def read_singles(rows, reader):
    """[(row, PGN as it came, record)] for single games, read in one go when they read as one input; a game that
    doesn't read is left out (it was checked when it was stored, so this is a file gone missing or changed)."""
    texts = [(row, single_games.read_pgn(row.match_id, row.game)) for row in rows]
    for row, raw in texts:
        if raw is None:
            log.warning("game %s/%s has no file, not kept", row.match_id, row.game)
    texts = [(row, raw.rstrip() + b"\n\n") for row, raw in texts if raw]
    joined = b"".join(raw for _, raw in texts)
    records = reader.read(joined) if texts else []
    if len(records) != len(texts):
        # one of them doesn't split as one game: read each on its own
        records = [(reader.read(raw) or [None])[0] for _, raw in texts]
        offsets = [0] * len(texts)
    else:
        offsets, at = [], 0
        for _, raw in texts:
            offsets.append(at)
            at += len(raw)
    out = []
    for (row, raw), start, record in zip(texts, offsets, records):
        if record is None or len(readers.spans(raw)) != 1 or not record["ok"] \
                or record["result"] not in uploads.FINAL:
            reason = (record or {}).get("error") or "it doesn't read as one finished game"
            log.warning("game %s/%s doesn't read, not kept: %s", row.match_id, row.game, reason)
            continue
        offset = record["offset"] - start
        out.append((row, raw[offset:offset + record["length"]], record))
    return out


def keep_singles(match_id, reader, rows=None):
    """Puts a match's waiting single games in its batches, in the order they finished. Returns how many it kept."""
    if rows is None:
        rows = list(Game.objects.filter(match_id=match_id, waiting=True, kept_in=None).order_by("finished_at", "pk"))
    opponents = dict(MatchResult.objects.filter(match_id=match_id).values_list("opponent", "pk"))
    found = read_singles(rows, reader)
    unread = {row.pk for row in rows} - {row.pk for row, _, _ in found}
    if unread:
        Game.objects.filter(pk__in=unread).update(waiting=False)
    context = cards.Context(Match.objects.get(pk=match_id))
    items = [(row.game, pgn, {**card_fields(record, pgn, context, row.colour, opponents.get(row.opponent),
                                            row.finished_at or row.received_at),
                              "result": row.result, "termination": row.termination}, row.pk)
             for row, pgn, record in found]
    while items:
        batch = catalogue.open_batch(match_id)
        group, items = items[:settings.NYAYO_BATCH_GAMES - batch.games], items[settings.NYAYO_BATCH_GAMES - batch.games:]

        def progress(batch, keys=[pk for *_, pk in group]):
            Game.objects.filter(pk__in=keys).update(kept_in=batch, waiting=False)

        catalogue.append(batch, [(game, pgn, fields) for game, pgn, fields, _ in group], reader.name, None, progress)
        if catalogue.full(batch):
            seal(batch, reader)
    if found:
        log.info("match %s: %d finished game%s kept", match_id, len(found), "s" if len(found) != 1 else "")
    return len(found)


def ended_batches(now=None):
    """Open batches of matches that have ended and have nothing more on the way."""
    quiet = (now or timezone.now()) - timedelta(seconds=settings.NYAYO_SEAL_AFTER)
    pending = Upload.objects.filter(state="pending").values("match_id")
    waiting = Game.objects.filter(waiting=True).values("match_id")
    return (Batch.objects.filter(state="open", games__gt=0, match__received_at__lt=quiet)
            .exclude(match__state="running").exclude(match_id__in=pending).exclude(match_id__in=waiting)
            .order_by("pk"))


def step(reader):
    """Does the oldest piece of work there is. False when there was none."""
    upload = Upload.objects.filter(state="pending").select_related("match").order_by("received_at", "pk").first()
    if upload is not None:
        process_upload(upload, reader)
        return True
    match_id = Game.objects.filter(waiting=True).order_by("finished_at", "pk").values_list("match_id", flat=True).first()
    if match_id is not None:
        keep_singles(match_id, reader)
        return True
    batch = ended_batches().first()
    if batch is not None:
        seal(batch, reader)
        log.info("sealed %s/%d short, its match has ended", batch.match_id, batch.number)
        return True
    return False


@contextlib.contextmanager
def only_worker():
    """Holds a lock next to the index database, on local disk, for as long as this worker runs, so two never work
    at once."""
    path = Path(settings.NYAYO_INDEX_DB).with_name("worker.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    file = open(path, "a+")
    try:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError(f"another worker holds {path}")
        yield
    finally:
        file.close()


def is_running():
    """Whether a worker holds the lock now: taking it for a moment and letting it go again says so, and costs nothing
    when one does."""
    try:
        with only_worker():
            return False
    except (RuntimeError, OSError):
        return True


def run(once=False, interval=1.0, reader=None):
    """Works until stopped, or with once until there is nothing left to do. Returns how many things it did."""
    reader = reader or readers.default()
    done = 0
    with only_worker():
        log.info("worker started, reading with %s", reader.name)
        while True:
            close_old_connections()
            try:
                did = step(reader)
            except Exception:
                log.exception("the worker failed, trying again shortly")
                did = False
                if once:
                    raise
            done += did
            if not did:
                if once:
                    return done
                time.sleep(interval)
