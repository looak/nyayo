"""The game reader: PGN in, one record per game out, in order.

The worker reads every game it keeps through this, an upload's before any of it is kept and a batch's when it is
indexed again. A record has the fields of elephant's phant-indexer's entries (its indexer/README.md, format 2) that
nyayo uses: the game's place in the input from 1, where it starts and how long it is in bytes (through its movetext),
whether every move read and was legal, its tags, its result and its plies, and when it didn't read the ply and the
reason. python-chess does it here; phant-indexer does the same far faster, behind the same interface.
"""

import io

import chess
import chess.pgn


class ReaderError(Exception):
    """The input as a whole couldn't be read (not a game that didn't read: that is a record)."""


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


def default():
    return PythonChess()
