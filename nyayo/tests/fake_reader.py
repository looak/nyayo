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
    stderr. With --merge, a position is written once, at the end, with the average of every result it was drawn
    with, in the order first drawn. With --stats FILE, its statistics as JSON there, by nyayo's names (datasets.py's
    STATS_FIELDS) until elephant's are settled."""
    import io
    import random

    import chess.pgn

    flags = dict(zip(args[::2], args[1::2]))
    per_game, min_ply = int(flags["--draw"]), int(flags.get("--min-ply", 0))
    rng, seen, merged = random.Random(int(flags.get("--seed", 0))), set(), {}
    counts = {"read": 0, "skipped": 0, "candidates": 0, "quiet": [], "short": 0, "written": 0, "duplicates": 0,
              "1.0": 0, "0.5": 0, "0.0": 0}
    stream = io.StringIO(data.decode("utf-8", "replace"))
    while (game := chess.pgn.read_game(stream)) is not None:
        label = {"1-0": "1.0", "0-1": "0.0", "1/2-1/2": "0.5"}.get(game.headers.get("Result"))
        if label is None:
            counts["skipped"] += 1
            continue
        counts["read"] += 1
        board, quiet = game.board(), []
        for ply, move in enumerate(game.mainline_moves(), start=1):
            board.push(move)
            if ply >= min_ply and not board.is_game_over():
                counts["candidates"] += 1
                if not board.is_check():
                    quiet.append(board.fen())
        counts["quiet"].append(len(quiet))
        taken = []
        for fen in rng.sample(quiet, len(quiet)):  # the candidates in a random order, the next one after a duplicate
            key = " ".join(fen.split()[:4])
            if merge:
                merged.setdefault(key, [fen, []])[1].append(float(label))
                taken.append(fen)
                if len(taken) == per_game:
                    break
            elif key not in seen:
                seen.add(key)
                taken.append(fen)
                if len(taken) == per_game:
                    break
            else:
                counts["duplicates"] += 1
        counts["short"] += len(taken) < per_game
        for fen in sorted(taken, key=quiet.index) if not merge else ():
            print(f"{fen} [{label}]")
            counts["written"] += 1
            counts[label] += 1
    for fen, results in merged.values():
        average = sum(results) / len(results)
        print(f"{fen} [{average:.2f}]")
        counts["written"] += 1
        counts["1.0" if average > 0.5 else "0.0" if average < 0.5 else "0.5"] += 1
    if merge:
        counts["duplicates"] = sum(len(results) - 1 for _, results in merged.values())
    if "--stats" in flags:
        quiet = sorted(counts["quiet"]) or [0]
        Path(flags["--stats"]).write_text(json.dumps({
            "format": 1, "games": {"read": counts["read"], "skipped": counts["skipped"]},
            "candidates": counts["candidates"], "quiet": sum(counts["quiet"]),
            "quiet_per_game": {"mean": round(sum(quiet) / len(quiet), 2), "median": quiet[len(quiet) // 2],
                               "p5": quiet[len(quiet) // 20], "min": quiet[0]},
            "short_games": counts["short"], "written": counts["written"], "duplicates": counts["duplicates"],
            "results": {"white": counts["1.0"], "draw": counts["0.5"], "black": counts["0.0"]}}))
    print(f"draw: {len(merged) if merge else len(seen)} positions written", file=sys.stderr)


data = sys.stdin.buffer.read()
if "--draw" in sys.argv:
    merge = "--merge" in sys.argv
    draw([a for a in sys.argv[1:] if a not in ("--quiet", "--merge")], data)
    sys.exit(0)
if data.startswith(b"FAIL"):
    print("can't decompress", file=sys.stderr)
    sys.exit(1)
for record in PythonChess().read(data):
    print(json.dumps({"format": 2, "reader": "9.9.9", **record}))
