"""What a game's card in the index says, worked out from the reader's record and the game's own text.

A card's facts come from **extractors**, each with a name and a version; the index records, per batch, which version
of each filled its cards, so `manage.py reindex` visits only batches an extractor has changed for since:

- **reader**: the game reader's facts (how the final position ended it by the rules, the start and opening hashes,
  promotions, castling, captures, checks, the fewest pieces, material). Its version is the reader's own.
- **ending**: how the game ended, in this order: by a rule, from the reader; else the PGN's `Termination` tag; else,
  for a game without one, deduced from the match's adjudication settings and the engines' scores in the comments
  (cutechess-ob's `-resign` and `-draw`), marked inferred; else unexplained.
- **eval**: the eval at plies 20 and 40 and the largest swing, from the comments' scores, where every move has one.
- **played**: when the game was played, as a Unix time: its own `GameEndTime` (or `GameStartTime`) when the PGN has
  one; else, for a game with only a `Date` tag, when nyayo received it (its upload's arrival, about when the workload
  ended) if that is near its date, else that date at midnight UTC; else when nyayo received it. Only the first is
  exact (a bit says so).
"""

import re
from datetime import datetime, timezone

from .models import MatchResult

ENDING_VERSION = "1"
EVAL_VERSION = "1"
PLAYED_VERSION = "2"

ENDED = ("", "checkmate", "stalemate", "insufficient_material", "fifty_moves", "repetition")
ENDINGS = ("unexplained", "rule", "resignation", "draw_adjudication", "adjudication", "time_forfeit", "illegal_move",
           "crash", "other")
# the Termination tag as cutechess and fastchess write it
TERMINATIONS = {"adjudication": "adjudication", "time forfeit": "time_forfeit", "illegal move": "illegal_move",
                "rules infraction": "illegal_move", "abandoned": "crash", "stalled connection": "crash",
                "unterminated": "crash", "emergency": "crash"}

# yes/no facts, bits of the card's flags column
INFERRED, EN_PASSANT, UNDERPROMOTION, KNIGHT_PROMOTION, MATE, PLAYED_TIME = 1, 2, 4, 8, 16, 32
READER_FLAGS = EN_PASSANT | UNDERPROMOTION | KNIGHT_PROMOTION | MATE
ENDING_FLAGS = INFERRED
PLAYED_FLAGS = PLAYED_TIME

COMMENT = re.compile(rb"\{([^}]*)\}")
SCORE = re.compile(rb"^\s*([+-]?)(M?)(\d+(?:\.\d+)?)")
FEN = re.compile(rb'\[FEN "([^"]+)"\]')
MATE_CP = 100000
EVAL_LIMIT = 10000
ADJUDICATION = re.compile(r"(\w+)\s*=\s*(-?\d+)")


def versions(reader):
    return {"reader": reader.name + (f" format {reader.format}" if hasattr(reader, "format") else ""),
            "ending": ENDING_VERSION, "eval": EVAL_VERSION, "played": PLAYED_VERSION}


def hash_int(text):
    """A 16-hex-digit hash as SQLite's signed 64-bit integer."""
    if not text:
        return None
    value = int(text, 16)
    return value - (1 << 64) if value >= 1 << 63 else value


# --- reader ---

def reader_fields(record):
    if not record.get("ok"):
        return {}
    promotions = record.get("promotions") or {}
    castled = record.get("castled") or {}
    material = record.get("material") or {}
    ended = record.get("ended") or ""
    flags = (EN_PASSANT if record.get("ep_captures") else 0) | \
        (UNDERPROMOTION if any(promotions.get(p) for p in "rbn") else 0) | \
        (KNIGHT_PROMOTION if promotions.get("n") else 0) | (MATE if ended == "checkmate" else 0)
    return {"ended": ENDED.index(ended) if ended in ENDED else 0, "start": hash_int(record.get("start")),
            "opening": hash_int(record.get("opening")), "promo_q": promotions.get("q", 0),
            "promo_r": promotions.get("r", 0), "promo_b": promotions.get("b", 0), "promo_n": promotions.get("n", 0),
            "castled_white": castled.get("white"), "castled_black": castled.get("black"),
            "captures": record.get("captures"), "ep_captures": record.get("ep_captures"), "checks": record.get("checks"),
            "min_pieces": record.get("min_pieces"), "material_20": material.get("20"),
            "material_40": material.get("40"), "material_end": material.get("end"), "reader_flags": flags}


# --- the comments ---

def scores(pgn):
    """Each comment's score in centipawns from the mover's side, in order; None for a book move or no score."""
    _, _, movetext = pgn.partition(b"\n\n")
    out = []
    for text in COMMENT.findall(movetext):
        found = SCORE.match(text)
        if not found:
            out.append(None)
            continue
        sign, mate, number = found.groups()
        value = MATE_CP if mate else round(float(number) * 100)
        out.append(-value if sign == b"-" else value)
    return out


def start_of(pgn):
    """(white moves first, the first move's number), from the FEN tag."""
    found = FEN.search(pgn.partition(b"\n\n")[0])
    fields = found.group(1).split() if found else []
    white = fields[1] != b"b" if len(fields) > 1 else True
    number = int(fields[5]) if len(fields) > 5 and fields[5].isdigit() else 1
    return white, number


