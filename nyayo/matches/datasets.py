"""Position datasets (game database, milestone 10): positions drawn from the games kept, for elephant's tuner.

A dataset is a job for the worker. Requested from the Exports page or `POST /api/exports`, it waits until the worker
has nothing else to do, then runs one batch a turn so uploads never wait behind it: the selected games of each sealed
batch, as stored, go to one `phant-indexer --draw` process that stays open from the first batch to the last, so it
dedupes positions across the whole dataset, and the `<fen> [result]` lines it writes become the dataset's file, zstd,
in NYAYO_DATASETS_DIR. One dataset runs at a time; one left running by a worker that stopped starts again.

The record stays when the file is deleted: the selection, the draw's settings and seed, nyayo's and the reader's
versions with the reader's sha256, the counts, and the manifest of every batch read with its sha256 and the games
taken from it. A rebuild reads exactly those games again, each batch checked against its sha256, with the same
settings and seed, and checks the positions' sha256 against the one recorded.
"""

import hashlib
import logging
import random
import subprocess
import threading
from pathlib import Path

import zstandard
from django.conf import settings
from django.utils import timezone

from . import cards, catalogue, selection
from .models import Batch, Dataset
from .reader import file_sha256

log = logging.getLogger("nyayo.datasets")

# phant-indexer --draw 10 --quiet --min-ply 16: ten quiet positions a game at most, past the 8-move book
DRAW = {"per_game": 10, "quiet": True, "min_ply": 16}
ZSTD_LEVEL = 10


class DatasetError(Exception):
    pass


def directory():
    return Path(settings.NYAYO_DATASETS_DIR)


def file_path(dataset):
    return directory() / f"positions-{dataset.pk}.txt.zst"


