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
import json
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

# phant-indexer --draw 10 --quiet: ten quiet positions a game at most; the first ply by the match's book
# (NYAYO_BOOK_MIN_PLY), and with merge each position written once with its results averaged
DRAW = {"per_game": 10, "quiet": True}
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


def book_key(book):
    """A book's name as the map has it: `8moves_v3.epd` is `8moves_v3`."""
    return Path(book or "").stem.lower()


def min_ply_for(book, draw):
    """The first ply positions are taken from for a match's book, from the map the dataset recorded."""
    if isinstance(draw.get("min_ply"), int):  # a dataset from before the map: one minimum for every book
        return draw["min_ply"]
    found = {book_key(name): ply for name, ply in draw["min_ply"]["books"].items()}
    return found.get(book_key(book), draw["min_ply"]["default"])


def draw_arguments(draw, manifest):
    """phant-indexer's flags for the draw's settings and seed. Until --draw takes a minimum per game or per input
    (elephant's, being settled), one --min-ply for the whole run: the lowest of the manifest's books, so no book loses
    positions it should have."""
    lowest = min((entry["min_ply"] for entry in manifest if "min_ply" in entry), default=min_ply_for(None, draw))
    flags = ["--draw", str(draw["per_game"]), "--min-ply", str(lowest), "--seed", str(draw["seed"])]
    return flags + (["--quiet"] if draw["quiet"] else []) + (["--merge"] if draw.get("merge") else [])


def request(requested_by="", note="", merge=False):
    """A new dataset, waiting for the worker: V1's selection, every game that read with a good label, and the book
    map in force now, kept with it."""
    chosen = selection.Selection(endings=selection.endings_kept())
    return Dataset.objects.create(requested_by=requested_by[:100], note=note, selection=chosen.described(),
                                  draw={**DRAW, "merge": bool(merge), "seed": random.randrange(1, 2 ** 31),
                                        "min_ply": {"books": dict(settings.NYAYO_BOOK_MIN_PLY),
                                                    "default": settings.NYAYO_MIN_PLY_DEFAULT}})


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


