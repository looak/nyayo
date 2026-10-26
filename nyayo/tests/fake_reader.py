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


def draw(args, data, merge):
    """--draw N [--quiet] [--min-ply P] [--merge] [--seed S] [--stats OUT] on standard input, as looak/elephant#34
    has it: up to N positions a game from its own ply P on (counted from where the game starts; a game's
    [DrawMinPly "P"] tag over --min-ply), not in check (the stand-in's "quiet"; the real one also runs a quiescence
    search) and never a mate or a stalemate, chosen by a generator seeded once. Deduped across the input: a position
    already taken passes to the game's next candidate. Each written `<fen> [result]`, white's result; with --merge each
    position once, at the end, its results averaged (4 places at most). --stats OUT: elephant's JSON (`"stats": 1`)."""
    import io
    import random

    import chess.pgn

    flags = dict(zip(args[::2], args[1::2]))
    per_game, default_ply = int(flags["--draw"]), int(flags.get("--min-ply", 0))
    rng, seen, merged = random.Random(int(flags.get("--seed", 1))), set(), {}
    games = {"read": 0, "used": 0, "skipped": 0, "unread": 0, "no_result": 0, "null_moves": 0, "own_minimum": 0,
             "bad_minimum_tags": 0}
    counts = {"candidates": 0, "quiet": [], "under": 0, "drawn": 0, "written": 0, "dropped": 0}
    results = {"white": 0, "draw": 0, "black": 0, "mixed": 0}
    stream = io.StringIO(data.decode("utf-8", "replace"))
    while (game := chess.pgn.read_game(stream)) is not None:
        games["read"] += 1
        label = {"1-0": 1.0, "0-1": 0.0, "1/2-1/2": 0.5}.get(game.headers.get("Result"))
        if label is None:
            games["skipped"] += 1
            games["no_result"] += 1
            continue
        games["used"] += 1
        min_ply = default_ply
        if "DrawMinPly" in game.headers:
            if game.headers["DrawMinPly"].isdigit():
                min_ply = int(game.headers["DrawMinPly"])
                games["own_minimum"] += 1
            else:
                games["bad_minimum_tags"] += 1
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
                merged.setdefault(key, [fen, []])[1].append(label)
                counts["drawn"] += 1
            elif key in seen:
                counts["dropped"] += 1
                continue
            else:
                seen.add(key)
            taken.append(fen)
            if len(taken) == per_game:
                break
        counts["under"] += len(taken) < per_game
        for fen in sorted(taken, key=quiet.index) if not merge else ():
            print(f"{fen} [{label}]")
            counts["written"] += 1
            results[{1.0: "white", 0.5: "draw", 0.0: "black"}[label]] += 1
    for fen, found in merged.values():
        average = round(sum(found) / len(found), 4)
        print(f"{fen} [{average}]")
        counts["written"] += 1
        results[{1.0: "white", 0.5: "draw", 0.0: "black"}.get(average, "mixed")] += 1
    if "--stats" in flags:
        quiet = sorted(counts["quiet"]) or [0]
        Path(flags["--stats"]).write_text(json.dumps({
            "stats": 1, "reader": "phant-indexer 9.9.9",
            "options": {"draw": per_game, "quiet": True, "min_ply": default_ply, "merge": merge,
                        "seed": int(flags.get("--seed", 1))},
            "inputs": [{"file": "-", "min_ply": None, "games": games["read"], "used": games["used"]}],
            "games": games,
            "positions": {
                "candidates": counts["candidates"], "quiet": sum(counts["quiet"]),
                "quiet_per_game": {"mean": round(sum(quiet) / len(quiet), 2), "median": quiet[len(quiet) // 2],
                                   "p5": quiet[len(quiet) // 20], "min": quiet[0]},
                "games_under_n": counts["under"], "written": counts["written"],
                "duplicates_dropped": None if merge else counts["dropped"],
                "drawn": counts["drawn"] if merge else None,
                "merged": sum(1 for _, found in merged.values() if len(found) > 1) if merge else None,
                "results": results}}))
    print(f"phant-indexer: {counts['written']} written", file=sys.stderr)


data = sys.stdin.buffer.read()
if "--draw" in sys.argv:
    draw([a for a in sys.argv[1:] if a not in ("--quiet", "--merge")], data, "--merge" in sys.argv)
    sys.exit(0)
if data.startswith(b"FAIL"):
    print("can't decompress", file=sys.stderr)
    sys.exit(1)
for record in PythonChess().read(data):
    print(json.dumps({"format": 2, "reader": "9.9.9", **record}))