def eval_fields(pgn, plies):
    """The eval from white's side at plies 20 and 40, and the largest change between two plies, in centipawns;
    only when every move has a comment, so the comments are the plies."""
    found = scores(pgn)
    if len(found) != plies or not plies:
        return {"eval_20": None, "eval_40": None, "swing": None}
    white_first, _ = start_of(pgn)
    white = []
    for index, score in enumerate(found):
        if score is None:
            white.append(None)
            continue
        score = max(-EVAL_LIMIT, min(EVAL_LIMIT, score))
        white.append(score if (index % 2 == 0) == white_first else -score)
    swings = [abs(b - a) for a, b in zip(white, white[1:]) if a is not None and b is not None]
    return {"eval_20": white[19] if plies >= 20 else None, "eval_40": white[39] if plies >= 40 else None,
            "swing": max(swings) if swings else None}


# --- how it ended ---

def parse_adjudication(text):
    """`movecount=3 score=400` as {"movecount": 3, "score": 400}; None for nothing."""
    found = dict((k, int(v)) for k, v in ADJUDICATION.findall(text or ""))
    return found or None


class Context:
    """What deciding a match's games' endings needs from the match: its adjudication settings and whether it reports
    any time loss, crash or illegal move (then an ending isn't deduced from scores: a stripped game can't show
    them)."""

    def __init__(self, match, win=None, draw=None):
        openbench = (match.settings or {}).get("openbench") or {}
        self.win = parse_adjudication(openbench.get("win_adj")) or win
        self.draw = parse_adjudication(openbench.get("draw_adj")) or draw
        faults = 0
        for row in MatchResult.objects.filter(match=match):
            for counts in (row.timelosses, row.crashes, row.illegal):
                faults += sum(v for v in (counts or {}).values() if isinstance(v, int))
        self.faults = faults


def resign_fits(found, result, white_first, rule):
    """cutechess's -resign, one-sided: the loser's own last `movecount` scores `score` cp or more below zero."""
    count, threshold = rule.get("movecount", 3), rule.get("score", 400)
    loser_white = result == "0-1"
    loser = [s for i, s in enumerate(found) if ((i % 2 == 0) == white_first) == loser_white]
    return len(loser) >= count and all(s is not None and s <= -threshold for s in loser[-count:])


def draw_fits(found, start_move, white_first, rule):
    """cutechess's -draw: past move `movenumber`, both engines within `score` cp for `movecount` moves each."""
    count, threshold, number = rule.get("movecount", 8), rule.get("score", 10), rule.get("movenumber", 40)
    last = found[-2 * count:]
    if len(found) < 2 * count or any(s is None or abs(s) > threshold for s in last):
        return False
    full_moves = start_move + (len(found) + (0 if white_first else 1)) // 2
    return full_moves >= number


def ending_fields(record, pgn, context):
    """{"ending": code, "ending_flags": INFERRED or 0}."""
    def out(ending, inferred=False):
        return {"ending": ENDINGS.index(ending), "ending_flags": INFERRED if inferred else 0}

    tags = record.get("tags") or {}
    result = tags.get("Result") or record.get("result")
    if record.get("ended"):
        return out("rule")
    termination = (tags.get("Termination") or "").strip().lower()
    if termination:
        return out(TERMINATIONS.get(termination, "other"))
    if result not in ("1-0", "0-1", "1/2-1/2") or context is None or context.faults:
        return out("unexplained")
    found = scores(pgn)
    white_first, start_move = start_of(pgn)
    if result == "1/2-1/2":
        if context.draw and draw_fits(found, start_move, white_first, context.draw):
            return out("draw_adjudication", inferred=True)
    elif context.win and resign_fits(found, result, white_first, context.win):
        return out("resignation", inferred=True)
    return out("unexplained")


# --- when it was played ---

def parse_time(text):
    """A fastchess/cutechess time tag, `2026-10-15T16:29:31 -0700`, as a Unix time; None for anything else."""
    for pattern in ("%Y-%m-%dT%H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return int(datetime.strptime((text or "").strip(), pattern).timestamp())
        except ValueError:
            continue
    return None


def parse_date(text):
    """A PGN `Date` tag, `2026.10.18` (`?` for what is unknown), as midnight UTC of that day; None if it isn't one."""
    try:
        return int(datetime.strptime((text or "").strip(), "%Y.%m.%d").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


# how far from a game's Date (midnight UTC) its arrival may be and still date it: the worker's own day can start up to
# 14 hours either side of UTC's, and a workload's upload comes after its games, so a day before to two after
ARRIVAL_BEFORE, ARRIVAL_AFTER = 86400, 2 * 86400


def played_fields(record, received=None):
    """{"played_at": Unix time, "played_flags": PLAYED_TIME when it is the game's own}. received is when nyayo got
    the game: a game with only a date is placed at its arrival on that date (so a workload sorts among the games of
    that day, not at midnight), and one with no date at all at its arrival."""
    tags = record.get("tags") or {}
    for name in ("GameEndTime", "GameStartTime"):
        found = parse_time(tags.get(name))
        if found is not None:
            return {"played_at": found, "played_flags": PLAYED_TIME}
    found = parse_date(tags.get("Date"))
    arrived = int(received.timestamp()) if received is not None else None
    if arrived is not None and (found is None or found - ARRIVAL_BEFORE <= arrived < found + ARRIVAL_AFTER):
        found = arrived
    return {"played_at": found, "played_flags": 0}


def all_fields(record, pgn, context, received=None):
    """Every extractor's columns for one game."""
    fields = reader_fields(record)
    fields.update(played_fields(record, received))
    fields.update(ending_fields(record, pgn, context))
    fields.update(eval_fields(pgn, record.get("plies") or 0))
    return fields