def manifest_for(chosen, draw):
    """[{id, match, number, sha256, book, min_ply, games: [[place, offset, length]]}] for the selection's games in
    sealed batches, in order, each batch with its match's book and the first ply it is drawn from; games in a batch
    still open wait for a later dataset."""
    where, params = chosen.where()
    db = catalogue.index()
    out = []
    batches = Batch.objects.filter(match__in=chosen.matches(), state="sealed", games__gt=0).select_related("match")
    for batch in batches.order_by("pk"):
        rows = db.execute(f"SELECT position, offset, length FROM card WHERE batch = ? AND {where} ORDER BY position",
                          (batch.pk, *params)).fetchall()
        if rows:
            book = (batch.match.settings or {}).get("book") or ""
            out.append({"id": batch.pk, "match": batch.match_id, "number": batch.number, "sha256": batch.sha256,
                        "book": book, "min_ply": min_ply_for(book, draw), "games": [list(row) for row in rows]})
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
        # the reader's statistics of the draw, JSON, read into the record once it's done
        self.stats_path = directory() / f"positions-{dataset.pk}.stats.json"
        self.stats_path.unlink(missing_ok=True)
        flags = [*draw_arguments(dataset.draw, dataset.manifest), "--stats", str(self.stats_path)]
        self.process = subprocess.Popen([*command, *flags], stdin=subprocess.PIPE,
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

    def stats(self):
        """The reader's statistics, None when it wrote none; the file goes once read, the record keeps them."""
        try:
            found = json.loads(self.stats_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        self.stats_path.unlink(missing_ok=True)
        return found

    def stop(self):
        if self.process.poll() is None:
            self.process.kill()
        self.out.close()
        self.partial.unlink(missing_ok=True)
        self.stats_path.unlink(missing_ok=True)


# the worker's one dataset in progress; a worker restarted finds its dataset "running" and starts it over
_run = None


def begin(dataset, reader):
    """Starts a dataset: its manifest from the selection (a rebuild keeps the one it has), the versions, the
    reader's process."""
    global _run
    if not dataset.rebuild_of_sha256:
        dataset.manifest = manifest_for(selection_of(dataset), dataset.draw)
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
    dataset.stats = run.stats()
    dataset.file_bytes = path.stat().st_size
    if dataset.rebuild_of_sha256:
        dataset.rebuild_matched = dataset.sha256 == dataset.rebuild_of_sha256
    dataset.state, dataset.finished_at = "done", timezone.now()
    dataset.save()
    # --draw's own summary on stderr: games, quiet positions looked at, the yield, written and duplicates
    summary = run.errors.decode("utf-8", "replace").strip()
    if summary:
        log.info("dataset %d: the reader says: %s", dataset.pk, " | ".join(summary.splitlines()))
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


# The reader's statistics, by the names it writes them (--stats). elephant's names are still being settled; these are
# nyayo's reading of the brief, and the only place to change when they differ.
STATS_FIELDS = {
    "games_read": ("games", "read"), "games_skipped": ("games", "skipped"), "candidates": ("candidates",),
    "quiet": ("quiet",), "per_game_mean": ("quiet_per_game", "mean"), "per_game_median": ("quiet_per_game", "median"),
    "per_game_p5": ("quiet_per_game", "p5"), "per_game_min": ("quiet_per_game", "min"),
    "short_games": ("short_games",), "written": ("written",), "duplicates": ("duplicates",),
    "white_wins": ("results", "white"), "draws": ("results", "draw"), "black_wins": ("results", "black"),
}
STATS_LABELS = [
    ("games_read", "games read"), ("games_skipped", "games skipped"), ("candidates", "candidate positions"),
    ("quiet", "naturally quiet"), ("per_game_mean", "quiet per game, mean"), ("per_game_median", "median"),
    ("per_game_p5", "5th percentile"), ("per_game_min", "minimum"), ("short_games", "games giving fewer than N"),
    ("written", "positions written"), ("duplicates", "duplicates dropped or merged"), ("white_wins", "white wins"),
    ("draws", "draws"), ("black_wins", "black wins"),
]


def stats_view(stats):
    """A dataset's statistics as the page shows them: the compact figures (written, quiet share, W/D/L shares) and
    the full table's rows, (label, value); None for a dataset without any."""
    if not isinstance(stats, dict):
        return None

    def get(name):
        value = stats
        for part in STATS_FIELDS[name]:
            value = value.get(part) if isinstance(value, dict) else None
        return value

    found = {name: get(name) for name in STATS_FIELDS}
    written = found["written"] or 0
    results = [found["white_wins"], found["draws"], found["black_wins"]]

    def share(part, whole):
        return f"{100 * part / whole:.0f}%" if isinstance(part, (int, float)) and whole else None

    return {
        "written": found["written"], "quiet_share": share(found["quiet"], found["candidates"]),
        "wdl": [share(n, written) for n in results] if all(isinstance(n, int) for n in results) else None,
        "rows": [(label, found[name]) for name, label in STATS_LABELS if found[name] is not None],
    }


def settings_view(draw):
    """A dataset's draw settings as the page shows them: compact (`10/game · merged`) and as rows, (label, value),
    the book map among them."""
    draw = draw or {}
    minimum = draw.get("min_ply")
    if isinstance(minimum, dict):
        books = ", ".join(f"{name} {ply}" for name, ply in sorted(minimum.get("books", {}).items()))
        first_ply = f"{books}; any other book {minimum.get('default')}" if books else f"{minimum.get('default')}"
    else:  # a dataset from before the map
        first_ply = f"{minimum} for every book"
    merge = bool(draw.get("merge"))
    return {
        "compact": f"{draw.get('per_game')}/game" + (" · merged" if merge else ""),
        "rows": [("positions per game", draw.get("per_game")), ("quiet only", "yes" if draw.get("quiet") else "no"),
                 ("merged", "yes, each position once with its results averaged" if merge else "no"),
                 ("seed", draw.get("seed")), ("first ply, by book", first_ply)],
    }
