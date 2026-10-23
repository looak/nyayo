"""Matches into nyayo, the counterpart of nyayo.runs.ingest for the match schema (docs/match-schema.md).

A match document is archived first, unchanged, as archive/matches/<id>.json, then indexed. The producer re-sends the
whole document as the match grows and says the state every time, so nothing here decides a state on its behalf.

Under one id only the state, the timestamps of progress, the counts, the producer's LLR, result and stats, the machines
that played and the pgn may change, and counts only grow. Anything else is a different match under the same id (409).
"""

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from nyayo.runs.ingest import IngestError, atomic_write, first_difference, invalid, timestamp

from . import schedule
from .models import GAME_ID_MAX, ID_PATTERN, KINDS, MATCH_ID_MAX, STATES, Engine, Match, MatchResult, MatchSnapshot

SCHEMA_VERSION = 1

ID = re.compile(ID_PATTERN)
COUNTS = ("games", "wins", "draws", "losses")
SIDES = ("timelosses", "crashes", "illegal")
GAME_STATES = ("scheduled", "playing", "finished")
GAME_RESULTS = ("1-0", "0-1", "1/2-1/2")
STARTED_FIXED = schedule.STARTED_FIXED


@dataclass
class Ingested:
    match: Match
    created: bool


class WrongBase(IngestError):
    """A report by changes whose base isn't the seq nyayo holds: 409, nothing applied. The answer says nyayo's seq."""

    def __init__(self, reason):
        super().__init__(409, reason)


def archive_every():
    """Seconds between archive writes of a match reported by changes."""
    return settings.NYAYO_ARCHIVE_EVERY


def archive_dir():
    return Path(settings.NYAYO_ARCHIVE_DIR) / "matches"


def archive_path(match_id):
    return archive_dir() / f"{match_id}.json"


def ingest(raw, machine=None, match_id=None):
    """Stores one match from its raw JSON bytes. machine is who sent it, match_id the id the request was addressed
    to. Returns Ingested(match, created); raises IngestError for a document that's invalid (400) or an update that
    changes what may not change (409)."""
    document = parse(raw)
    validate(document, match_id)
    m = document["match"]
    report = document.get("report")
    base = report["base"] if report else None

    existing = Match.objects.filter(pk=m["id"]).first()
    if base is not None:
        # a report by changes goes on top of what nyayo holds, or not at all
        if existing is None or existing.report_seq is None:
            raise WrongBase(f"report.base is {base} but nyayo holds no seq for {m['id']}, send the whole document")
        previous = read_archived(existing.pk)
        if previous is not None:
            check_update(previous, document, changes=True)
    elif existing is not None:
        previous = read_archived(existing.pk)
        if previous is not None:
            check_update(previous, document)
        else:
            check_update_against(existing, document)

    if base is None:
        write_archive(m["id"], raw, machine)
    with transaction.atomic():
        if base is not None:
            # compare and set in one statement: another report in between makes this one the wrong one
            if not Match.objects.filter(pk=existing.pk, report_seq=base).update(report_seq=report["seq"]):
                current = Match.objects.values_list("report_seq", flat=True).get(pk=existing.pk)
                raise WrongBase(f"report.base is {base} but nyayo's seq for {m['id']} is {current}, send the whole "
                                f"document")
            existing.report_seq = report["seq"]
        match = create_match(document, machine) if existing is None else update_match(existing, document,
                                                                                         changes=base is not None)
        snapshot(match, document)
    if base is not None:
        archive_merged(match, document, machine)
    return Ingested(match, existing is None)


# --- parsing & validation ---

def parse(raw):
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise invalid(f"not JSON: {error}")
    if not isinstance(document, dict):
        raise invalid("the body must be a JSON object")
    return document


