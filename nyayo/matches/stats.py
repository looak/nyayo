"""Elo, its error and a gauntlet's performance rating, from the counts a producer sends. The producer's own figures are
never used; the counts are, so two producers' matches read the same way.

Points come from wins, draws and losses of every game. The error comes from the pentanomial when it covers all the
games (games played in pairs on one opening share a result more than chance, which the trinomial would ignore), else
from the trinomial.
"""

import math
from dataclasses import dataclass

# 95% two sided
Z = 1.959964

# LL LD DD DW WW in pairs, as points of the pair's two games
PENTA_POINTS = (0.0, 0.25, 0.5, 0.75, 1.0)


@dataclass
class Rating:
    games: int
    score: float | None  # points over games, 0 to 1
    elo: float | None
    error: float | None  # the 95% interval is elo ± error
    paired: bool  # the error came from the pentanomial


def elo(score):
    """The Elo difference a score implies, None at 0 and 1 where it's unbounded."""
    if score is None or score <= 0 or score >= 1:
        return None
    return -400 * math.log10(1 / score - 1)


def clipped_elo(score):
    """As elo() but held just inside (0, 1), for the ends of an interval."""
    return elo(min(max(score, 1e-6), 1 - 1e-6))


def rating(wins, draws, losses, penta=None):
    games = wins + draws + losses
    if games == 0:
        return Rating(0, None, None, None, False)
    score = (wins + draws / 2) / games
    pairs = sum(penta) if penta else 0
    paired = pairs > 0 and pairs * 2 == games
    if paired:
        mean = sum(n * p for n, p in zip(penta, PENTA_POINTS)) / pairs
        variance = sum(n * (p - mean) ** 2 for n, p in zip(penta, PENTA_POINTS)) / pairs
        deviation = math.sqrt(variance / pairs)
    else:
        variance = (wins * (1 - score) ** 2 + draws * (0.5 - score) ** 2 + losses * score ** 2) / games
        deviation = math.sqrt(variance / games)
    value = elo(score)
    if value is None or deviation == 0:
        return Rating(games, score, value, None, paired)
    low, high = clipped_elo(score - Z * deviation), clipped_elo(score + Z * deviation)
    return Rating(games, score, value, (high - low) / 2, paired)


@dataclass
class Row:
    result: object  # the MatchResult
    rating: Rating


@dataclass
class Summary:
    rows: list
    overall: Rating
    wins: int
    draws: int
    losses: int
    penta: list | None
    performance: float | None  # gauntlets: the opponents' average rating plus the Elo of the overall score
    performance_games: int


def summarize(results, excluded=()):
    """A row per opponent and the figures over all but the excluded ones; excluding is a view's choice and never
    changes what's stored."""
    results = list(results)
    rows = [Row(r, rating(r.wins, r.draws, r.losses, r.penta)) for r in results]
    counted = [r for r in results if r.opponent not in excluded and r.games]
    wins, draws, losses = (sum(getattr(r, name) for r in counted) for name in ("wins", "draws", "losses"))
    penta = None
    if counted and all(r.penta for r in counted):
        penta = [sum(r.penta[i] for r in counted) for i in range(5)]
    overall = rating(wins, draws, losses, penta)

    performance = None
    if counted and overall.elo is not None and all(r.rating and r.rating.get("value") is not None for r in counted):
        games = sum(r.games for r in counted)
        average = sum(r.rating["value"] * r.games for r in counted) / games
        performance = average + overall.elo
    return Summary(rows, overall, wins, draws, losses, penta, performance, wins + draws + losses)