def nyayo_version():
    """The checkout nyayo runs from, as `git describe` says it; "unknown" outside a git checkout."""
    try:
        found = subprocess.run(["git", "-C", str(settings.BASE_DIR), "describe", "--always", "--dirty", "--tags"],
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return found.stdout.strip() or "unknown"


def draw_arguments(draw):
    """phant-indexer's flags for the draw's settings and seed."""
    flags = ["--draw", str(draw["per_game"]), "--min-ply", str(draw["min_ply"]), "--seed", str(draw["seed"])]
    return flags + (["--quiet"] if draw["quiet"] else [])


def request(requested_by="", note=""):
    """A new dataset, waiting for the worker: V1's selection, every game that read with a good label."""
    chosen = selection.Selection(endings=selection.endings_kept())
    return Dataset.objects.create(requested_by=requested_by[:100], note=note, selection=chosen.described(),
                                  draw={**DRAW, "seed": random.randrange(1, 2 ** 31)})


def rebuild(dataset):
    """Runs a finished dataset again from its record: the same batches and games, settings and seed."""
    if dataset.state != "done":
        raise DatasetError("only a finished dataset can be rebuilt")
    dataset.rebuild_of_sha256, dataset.rebuild_matched = dataset.sha256, None
    reset(dataset, "requested")
    dataset.save()


def delete_file(dataset):
    """Removes the dataset's file and keeps its record."""
    file_path(dataset).unlink(missing_ok=True)
    dataset.file_deleted_at = timezone.now()
    dataset.save(update_fields=["file_deleted_at"])


def reset(dataset, state):
    dataset.state, dataset.error = state, ""
    dataset.started_at = dataset.finished_at = None
    dataset.batches_done = dataset.games = dataset.positions = 0


def manifest_for(chosen):
    """[{id, match, number, sha256, games: [[place, offset, length]]}] for the selection's games in sealed batches,
    in order; games in a batch still open wait for a later dataset."""
    where, params = chosen.where()
    db = catalogue.index()
    out = []
    for batch in Batch.objects.filter(match__in=chosen.matches(), state="sealed", games__gt=0).order_by("pk"):
        rows = db.execute(f"SELECT position, offset, length FROM card WHERE batch = ? AND {where} ORDER BY position",
                          (batch.pk, *params)).fetchall()
        if rows:
            out.append({"id": batch.pk, "match": batch.match_id, "number": batch.number, "sha256": batch.sha256,
                        "games": [list(row) for row in rows]})
    return out


def selection_of(dataset):
    given = dataset.selection or {}
    return selection.Selection(matches=given.get("matches") or (), openbench=given.get("openbench") or (),
                               kind=given.get("kind"), book=given.get("book"), tc=given.get("tc"),
                               endings=[cards.ENDINGS.index(e) for e in given["endings"]]
                               if given.get("endings") is not None else None)


def batch_games(entry):
    """The games a manifest entry names, from the sealed batch alone, after checking its sha256."""
    batch = Batch.objects.filter(pk=entry["id"], match_id=entry["match"], number=entry["number"]).first()
    if batch is None:
        raise DatasetError(f"batch {entry['id']} ({entry['match']} {entry['number']}) is gone")
    stored = catalogue.sealed_path(batch).read_bytes()
    if hashlib.sha256(stored).hexdigest() != entry["sha256"]:
        raise DatasetError(f"batch {entry['id']} ({entry['match']} {entry['number']}) changed: its sha256 differs")
    data = zstandard.ZstdDecompressor().decompress(stored)
    return b"".join(data[offset:offset + length].rstrip() + b"\n\n" for _, offset, length in entry["games"])


class Run:
    """One dataset being drawn: the reader's process, open from the first batch to the last, and its output going
    to the file as it comes."""

    def __init__(self, dataset, reader):
        command = getattr(reader, "command", None)
        if command is None:
            raise DatasetError(f"drawing positions needs phant-indexer (NYAYO_READER); the reader is {reader.name}")
        self.dataset = dataset
        directory().mkdir(parents=True, exist_ok=True)
        self.partial = file_path(dataset).with_suffix(".zst.part")
        self.process = subprocess.Popen([*command, *draw_arguments(dataset.draw)], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.out = open(self.partial, "wb")
        self.stream = zstandard.ZstdCompressor(level=ZSTD_LEVEL).stream_writer(self.out, closefd=False)
        self.digest, self.positions, self.errors = hashlib.sha256(), 0, b""
        self.drains = [threading.Thread(target=self.drain, daemon=True),
                       threading.Thread(target=self.drain_errors, daemon=True)]
        for thread in self.drains:
            thread.start()

    def drain(self):
        for line in self.process.stdout:
            if line.strip():
                self.stream.write(line)
                self.digest.update(line)
                self.positions += 1

    def drain_errors(self):
        self.errors = self.process.stderr.read()[-2000:]

    def feed(self, pgn):
        self.process.stdin.write(pgn)
        self.process.stdin.flush()

    def finish(self):
        """Closes the reader's input and waits for its last positions. Returns the file's path."""
        self.process.stdin.close()
        for thread in self.drains:
            thread.join()
        code = self.process.wait()
        self.stream.close()
        self.out.close()
        if code:
            raise DatasetError(f"{' '.join(self.process.args[1:3])}... exited {code}: "
                               f"{self.errors.decode('utf-8', 'replace').strip()[:500]}")
        final = file_path(self.dataset)
        self.partial.replace(final)
        return final

    def stop(self):
        if self.process.poll() is None:
            self.process.kill()
        self.out.close()
        self.partial.unlink(missing_ok=True)


# the worker's one dataset in progress; a worker restarted finds its dataset "running" and starts it over
_run = None


def begin(dataset, reader):
    """Starts a dataset: its manifest from the selection (a rebuild keeps the one it has), the versions, the
    reader's process."""
    global _run
    if not dataset.rebuild_of_sha256:
        dataset.manifest = manifest_for(selection_of(dataset))
    reset(dataset, "running")
    dataset.started_at = timezone.now()
    dataset.batches_total = len(dataset.manifest)
    dataset.nyayo_version = nyayo_version()[:100]
    dataset.reader = reader.name
    path = getattr(reader, "path", None)
    dataset.reader_sha256 = file_sha256(path) if path else ""
    dataset.file_deleted_at = None
    dataset.save()
    _run = Run(dataset, reader)
    log.info("dataset %d: %d batches to draw from", dataset.pk, dataset.batches_total)


def end(dataset):
    """The last batch is in: the positions are written, their sha256 recorded and, for a rebuild, compared."""
    global _run
    run, _run = _run, None
    path = run.finish()
    dataset.positions, dataset.sha256 = run.positions, run.digest.hexdigest()
    dataset.file_bytes = path.stat().st_size
    if dataset.rebuild_of_sha256:
        dataset.rebuild_matched = dataset.sha256 == dataset.rebuild_of_sha256
    dataset.state, dataset.finished_at = "done", timezone.now()
    dataset.save()
    log.info("dataset %d: %d positions from %d games%s", dataset.pk, dataset.positions, dataset.games,
             "" if dataset.rebuild_matched is None else
             (", the same as before" if dataset.rebuild_matched else ", NOT the same as before"))


def fail(dataset, error):
    global _run
    if _run is not None:
        _run.stop()
        _run = None
    dataset.state, dataset.error, dataset.finished_at = "failed", str(error)[:2000], timezone.now()
    dataset.save()
    log.warning("dataset %d failed: %s", dataset.pk, error)


def step(reader):
    """One turn of the dataset in progress, or the start of the oldest one waiting. False when there is none."""
    dataset = Dataset.objects.filter(state__in=("requested", "running")).order_by("requested_at", "pk").first()
    if dataset is None:
        return False
    try:
        if _run is None or _run.dataset.pk != dataset.pk:
            if _run is not None:
                _run.stop()
            begin(dataset, reader)
            return True
        if dataset.batches_done < dataset.batches_total:
            entry = dataset.manifest[dataset.batches_done]
            _run.feed(batch_games(entry))
            dataset.batches_done += 1
            dataset.games += len(entry["games"])
            dataset.positions = _run.positions
            dataset.save(update_fields=["batches_done", "games", "positions"])
        if dataset.batches_done >= dataset.batches_total:
            end(dataset)
    except (DatasetError, OSError) as error:
        fail(dataset, error)
    return True
