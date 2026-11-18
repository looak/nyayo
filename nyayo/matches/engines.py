"""The catalogue of engines (docs/match-schema.md, The catalogue): finding a row for what a report calls an engine,
filling its catalogue fields from the report, and merging two rows that turned out to be one.

A row is found by `(name, version, commit)` exactly, or failing that by an alias: another name the same engine was
reported under (`leorik` for `Leorik 3.0`). Filling never overwrites: what a row has, a report doesn't change.
"""

import re

from django.db import transaction
from django.db.models import Q

from .models import Engine, Match, MatchResult

TEXT_FIELDS = ("url", "ref", "asset", "binary")
RATING_KEYS = ("value", "list", "as_of", "source")
# what a producer may send on an engine, besides its name and version: `repo` is what uwanja sends for a subject
URL_KEYS = ("url", "repo")


def clean_rating(rating):
    """A rating as the catalogue keeps it, or None when there is none: a number, or { value, list, as_of, source }."""
    if isinstance(rating, (int, float)) and not isinstance(rating, bool):
        rating = {"value": rating}
    if not isinstance(rating, dict) or not isinstance(rating.get("value"), (int, float)):
        return None
    return {key: rating[key] for key in RATING_KEYS if rating.get(key) is not None}


def find(info):
    """The row for an engine as a report names it, or None: the exact identity, else an alias (compared without case).
    An alias is a name, so it stands for the engine whatever version the report gave, unless the report names one and
    the row has another."""
    name, version, commit = info["name"], info.get("version") or "", info.get("commit") or ""
    exact = Engine.objects.filter(name=name, version=version, commit=commit).first()
    if exact is not None:
        return exact
    wanted = name.casefold()
    for engine in Engine.objects.exclude(aliases=[]).order_by("pk"):
        if wanted not in (alias.casefold() for alias in engine.aliases if isinstance(alias, str)):
            continue
        if version and engine.version and version != engine.version:
            continue
        if commit and engine.commit and commit != engine.commit:
            continue
        return engine
    return None


def engine_for(info, extra=None):
    """The row for a reported engine, made if there is none, with its catalogue fields filled from `extra` (see
    `fill`)."""
    engine = find(info)
    if engine is None:
        engine, _ = Engine.objects.get_or_create(name=info["name"], version=info.get("version") or "",
                                                 commit=info.get("commit") or "")
    fill(engine, extra)
    return engine


def fill(engine, extra):
    """Sets the catalogue fields `extra` has and the row hasn't, and saves when it set any. `extra` is a dict with any
    of url, ref, asset, binary, options and rating; the row's own values are never replaced."""
    if not extra:
        return False
    changed = []
    for name in TEXT_FIELDS:
        value = extra.get(name)
        if isinstance(value, str) and value and not getattr(engine, name):
            setattr(engine, name, value)
            changed.append(name)
    if isinstance(extra.get("options"), dict) and extra["options"] and not engine.options:
        engine.options = extra["options"]
        changed.append("options")
    rating = clean_rating(extra.get("rating"))
    if rating and not engine.rating:
        engine.rating = rating
        changed.append("rating")
    if changed:
        engine.save(update_fields=changed)
    return bool(changed)


def declared(info):
    """What a participant of a match document says about where it comes from: `url` (or `repo`), `ref`, `asset`,
    `binary`, and its rating. All optional; uwanja sends `repo` on the subject only, and no `ref`."""
    extra = {name: info.get(name) for name in ("ref", "asset", "binary")}
    extra["url"] = next((info[key] for key in URL_KEYS if isinstance(info.get(key), str) and info[key]), None)
    extra["rating"] = info.get("rating")
    return extra


def configured(document):
    """The opponents' entries of the gauntlet file in the document's `config` (`config.gauntlet`, as the producer
    normalised it), by opponent id. Opaque to nyayo otherwise, so this reads what is there and ignores the rest."""
    gauntlet = ((document.get("config") or {}).get("gauntlet") if isinstance(document.get("config"), dict) else None)
    if not isinstance(gauntlet, dict):
        return {}
    entries = gauntlet.get("opponents") or gauntlet.get("opponent") or []
    return {entry["id"]: entry for entry in entries if isinstance(entry, dict) and isinstance(entry.get("id"), str)}


def extra_for(document, participant, role):
    """What the document says about one participant for `fill`: its own keys first, then its entry in `config`."""
    extra = declared(participant)
    if role == "opponent":
        entry = configured(document).get(participant.get("id"))
        if entry:
            for name, value in declared(entry).items():
                if not extra.get(name):
                    extra[name] = value
            if isinstance(entry.get("options"), dict):
                extra["options"] = entry["options"]
    return extra


def fill_from_document(match, document):
    """Fills the rows of a stored match's subject and opponents from its document, as ingest does."""
    if isinstance(document.get("subject"), dict):
        fill(match.subject, extra_for(document, document["subject"], "subject"))
    by_id = {o["id"]: o for o in document.get("opponents") or [] if isinstance(o, dict) and "id" in o}
    for result in match.results.select_related("engine"):
        if result.opponent in by_id:
            fill(result.engine, extra_for(document, by_id[result.opponent], "opponent"))


