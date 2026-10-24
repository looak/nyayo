"""Demo SPRTs and gauntlets for polishing the pages: made-up tests with a history, through the real ingest so the archive, the
snapshots and the pages are exactly what a producer's reports would give. `manage.py seed_demo` runs this and only
in debug mode; the numbers are simulated, nothing here is a result.

Real OpenBench tests have one snapshot each (they were resent after the fact), so none draws a progress graph; these do.
"""

import bz2
import json
import math
import random
from datetime import timedelta

import chess
import chess.pgn
from django.utils import timezone

from . import catalogue, games, reader, uploads, worker
from .ingest import delete_archived, ingest
from .models import Batch, Game, Match, MatchSnapshot, Upload

ELO0, ELO1, ALPHA, BETA = 0.0, 5.0, 0.05, 0.05
LOWER, UPPER = math.log(BETA / (1 - ALPHA)), math.log((1 - BETA) / ALPHA)
DRAW_RATE = 0.55
PAIRS_PER_REPORT = 16  # a workload: 32 games
MINUTES_PER_REPORT = 4

# id, state, true Elo, reports sent, minutes since the last report, the SPRT's result
SCENARIOS = [
    ("demo-sprt-passed", "finished", 7.0, None, 600, "passed"),
    ("demo-sprt-failed", "finished", -5.0, None, 540, "failed"),
    ("demo-sprt-stopped", "stopped", 1.5, 38, 300, None),
    ("demo-sprt-running", "running", 4.0, 45, 1, None),
    ("demo-sprt-silent", "running", 3.0, 30, 47, None),
    ("demo-sprt-new", "running", 3.0, 2, 1, None),
]

# a gauntlet's field, as uwanja lists it: id, name, version, CCRL Blitz rating
FIELD = [("barbarossa", "Barbarossa", "0.6.0", 2465), ("belette", "Belette", "1.0", 2512),
         ("apotheosis", "Apotheosis", "3", 2388), ("blunder", "Blunder", "8.5.5", 2440)]
ROUNDS = 12  # each a pair, one game per colour, against every opponent: 96 games
GAUNTLET_CONCURRENCY = 6
GAME_MINUTES = 3

# id, state, the subject's true rating, games finished, games in play (cut off once stopped), minutes since the last
# report
GAUNTLETS = [
    ("demo-gauntlet-running", "running", 2450, 40, 6, 1),
    ("demo-gauntlet-finished", "finished", 2460, 96, 0, 300),
    ("demo-gauntlet-stopped", "stopped", 2430, 58, 3, 200),
    ("demo-gauntlet-silent", "running", 2420, 33, 6, 41),
    ("demo-gauntlet-new", "running", 2450, 0, 6, 1),
]


def score_of(elo):
    return 1 / (1 + 10 ** (-elo / 400))


def llr_of(wins, draws, losses, penta):
    """The normal approximation of the pentanomial GSPRT: good enough to look like one."""
    pairs = sum(penta)
    if pairs == 0:
        return 0.0
    points = (0.0, 0.25, 0.5, 0.75, 1.0)
    mean = sum(n * p for n, p in zip(penta, points)) / pairs
    variance = sum(n * (p - mean) ** 2 for n, p in zip(penta, points)) / pairs or 1e-9
    s0, s1 = score_of(ELO0), score_of(ELO1)
    return pairs * (s1 - s0) * (2 * mean - s0 - s1) / (2 * variance)


def play_pair(rng, score):
    """Two games from one opening as the subject's results (1, 0.5, 0)."""
    win = score - DRAW_RATE / 2
    out = []
    for _ in range(2):
        draw = rng.random() < DRAW_RATE
        out.append(0.5 if draw else (1.0 if rng.random() < win / (1 - DRAW_RATE) else 0.0))
    return out


def stats_of(wins, draws, losses, penta):
    from . import stats

    rating = stats.rating(wins, draws, losses, penta)
    return {"elo": round(rating.elo or 0.0, 2), "error": round(rating.error or 99.0, 2), "model": "penta"}


def machines(games, now):
    """Two workers sharing the games, the way OpenBench's reporter lists them (details as the schema allows)."""
    first = games * 7 // 10
    return [
        {"name": "hunt", "user": "alex", "games": first, "crashes": 0, "timeloss": 1 if games > 500 else 0,
         "updated_at": now.isoformat(), "cpu": "AMD Ryzen 9 7950X 16-Core Processor", "physical_cores": 16,
         "logical_cores": 32, "ram_mb": 65536, "os": "Linux 6.8.0", "concurrency": 14,
         "cpu_flags": ["SSE41", "AVX2", "BMI2", "POPCNT", "AVX512F", "AVX512VNNI"]},
        {"name": "babylon", "user": "alex", "games": games - first, "crashes": 0, "timeloss": 0,
         "updated_at": now.isoformat()},
    ]


