"""Positions for people: the board as SVG and moves in standard algebraic notation, by python-chess."""

import chess
import chess.svg

EXPECTED = "#15781b99"
AVOID = "#b3261e99"


def parse(fen):
    try:
        return chess.Board(fen)
    except ValueError:
        return None


def from_san(board, san):
    """The move a suite's SAN names (EPD may add ! or ?), or None when it isn't legal here."""
    try:
        return board.parse_san(san.rstrip("!?"))
    except ValueError:
        return None


def san(fen, uci):
    """uci as SAN in the position, or uci unchanged when it can't be read as a legal move there."""
    board = parse(fen) if uci else None
    if board is None:
        return uci
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return uci
    return board.san(move) if board.is_legal(move) else uci


def board_svg(fen, expected=(), avoid=(), size=360):
    """The position from the side to move, with an arrow per expected move and per move to avoid; None for a FEN that
    doesn't parse."""
    board = parse(fen)
    if board is None:
        return None
    arrows = []
    for moves, color in ((expected, EXPECTED), (avoid, AVOID)):
        for text in moves:
            move = from_san(board, text)
            if move is not None:
                arrows.append(chess.svg.Arrow(move.from_square, move.to_square, color=color))
    return chess.svg.board(board, orientation=board.turn, arrows=arrows, size=size)
