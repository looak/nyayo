"""Uploads of finished games, kept as data (docs/match-schema.md, Batches).

A producer that plays many short games sends them in bulk once played: `PUT /api/matches/{id}/pgns/{name}`, an
OpenBench workload's .pgn.bz2 as its worker uploaded it. The request reads none of it: it checks what needs no
reading (the match, the name, the size, a stored upload of that name), writes the bytes to uploads/<match id>/<name>,
records the upload as pending and answers 202. nyayo's worker (worker.py) reads it within a second or so, checks every
game and appends them to the match's batches, or refuses it whole.
"""

import bz2
import hashlib
import os
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from nyayo.runs.ingest import IngestError, invalid
from nyayo.runs.views import decompress

from .ingest import valid_id
from .models import GAME_ID_MAX, MATCH_ID_MAX, Match, Upload

FINAL = ("1-0", "0-1", "1/2-1/2")
BZIP2, PGN = "application/x-bzip2", "application/x-chess-pgn"


def uploads_dir():
    return Path(settings.NYAYO_UPLOADS_DIR)


def upload_path(match_id, name):
    return uploads_dir() / match_id / name


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


def write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "wb") as file:
        file.write(body)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def store(match_id, name, body, content_type, encoding, machine):
    """Takes one upload without reading it. Returns 202 when it is queued for the worker (new, or a refused one sent
    again), 200 for a resend of one pending or done. 400 for a bad name or Content-Type, 404 for an unknown match,
    409 for different content under the name of one pending or done, 413 when it is too large."""
    if not valid_id(name, GAME_ID_MAX):
        raise invalid(f"batch id must be 1 to {GAME_ID_MAX} characters of letters, digits, . _ -")
    if not valid_id(match_id, MATCH_ID_MAX) or not Match.objects.filter(pk=match_id).exists():
        raise IngestError(404, f"unknown match {match_id}, PUT the match document first")
    if len(body) > settings.NYAYO_MAX_BATCH_UPLOAD_BYTES:
        raise IngestError(413, f"batch is larger than {settings.NYAYO_MAX_BATCH_UPLOAD_BYTES} bytes")
    content_type = (content_type or "").split(";")[0].strip().lower()
    if content_type not in (BZIP2, PGN):
        raise invalid(f"Content-Type must be {BZIP2} or {PGN}")
    encoding = (encoding or "").strip().lower()
    if encoding in ("identity",):
        encoding = ""
    if encoding not in ("", "gzip") or (encoding and content_type == BZIP2):
        raise invalid("Content-Encoding must be gzip, for a PGN, or none")
    digest = hashlib.sha256(body).hexdigest()

    row = Upload.objects.filter(match_id=match_id, name=name).first()
    if row is not None and row.state != "refused":
        if row.sha256 == digest:
            return 200
        raise IngestError(409, f"batch {name} is stored with different content, it can't change")

    write(upload_path(match_id, name), body)
    fields = dict(bytes=len(body), sha256=digest, content_type=content_type, encoding=encoding, machine=machine,
                  received_at=timezone.now(), state="pending", reason="", games=0, appended=0, reader="", batches=[],
                  done_at=None)
    if row is None:
        Upload.objects.create(match_id=match_id, name=name, **fields)
    else:
        # a refused upload kept none of its games: sent again, the same or fixed, it is read again from the start
        for field, value in fields.items():
            setattr(row, field, value)
        row.save()
    return 202


def read(upload):
    """An upload's PGN, decompressed; IngestError 400 with the reason when it doesn't decompress."""
    body = upload_path(upload.match_id, upload.name).read_bytes()
    if upload.content_type == BZIP2:
        return bunzip(body, settings.NYAYO_MAX_BATCH_BYTES)
    return decompress(body, upload.encoding or None)


def forget(match_id):
    """Removes a match's uploads, files and rows (purge_batches). Returns how many."""
    rows = Upload.objects.filter(match_id=match_id)
    count = rows.count()
    for row in rows:
        upload_path(match_id, row.name).unlink(missing_ok=True)
    rows.delete()
    try:
        (uploads_dir() / match_id).rmdir()
    except OSError:
        pass
    return count