def document(match_id, state, now, started, counts, llr, result, stats_):
    wins, draws, losses, penta = counts
    return {
        "schema_version": 1,
        "match": {
            "id": match_id, "kind": "sprt", "state": state, "name": match_id.removeprefix("demo-").replace("-", " "),
            "producer": {"name": "OpenBench", "version": "demo", "url": "http://localhost:8000/test/0/"},
            "started_at": started.isoformat(), "updated_at": now.isoformat(),
            "finished_at": now.isoformat() if state != "running" else None,
            "host": {"name": "demo"},
            "settings": {"tc": "8+0.08", "book": "8moves_v3.epd", "runner": {"name": "cutechess-ob"},
                         "pgn_options": [], "openbench": {}},
        },
        "subject": {"name": "Elephant", "branch": "feature-x", "commit": "a1b2c3d4e5f60718", "bench": 2301556,
                    "options": {"Threads": "1", "Hash": "16"}},
        "opponents": [{"id": "base", "name": "Elephant", "branch": "main", "commit": "0f1e2d3c4b5a6978",
                       "bench": 2299871, "options": {"Threads": "1", "Hash": "16"}}],
        "results": [{"opponent": "base", "games": wins + draws + losses, "wins": wins, "draws": draws,
                     "losses": losses, "penta": penta, "timelosses": {"subject": 0, "opponent": 0},
                     "crashes": {"subject": 0, "opponent": 0}, "illegal": {"subject": 0, "opponent": 0}}],
        "sprt": {"elo0": ELO0, "elo1": ELO1, "alpha": ALPHA, "beta": BETA, "model": "penta", "llr": round(llr, 3),
                 "lower": round(LOWER, 3), "upper": round(UPPER, 3), "result": result},
        "stats": stats_, "machines": machines(wins + draws + losses, now),
    }


def forget(match_id):
    match = Match.objects.filter(pk=match_id).first()
    if match is not None:
        catalogue.purge(match_id)
        games.delete_files(match_id)
        delete_archived(match_id)
        match.delete()


def seed_one(match_id, state, elo, reports, silent_minutes, result, now):
    """Reports a test to nyayo as OpenBench would, one workload at a time, ending `silent_minutes` ago. A test with
    no report count runs to its bound (or, with a result, ends at it)."""
    rng = random.Random(match_id)
    forget(match_id)
    wins = draws = losses = 0
    penta = [0, 0, 0, 0, 0]
    score = score_of(elo)
    steps, llr_now = [], 0.0
    while True:
        for first, second in [play_pair(rng, score) for _ in range(PAIRS_PER_REPORT)]:
            total = first + second
            wins += (first == 1) + (second == 1)
            draws += (first == 0.5) + (second == 0.5)
            losses += (first == 0) + (second == 0)
            penta[round(total * 2)] += 1
        llr_now = llr_of(wins, draws, losses, penta)
        steps.append((wins, draws, losses, list(penta), llr_now))
        if reports and len(steps) >= reports:
            break
        if not reports and (llr_now <= LOWER or llr_now >= UPPER or len(steps) > 400):
            break
    end = now - timedelta(minutes=silent_minutes)
    started = end - timedelta(minutes=MINUTES_PER_REPORT * len(steps))
    for i, (w, d, l, p, llr) in enumerate(steps):
        at = started + timedelta(minutes=MINUTES_PER_REPORT * (i + 1))
        last = i == len(steps) - 1
        doc = document(match_id, state if last else "running", at, started, (w, d, l, p), llr,
                       result if last else None, stats_of(w, d, l, p))
        ingest(json.dumps(doc).encode(), None, match_id)
        snap = MatchSnapshot.objects.filter(match_id=match_id).order_by("-pk").first()
        if snap is not None and snap.received_at != at:
            MatchSnapshot.objects.filter(pk=snap.pk).update(received_at=at)
    Match.objects.filter(pk=match_id).update(received_at=end)


# --- a few games, in the layout OpenBench's cutechess writes


