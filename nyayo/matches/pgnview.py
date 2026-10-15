"""A game's PGN for people: its moves with what the engines said about them, and the board at any ply.

The comments are fastchess's (and cutechess's): `{+0.35/18 1.204s}`, an engine's score from its own side, the depth it
reached and the time it took, or `{+M5/22 0.4s}` for a mate in 5, or `{book}`.
"""

import io
import re
from dataclasses import dataclass

import chess
import chess.pgn
import chess.svg

COMMENT = re.compile(r"^\s*([+-]?)(M?)(\d+(?:\.\d+)?)\s*/\s*(\d+)\s+(\d+(?:\.\d+)?)\s*s")
# a mate is drawn as this many pawns, and scores beyond it are clipped
GRAPH_LIMIT = 5.0
GRAPH_WIDTH, GRAPH_HEIGHT = 400, 90


@dataclass
class Move:
    ply: int  # 1 for white's first move
    san: str
    uci: str
    colour: str  # white | black
    score: str = ""  # as the engine wrote it, from its own side: +0.35, -M3
    pawns: float | None = None  # from white's side, mates clipped; for the graph
    depth: int | None = None
    seconds: float | None = None
    comment: str = ""

    @property
    def number(self):
        return (self.ply + 1) // 2

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
        score, pawns, depth, seconds = parse_comment(node.comment)
        if pawns is not None and colour == "black":
            pawns = -pawns
        moves.append(Move(ply=len(moves) + 1, san=board.san(move), uci=move.uci(), colour=colour, score=score,
                          pawns=pawns, depth=depth, seconds=seconds, comment=node.comment.strip()))
        board.push(move)
    return Parsed(dict(game.headers), moves, game.headers.get("Result", "*"), start)


def board_at(parsed, ply):
    """The position after ply plies and the move that led to it."""
    board = parsed.start.copy()
    last = None
    for move in parsed.moves[:ply]:
        last = chess.Move.from_uci(move.uci)
        board.push(last)
    return board, last


def board_svg(parsed, ply, orientation=chess.WHITE, size=400):
    board, last = board_at(parsed, ply)
    check = board.king(board.turn) if board.is_check() else None
    return chess.svg.board(board, orientation=orientation, lastmove=last, check=check, size=size)


def pairs(moves):
    """The moves as rows of a score sheet: (number, white's move or None, black's move or None)."""
    rows = []
    for move in moves:
        if move.colour == "white" or not rows:
            rows.append([move.number, move if move.colour == "white" else None, None if move.colour == "white" else move])
        else:
            rows[-1][2] = move
    return [tuple(row) for row in rows]


def graph(moves, ply=None):
    """The evaluation from white's side as SVG polyline points, with the x of the chosen ply; None when no move has
    a score."""
    scored = [m for m in moves if m.pawns is not None]
    if len(scored) < 2:
        return None
    total = max(len(moves), 2)
    half = GRAPH_HEIGHT / 2

    def y(pawns):
        return half - max(-GRAPH_LIMIT, min(GRAPH_LIMIT, pawns)) / GRAPH_LIMIT * half

    def x(index):
        return index / (total - 1) * GRAPH_WIDTH

    points = " ".join(f"{x(m.ply - 1):.1f},{y(m.pawns):.1f}" for m in scored)
    return {"points": points, "width": GRAPH_WIDTH, "height": GRAPH_HEIGHT, "mid": half,
            "cursor": x(ply - 1) if ply else None}
