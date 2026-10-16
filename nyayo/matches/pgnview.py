"""A game's PGN for people: its moves with what the engines said about them, the clocks they imply, and the board at
any ply.

The comments are fastchess's, as its -pgnout options write them:

    {+0.28/11 0.116s, n=278950, nps=0, pv="g1f3 d7d5 b1c3"}        score/depth time, then the options asked for
    {+0.02/12 0.185s, tl=1.855s, latency=0.000s, n=450656, sd=0, nps=0, hashfull=0, tbhits=0, pv="g1f3 g8f6"}
    {+M3/3 0.000s, n=227, nps=0, pv="e2h2 g8f8 h2h8"}               a mate, the distance in plies
    {+M1/1 0.000s, n=19, nps=0, pv="h2h8", White mates}             the reason for the end comes last
    {/0 0.116s}   a move with no score        {book}   a book move

The score is the mover's, in pawns; 0.00 has no sign. A value the engine didn't report is written as 0, so n=0 and
nps=0 are read as not reported. The PV is UCI moves in double quotes, shown here as SAN. tl= is the time left after the
move and, when present, the clocks use it instead of reconstructing them.
"""

import io
import math
import re
from dataclasses import dataclass

import chess
import chess.pgn
import chess.svg

COMMENT = re.compile(r"^\s*(?:([+-]?)(M?)(\d+(?:\.\d+)?))?\s*/\s*(\d+)\s+(\d+(?:\.\d+)?)\s*s")
NODES = re.compile(r"\bn\s*=\s*(\d+)")
NPS = re.compile(r"\bnps\s*=\s*(\d+)")
SELDEPTH = re.compile(r"\bsd\s*=\s*(\d+)")
TIME_LEFT = re.compile(r"\btl\s*=\s*(-?\d+(?:\.\d+)?)\s*s")
PV = re.compile(r'\bpv\s*=\s*(?:"([^"]*)"|([^,}]+))')
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
    pv: str = ""  # as SAN from the position the move was played in
    time_left: float | None = None  # seconds the mover had left after the move, when fastchess wrote it
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
    """(score text, pawns from the mover's side, depth, seconds), or Nones for a comment that isn't an engine's.

    A mate's distance is in plies in the comment (+M9 is mate in 5) and shown in moves. A move with no score
    (`/0 0.116s`) has no score and no depth, only the time."""
    found = COMMENT.match(text or "")
    if not found:
        return "", None, None, None
    sign, mate, number, depth, seconds = found.groups()
    depth, seconds = int(depth) or None, float(seconds)
    if number is None:
        return "", None, depth, seconds
    sign = sign or "+"
    if mate:
        pawns = GRAPH_LIMIT * 2 if sign == "+" else -GRAPH_LIMIT * 2
        return f"{sign}M{(int(float(number)) + 1) // 2}", pawns, depth, seconds
    value = float(number)
    return f"{sign}{number}", value if sign == "+" else -value, depth, seconds


def parse_extras(text):
    """(nodes, nps, pv as UCI moves, time left in seconds) from the rest of a comment; None, None, [] and None where
    it doesn't say, and for the zeros fastchess writes for what the engine didn't report."""
    text = text or ""
    nodes, nps, pv, left = NODES.search(text), NPS.search(text), PV.search(text), TIME_LEFT.search(text)
    return (int(nodes.group(1)) or None if nodes else None, int(nps.group(1)) or None if nps else None,
            ((pv.group(1) if pv.group(1) is not None else pv.group(2)).split() if pv else []),
            float(left.group(1)) if left else None)


def pv_as_san(board, uci_moves):
    """A principal variation as SAN from board, up to the first move that isn't legal there."""
    board, out = board.copy(), []
    for text in uci_moves:
        try:
            move = chess.Move.from_uci(text)
        except ValueError:
            break
        if move not in board.legal_moves:
            break
        out.append(board.san(move))
        board.push(move)
    return " ".join(out)


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
        nodes, nps, pv, left = parse_extras(node.comment)
        moves.append(Move(
            ply=len(moves) + 1, number=board.fullmove_number, san=board.san(move), uci=move.uci(), colour=colour,
            score=score, pawns=None if own is None else (own if colour == "white" else -own), own_pawns=own,
            depth=depth, seconds=seconds, nodes=nodes, nps=nps, pv=pv_as_san(board, pv), time_left=left,
            comment=node.comment.strip()))
        board.push(move)
    return Parsed(dict(game.headers), moves, game.headers.get("Result", "*"), start)


