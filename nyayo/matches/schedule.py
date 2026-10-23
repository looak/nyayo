"""A match's schedule as rows, and the rules a change to it has to keep (docs/match-schema.md, Reports by changes).

A whole document replaces the schedule; a report by changes carries only the entries that changed and adds or replaces
them. Either way the digest is kept up to date for the entries that changed, never recomputed from the whole schedule
on a change: the old entry's hash is XORed out and the new one's in.
"""

from nyayo.runs.ingest import IngestError

from . import digest
from .models import ScheduleEntry

# of a scheduled game, the runner may still correct anything (fastchess has the last word on colours); once it has
# started these are fixed, the rest of the entry is what happens to it
STARTED_FIXED = ("round", "opponent", "subject_colour")


def check_entry(before, after):
    """The rules for one entry that was already there: a game that has started keeps its round, opponent and colour,
    and a finished one its result. A playing game may be reported scheduled again (a resumed gauntlet replays what was
    cut off). before and after are entries as dicts; IngestError 409 naming the entry and the field."""
    if before.get("state") == "scheduled":
        return
    number = before.get("number")
    for name in STARTED_FIXED:
        if after.get(name) != before.get(name):
            raise IngestError(409, f"games[{number}].{name} changed, a started game's {name} is fixed")
    if before.get("state") == "finished" and (after.get("state"), after.get("result")) != ("finished",
                                                                                         before.get("result")):
        raise IngestError(409, f"games[{number}] finished with {before.get('result')} and can't change")


def row_for(match, entry):
    return ScheduleEntry(
        match=match, number=entry["number"], round=entry.get("round"), opponent=entry["opponent"],
        subject_colour=entry["subject_colour"], state=entry["state"], result=entry.get("result"),
        termination=entry.get("termination"), fault=entry.get("fault"), hash=digest.entry_hash(entry))


def forget_cache(match):
    match._schedule = None


def replace(match, games):
    """The schedule becomes exactly games (a whole document's). The rules against what was there are checked by the
    caller; here rows are added, updated and removed, and the digest made from every hash."""
    old = {row.number: row for row in ScheduleEntry.objects.filter(match=match)}
    new = {entry["number"]: entry for entry in games or []}
    gone = [row.pk for number, row in old.items() if number not in new]
    if gone:
        ScheduleEntry.objects.filter(pk__in=gone).delete()
    added, changed = [], []
    for number, entry in new.items():
        row = row_for(match, entry)
        if number not in old:
            added.append(row)
        elif old[number].hash != row.hash:
            row.pk = old[number].pk
            changed.append(row)
    if added:
        ScheduleEntry.objects.bulk_create(added, batch_size=500)
    if changed:
        ScheduleEntry.objects.bulk_update(changed, ["round", "opponent", "subject_colour", "state", "result",
                                                    "termination", "fault", "hash"], batch_size=500)
    match.schedule_digest = digest.digest(list(new.values()))
    forget_cache(match)


def apply_changes(match, entries):
    """The changed entries on top of the schedule as it is, each checked against the entry it replaces; the digest is
    updated by those entries only."""
    old = {row.number: row for row in ScheduleEntry.objects.filter(match=match, number__in=[e["number"]
                                                                                           for e in entries])}
    value = match.schedule_digest or digest.EMPTY
    added, changed = [], []
    for entry in entries:
        row = row_for(match, entry)
        before = old.get(entry["number"])
        if before is None:
            added.append(row)
            value = digest.xor(value, row.hash)
            continue
        check_entry(before.as_dict(), entry)
        if before.hash != row.hash:
            row.pk = before.pk
            changed.append(row)
            value = digest.xor(value, before.hash, row.hash)
    if added:
        ScheduleEntry.objects.bulk_create(added, batch_size=500)
    if changed:
        ScheduleEntry.objects.bulk_update(changed, ["round", "opponent", "subject_colour", "state", "result",
                                                    "termination", "fault", "hash"], batch_size=500)
    match.schedule_digest = value
    forget_cache(match)
