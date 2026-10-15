"""A game's PGN for people: its moves with what the engines said about them, the clocks they imply, and the board at
any ply.

The comments are fastchess's (and cutechess's): `{+0.35/18 1.204s}`, an engine's score from its own side, the depth it
reached and the time it took, or `{+M5/22 0.4s}` for a mate in 5, or `{book}`. When the runner has fastchess write
more (nodes, nps, the principal variation) they are read too, as `nodes=N` / `n=N`, `nps=N` and `pv=...` in the
comment; nothing breaks when they aren't there, the page just has less to show.
"""

import io
import math
import re
from dataclasses import dataclass

import chess
import chess.pgn
import chess.svg

COMMENT = re.compile(r"^\s*([+-]?)(M?)(\d+(?:\.\d+)?)\s*/\s*(\d+)\s+(\d+(?:\.\d+)?)\s*s")
NODES = re.compile(r"\b(?:nodes|n)\s*=\s*(\d+)|\bnodes\s+(\d+)", re.I)
NPS = re.compile(r"\bnps\s*[= ]\s*(\d+)", re.I)
PV = re.compile(r"\bpv\s*[= ]\s*([^,}]+)", re.I)
TIME_CONTROL = re.compile(r"^\s*(?:\d+/)?(\d+(?:\.\d+)?)(?:\+(\d+(?:\.\d+)?))?\s*$")

# the evaluation graph's range is held to this many pawns either way, a mate is drawn as the limit
GRAPH_LIMIT = 5.0
GRAPH_WIDTH, GRAPH_HEIGHT = 640, 300
PAD_LEFT, PAD_RIGHT, PAD_TOP, PAD_BOTTOM = 52, 14, 14, 26


@dataclass
class Move:
    ply: int  # 1 for the first move played, not necessarily white's: a game may start from a position
    number: int  # the move number as a score sheet writes it
    san: str
    uci: str
    colour: str  # white | black
    score: str = ""  # as the engine wrote it, from its own side: +0.35, -M3
    pawns: float | None = None  # from white's side, mates clipped; for the one-line graph
    own_pawns: float | None = None  # from the mover's side, as the engine reported it
    depth: int | None = None
    seconds: float | None = None
    nodes: int | None = None
    nps: int | None = None
    pv: str = ""
    comment: str = ""

    @property
    def book(self):
        return self.comment.strip().lower() == "book"


@dataclass
class Parsed:
    headers: dict
    moves: list
    result: str
    start: chess.Board


def parse_comment(text):
    """(score text, pawns from the mover's side, depth, seconds), or Nones for a comment that isn't an engine's."""
    found = COMMENT.match(text or "")
    if not found:
        return "", None, None, None
    sign, mate, number, depth, seconds = found.groups()
    sign = sign or "+"
    if mate:
        pawns = GRAPH_LIMIT * 2 if sign == "+" else -GRAPH_LIMIT * 2
        return f"{sign}M{number}", pawns, int(depth), float(seconds)
    value = float(number)
    return f"{sign}{number}", value if sign == "+" else -value, int(depth), float(seconds)


def parse_extras(text):
    """(nodes, nps, pv) from the rest of a comment, None and "" where it doesn't say."""
    text = text or ""
    nodes, nps, pv = NODES.search(text), NPS.search(text), PV.search(text)
    return (int(nodes.group(1) or nodes.group(2)) if nodes else None, int(nps.group(1)) if nps else None,
            pv.group(1).strip() if pv else "")


def parse_time_control(text):
    """(base seconds, increment seconds) from `120+1` or `300`; None for anything else, and the clocks are left out."""
    found = TIME_CONTROL.match(text or "")
    if not found:
        return None
    return float(found.group(1)), float(found.group(2) or 0)


def load(raw):
    """The PGN as Parsed, None when there's no game in it."""
    game = chess.pgn.read_game(io.StringIO(raw.decode("utf-8", errors="replace")))
    if game is None:
        return None
    board = game.board()
    start = board.copy()
    moves = []
    for node in game.mainline():
        move = node.move
        colour = "white" if board.turn == chess.WHITE else "black"
        score, own, depth, seconds = parse_comment(node.comment)
        nodes, nps, pv = parse_extras(node.comment)
        moves.append(Move(
            ply=len(moves) + 1, number=board.fullmove_number, san=board.san(move), uci=move.uci(), colour=colour,
            score=score, pawns=None if own is None else (own if colour == "white" else -own), own_pawns=own,
            depth=depth, seconds=seconds, nodes=nodes, nps=nps, pv=pv, comment=node.comment.strip()))
        board.push(move)
    return Parsed(dict(game.headers), moves, game.headers.get("Result", "*"), start)


# --- the board ---

def position(parsed, ply):
    """The position after ply plies and the move that led to it."""
    board = parsed.start.copy()
    last = None
    for move in parsed.moves[:ply]:
        last = chess.Move.from_uci(move.uci)
        board.push(last)
    return board, last


def board_svg(parsed, ply, orientation=chess.WHITE, size=400):
    board, last = position(parsed, ply)
    check = board.king(board.turn) if board.is_check() else None
    return chess.svg.board(board, orientation=orientation, lastmove=last, check=check, size=size)