def is_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def valid_id(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit and ID.match(value) is not None


def validate(document, match_id=None):
    if document.get("schema_version") != SCHEMA_VERSION:
        raise invalid(f"schema_version must be {SCHEMA_VERSION}, got {document.get('schema_version')!r}")
    m = document.get("match")
    if not isinstance(m, dict):
        raise invalid("match is missing")

    if not valid_id(m.get("id"), MATCH_ID_MAX):
        raise invalid(f"match.id must be 1 to {MATCH_ID_MAX} characters of letters, digits, . _ -, got {m.get('id')!r}")
    if m.get("id") == "batches":
        raise invalid("match.id batches is reserved")
    if match_id is not None and m["id"] != match_id:
        raise invalid(f"match.id {m['id']} doesn't match the url's {match_id}")
    if m.get("kind") not in KINDS:
        raise invalid(f"match.kind must be one of {', '.join(KINDS)}, got {m.get('kind')!r}")
    if m.get("state") not in STATES:
        raise invalid(f"match.state must be one of {', '.join(STATES)}, got {m.get('state')!r}")
    producer = m.get("producer")
    if not isinstance(producer, dict) or not isinstance(producer.get("name"), str) or not producer["name"]:
        raise invalid("match.producer.name is missing")
    for name in ("host", "settings"):
        if not isinstance(m.get(name, {}), dict):
            raise invalid(f"match.{name} must be an object")
    if m.get("name") is not None and not isinstance(m["name"], str):
        raise invalid("match.name must be a string")
    timestamp(m, "started_at", where="match")
    timestamp(m, "updated_at", required=True, where="match")
    timestamp(m, "finished_at", where="match")

    validate_engine(document.get("subject"), "subject")
    opponents = document.get("opponents")
    if not isinstance(opponents, list):
        raise invalid("opponents must be a list")
    known = set()
    for i, opponent in enumerate(opponents):
        where = f"opponents[{i}]"
        validate_engine(opponent, where)
        if not valid_id(opponent.get("id"), GAME_ID_MAX):
            raise invalid(f"{where}.id must be 1 to {GAME_ID_MAX} characters of letters, digits, . _ -")
        if opponent["id"] in known:
            raise invalid(f"{where}.id {opponent['id']} is used twice")
        known.add(opponent["id"])
        rating = opponent.get("rating")
        if rating is not None and (not isinstance(rating, dict) or not isinstance(rating.get("value"), (int, float))):
            raise invalid(f"{where}.rating needs a numeric value")

    results = document.get("results")
    if not isinstance(results, list):
        raise invalid("results must be a list")
    seen = set()
    for i, row in enumerate(results):
        validate_result(row, f"results[{i}]", known, seen)

    sprt = document.get("sprt")
    if sprt is not None and not isinstance(sprt, dict):
        raise invalid("sprt must be an object or null")
    if sprt and sprt.get("llr") is not None and (not isinstance(sprt["llr"], (int, float))
                                                  or isinstance(sprt["llr"], bool)):
        raise invalid("sprt.llr must be a number")
    validate_stats(document.get("stats"))
    if not isinstance(document.get("machines", []), list):
        raise invalid("machines must be a list")
    validate_schedule(document, known)
    validate_report(document)


def validate_report(document):
    report = document.get("report")
    if report is None:
        return
    if not isinstance(report, dict):
        raise invalid("report must be an object")
    seq, base = report.get("seq"), report.get("base")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        raise invalid("report.seq must be a whole number, 1 or more")
    if base is not None and (not isinstance(base, int) or isinstance(base, bool) or base < 1 or base >= seq):
        raise invalid("report.base must be null or the whole number of an earlier report, below report.seq")
    if base is not None and document.get("games") is None:
        raise invalid("a report with a base carries the schedule entries that changed in games")
    report["seq"], report["base"] = seq, base


def validate_stats(stats):
    if stats is None:
        return
    if not isinstance(stats, dict):
        raise invalid("stats must be an object or null")
    for name in ("elo", "error"):
        value = stats.get(name)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value != value:
            raise invalid(f"stats.{name} must be a number")
    if stats.get("model") not in ("penta", "tri"):
        raise invalid("stats.model must be penta or tri")


def validate_schedule(document, opponents):
    progress = document.get("progress")
    if progress is not None and (not isinstance(progress, dict) or not all(is_count(v) for v in progress.values())):
        raise invalid("progress must map total, played and playing to whole numbers")
    games = document.get("games")
    if games is None:
        return
    if not isinstance(games, list):
        raise invalid("games must be a list")
    numbers = set()
    for i, game in enumerate(games):
        where = f"games[{i}]"
        if not isinstance(game, dict):
            raise invalid(f"{where} must be an object")
        if not isinstance(game.get("number"), int) or isinstance(game["number"], bool) or game["number"] < 0:
            raise invalid(f"{where}.number must be a whole number")
        if game["number"] in numbers:
            raise invalid(f"{where}.number {game['number']} is used twice")
        numbers.add(game["number"])
        if game.get("opponent") not in opponents:
            raise invalid(f"{where}.opponent {game.get('opponent')!r} isn't in opponents")
        if game.get("subject_colour") not in ("white", "black"):
            raise invalid(f"{where}.subject_colour must be white or black")
        if game.get("state") not in GAME_STATES:
            raise invalid(f"{where}.state must be one of {', '.join(GAME_STATES)}")
        if game.get("result") is not None and game["result"] not in GAME_RESULTS:
            raise invalid(f"{where}.result must be one of {', '.join(GAME_RESULTS)} or null")
        if game["state"] == "finished" and game.get("result") is None:
            raise invalid(f"{where} is finished and has no result")


def validate_engine(engine, where):
    if not isinstance(engine, dict):
        raise invalid(f"{where} is missing")
    if not isinstance(engine.get("name"), str) or not engine["name"]:
        raise invalid(f"{where}.name is missing")
    for name in ("version", "commit"):
        if engine.get(name) is not None and not isinstance(engine[name], str):
            raise invalid(f"{where}.{name} must be a string or null")
    if not isinstance(engine.get("options", {}) or {}, dict):
        raise invalid(f"{where}.options must be an object")


def validate_result(row, where, opponents, seen):
    if not isinstance(row, dict):
        raise invalid(f"{where} must be an object")
    opponent = row.get("opponent")
    if opponent not in opponents:
        raise invalid(f"{where}.opponent {opponent!r} isn't in opponents")
    if opponent in seen:
        raise invalid(f"{where}.opponent {opponent} has two rows")
    seen.add(opponent)
    for name in COUNTS:
        if not is_count(row.get(name)):
            raise invalid(f"{where}.{name} must be a whole number, 0 or more")
    if row["games"] != row["wins"] + row["draws"] + row["losses"]:
        raise invalid(f"{where}: games {row['games']} isn't wins + draws + losses")
    penta = row.get("penta")
    if penta is not None and (not isinstance(penta, list) or len(penta) != 5 or not all(map(is_count, penta))):
        raise invalid(f"{where}.penta must be null or five whole numbers")
    for name in SIDES:
        sides = row.get(name)
        if sides is None:
            continue
        if not isinstance(sides, dict) or not all(is_count(v) for v in sides.values()):
            raise invalid(f"{where}.{name} must map subject and opponent to whole numbers")


# --- what may change under one id ---

def fixed_part(document):
    """The document without what a producer may change as the match goes on."""
    fixed = copy.deepcopy(document)
    for name in ("results", "machines", "pgn", "progress", "games", "stats", "report"):
        fixed.pop(name, None)
    m = fixed.get("match") or {}
    for name in ("state", "updated_at", "finished_at"):
        m.pop(name, None)
    if isinstance(fixed.get("sprt"), dict):
        for name in ("llr", "result"):
            fixed["sprt"].pop(name, None)
    return fixed


def check_update(previous, document, changes=False):
    changed = first_difference(fixed_part(previous), fixed_part(document), "")
    if changed:
        raise IngestError(409, f"{changed.lstrip('.')} changed, only the state, counts and the producer's LLR and "
                               f"result may change on a recorded match")
    if not changes:  # a report by changes is checked entry by entry against the rows, as it is applied
        check_schedule(previous.get("games"), document.get("games"))
    old = {row["opponent"]: row for row in previous.get("results") or [] if isinstance(row, dict)}
    new = {row["opponent"]: row for row in document["results"]}
    for opponent, before in old.items():
        if opponent not in new:
            raise IngestError(409, f"results for {opponent} are missing, counts only grow")
        check_counts_grow(opponent, before, new[opponent])


def check_schedule(before, after):
    """A game that has started keeps its number, round, opponent and colour, and a finished one its result; the
    schedule may grow, and a scheduled game may be corrected or dropped. A playing game may be reported as scheduled
    again: a resumed gauntlet replays what was cut off."""
    if not before:
        return
    now = {game["number"]: game for game in after or []}
    for game in before:
        if not isinstance(game, dict) or game.get("state") == "scheduled":
            continue
        number = game.get("number")
        current = now.get(number)
        if current is None:
            raise IngestError(409, f"games[{number}] is gone, a started game stays in the schedule")
        schedule.check_entry(game, current)


def check_update_against(existing, document):
    """Without the archived version, at least hold that counts only grow."""
    if (document.get("report") or {}).get("base") is None:
        check_schedule(existing.schedule, document.get("games"))
    new = {row["opponent"]: row for row in document["results"]}
    for result in existing.results.all():
        if result.opponent not in new:
            if result.games == 0:
                continue
            raise IngestError(409, f"results for {result.opponent} are missing, counts only grow")
        before = {name: getattr(result, name) for name in COUNTS + SIDES}
        before["penta"] = result.penta
        check_counts_grow(result.opponent, before, new[result.opponent])


def check_counts_grow(opponent, before, after):
    def refuse(name, old, new):
        raise IngestError(409, f"results[{opponent}].{name} went from {old} to {new}, counts only grow")

    for name in COUNTS:
        if after[name] < before[name]:
            refuse(name, before[name], after[name])
    if before.get("penta") is not None:
        if after.get("penta") is None:
            refuse("penta", before["penta"], None)
        for old, new in zip(before["penta"], after["penta"]):
            if new < old:
                refuse("penta", before["penta"], after["penta"])
    for name in SIDES:
        for side, old in (before.get(name) or {}).items():
            new = (after.get(name) or {}).get(side, 0)
            if new < old:
                refuse(f"{name}.{side}", old, new)


# --- archive ---

def read_archived(match_id):
    try:
        return json.loads(archive_path(match_id).read_bytes())
    except (OSError, ValueError):
        return None


def write_archive(match_id, raw, machine):
    """The raw JSON, replaced on update, and a sidecar with who sent it so a rebuild keeps that."""
    folder = archive_dir()
    folder.mkdir(parents=True, exist_ok=True)
    atomic_write(archive_path(match_id), raw)
    meta = {"machine": machine.name if machine else None, "received_at": timezone.now().isoformat()}
    atomic_write(folder / f"{match_id}.meta.json", json.dumps(meta).encode("utf-8"))


def archive_merged(match, document, machine):
    """The archive of a match reported by changes: the document as it now stands, with the whole schedule from the
    rows, written when the match isn't running any more and otherwise at most every NYAYO_ARCHIVE_EVERY seconds, so a
    lost database can be rebuilt from it but a report costs what changed, not what the schedule weighs."""
    path = archive_path(match.pk)
    if match.state == "running" and archive_every() > 0:
        try:
            if timezone.now().timestamp() - path.stat().st_mtime < archive_every():
                return
        except OSError:
            pass
    merged = copy.deepcopy(document)
    merged["games"] = match.schedule
    merged["report"] = {"seq": match.report_seq, "base": None}
    write_archive(match.pk, json.dumps(merged).encode("utf-8"), machine)


def delete_archived(match_id):
    for path in (archive_path(match_id), archive_dir() / f"{match_id}.meta.json"):
        path.unlink(missing_ok=True)


# --- indexing ---

def engine_for(info):
    engine, _ = Engine.objects.get_or_create(name=info["name"], version=info.get("version") or "",
                                             commit=info.get("commit") or "")
    return engine


def participant_info(info, *names):
    return {name: info.get(name) for name in names if info.get(name) is not None}


def apply_result(result, row):
    for name in COUNTS:
        setattr(result, name, row[name])
    result.penta = row.get("penta")
    for name in SIDES:
        setattr(result, name, row.get(name) or {})


def create_match(document, machine):
    m = document["match"]
    host = m.get("host") or {}
    now = timezone.now()
    match = Match.objects.create(
        id=m["id"], machine=machine, kind=m["kind"], state=m["state"], producer=m["producer"],
        producer_name=m["producer"]["name"], name=m.get("name") or "", started_at=timestamp(m, "started_at"),
        finished_at=timestamp(m, "finished_at"), host_name=host.get("name") or "", host=host,
        settings=m.get("settings") or {}, subject=engine_for(document["subject"]),
        subject_info=participant_info(document["subject"], "branch", "repo", "bench", "options"),
        sprt=document.get("sprt"), stats=document.get("stats"), machines=document.get("machines") or [],
        progress=document.get("progress"), received_at=now,
        updated_at=timestamp(m, "updated_at"), report_seq=(document.get("report") or {}).get("seq"),
    )
    schedule.replace(match, document.get("games"))
    match.save(update_fields=["schedule_digest"])
    rows = {row["opponent"]: row for row in document["results"]}
    for ordinal, opponent in enumerate(document["opponents"]):
        result = MatchResult(
            match=match, ordinal=ordinal, opponent=opponent["id"], engine=engine_for(opponent),
            info=participant_info(opponent, "branch", "repo", "bench", "options"), rating=opponent.get("rating"))
        apply_result(result, rows.get(opponent["id"]) or {name: 0 for name in COUNTS})
        result.save()
    return match


def update_match(match, document, changes=False):
    m = document["match"]
    match.stats = document.get("stats")
    match.state = m["state"]
    match.finished_at = timestamp(m, "finished_at")
    match.updated_at = timestamp(m, "updated_at")
    match.sprt = document.get("sprt")
    match.machines = document.get("machines") or []
    match.progress = document.get("progress")
    report = document.get("report")
    match.report_seq = report["seq"] if report else None
    if changes:
        schedule.apply_changes(match, document.get("games") or [])
    else:
        schedule.replace(match, document.get("games"))
    match.save(update_fields=["state", "finished_at", "updated_at", "sprt", "stats", "machines", "progress",
                              "report_seq", "schedule_digest"])
    rows = {row["opponent"]: row for row in document["results"]}
    for result in match.results.all():
        row = rows.get(result.opponent)
        if row is not None:
            apply_result(result, row)
            result.save()
    return match


def snapshot(match, document):
    """Appends the counts as they now stand when they differ from the last snapshot's (and for the first report, as
    the start of the progress graph). A re-send with the same counts adds nothing."""
    counts = {r.opponent: [r.wins, r.draws, r.losses] for r in match.results.all()}
    last = match.snapshots.order_by("-received_at", "-pk").first()
    if last is not None and last.counts == counts:
        return
    wins, draws, losses = (sum(c[i] for c in counts.values()) for i in range(3))
    llr = (document.get("sprt") or {}).get("llr")
    MatchSnapshot.objects.create(match=match, games=wins + draws + losses, wins=wins, draws=draws, losses=losses,
                                 counts=counts, llr=llr, stats=document.get("stats"))
