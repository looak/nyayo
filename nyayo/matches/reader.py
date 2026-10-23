"""The game reader: PGN in, one record per game out, in order.

The worker reads every game it keeps through this, an upload's before any of it is kept and a batch's when it is
indexed again. A record has the fields of elephant's phant-indexer's entries (its indexer/README.md, format 2) that
nyayo uses: the game's place in the input from 1, where it starts and how long it is in bytes (through its movetext),
whether every move read and was legal, its tags, its result and its plies, and when it didn't read the ply and the
reason. python-chess does it here; elephant's phant-indexer does the same far faster, behind the same interface, when
NYAYO_READER names its binary: a release pinned by its sha256 (NYAYO_READER_SHA256), checked before it is ever run.
"""

import hashlib
import io
import json
import subprocess
from pathlib import Path

import chess
import chess.pgn
from django.conf import settings


class ReaderError(Exception):
    """The input as a whole couldn't be read (not a game that didn't read: that is a record)."""


class WrongReader(Exception):
    """The reader configured isn't the one pinned: the worker doesn't start."""


def spans(data):
    """(offset, length) of each game in a PGN as bytes, split where python-chess splits them for the PGN producers
    write: a game starts at a tag line, and a tag line after a blank line ends the game before it once that game has
    moves. Braced comments are skipped over, so a line inside one never starts a game."""
    found, start, end, moved, blank, depth, pos = [], None, 0, False, True, 0, 0
    for line in data.splitlines(keepends=True):
        here, pos = pos, pos + len(line)
        stripped = line.strip()
        if depth == 0:
            if not stripped:
                blank = True
                continue
            if stripped.startswith(b"%") or (stripped.startswith(b";") and not moved):
                continue
            if stripped.startswith(b"[") and (start is None or (moved and blank)):
                if start is not None:
                    found.append((start, end - start))
                start, moved = here, False
            elif start is None:
                continue
            elif not (stripped.startswith(b"[") and not moved):
                moved = True
        depth = comment_depth(line, depth)
        end = here + len(line.rstrip())
        blank = False
    if start is not None:
        found.append((start, end - start))
    return found


def comment_depth(line, depth):
    """Whether a braced comment is still open after this line: 1 or 0 (comments don't nest in PGN)."""
    for byte in line:
        if depth:
            if byte == 0x7D:  # }
                depth = 0
        elif byte == 0x7B:  # {
            depth = 1
        elif byte == 0x3B:  # ; a comment to the end of the line
            break
    return depth


class PythonChess:
    """The reader, in python-chess. A game's moves are replayed with read_game, so a record says what nyayo's checks
    always said."""

    name = f"python-chess {chess.__version__}"

    def read(self, data):
        records = []
        for number, (offset, length) in enumerate(spans(data), 1):
            records.append(self.record(number, offset, length, data[offset:offset + length]))
        return records

    def record(self, number, offset, length, chunk):
        record = {"game": number, "offset": offset, "length": length, "ok": False, "tags": {}, "result": "*",
                  "plies": 0}
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            record["error"] = {"ply": 0, "reason": "not UTF-8"}
            return record
        stream = io.StringIO(text)
        try:
            game = chess.pgn.read_game(stream)
        except (ValueError, KeyError) as error:
            record["error"] = {"ply": 0, "reason": f"not a PGN: {error}"}
            return record
        if game is None:
            record["error"] = {"ply": 0, "reason": "no game"}
            return record
        record["tags"] = dict(game.headers)
        result = game.headers.get("Result", "*")
        record["result"] = result if result in ("1-0", "0-1", "1/2-1/2") else "*"
        record["plies"] = sum(1 for _ in game.mainline_moves())
        if game.errors:
            record["error"] = {"ply": record["plies"] + 1, "reason": str(game.errors[0])}
            return record
        if chess.pgn.read_game(stream) is not None:
            record["error"] = {"ply": 0, "reason": "two games where one was expected, a blank line is missing"}
            return record
        record["ok"] = True
        return record


class PhantIndexer:
    """elephant's game reader, a binary: the PGN on standard input, one JSON line per game on standard output, in
    the format its indexer/README.md gives (1 or 2; nyayo reads the fields both have)."""

    FORMATS = (1, 2)

    def __init__(self, path, sha256, launcher=()):
        self.path = Path(path)
        digest = file_sha256(self.path)
        if not sha256 or digest != sha256.lower():
            raise WrongReader(f"{self.path} has sha256 {digest}, not the pinned {sha256 or '(none set)'}; "
                              f"set NYAYO_READER_SHA256 to the release's")
        self.command = [*launcher, str(self.path)]
        version = subprocess.run([*self.command, "--version"], capture_output=True, text=True, timeout=30)
        words = version.stdout.split()
        # phant-indexer <elephant version> format <format version>
        if version.returncode or len(words) != 4 or words[0] != "phant-indexer" or words[2] != "format":
            raise WrongReader(f"{self.path} --version said {version.stdout.strip()!r}, not phant-indexer's")
        self.version, self.format = words[1], int(words[3])
        if self.format not in self.FORMATS:
            raise WrongReader(f"{self.path} writes format {self.format}, nyayo reads {self.FORMATS}")
        self.name = f"phant-indexer {self.version}"

    def read(self, data):
        done = subprocess.run(self.command, input=data, capture_output=True)
        if done.returncode:
            raise ReaderError(f"{self.name} exited {done.returncode}: "
                              f"{done.stderr.decode('utf-8', 'replace').strip()[:300]}")
        records = []
        for line in done.stdout.splitlines():
            if line.strip():
                record = json.loads(line)
                record.setdefault("tags", {})
                records.append(record)
        return records


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def default():
    """The reader the worker uses: phant-indexer when NYAYO_READER names it, else python-chess."""
    if settings.NYAYO_READER:
        return PhantIndexer(settings.NYAYO_READER, settings.NYAYO_READER_SHA256)
    return PythonChess()
