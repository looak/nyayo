"""Every game nyayo keeps as data: its batches and the index of cards (docs/match-schema.md, Batches).

A match's games go into nyayo's own batches, 64 games each (NYAYO_BATCH_GAMES), one match per batch. The batch being
filled is open: a plain PGN file, batches/<match id>/<number>.pgn, the games appended as they came, and its row says
how many games and bytes of it are committed, so a worker stopped half way truncates what came after and carries on.
At 64 games, or when its match has ended, it is sealed: compressed to <number>.pgn.zst, which never changes again,
and the plain file goes.

Each game has a card in the index database, a SQLite file of its own on local disk (NYAYO_INDEX_DB): where the game
is (the batch's id in nyayo's database, its place in the batch, its offset and length in the batch's PGN) and what it
was. Keys are integers into nyayo's database, not names. The cards are derived: the batches and their rows' `sources`
(which uploads and games they hold, in order) are enough to write them again.
"""

import hashlib
import os
import sqlite3
import threading
from pathlib import Path

import zstandard
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Batch

RESULT_CODES = {"*": 0, "1-0": 1, "0-1": 2, "1/2-1/2": 3}
RESULTS = {code: result for result, code in RESULT_CODES.items()}
COLOUR_CODES = {"": 0, "white": 1, "black": 2}
COLOURS = {code: colour for colour, code in COLOUR_CODES.items()}
ZSTD_LEVEL = 19

SCHEMA_VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS card (
    batch INTEGER NOT NULL,         -- Batch.id in nyayo's database
    position INTEGER NOT NULL,      -- the game's place in its batch, from 1
    game TEXT NOT NULL,             -- the producer's id for it, as links and resends name it
    offset INTEGER NOT NULL,        -- where it starts in the batch's PGN, decompressed
    length INTEGER NOT NULL,
    colour INTEGER NOT NULL,        -- the subject's: 0 unknown, 1 white, 2 black
    opponent INTEGER,               -- MatchResult.id in nyayo's database
    result INTEGER NOT NULL,        -- 0 *, 1 1-0, 2 0-1, 3 1/2-1/2
    termination TEXT,               -- the PGN's Termination tag, where it has one
    plies INTEGER NOT NULL,
    PRIMARY KEY (batch, position)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS card_game ON card (game);
"""

_local = threading.local()


def index():
    """This thread's connection to the index database, made (and the schema with it) on first use."""
    path = Path(settings.NYAYO_INDEX_DB)
    cached = getattr(_local, "connections", {})
    if path in cached:
        return cached[path]
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    if db.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        db.executescript(SCHEMA)
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    cached[path] = db
    _local.connections = cached
    return db


def close_index():
    for db in getattr(_local, "connections", {}).values():
        db.close()
    _local.connections = {}


# --- the files ---

def batches_dir():
    return Path(settings.NYAYO_BATCHES_DIR)


def open_path(batch):
    return batches_dir() / batch.match_id / f"{batch.number}.pgn"


def sealed_path(batch):
    return batches_dir() / batch.match_id / f"{batch.number}.pgn.zst"


def batch_bytes(batch):
    """The batch's whole PGN, decompressed."""
    if batch.state == "sealed":
        return zstandard.ZstdDecompressor().decompress(sealed_path(batch).read_bytes())
    with open(open_path(batch), "rb") as file:
        return file.read(batch.bytes)


def read_at(batch, offset, length):
    """length bytes from offset in the batch's PGN, decompressing a sealed batch only up to there."""
    if batch.state == "sealed":
        with open(sealed_path(batch), "rb") as file:
            reader = zstandard.ZstdDecompressor().stream_reader(file)
            reader.read(offset)
            return reader.read(length)
    with open(open_path(batch), "rb") as file:
        file.seek(offset)
        return file.read(length)


# --- appending and sealing ---

def open_batch(match_id):
    """The match's open batch, made when it has none."""
    batch = Batch.objects.filter(match_id=match_id, state="open").order_by("number").first()
    if batch is not None:
        return batch
    last = Batch.objects.filter(match_id=match_id).order_by("-number").values_list("number", flat=True).first()
    return Batch.objects.create(match_id=match_id, number=(last or 0) + 1)


def recover(batch):
    """An open batch's file cut back to what its row committed: a worker stopped between writing games and saying
    so leaves bytes nobody counted."""
    path = open_path(batch)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as file:
        if file.tell() != batch.bytes:
            file.truncate(batch.bytes)


def append(batch, games, reader, source, on_commit=None):
    """Appends games, [(game id, PGN bytes, card fields)], to an open batch with room for all of them, then writes
    their cards. source is what the batch's row records for them: {"upload": name, "first": n} for a run of an
    upload's games, or None for single games, recorded by id. on_commit(batch) runs inside the same transaction, for
    the caller to record its own progress with the batch's."""
    recover(batch)
    offsets, data = [], bytearray()
    for _, pgn, _ in games:
        offsets.append(batch.bytes + len(data))
        data += pgn.rstrip() + b"\n\n"
    with open(open_path(batch), "ab") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    first = batch.games + 1
    with transaction.atomic():
        batch.games += len(games)
        batch.bytes += len(data)
        batch.reader = reader
        sources = list(batch.sources)
        if source is not None:
            if sources and sources[-1].get("upload") == source["upload"] and \
                    sources[-1]["first"] + sources[-1]["count"] == source["first"]:
                sources[-1] = {**sources[-1], "count": sources[-1]["count"] + len(games)}
            else:
                sources.append({**source, "count": len(games)})
        else:
            sources.extend({"game": game_id} for game_id, _, _ in games)
        batch.sources = sources
        batch.save(update_fields=["games", "bytes", "reader", "sources"])
        if on_commit is not None:
            on_commit(batch)
    write_cards(batch, [(first + i, game_id, offsets[i], len(pgn.rstrip()), fields)
                        for i, (game_id, pgn, fields) in enumerate(games)])
    return batch


def write_cards(batch, cards):
    """[(position, game id, offset, length, fields)] into the index; fields: colour, opponent, result, termination,
    plies."""
    db = index()
    db.execute("BEGIN")
    db.executemany(
        "INSERT OR REPLACE INTO card (batch, position, game, offset, length, colour, opponent, result, termination, "
        "plies) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(batch.pk, position, game_id, offset, length, COLOUR_CODES.get(f.get("colour") or "", 0),
          f.get("opponent"), RESULT_CODES.get(f.get("result"), 0), f.get("termination") or None, f.get("plies", 0))
         for position, game_id, offset, length, f in cards])
    db.execute("COMMIT")


