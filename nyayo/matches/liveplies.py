"""A live game's moves by changes: the per-ply record and the current state (docs/match-schema.md, Moves by changes).

Next to a live game's PGN (games/<match id>/<game>.pgn) nyayo keeps two small files, derived and only for as long as
the game is live:

- `<game>.plies`: one JSON line per ply, appended once, as the pages want it: the ply, its SAN and UCI, the position
  after it, the comment as written and read once (score, depth, time, nodes, nps, time left, the PV as UCI moves).
- `<game>.state`: the current state, replaced as a whole: the ply, the position after the last move, the last move,
  the latest score and depth of each side, the clocks, and where the PGN's result token starts so a move is appended to
  the file without reading it. It names the PGN's size it describes; a state that doesn't describe the file as it is
  is not trusted.

A PATCH reads the state, pushes the new moves onto its position with python-chess, appends to the record and writes the
new moves at the end of the PGN: it costs the new moves, not the game. A whole PGN (the first send, a resync, a
resumed game) builds both again from the text. The sealed game is fastchess's own final PGN, whole; when it is sealed
the files are deleted, after checking that its moves are the ones appended (when they differ the final PGN wins and the
difference is logged).
"""

import io
import json
import logging
import os
import re

import chess
import chess.pgn

from nyayo.runs.ingest import IngestError, atomic_write, invalid

from . import pgnview

log = logging.getLogger("nyayo.liveplies")

STATE_VERSION = 1
MAX_MOVES = 2000
RESULT_TOKEN = re.compile(r"\s*(\*|1-0|0-1|1/2-1/2)\s*$")


class NotAtEnd(IngestError):
    """A request that doesn't follow what nyayo holds: 409, and the plies nyayo does hold (None when it holds none)."""

    def __init__(self, reason, plies):
        super().__init__(409, reason)
        self.plies = plies


def side_files(pgn_path):
    """(record path, state path) beside a game's PGN."""
    stem = pgn_path.with_suffix("")
    return stem.with_name(stem.name + ".plies"), stem.with_name(stem.name + ".state")


def forget(pgn_path):
    for path in side_files(pgn_path):
        path.unlink(missing_ok=True)


def load_state(pgn_path):
    """The state of a game, or None when there is none or it doesn't describe the PGN as it is on disk."""
    _, state_path = side_files(pgn_path)
    try:
        state = json.loads(state_path.read_bytes())
        size = pgn_path.stat().st_size
    except (OSError, ValueError):
        return None
    if state.get("v") != STATE_VERSION or state.get("size") != size:
        return None
    return state


def plies_held(pgn_path):
    """The plies nyayo holds of a live game, None when it holds no state of it."""
    state = load_state(pgn_path)
    return None if state is None else state["ply"]


# --- one ply ---

def clock_after(left, control, colour, seconds, time_left):
    """The clock of the side that moved, in milliseconds, after its move: fastchess's tl= when the move says it, else
    the time before plus the increment less what the move used (book moves carry no time and cost none)."""
    if time_left is not None:
        left[colour] = time_left * 1000
    elif seconds is not None and left.get(colour) is not None and control is not None:
        left[colour] = max(0.0, left[colour] + (control[1] - seconds) * 1000)


def ply_record(board, move, comment, ply):
    """The record of a move about to be played on board (its position before the move), and the move's facts."""
    colour = "white" if board.turn == chess.WHITE else "black"
    san = board.san(move)
    score, own, depth, seconds = pgnview.parse_comment(comment)
    nodes, nps, pv, time_left = pgnview.parse_extras(comment)
    board.push(move)
    record = {"ply": ply, "san": san, "uci": move.uci(), "colour": colour, "fen": board.fen(), "comment": comment,
              "score": score, "pawns": None if own is None else (own if colour == "white" else -own),
              "depth": depth, "seconds": seconds, "nodes": nodes, "nps": nps, "pv": " ".join(pv), "tl": time_left}
    return record, seconds, time_left


def apply(state, records_facts):
    """The state after plies: [(record, seconds, time_left)] added to it."""
    for record, seconds, time_left in records_facts:
        colour = record["colour"]
        state["ply"] = record["ply"]
        state["fen"] = record["fen"]
        state["last"] = {"san": record["san"], "uci": record["uci"], "colour": colour}
        if record["score"]:
            state["latest"][colour] = {"score": record["score"], "depth": record["depth"]}
        clock_after(state["left"], state["control"], colour, seconds, time_left)
    return state


def movetext(board, moves):
    """The text a list of [(SAN, comment)] adds to a PGN's movetext, starting at board: move numbers, comments in
    braces, as fastchess writes them. Returns it without the result token."""
    out, board = [], board.copy()
    first = True
    for san, comment in moves:
        move = board.parse_san(san)
        if board.turn == chess.WHITE:
            out.append(f"{board.fullmove_number}.")
        elif first:
            out.append(f"{board.fullmove_number}...")
        out.append(san)
        if comment:
            out.append("{" + comment + "}")
        board.push(move)
        first = False
    return " ".join(out)


# --- building both from a whole PGN ---

def result_start(text):
    """Where the result token of a PGN's movetext starts, or the end of the text when it has none."""
    found = RESULT_TOKEN.search(text)
    return found.start(1) if found else len(text.rstrip())


