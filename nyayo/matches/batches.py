"""Batches of finished games, kept as data (docs/match-schema.md, Batches).

A producer that plays many short games sends them in bulk once played: `PUT /api/matches/{id}/pgns/{batch}`. The batch
is checked whole before anything is stored (it decompresses, holds a game, every game parses, every move is legal and
every result is final), then kept as sent in games/batches/<match id>/<batch>.pgn.bz2 with one index row per batch and
none per game. A batch is sealed on arrival. Batches are never purged with the live games; `manage.py purge_batches`
removes a match's by hand.
"""

import bz2
import hashlib
import io
from pathlib import Path

import chess.pgn
from django.conf import settings
from django.utils import timezone

from nyayo.runs.ingest import IngestError, atomic_write, invalid
from nyayo.runs.views import decompress

from .ingest import valid_id
from .models import GAME_ID_MAX, MATCH_ID_MAX, GameBatch, Match

FINAL = ("1-0", "0-1", "1/2-1/2")
BZIP2, PGN = "application/x-bzip2", "application/x-chess-pgn"


def batches_dir():
    return Path(settings.NYAYO_BATCHES_DIR)


def batch_path(match_id, batch):
    return batches_dir() / match_id / f"{batch}.pgn.bz2"


def bunzip(body, limit):
    """Everything in a bz2 body (concatenated streams are one batch), refusing what inflates past the limit."""
    out, size, data = [], 0, body
    while data:
        inflater = bz2.BZ2Decompressor()
        try:
            chunk = inflater.decompress(data, max_length=1 << 20)
            while True:
                size += len(chunk)
                if size > limit:
                    raise invalid(f"batch is larger than {limit} bytes once decompressed")
                out.append(chunk)
                if inflater.eof:
                    break
                if inflater.needs_input:
                    raise invalid("bad bzip2 body: it ends early")
                chunk = inflater.decompress(b"", max_length=1 << 20)
        except (OSError, ValueError) as error:
            raise invalid(f"bad bzip2 body: {error}")
        data = inflater.unused_data
    return b"".join(out)


def inflate(body, content_type, encoding):
    """The batch's PGN text as bytes. bz2 for application/x-bzip2; otherwise PGN, plain or gzipped."""
    if content_type == BZIP2:
        return bunzip(body, settings.NYAYO_MAX_BATCH_BYTES)
    if content_type != PGN:
        raise invalid(f"Content-Type must be {BZIP2} or {PGN}")
    return decompress(body, encoding)


def check(raw, opponents):
    """Parses every game of a batch and returns how many there are; IngestError 400 naming the game by its position
    for the first thing wrong."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise invalid("the batch isn't UTF-8")
    stream = io.StringIO(text)
    count = 0
    while True:
        try:
            game = chess.pgn.read_game(stream)
        except (ValueError, KeyError) as error:
            raise invalid(f"game {count + 1}: not a PGN: {error}")
        if game is None:
            break
        count += 1
        where = f"game {count} ({game.headers.get('White', '?')} v {game.headers.get('Black', '?')})"
        if game.errors:
            raise invalid(f"{where}: illegal or unreadable move: {game.errors[0]}")
        result = game.headers.get("Result", "*")
        if result not in FINAL:
            raise invalid(f"{where}: [Result] must be one of {', '.join(FINAL)}, got {result!r}")
        colour, opponent = game.headers.get("Subject", ""), game.headers.get("Opponent", "")
        if colour and colour.lower() not in ("white", "black"):
            raise invalid(f"{where}: [Subject] must be white or black, got {colour!r}")
        if opponent and opponent not in opponents:
            raise invalid(f"{where}: [Opponent] {opponent!r} isn't an opponent of the match")
    if count == 0:
        raise invalid("the batch holds no game")
    return count


def store(match_id, batch, body, content_type, encoding, machine):
    """Stores one batch. Returns True when it is new, False for an identical resend. 400 for anything wrong with it,
    404 for an unknown match, 409 for different content under a stored batch's name."""
    if not valid_id(batch, GAME_ID_MAX):
        raise invalid(f"batch id must be 1 to {GAME_ID_MAX} characters of letters, digits, . _ -")
    if not valid_id(match_id, MATCH_ID_MAX) or not Match.objects.filter(pk=match_id).exists():
        raise IngestError(404, f"unknown match {match_id}, PUT the match document first")
    if len(body) > settings.NYAYO_MAX_BATCH_UPLOAD_BYTES:
        raise IngestError(413, f"batch is larger than {settings.NYAYO_MAX_BATCH_UPLOAD_BYTES} bytes")

    content_type = (content_type or "").split(";")[0].strip().lower()
    raw = inflate(body, content_type, encoding)
    stored = body if content_type == BZIP2 else bz2.compress(raw)
    digest = hashlib.sha256(stored).hexdigest()

    row = GameBatch.objects.filter(match_id=match_id, batch=batch).first()
    if row is not None:
        if row.sha256 == digest:
            return False
        raise IngestError(409, f"batch {batch} is sealed with different content, it can't change")

    opponents = set(Match.objects.get(pk=match_id).results.values_list("opponent", flat=True))
    games = check(raw, opponents)

    path = batch_path(match_id, batch)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, stored)
    GameBatch.objects.create(match_id=match_id, batch=batch, games=games, bytes=len(stored), sha256=digest,
                             machine=machine, received_at=timezone.now())
    return True


def delete_batches(match_id):
    """Removes a match's batch files and rows. Returns the number of batches."""
    rows = GameBatch.objects.filter(match_id=match_id)
    count = rows.count()
    for row in rows:
        batch_path(match_id, row.batch).unlink(missing_ok=True)
    rows.delete()
    try:
        (batches_dir() / match_id).rmdir()
    except OSError:
        pass
    return count
