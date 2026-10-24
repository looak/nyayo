"""The digest of a match's schedule (docs/match-schema.md, The schedule's digest).

An entry's hash is the SHA-256 of its canonical JSON: the eight fields an entry has and no others, a missing one as
null, keys sorted at every level, no whitespace, ASCII. The digest is the XOR of every entry's hash as 64 lowercase hex
digits, so changing an entry costs its hash and not the schedule: XOR the old hash out and the new one in. It is a
check that both sides hold the same schedule, not a security measure.
"""

import hashlib
import json

FIELDS = ("number", "round", "opponent", "subject_colour", "state", "result", "termination", "fault")
EMPTY = "0" * 64


def canonical(entry):
    return json.dumps({name: entry.get(name) for name in FIELDS}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def entry_hash(entry):
    return hashlib.sha256(canonical(entry).encode("ascii")).hexdigest()


def xor(*hashes):
    """The XOR of 64-digit hex hashes, as 64 hex digits."""
    value = 0
    for item in hashes:
        value ^= int(item, 16)
    return f"{value:064x}"


def digest(entries):
    return xor(*(entry_hash(entry) for entry in entries)) if entries else EMPTY