def opening(rng, plies):
    board = chess.Board()
    for _ in range(plies):
        moves = list(board.legal_moves)
        if not moves:
            break
        board.push(rng.choice(moves))
    return board


def make_game(rng, number, result, length, colour, finished=True, players=("Elephant feature-x", "Elephant main"),
              opponent="base", tc="8+0.08"):
    """A PGN of random legal moves with cutechess-style comments `{+0.35 12/18 104 1532876}`: worker-stripped, no
    Termination. Random play, so it only shows how a game looks, not how the engine plays. `players` is the subject's
    name, then the opponent's."""
    board = opening(rng, 8)
    start = board.fen()
    game = chess.pgn.Game()
    game.setup(board)
    white, black = players if colour == "white" else players[::-1]
    game.headers.update({"Event": "demo", "Site": "demo", "Date": "2026.10.18", "Round": str(number),
                         "White": white, "Black": black, "Result": result if finished else "*",
                         "TimeControl": tc, "Subject": colour, "Opponent": opponent, "FEN": start,
                         "SetUp": "1"})
    node, score = game, rng.uniform(-0.3, 0.3)
    for ply in range(length):
        moves = list(board.legal_moves)
        if not moves:
            break
        move = rng.choice(moves)
        node = node.add_variation(move)
        board.push(move)
        score += rng.uniform(-0.25, 0.25) + (0.03 if result == "1-0" else -0.03 if result == "0-1" else 0)
        node.comment = (f"{score:+.2f} {rng.randint(9, 16)}/{rng.randint(14, 24)} {rng.randint(40, 160)} "
                        f"{rng.randint(300_000, 2_500_000)}")
    exporter = chess.pgn.StringExporter(headers=True, variations=False, comments=True)
    text = game.accept(exporter)
    return text if finished else text.replace(" *", "") + " *"


def seed_games(match_id, now_state):
    rng = random.Random(match_id + "-games")
    results = ["1-0", "0-1", "1/2-1/2", "1-0", "1/2-1/2", "0-1"]
    for number, result in enumerate(results, start=1):
        colour = "white" if number % 2 else "black"
        text = make_game(rng, number, result, rng.randint(50, 110), colour)
        games.store(match_id, str(number), text.encode())
    if now_state == "running":
        games.store(match_id, "7", make_game(rng, 7, "*", rng.randint(20, 40), "white", finished=False).encode())


def seed_batch(match_id):
    rng = random.Random(match_id + "-batch")
    text = "\n\n".join(make_game(rng, n, rng.choice(["1-0", "0-1", "1/2-1/2"]), rng.randint(40, 90),
                                  "white" if n % 2 else "black") for n in range(1, 9))
    uploads.store(match_id, "demo.0", bz2.compress(text.encode()), uploads.BZIP2, None, None)
    # put away at once, as the worker would within a second, and sealed short: the test has ended
    worker.process_upload(Upload.objects.select_related("match").get(match_id=match_id, name="demo.0"),
                          reader.default())
    for batch in Batch.objects.filter(match_id=match_id, state="open"):
        catalogue.seal(batch)


# --- gauntlets: one report with the whole schedule, then a PGN for every game started


def gauntlet_schedule(rng, rating, finished, playing):
    """Every game of the gauntlet in uwanja's order, a round of pairs against each opponent in turn; the first
    `finished` played out, the next `playing` under way."""
    entries = []
    for round_ in range(1, ROUNDS + 1):
        for opponent, _, _, strength in FIELD:
            for colour in ("white", "black"):
                entries.append({"number": len(entries) + 1, "round": round_, "opponent": opponent,
                                "subject_colour": colour, "state": "scheduled", "result": None,
                                "termination": None, "fault": None})
    score = {opponent: score_of(rating - strength) for opponent, _, _, strength in FIELD}
    for entry in entries[:finished]:
        points = play_pair(rng, score[entry["opponent"]])[0]
        won = "1-0" if entry["subject_colour"] == "white" else "0-1"
        lost = "0-1" if won == "1-0" else "1-0"
        entry.update(state="finished", result={1.0: won, 0.5: "1/2-1/2", 0.0: lost}[points],
                     termination="adjudication" if points == 0.5 else rng.choice(["checkmate", "adjudication"]))
    for entry in entries[finished:finished + playing]:
        entry["state"] = "playing"
    return entries


def subject_points(entry):
    won = "1-0" if entry["subject_colour"] == "white" else "0-1"
    return 1.0 if entry["result"] == won else 0.5 if entry["result"] == "1/2-1/2" else 0.0


