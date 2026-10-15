"""Games as they are played: the PGN so far, re-sent whole after every move (docs/match-schema.md, Games).

The PGN is a file, games/<match id>/<game>.pgn, replaced on every PUT. The index row (models.Game) is written when a
game is first seen and again only when its result or termination changes, so the stream of moves costs a file write
and a read, not a database write. A game with a final result is sealed.

Which of the game's players is the subject and which opponent it is comes from two optional tags the runner can add,
`Subject` (white or black) and `Opponent` (the opponent's id in the match); without them the players' names are
matched against the match's engines, and left blank when that's ambiguous.
"""

import io
import shutil
from pathlib import Path

import chess.pgn
from django.conf import settings
from django.utils import timezone

from nyayo.runs.ingest import IngestError, atomic_write, invalid

from .ingest import valid_id
from .models import GAME_ID_MAX, MATCH_ID_MAX, RESULTS, Game, Match


def games_dir():
    return Path(settings.NYAYO_GAMES_DIR)


def game_path(match_id, game):
    return games_dir() / match_id / f"{game}.pgn"


def read_pgn(match_id, game):
    """The stored PGN, or None."""
    if not (valid_id(match_id, MATCH_ID_MAX) and valid_id(game, GAME_ID_MAX)):
        return None
    try:
        return game_path(match_id, game).read_bytes()
    except OSError:
        return None


def delete_files(match_id):
    shutil.rmtree(games_dir() / match_id, ignore_errors=True)


def delete_game_file(match_id, game):
    game_path(match_id, game).unlink(missing_ok=True)
    try:
        (games_dir() / match_id).rmdir()
    except OSError:
        pass


def store(match_id, game_id, raw):
    """Stores the PGN so far of one game of a match. Returns True when the game is new. Raises IngestError: 400 for
    a PGN that isn't one, 404 for a match nyayo hasn't been told about, 409 for a game that's sealed."""
    if not valid_id(game_id, GAME_ID_MAX):
        raise invalid(f"game id must be 1 to {GAME_ID_MAX} characters of letters, digits, . _ -")
    if not Match.objects.filter(pk=match_id).exists():
        raise IngestError(404, f"unknown match {match_id}, PUT the match document first")

    headers, _ = parse(raw)
    result = headers.get("Result", "*")
    if result not in RESULTS:
        raise invalid(f"[Result] must be one of {', '.join(RESULTS)}, got {result!r}")
    termination = headers.get("Termination", "")[:100]

    # No transaction around this: the common case, one more move, only reads, and in SQLite an IMMEDIATE transaction
    # would take the write lock for it anyway.
    row = Game.objects.filter(match_id=match_id, game=game_id).order_by().first()
    if row is not None and not row.live:
        if read_pgn(match_id, game_id) == raw:
            return False
        raise IngestError(409, f"game {game_id} is sealed with result {row.result}, it can't change")
    path = game_path(match_id, game_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, raw)

    now = timezone.now()
    if row is None:
        match = Match.objects.select_related("subject").get(pk=match_id)
        opponent, colour = identify(match, game_id, headers)
        Game.objects.create(
            match=match, game=game_id, opponent=opponent, colour=colour, white=headers.get("White", "")[:200],
            black=headers.get("Black", "")[:200], result=result, termination=termination, started_at=now,
            finished_at=now if result != "*" else None, received_at=now)
        return True
    if (result, termination) != (row.result, row.termination):
        row.result, row.termination = result, termination
        row.finished_at = now if result != "*" else None
        row.save(update_fields=["result", "termination", "finished_at"])
    return False


def parse(raw):
    """The PGN's headers as a dict and its number of plies; 400 when there's no game or a move is illegal."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise invalid("the PGN isn't UTF-8")
    try:
        game = chess.pgn.read_game(io.StringIO(text))
    except (ValueError, KeyError) as error:
        raise invalid(f"not a PGN: {error}")
    if game is None:
        raise invalid("no game in the PGN")
    if game.errors:
        raise invalid(f"the PGN has an illegal or unreadable move: {game.errors[0]}")
    return dict(game.headers), sum(1 for _ in game.mainline_moves())


def labels(engine, opponent=None):
    """How a PGN might name an engine, most specific first: name and version, the match's id for it, the bare name."""
    full = f"{engine.name} {engine.version}".strip()
    specific = {full} | ({opponent} if opponent else set())
    return specific, {engine.name}


def identify(match, game_id, headers):
    """(opponent id, subject's colour) for a game, blank where the PGN doesn't say and the names can't tell."""
    # the runner's schedule says it outright when the match has one
    for entry in match.schedule:
        if str(entry.get("number")) == game_id and entry.get("opponent") and entry.get("subject_colour"):
            return entry["opponent"], entry["subject_colour"]
    opponents = list(match.results.select_related("engine"))
    ids = {r.opponent for r in opponents}
    colour, opponent = headers.get("Subject", "").lower(), headers.get("Opponent", "")
    if colour in ("white", "black") and opponent in ids:
        return opponent, colour
    if opponent and opponent not in ids:
        raise invalid(f"[Opponent] {opponent!r} isn't an opponent of the match")
    if colour not in ("white", "black"):
        colour = ""

    def candidates(player):
        """Who a player's name could be, as the rank of the match (0 exact, 1 bare name) and the possible roles."""
        for rank in (0, 1):
            found = []
            specific, bare = labels(match.subject)
            if player in (specific, bare)[rank]:
                found.append("subject")
            for result in opponents:
                specific, bare = labels(result.engine, result.opponent)
                if player in (specific, bare)[rank]:
                    found.append(result.opponent)
            if found:
                return found
        return []

    white, black = candidates(headers.get("White", "")), candidates(headers.get("Black", ""))
    if len(white) == 1 and len(black) == 1:
        if white[0] == "subject" and black[0] != "subject":
            return black[0], "white"
        if black[0] == "subject" and white[0] != "subject":
            return white[0], "black"
    return opponent, colour