# --- the list ---

def catalogue():
    """The rows the engines page lists: those with a `url` and a `ref`, and those played as a gauntlet's opponent;
    not every commit an SPRT reported."""
    played = MatchResult.objects.filter(match__kind="gauntlet").values("engine")
    return Engine.objects.filter((~Q(url="") & ~Q(ref="")) | Q(pk__in=played)).order_by("name", "version")


# --- merging ---

class MergeError(Exception):
    """Said to the person who asked, as it is."""


@transaction.atomic
def merge(keep, drop):
    """Keeps `keep`, repoints every match (subject and opponents) from `drop` to it, adds `drop`'s name and aliases to
    the aliases, fills what `keep` lacks from `drop`, and deletes `drop`. Final: there is no undoing it."""
    if keep.pk == drop.pk:
        raise MergeError("An engine can't be merged into itself.")
    Match.objects.filter(subject=drop).update(subject=keep)
    MatchResult.objects.filter(engine=drop).update(engine=keep)
    fill(keep, {**{name: getattr(drop, name) for name in TEXT_FIELDS}, "options": drop.options, "rating": drop.rating})
    aliases = [a for a in keep.aliases if isinstance(a, str)]
    for name in [drop.name] + [a for a in drop.aliases if isinstance(a, str)]:
        if name != keep.name and name not in aliases:
            aliases.append(name)
    keep.aliases = aliases
    keep.save(update_fields=["aliases"])
    drop.delete()
    return keep


def parse_aliases(text):
    """Aliases typed one per line or separated by commas."""
    seen, out = set(), []
    for piece in re.split(r"[\n,]", text or ""):
        piece = piece.strip()
        if piece and piece.casefold() not in seen:
            seen.add(piece.casefold())
            out.append(piece)
    return out


# --- backfill ---

# what a gauntlet file says about an engine that the catalogue keeps; a `cmd` engine (a local build) has no url
FILE_KEYS = ("url", "ref", "asset", "binary", "options", "rating")

def slug(text):
    """uwanja's engine id from a name or a URL (uwanja/config.py `_slug`), so a gauntlet file's opponents can be
    matched to the ids of the match they made."""
    text = re.sub(r"/releases/.*$|\.git$|\.exe$", "", str(text).rstrip("/"))
    piece = re.split(r"[/\\]", text)[-1]
    return re.sub(r"[^A-Za-z0-9._-]+", "-", piece).strip("-").lower() or "engine"


def file_ids(tables):
    """The ids uwanja gives a file's engines: the `id` key, else the slug of the name or URL, made unique with -2, -3."""
    taken, ids = set(), []
    for table in tables:
        ident = table.get("id") or slug(table.get("name") or table.get("url") or table.get("cmd") or "")
        base, number = ident, 2
        while ident in taken:
            ident, number = f"{base}-{number}", number + 1
        taken.add(ident)
        ids.append(ident)
    return ids


def backfill_matches():
    """Fills the catalogue from every stored gauntlet: its archived document where there is one (what ingest does for a
    new report), and else what the index kept (an opponent's rating and `repo`). Returns the number of rows changed."""
    from .ingest import read_archived

    changed = 0
    for match in Match.objects.filter(kind="gauntlet").select_related("subject"):
        document = read_archived(match.pk)
        before = {}
        for result in match.results.select_related("engine"):
            before[result.engine_id] = (result.engine.url, result.engine.ref, result.engine.rating)
            fill(result.engine, {"rating": result.rating, "url": (result.info or {}).get("repo")})
        if document:
            fill_from_document(match, document)
        for result in match.results.select_related("engine"):
            if before.get(result.engine_id) != (result.engine.url, result.engine.ref, result.engine.rating):
                changed += 1
                before[result.engine_id] = (result.engine.url, result.engine.ref, result.engine.rating)
    return changed


def backfill_file(data, notes):
    """Fills the subject's and the opponents' rows from a gauntlet file (uwanja's TOML, already parsed): its `name` is the match's name, each
    `[[opponent]]` is found through that match by its id. Returns the number of rows changed; what could not be
    matched is appended to `notes`."""
    match = Match.objects.filter(name=data.get("name"), kind="gauntlet").first()
    tables = data.get("opponent") or []
    if match is None:
        notes.append(f"no stored gauntlet is named {data.get('name')!r}")
        return 0
    changed = 0
    subject = data.get("subject")
    if isinstance(subject, dict) and fill(match.subject, {name: subject.get(name) for name in FILE_KEYS}):
        changed += 1
    results = {r.opponent: r for r in match.results.select_related("engine")}
    for ident, table in zip(file_ids(tables), tables):
        result = results.get(ident)
        if result is None:
            notes.append(f"{match.name}: no opponent {ident!r} in the stored match")
            continue
        extra = {name: table.get(name) for name in FILE_KEYS}
        if fill(result.engine, extra):
            changed += 1
    return changed