def gauntlet_results(entries):
    """Each opponent's counts from the finished games, with the pentanomial of the pairs played out."""
    rows = []
    for opponent, *_ in FIELD:
        points = [subject_points(e) for e in entries if e["opponent"] == opponent and e["state"] == "finished"]
        penta = [0, 0, 0, 0, 0]
        for i in range(0, len(points) - 1, 2):
            penta[round((points[i] + points[i + 1]) * 2)] += 1
        rows.append({"opponent": opponent, "games": len(points), "wins": points.count(1.0),
                     "draws": points.count(0.5), "losses": points.count(0.0), "penta": penta,
                     "timelosses": {"subject": 0, "opponent": 1 if opponent == "belette" and len(points) > 20 else 0},
                     "crashes": {"subject": 0, "opponent": 0}, "illegal": {"subject": 0, "opponent": 0}})
    return rows


def seed_gauntlet(match_id, state, rating, finished, playing, silent_minutes, now):
    """Reports a gauntlet as uwanja would at its latest report, then stores the PGN of every game it started, each
    timed as GAUNTLET_CONCURRENCY games at a time would have played it."""
    rng = random.Random(match_id)
    forget(match_id)
    entries = gauntlet_schedule(rng, rating, finished, playing)
    end = now - timedelta(minutes=silent_minutes)
    waves = -(-finished // GAUNTLET_CONCURRENCY)
    started = end - timedelta(minutes=GAME_MINUTES * (waves + 1))
    document = {
        "schema_version": 1,
        "match": {
            "id": match_id, "kind": "gauntlet", "state": state,
            "name": "elephant-1.3.0 " + match_id.removeprefix("demo-gauntlet-"),
            "producer": {"name": "uwanja", "version": "demo"}, "started_at": started.isoformat(),
            "updated_at": end.isoformat(), "finished_at": end.isoformat() if state != "running" else None,
            "host": {"name": "demo", "logical_cores": 32},
            "settings": {"tc": "2+0.02", "book": "UHO_Lichess_4852_v1.epd", "concurrency": GAUNTLET_CONCURRENCY,
                         "runner": {"name": "fastchess", "version": "1.4.0"}},
        },
        "subject": {"name": "Elephant", "version": "1.3.0", "commit": "a1b2c3d4e5f60718", "bench": 2301556,
                    "options": {"Threads": 1, "Hash": 16}},
        "opponents": [{"id": opponent, "name": name, "version": version, "options": {"Threads": 1},
                       "rating": {"value": strength, "list": "CCRL Blitz", "as_of": "2026-10-01"}}
                      for opponent, name, version, strength in FIELD],
        "results": gauntlet_results(entries),
        "sprt": None, "machines": [],
        "progress": {"total": len(entries), "played": finished, "playing": playing if state == "running" else 0},
        "games": entries,
    }
    ingest(json.dumps(document).encode(), None, match_id)
    Match.objects.filter(pk=match_id).update(received_at=end)
    names = {opponent: f"{name} {version}" for opponent, name, version, _ in FIELD}
    for i, entry in enumerate(entries[:finished + playing]):
        number, done = str(entry["number"]), entry["state"] == "finished"
        length = rng.randint(50, 110) if done else rng.randint(10, 60)
        text = make_game(rng, entry["number"], entry["result"] or "*", length, entry["subject_colour"], done,
                         ("Elephant 1.3.0", names[entry["opponent"]]), entry["opponent"], "2+0.02")
        games.store(match_id, number, text.encode())
        begun = started + timedelta(minutes=GAME_MINUTES * (i // GAUNTLET_CONCURRENCY))
        Game.objects.filter(match_id=match_id, game=number).update(
            started_at=begun, finished_at=begun + timedelta(minutes=GAME_MINUTES) if done else None)


def seed():
    """Makes (or remakes) every scenario. Returns their ids."""
    now = timezone.now()
    for match_id, state, elo, reports, silent, result in SCENARIOS:
        seed_one(match_id, state, elo, reports, silent, result, now)
    seed_games("demo-sprt-running", "running")
    seed_games("demo-sprt-passed", "finished")
    seed_batch("demo-sprt-passed")
    for match_id, state, rating, finished, playing, silent in GAUNTLETS:
        seed_gauntlet(match_id, state, rating, finished, playing, silent, now)
    return [s[0] for s in SCENARIOS] + [g[0] for g in GAUNTLETS]