def pairs(moves):
    """The moves as rows of a score sheet: (number, white's move or None, black's move or None)."""
    rows = []
    for move in moves:
        if move.colour == "white" or not rows:
            rows.append([move.number, move if move.colour == "white" else None,
                         None if move.colour == "white" else move])
        else:
            rows[-1][2] = move
    return [tuple(row) for row in rows]


# --- clocks and what each engine is doing ---

def clocks(moves, control, ply):
    """What each side has left after ply plies, in milliseconds, from the time control and the time each move took:
    the base time, plus the increment per move, minus what the moves used. Book moves carry no time and cost none.
    A reconstruction, not a reading of the engines' clocks; None when the time control isn't known."""
    if control is None:
        return None
    base, increment = control
    left = {"white": base * 1000, "black": base * 1000}
    for move in moves[:ply]:
        if move.seconds is None:
            continue
        left[move.colour] += (increment - move.seconds) * 1000
    return {colour: max(0.0, value) for colour, value in left.items()}


def last_moves(moves, ply):
    """Each colour's latest move at or before ply."""
    latest = {"white": None, "black": None}
    for move in moves[:ply]:
        latest[move.colour] = move
    return latest


# --- graphs: a value per move, a line per engine ---

METRICS = (
    ("eval", "Eval", lambda m: m.own_pawns, "pawns"),
    ("depth", "Depth", lambda m: m.depth, ""),
    ("time", "Time", lambda m: m.seconds, "s"),
    ("nodes", "Nodes", lambda m: m.nodes, ""),
    ("nps", "NPS", lambda m: m.nps, ""),
)


def human(value):
    """12.3k, 4.5M, 7 or 0.35, short enough for an axis."""
    magnitude = abs(value)
    if magnitude >= 1e9:
        return f"{value / 1e9:.3g}G"
    if magnitude >= 1e6:
        return f"{value / 1e6:.3g}M"
    if magnitude >= 1e4:
        return f"{value / 1e3:.3g}k"
    return f"{value:.3g}"


def nice_step(span, ticks=4):
    """A round step (1, 2 or 5 times a power of ten) that covers span in about `ticks` steps."""
    if span <= 0:
        return 1.0
    raw = span / ticks
    power = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 2.5, 5, 10):
        if raw <= factor * power:
            return factor * power
    return 10 * power


def build_graph(key, label, unit, getter, moves, ply):
    """One metric as SVG-ready geometry with a line per colour, None when there's less than two values to draw."""
    series = {"white": [], "black": []}
    for move in moves:
        value = getter(move)
        if value is not None:
            series[move.colour].append((move, value))
    if sum(len(points) for points in series.values()) < 2:
        return None

    numbers = [m.number for m in moves]
    low_x, high_x = min(numbers), max(numbers)
    if high_x == low_x:
        high_x = low_x + 1
    values = [v for points in series.values() for _, v in points]
    if key == "eval":
        limit = min(GRAPH_LIMIT, max(1.0, math.ceil(max(abs(v) for v in values))))
        low_y, high_y, step = -limit, limit, limit / 2 if limit > 2 else 1.0
        clip = (lambda v: max(-limit, min(limit, v)))
    else:
        step = nice_step(max(values))
        low_y, high_y = 0.0, math.ceil(max(values) / step) * step or step
        clip = float

    def x(number):
        return PAD_LEFT + (number - low_x) / (high_x - low_x) * (GRAPH_WIDTH - PAD_LEFT - PAD_RIGHT)

    def y(value):
        return PAD_TOP + (high_y - clip(value)) / (high_y - low_y) * (GRAPH_HEIGHT - PAD_TOP - PAD_BOTTOM)

    lines = []
    for colour, points in series.items():
        lines.append({"colour": colour,
                      "points": " ".join(f"{x(m.number):.1f},{y(v):.1f}" for m, v in points),
                      "dots": [{"x": round(x(m.number), 1), "y": round(y(v), 1),
                                "tip": f"{m.number}{'.' if colour == 'white' else '…'} {m.san}: {human(v)}"
                                       f"{' ' + unit if unit and unit != 'pawns' else ''}"}
                               for m, v in points]})
    yticks, value = [], low_y
    while value <= high_y + 1e-9:
        yticks.append({"y": round(y(value), 1), "label": f"{value:+.1f}" if key == "eval" and value
                       else human(value)})
        value += step
    count = max(1, min(10, high_x - low_x))
    xstep = max(1, math.ceil((high_x - low_x) / count))
    xticks = [{"x": round(x(n), 1), "label": str(n)} for n in range(low_x, high_x + 1, xstep)]
    cursor = None
    if ply and 0 < ply <= len(moves):
        cursor = round(x(moves[ply - 1].number), 1)
    return {"key": key, "label": label, "width": GRAPH_WIDTH, "height": GRAPH_HEIGHT, "lines": lines,
            "yticks": yticks, "xticks": xticks, "cursor": cursor, "left": PAD_LEFT, "right": GRAPH_WIDTH - PAD_RIGHT,
            "top": PAD_TOP, "bottom": GRAPH_HEIGHT - PAD_BOTTOM}


def graphs(moves, ply=None):
    """Every metric the game has values for, in the order the buttons show them."""
    built = (build_graph(key, label, unit, getter, moves, ply) for key, label, getter, unit in METRICS)
    return [g for g in built if g is not None]
