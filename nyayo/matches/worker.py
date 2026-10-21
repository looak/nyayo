"""nyayo's worker: the games kept as data, put away in the background (`manage.py worker`, nyayo-worker.service).

It looks for work every second or so, oldest first, one thing at a time; the queue is the database. Nothing a
producer sends waits for it.

- **A pending upload** is read whole first, every game through the reader and checked as the endpoint used to check
  them (every move legal, a final result, [Subject] white or black, [Opponent] one of the match's). The first game
  that fails refuses the upload with the reason, and none of it is kept. Then its games go, as they came, into the
  match's open batch, sealed at 64 games, each with its card; then the upload is done and its file deleted. Its row
  counts the games appended, and each batch's row which uploads' games it holds, so a worker stopped half way carries
  on where it was and appends nothing twice.
- **A match that has ended** (finished, stopped or broken, nothing heard for NYAYO_SEAL_AFTER seconds and no upload
  pending) has its open batch sealed short.
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

from . import catalogue, reader as readers, uploads
from .models import Batch, Upload

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


def card_fields(record, opponents):
    tags = record["tags"]
    return {"colour": tags.get("Subject", "").lower(), "opponent": opponents.get(tags.get("Opponent", "")),
            "result": tags.get("Result", "*"), "termination": tags.get("Termination", "")[:100],
            "plies": record["plies"]}


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
    records = reader.read(raw)
    reason = problem(records, opponents)
    if reason:
        return refuse(upload, reason)
    if (upload.games, upload.reader) != (len(records), reader.name):
        upload.games, upload.reader = len(records), reader.name
        upload.save(update_fields=["games", "reader"])

    games = [(f"{upload.name}.{r['game']}", raw[r["offset"]:r["offset"] + r["length"]], card_fields(r, opponents))
             for r in records]
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
            catalogue.seal(batch)
    upload.state, upload.done_at = "done", timezone.now()
    upload.save(update_fields=["state", "done_at"])
    uploads.upload_path(upload.match_id, upload.name).unlink(missing_ok=True)
    log.info("upload %s/%s: %d games kept", upload.match_id, upload.name, len(games))


def ended_batches(now=None):
    """Open batches of matches that have ended and have nothing more on the way."""
    quiet = (now or timezone.now()) - timedelta(seconds=settings.NYAYO_SEAL_AFTER)
    pending = Upload.objects.filter(state="pending").values("match_id")
    return (Batch.objects.filter(state="open", games__gt=0, match__received_at__lt=quiet)
            .exclude(match__state="running").exclude(match_id__in=pending).order_by("pk"))


def step(reader):
    """Does the oldest piece of work there is. False when there was none."""
    upload = Upload.objects.filter(state="pending").select_related("match").order_by("received_at", "pk").first()
    if upload is not None:
        process_upload(upload, reader)
        return True
    batch = ended_batches().first()
    if batch is not None:
        catalogue.seal(batch)
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
