"""A gauntlet file in uwanja's format (`[subject]` and `[[opponent]]`), built from the engine catalogue and written as TOML.

The shape is uwanja's own (its `config.py`, settled 2026-11-18): top-level `name`, `tc`, `book`, `rounds`, `concurrency`;
`[subject]` is a catalogue engine's `url` and `ref` (or a local `cmd` and a `name`); each `[[opponent]]` has `url`, `ref`,
`asset`, `binary`, `options`, an `id` and `rating = { value, list, as_of, source }`. A subject has no rating. Nothing here
imports uwanja: the rules a file must meet are mirrored in the tests.

The writer is small and only writes what the file holds (strings, numbers, booleans, lists and tables of them), with
strings escaped as TOML basic strings, so a quote, a backslash or a newline in a name or an option can't change the file.
"""

import json
import math
import re
from dataclasses import dataclass, field

from . import engines

NAME = re.compile(r"[A-Za-z0-9._-]+")
BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
DEFAULT_TC = "120+1"
DEFAULT_BOOK = "../books/UHO_Lichess_4852_v1.epd"
DEFAULT_ROUNDS = 50
DEFAULT_CONCURRENCY = 10
DEFAULT_RADIUS = 150
DEFAULT_SUBJECT_OPTIONS = {"Threads": 1, "Hash": 64}
RATING_KEYS = ("value", "list", "as_of", "source")


# --- the writer ---

def string(text):
    """A TOML basic string. JSON's escapes are TOML's (\\" \\\\ \\b \\t \\n \\f \\r and \\uXXXX for the other controls);
    DEL is the one control JSON leaves bare and TOML doesn't allow."""
    return json.dumps(str(text), ensure_ascii=False).replace("\x7f", "\\u007f")


def key(name):
    name = str(name)
    return name if BARE_KEY.fullmatch(name) else string(name)


def value(item):
    """A TOML value: a string, a bool, a number, a list, or an inline table."""
    if isinstance(item, bool):
        return "true" if item else "false"
    if isinstance(item, int):
        return str(item)
    if isinstance(item, float):
        if math.isnan(item) or math.isinf(item):
            raise ValueError("a gauntlet file can't hold %r" % item)
        return str(int(item)) if item == int(item) and abs(item) < 1e15 else repr(item)
    if isinstance(item, (list, tuple)):
        return "[" + ", ".join(value(i) for i in item) + "]"
    if isinstance(item, dict):
        return table(item)
    return string(item)


def table(items):
    """An inline table: `{ Threads = 1, Hash = 64 }`."""
    if not items:
        return "{}"
    return "{ " + ", ".join(f"{key(k)} = {value(v)}" for k, v in items.items()) + " }"


# --- the file ---

@dataclass
class Engine:
    """One engine of the file: what its table says, in the order it is written."""
    fields: dict = field(default_factory=dict)
    comment: str = ""


def render_engine(header, entry):
    comment = re.sub(r"[\x00-\x1f\x7f]+", " ", entry.comment).strip()   # a name with a newline mustn't end the comment
    lines = [header + (f"  # {comment}" if comment else "")]
    for name, item in entry.fields.items():
        lines.append(f"{key(name)} = {value(item)}")
    return "\n".join(lines)


def dump(settings, subject, opponents):
    """The file as text. `settings` holds name, tc, book, rounds, concurrency; `subject` and each opponent are
    `Engine`s (the keys of their tables, and a comment for the opponents' lines)."""
    head = [f"{key(name)} = {value(item)}" for name, item in settings.items() if item not in (None, "")]
    parts = ["\n".join(head), render_engine("[subject]", subject)]
    parts += [render_engine("[[opponent]]", entry) for entry in opponents]
    return "\n\n".join(parts) + "\n"


# --- from the catalogue ---

def rating_of(engine):
    """The list's rating of a row as the file takes it, { value, list, as_of, source }, or None."""
    rating = engines.clean_rating(engine.rating)
    return {name: rating[name] for name in RATING_KEYS if name in rating} if rating else None


def centre(engine):
    """Where an opponent sits for a rating range: the list's number, else ours."""
    rating = rating_of(engine)
    if rating:
        return rating["value"]
    return engine.ours["value"] if isinstance(engine.ours, dict) else None


def within(candidates, middle, radius):
    """The candidates whose rating is within `radius` of `middle`, nearest first; one without a rating is not."""
    found = [(abs(centre(e) - middle), e) for e in candidates if centre(e) is not None and abs(centre(e) - middle) <= radius]
    return [e for _, e in sorted(found, key=lambda pair: (pair[0], pair[1].name, pair[1].version))]


def unique(ident, taken):
    base, number = ident, 2
    while ident in taken:
        ident, number = f"{base}-{number}", number + 1
    taken.add(ident)
    return ident


def engine_fields(row):
    """What the file takes for a catalogue row: url, ref, asset, binary and the row's default options."""
    out = {"url": row.url, "ref": row.ref}
    for name in ("asset", "binary"):
        if getattr(row, name):
            out[name] = getattr(row, name)
    return out


def opponent_entries(rows, taken):
    """The `[[opponent]]` entries for catalogue rows (each with a `url` and a `ref`), each with an id the file is
    sure to keep, its row's options and its list rating."""
    out = []
    for row in rows:
        ident = unique(engines.slug(row.name), taken) if engines.slug(row.name) not in taken \
            else unique(engines.slug(f"{row.name}-{row.ref}"), taken)
        fields = {**engine_fields(row), "id": ident}
        if row.options:
            fields["options"] = row.options
        rating = rating_of(row)
        if rating:
            fields["rating"] = rating
        label = f"{row.name} {row.version}".strip()
        if isinstance(row.ours, dict) and isinstance(row.ours.get("value"), (int, float)):
            label += f", ours {row.ours['value']:.0f}"
        out.append(Engine(fields, label))
    return out


def subject_entry(row=None, cmd="", name="", options=None):
    """The `[subject]`: a catalogue row (url and ref) or a local `cmd` with its `name`. Returns (entry, the id uwanja
    will give it, so no opponent takes it)."""
    if row is not None:
        fields = engine_fields(row)
        fields["options"] = options if options is not None else (row.options or DEFAULT_SUBJECT_OPTIONS)
        ident = engines.slug(row.url)
    else:
        fields = {"cmd": cmd}
        if name:
            fields["name"] = name
        fields["options"] = options if options is not None else DEFAULT_SUBJECT_OPTIONS
        ident = engines.slug(name or cmd)
    return Engine(fields), ident


def default_name(row=None, name="", cmd=""):
    """A file name from the subject: `elephant-1.2.0-gauntlet`."""
    if row is not None:
        parts = [engines.slug(row.name), engines.slug(row.ref) if row.ref else ""]
    else:
        parts = [engines.slug(name or cmd)]
    return "-".join(p for p in parts if p) + "-gauntlet"


def build(settings, subject_row, subject_cmd, subject_name, subject_options, opponent_rows):
    """The gauntlet file's text. The caller has checked the settings and that there is a subject and an opponent."""
    entry, ident = subject_entry(subject_row, subject_cmd, subject_name, subject_options)
    taken = {ident}
    return dump(settings, entry, opponent_entries(opponent_rows, taken))
