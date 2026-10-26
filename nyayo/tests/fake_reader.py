"""A stand-in for phant-indexer in the tests: the same command line and output, from python-chess."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

if sys.argv[1:] == ["--version"]:
    print(os.environ.get("FAKE_READER_VERSION", "phant-indexer 9.9.9 format 2"))
    sys.exit(0)

from nyayo.matches.reader import PythonChess  # noqa: E402


def draw(args, data):
    """--draw N [--min-ply M] [--seed S] [--quiet] as elephant's contract has it (elephant#34): up to N positions
    a game from ply M on, not in check (the stand-in's "quiet"; the real one also runs a quiescence search), never a
    mate or a stalemate, chosen by a generator seeded once; deduped across the whole input, a position already taken
    passed over for the game's next candidate; each written `<fen> [result]` with white's result, and a summary on
    stderr."""
    import io
    import random

    import chess.pgn

    flags = dict(zip(args[::2], args[1::2]))
    per_game, min_ply = int(flags["--draw"]), int(flags.get("--min-ply", 0))
    rng, seen = random.Random(int(flags.get("--seed", 0))), set()
    stream = io.StringIO(data.decode("utf-8", "replace"))
    while (game := chess.pgn.read_game(stream)) is not None:
        label = {"1-0": "1.0", "0-1": "0.0", "1/2-1/2": "0.5"}.get(game.headers.get("Result"))
        if label is None:
            continue
        board, quiet = game.board(), []
        for ply, move in enumerate(game.mainline_moves(), start=1):
            board.push(move)
            if ply >= min_ply and not board.is_check() and not board.is_game_over():
                quiet.append(board.fen())
        taken = []
        for fen in rng.sample(quiet, len(quiet)):  # the candidates in a random order, the next one after a duplicate
            key = " ".join(fen.split()[:4])
            if key not in seen:
                seen.add(key)
                taken.append(fen)
                if len(taken) == per_game:
                    break
        for fen in sorted(taken, key=quiet.index):
            print(f"{fen} [{label}]")
    print(f"draw: {len(seen)} positions written", file=sys.stderr)


data = sys.stdin.buffer.read()
if "--draw" in sys.argv:
    draw([a for a in sys.argv[1:] if a != "--quiet"], data)
    sys.exit(0)
if data.startswith(b"FAIL"):
    print("can't decompress", file=sys.stderr)
    sys.exit(1)
for record in PythonChess().read(data):
    print(json.dumps({"format": 2, "reader": "9.9.9", **record}))