def seal(batch):
    """Compresses an open batch into its sealed file; its row says so only once the file is there."""
    recover(batch)
    plain = batch_bytes(batch)
    stored = zstandard.ZstdCompressor(level=ZSTD_LEVEL).compress(plain)
    path = sealed_path(batch)
    temporary = path.with_suffix(".zst.tmp")
    with open(temporary, "wb") as file:
        file.write(stored)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
    batch.state, batch.stored_bytes = "sealed", len(stored)
    batch.sha256, batch.sealed_at = hashlib.sha256(stored).hexdigest(), timezone.now()
    batch.save(update_fields=["state", "stored_bytes", "sha256", "sealed_at"])
    open_path(batch).unlink(missing_ok=True)
    return batch


def full(batch):
    return batch.games >= settings.NYAYO_BATCH_GAMES


def game_ids(batch):
    """The ids of a batch's games in order, from its sources."""
    ids = []
    for source in batch.sources:
        if "upload" in source:
            ids.extend(f"{source['upload']}.{source['first'] + i}" for i in range(source["count"]))
        else:
            ids.append(source["game"])
    return ids


# --- reading ---

CARD_COLUMNS = "batch, position, game, offset, length, colour, opponent, result, termination, plies"


def as_card(row):
    batch, position, game, offset, length, colour, opponent, result, termination, plies = row
    return {"batch": batch, "position": position, "game": game, "offset": offset, "length": length,
            "colour": COLOURS.get(colour, ""), "opponent": opponent, "result": RESULTS.get(result, "*"),
            "termination": termination or "", "plies": plies}


def find(match_id, game_id):
    """(Batch, card) for a game kept in a batch, or None."""
    ids = set(Batch.objects.filter(match_id=match_id).values_list("pk", flat=True))
    if not ids:
        return None
    rows = index().execute(f"SELECT {CARD_COLUMNS} FROM card WHERE game = ?", (game_id,)).fetchall()
    for row in rows:
        if row[0] in ids:
            return Batch.objects.get(pk=row[0]), as_card(row)
    return None


def read_game(match_id, game_id):
    """A kept game's PGN, as it came, or None."""
    found = find(match_id, game_id)
    if found is None:
        return None
    batch, card = found
    try:
        return read_at(batch, card["offset"], card["length"]) + b"\n"
    except OSError:
        return None


def cards(match_id):
    """Every card of a match's games, in batch order."""
    ids = list(Batch.objects.filter(match_id=match_id).order_by("number").values_list("pk", flat=True))
    out = []
    db = index()
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        rows = db.execute(f"SELECT {CARD_COLUMNS} FROM card WHERE batch IN ({','.join('?' * len(chunk))})",
                          chunk).fetchall()
        order = {pk: i for i, pk in enumerate(chunk)}
        rows.sort(key=lambda row: (order[row[0]], row[1]))
        out.extend(as_card(row) for row in rows)
    return out


def forget(batches):
    """Removes batches' files, cards and rows (purge_batches)."""
    batches = list(batches)
    db = index()
    db.execute("BEGIN")
    db.executemany("DELETE FROM card WHERE batch = ?", [(b.pk,) for b in batches])
    db.execute("COMMIT")
    for batch in batches:
        open_path(batch).unlink(missing_ok=True)
        sealed_path(batch).unlink(missing_ok=True)
        Batch.objects.filter(pk=batch.pk).delete()


def purge(match_id):
    """Everything a match keeps as data: its batches with their cards, and its uploads. Returns (batches, uploads)."""
    from . import uploads

    batches = list(Batch.objects.filter(match_id=match_id))
    forget(batches)
    try:
        (batches_dir() / match_id).rmdir()
    except OSError:
        pass
    return len(batches), uploads.forget(match_id)