def rebuild(pgn_path, raw, game, match_tc):
    """Builds the record and the state of a live game from its whole PGN (already read as game, without errors).
    Returns the plies."""
    board = game.board()
    chess960 = board.chess960
    control = pgnview.parse_time_control(game.headers.get("TimeControl") or match_tc)
    left = {"white": control[0] * 1000 if control else None, "black": control[0] * 1000 if control else None}
    state = {"v": STATE_VERSION, "ply": 0, "fen": board.fen(), "chess960": chess960, "last": None,
             "latest": {}, "left": left, "control": control, "size": len(raw)}
    lines, facts = [], []
    for ply, node in enumerate(game.mainline(), 1):
        record, seconds, time_left = ply_record(board, node.move, (node.comment or "").strip(), ply)
        lines.append(json.dumps(record, separators=(",", ":")))
        facts.append((record, seconds, time_left))
    apply(state, facts)
    text = raw.decode("utf-8")
    state["pgn_end"] = len(text[:result_start(text)].encode("utf-8"))
    plies_path, state_path = side_files(pgn_path)
    data = ("\n".join(lines) + "\n" if lines else "").encode("utf-8")
    atomic_write(plies_path, data)
    state["plies_size"] = len(data)
    atomic_write(state_path, json.dumps(state, separators=(",", ":")).encode("utf-8"))
    return state["ply"]


def check_sealed(pgn_path, game):
    """The final PGN is the last word. When there is a record of the moves appended, say if it differs from the final
    PGN's, then delete the live files."""
    plies_path, _ = side_files(pgn_path)
    try:
        appended = [json.loads(line)["uci"] for line in plies_path.read_text("utf-8").splitlines() if line]
    except (OSError, ValueError):
        appended = None
    if appended:
        final = [node.move.uci() for node in game.mainline()]
        if final[:len(appended)] != appended:
            at = next((i for i, (a, b) in enumerate(zip(appended, final)) if a != b), min(len(appended), len(final)))
            log.warning("%s: the final PGN differs from the moves appended at ply %d (appended %s, final %s), "
                        "the final PGN stands", pgn_path, at + 1, appended[at:at + 1], final[at:at + 1])
    forget(pgn_path)


# --- the request ---

def parse_body(raw):
    """(from, [(san, comment)]) from a moves request's JSON; IngestError 400 for anything else."""
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, ValueError) as error:
        raise invalid(f"not JSON: {error}")
    if not isinstance(document, dict):
        raise invalid("the body must be a JSON object")
    start, moves = document.get("from"), document.get("moves")
    if not isinstance(start, int) or isinstance(start, bool) or start < 0:
        raise invalid("from must be the number of plies nyayo holds, a whole number")
    if not isinstance(moves, list) or len(moves) > MAX_MOVES:
        raise invalid(f"moves must be a list of at most {MAX_MOVES} moves")
    out = []
    for i, move in enumerate(moves):
        if not isinstance(move, dict) or not isinstance(move.get("san"), str) or not move["san"].strip():
            raise invalid(f"moves[{i}].san must be the move in SAN")
        comment = move.get("comment")
        if comment is not None and (not isinstance(comment, str) or "{" in comment or "}" in comment):
            raise invalid(f"moves[{i}].comment must be text without braces, or null")
        out.append((move["san"].strip(), (comment or "").strip()))
    return start, out


def append(pgn_path, start, moves):
    """Appends moves (from parse_body) to the live game whose PGN is at pgn_path, if start is the plies held. Returns
    the plies held afterwards. NotAtEnd (409) when the game isn't held or start isn't its end, IngestError 400 naming
    the first move that isn't legal, nothing appended."""
    state = load_state(pgn_path)
    if state is None:
        raise NotAtEnd("nyayo holds no appendable record of this game, send the whole PGN", 0)
    if start != state["ply"]:
        raise NotAtEnd(f"from is {start} but nyayo holds {state['ply']} plies of this game, send the whole PGN",
                       state["ply"])
    if not moves:
        return state["ply"]

    board = chess.Board(state["fen"], chess960=state.get("chess960", False))
    facts = []
    for i, (san, comment) in enumerate(moves):
        try:
            move = board.parse_san(san)
        except ValueError as error:
            raise invalid(f"moves[{i}] ({san}) at ply {state['ply'] + i + 1}: {error}")
        facts.append(ply_record(board, move, comment, state["ply"] + i + 1))
    text = movetext(chess.Board(state["fen"], chess960=state.get("chess960", False)), moves)

    plies_path, state_path = side_files(pgn_path)
    lines = "".join(json.dumps(record, separators=(",", ":")) + "\n" for record, _, _ in facts).encode("utf-8")
    with open(pgn_path, "r+b") as file:
        file.seek(state["pgn_end"] - 1)
        before = file.read(1)
        file.seek(state["pgn_end"])
        # a space before the first new move unless the movetext so far already ends in white space
        added = (b"" if before in (b" ", b"\n", b"") else b" ") + text.encode("utf-8") + b" *\n"
        file.write(added)
        file.truncate()
        size = file.tell()
        pgn_end = state["pgn_end"] + len(added) - len(b" *\n")
    with open(plies_path, "ab") as file:
        file.truncate(state["plies_size"])
        file.seek(0, os.SEEK_END)
        file.write(lines)
    state = apply(state, facts)
    state.update(size=size, pgn_end=pgn_end + 1, plies_size=state["plies_size"] + len(lines))
    atomic_write(state_path, json.dumps(state, separators=(",", ":")).encode("utf-8"))
    return state["ply"]
