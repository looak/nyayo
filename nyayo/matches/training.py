"""Training positions' games: a selection's games as stored, one zstd stream, and a manifest that can make it again
(`manage.py export_training`; docs/match-schema.md, Training export).

Elephant's tuner draws its positions from the export with `phant-indexer --draw`. The games come from sealed batches
only: a sealed batch never changes, so its sha256 and each game's place, offset and length in it, written in the
manifest, are enough to take exactly the same games again, from the batches alone, after the index is rebuilt or on
another machine. Games still in an open batch, or not in a batch yet, wait for the next export.
"""

import hashlib
import json
import subprocess
from pathlib import Path

import zstandard
from django.conf import settings
from django.utils import timezone

from . import catalogue
from .models import Batch

MANIFEST_FORMAT = 1
ZSTD_LEVEL = 10


class Mismatch(Exception):
    """A batch named by a manifest isn't the one it was taken from: gone, or its sha256 differs."""


def nyayo_version():
    """The checkout nyayo runs from, as `git describe` says it; "unknown" outside a git checkout."""
    try:
        found = subprocess.run(["git", "-C", str(settings.BASE_DIR), "describe", "--always", "--dirty", "--tags"],
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return found.stdout.strip() or "unknown"


def chosen(selection):
    """[(batch, [(position, game id, offset, length)])] for the selection's games in sealed batches, in order, and
    how many of its games are left out for being in a batch still open."""
    matches = selection.matches()
    batches = list(Batch.objects.filter(match__in=matches, games__gt=0).order_by("match_id", "number"))
    where, params = selection.where()
    db = catalogue.index()
    out, waiting = [], 0
    for batch in batches:
        rows = db.execute(f"SELECT position, game, offset, length FROM card WHERE batch = ? AND {where} "
                          "ORDER BY position", (batch.pk, *params)).fetchall()
        if not rows:
            continue
        if batch.state != "sealed":
            waiting += len(rows)
            continue
        out.append((batch, rows))
    return out, waiting


def sealed_bytes(batch, sha256=None):
    """A sealed batch's PGN, decompressed, after checking its file against sha256 (the batch's own when None)."""
    path = catalogue.sealed_path(batch)
    try:
        stored = path.read_bytes()
    except OSError as error:
        raise Mismatch(f"batch {batch.pk} ({batch.match_id} {batch.number}): {error}")
    expected = sha256 or batch.sha256
    if hashlib.sha256(stored).hexdigest() != expected:
        raise Mismatch(f"batch {batch.pk} ({batch.match_id} {batch.number}) isn't the one taken: its sha256 differs")
    return zstandard.ZstdDecompressor().decompress(stored)


def write(out, taken):
    """Writes the games, [(batch, sha256, [(position, offset, length)])], to the open binary file out as one zstd
    stream: each game's PGN as stored, comments and all, then a blank line. Returns how many games."""
    count = 0
    with zstandard.ZstdCompressor(level=ZSTD_LEVEL).stream_writer(out, closefd=False) as stream:
        for batch, sha256, games in taken:
            data = sealed_bytes(batch, sha256)
            for _, offset, length in games:
                stream.write(data[offset:offset + length].rstrip() + b"\n\n")
                count += 1
    return count


def export(selection, name, reader_name):
    """Writes `<name>.pgn.zst` and `<name>.manifest.json` for the selection. Returns the manifest."""
    found, waiting = chosen(selection)
    taken = [(batch, batch.sha256, [(position, offset, length) for position, _, offset, length in rows])
             for batch, rows in found]
    games_path, manifest_path = Path(f"{name}.pgn.zst"), Path(f"{name}.manifest.json")
    with open(games_path, "wb") as out:
        count = write(out, taken)
    manifest = {
        "format": MANIFEST_FORMAT, "name": Path(name).name, "made_at": timezone.now().isoformat(timespec="seconds"),
        "filters": selection.described(), "games": count, "left_out_in_open_batches": waiting,
        "nyayo": nyayo_version(), "reader": reader_name,
        "batches": [{"id": batch.pk, "match": batch.match_id, "number": batch.number, "sha256": batch.sha256,
                     "reader": batch.reader,
                     "games": [{"position": position, "game": game, "offset": offset, "length": length}
                               for position, game, offset, length in rows]}
                    for batch, rows in found],
    }
    manifest_path.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest


def again(manifest_path, name):
    """Writes `<name>.pgn.zst` with exactly the games a manifest names, from the batches alone, each checked against
    the sha256 the manifest recorded. Returns how many games; Mismatch for a batch that isn't the one taken."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    batches = Batch.objects.in_bulk([entry["id"] for entry in manifest["batches"]])
    taken = []
    for entry in manifest["batches"]:
        batch = batches.get(entry["id"])
        if batch is None or (batch.match_id, batch.number) != (entry["match"], entry["number"]):
            raise Mismatch(f"batch {entry['id']} ({entry['match']} {entry['number']}) is gone")
        taken.append((batch, entry["sha256"], [(g["position"], g["offset"], g["length"]) for g in entry["games"]]))
    with open(f"{name}.pgn.zst", "wb") as out:
        return write(out, taken)