def plain_pgn(raw):
    """The game's PGN as plain movetext: no comments, no NAGs, no side lines."""
    game = chess.pgn.read_game(io.StringIO(raw.decode("utf-8", errors="replace")))
    if game is None:
        return ""
    return game.accept(chess.pgn.StringExporter(headers=True, comments=False, variations=False))


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
    A reconstruction, not a reading of the engines' clocks, except for a side whose latest move says tl= (what
    fastchess left in its own PGN), which wins; None when the time control isn't known and no move says."""
    base, increment = control if control is not None else (None, 0.0)
    left = {"white": None if base is None else base * 1000, "black": None if base is None else base * 1000}
    for move in moves[:ply]:
        if move.time_left is not None:
            left[move.colour] = move.time_left * 1000
        elif move.seconds is not None and left[move.colour] is not None:
            left[move.colour] += (increment - move.seconds) * 1000
    if all(value is None for value in left.values()):
        return None
    return {colour: None if value is None else max(0.0, value) for colour, value in left.items()}


def last_moves(moves, ply):
    """Each colour's latest move at or before ply."""
    latest = {"white": None, "black": None}
    for move in moves[:ply]:
        latest[move.colour] = move
    return latest


# --- graphs ---

METRICS = (
    ("eval", "Eval", lambda m: m.own_pawns, "pawns"),
    ("depth", "Depth", lambda m: m.depth, ""),
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
        peak = max(abs(v) for v in values)
        limit = min(GRAPH_LIMIT, max(1.5, peak * 1.35))
        low_y, high_y, step = -limit, limit, limit / 2 if limit > 2 else 1.0
        clip = (lambda v: max(-limit, min(limit, v)))
    else:
        top = max(values) * 1.15
        step = nice_step(top)
        low_y, high_y = 0.0, math.ceil(top / step) * step or step
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

    # a column per move number to click: it goes to the end of that move, where both engines have answered, and its
    # tooltip gives both values
    last_ply, shown = {}, {}
    for move in moves:
        last_ply[move.number] = move.ply
    for colour, points in series.items():
        for move, value in points:
            shown.setdefault(move.number, {})[colour] = human(value)
    spacing = (GRAPH_WIDTH - PAD_LEFT - PAD_RIGHT) / (high_x - low_x)
    columns = []
    for number, target in sorted(last_ply.items()):
        values = shown.get(number, {})
        tip = f"move {number}" + "".join(f" · {colour} {values[colour]}" for colour in ("white", "black")
                                          if colour in values)
        left = max(PAD_LEFT, x(number) - spacing / 2)
        right = min(GRAPH_WIDTH - PAD_RIGHT, x(number) + spacing / 2)
        columns.append({"x": round(left, 1), "width": round(max(right - left, 1.0), 1), "ply": target, "tip": tip})
    return {"key": key, "label": label, "width": GRAPH_WIDTH, "height": GRAPH_HEIGHT, "lines": lines,
            "yticks": yticks, "xticks": xticks, "cursor": cursor, "left": PAD_LEFT, "right": GRAPH_WIDTH - PAD_RIGHT,
            "top": PAD_TOP, "bottom": GRAPH_HEIGHT - PAD_BOTTOM, "columns": columns,
            "plot_height": GRAPH_HEIGHT - PAD_BOTTOM - PAD_TOP}


def build_time_graph(moves, ply, control):
    """Lichess-like time chart: per-move spent bars and remaining-time lines, mirrored around a middle axis.

    White is above the middle, black below it, for both spent bars and time-left lines.
    """
    points = [move for move in moves if move.seconds is not None]
    if len(points) < 2:
        return None

    numbers = [m.number for m in moves]
    low_x, high_x = min(numbers), max(numbers)
    if high_x == low_x:
        high_x = low_x + 1

    def x(number):
        return PAD_LEFT + (number - low_x) / (high_x - low_x) * (GRAPH_WIDTH - PAD_LEFT - PAD_RIGHT)

    top, bottom = PAD_TOP, GRAPH_HEIGHT - PAD_BOTTOM
    middle = (top + bottom) / 2
    half = (bottom - top) / 2
    bar_room = half - 10
    line_room = half - 10
    max_spent = (max(move.seconds for move in points) or 1.0) * 1.15

    base, increment = control if control is not None else (None, 0.0)
    left = {"white": base, "black": base}
    left_points = {"white": [], "black": []}
    bars = []
    for move in moves:
        if move.seconds is not None:
            spent = move.seconds / max_spent if max_spent else 0.0
            height = max(1.0, spent * bar_room)
            y0, y1 = (middle - height, middle) if move.colour == "white" else (middle, middle + height)
            bars.append({
                "x": round(x(move.number) - 5, 1), "y": round(y0, 1), "height": round(max(y1 - y0, 1.0), 1),
                "colour": move.colour, "tip": f"{move.number}{'.' if move.colour == 'white' else '…'} {move.san}: {move.seconds:.3g} s"
            })
        if move.time_left is not None:
            left[move.colour] = move.time_left
        elif move.seconds is not None and left[move.colour] is not None:
            left[move.colour] = max(0.0, left[move.colour] + increment - move.seconds)
        if left[move.colour] is not None:
            left_points[move.colour].append((move, left[move.colour]))

    max_left = max((value for side in left_points.values() for _, value in side), default=0.0) * 1.15
    max_left = max(max_left, 1.0)

    def y_left(colour, value):
        ratio = min(1.0, max(0.0, value / max_left))
        return (middle - ratio * line_room) if colour == "white" else (middle + ratio * line_room)

    lines = []
    for colour, side in left_points.items():
        lines.append({
            "colour": colour,
            "points": " ".join(f"{x(move.number):.1f},{y_left(colour, value):.1f}" for move, value in side),
            "dots": [{"x": round(x(move.number), 1), "y": round(y_left(colour, value), 1),
                      "tip": f"{move.number}{'.' if colour == 'white' else '…'} {move.san}: {value:.1f} s left"}
                     for move, value in side],
        })

    count = max(1, min(10, high_x - low_x))
    xstep = max(1, math.ceil((high_x - low_x) / count))
    xticks = [{"x": round(x(n), 1), "label": str(n)} for n in range(low_x, high_x + 1, xstep)]
    cursor = None
    if ply and 0 < ply <= len(moves):
        cursor = round(x(moves[ply - 1].number), 1)

    last_ply = {}
    for move in moves:
        last_ply[move.number] = move.ply
    spacing = (GRAPH_WIDTH - PAD_LEFT - PAD_RIGHT) / (high_x - low_x)
    columns = []
    for number, target in sorted(last_ply.items()):
        left_x = max(PAD_LEFT, x(number) - spacing / 2)
        right_x = min(GRAPH_WIDTH - PAD_RIGHT, x(number) + spacing / 2)
        columns.append({"x": round(left_x, 1), "width": round(max(right_x - left_x, 1.0), 1), "ply": target,
                        "tip": f"move {number}"})

    ticks = []
    for ratio in (1.0, 0.5):
        value = max_left * ratio
        ticks.append({"y": round(middle - ratio * line_room, 1), "label": f"{value:.0f}s"})
        ticks.append({"y": round(middle + ratio * line_room, 1), "label": f"-{value:.0f}s"})

    return {"key": "time", "label": "Time", "width": GRAPH_WIDTH, "height": GRAPH_HEIGHT, "lines": lines,
            "bars": bars, "yticks": ticks, "xticks": xticks, "cursor": cursor, "left": PAD_LEFT,
            "right": GRAPH_WIDTH - PAD_RIGHT, "top": PAD_TOP, "bottom": GRAPH_HEIGHT - PAD_BOTTOM,
            "columns": columns, "plot_height": GRAPH_HEIGHT - PAD_BOTTOM - PAD_TOP, "middle": round(middle, 1)}


def graphs(moves, ply=None, control=None):
    """Every metric the game has values for, in the order the buttons show them."""
    by_key = {key: build_graph(key, label, unit, getter, moves, ply) for key, label, getter, unit in METRICS}
    by_key["time"] = build_time_graph(moves, ply, control)
    return [by_key[key] for key in ("eval", "depth", "time", "nodes", "nps") if by_key.get(key) is not None]
