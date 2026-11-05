"""Giving the matches taken from OpenBench the ids of tests' matches (`manage.py rename_matches`).

`openbench-<anything>-<N>` becomes `<mode>-<N>`: the mode of lab test N where there is one, else the match's kind.
Everything that holds the id is moved, in this order, and each step can be repeated:

1. the database, one transaction per match: a copy of the Match row under the new id, every row that refers to the
   old one pointed at the new (found from the model, so a table added later is not forgotten), the old row deleted,
   and a `RenamedMatch` row written, which is what links and a second run find the old id by;
2. the datasets that name the match (`manifest[].match` and `selection.matches`);
3. the files, from the `RenamedMatch` rows, so a run that stopped after step 1 finishes them: archive/matches/<id>.json
   (its match.id rewritten) and .meta.json, games/<id>/, the batches directory's <id>/ and uploads/<id>/.

The index database (NYAYO_INDEX_DB) holds batch and match-result row numbers, never a match id, so it is untouched.
The step a run stopped in is told by what is left under an old name; running it again does the rest and nothing twice.
A new id that is taken (a match, a tombstone, a file or directory that would be overwritten, two matches wanting the
same one) is refused before anything is changed, naming every one. Services that write matches (nyayo, nyayo-worker)
must be stopped while it runs.
"""

import json
import os
import re
from dataclasses import dataclass, field

from django.db import transaction

from nyayo.lab.ids import OLD_FORM, PREFIXES
from nyayo.runs.ingest import atomic_write

from . import catalogue, games, ingest, uploads
from .models import ID_PATTERN, MATCH_ID_MAX, Dataset, Match, RenamedMatch, Tombstone

# a match kind to the prefix its renamed id takes, when no lab test says its mode: tests of mode SPRT and GAMES make
# matches of these kinds (lab/document.py), and the OpenBench server's were all sprt
KIND_PREFIX = {"sprt": "sprt", "games": "games"}


class RenameError(Exception):
    """Why nothing was changed: the refusals, one per line."""

    def __init__(self, problems):
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass
class Plan:
    matches: dict = field(default_factory=dict)    # old id -> new id, the matches still under their old ids
    problems: list = field(default_factory=list)
    notes: list = field(default_factory=list)       # things to look at that do not stop it


def number_of(old_id):
    return int(OLD_FORM.match(old_id)["number"])


def new_id_for(match, tests):
    """The id the match takes, or None when there is no rule for it. tests: {test id: Test}."""
    test = tests.get(number_of(match.pk))
    prefix = PREFIXES.get(test.test_mode) if test is not None else KIND_PREFIX.get(match.kind)
    return f"{prefix}-{number_of(match.pk)}" if prefix else None


def plan_database():
    """What step 1 would do, and what stops it: every old-form Match, and the refusals."""
    from nyayo.lab.models import Test

    plan = Plan()
    old = [m for m in Match.objects.all() if OLD_FORM.match(m.pk)]
    tests = {t.id: t for t in Test.objects.filter(id__in=[number_of(m.pk) for m in old])}
    wanted = {}
    for match in old:
        new = new_id_for(match, tests)
        if new is None:
            plan.problems.append(f"{match.pk}: kind {match.kind} has no rule and there is no lab test {number_of(match.pk)}")
            continue
        if len(new) > MATCH_ID_MAX or not re.match(ID_PATTERN, new):
            plan.problems.append(f"{match.pk}: {new} is not a valid match id")
        wanted.setdefault(new, []).append(match.pk)
        plan.matches[match.pk] = new
    for new, olds in sorted(wanted.items()):
        if len(olds) > 1:
            plan.problems.append(f"{', '.join(sorted(olds))} would all become {new}")
        if Match.objects.filter(pk=new).exists():
            plan.problems.append(f"{olds[0]}: a match {new} exists already")
        if Tombstone.objects.filter(pk=new).exists():
            plan.problems.append(f"{olds[0]}: {new} is the id of a match that was deleted")
        if RenamedMatch.objects.filter(old_id=new).exists():
            plan.problems.append(f"{olds[0]}: {new} was the old id of another match")
    for match in old:
        # the match's own JSON columns are not rewritten: a mention of its id there is only told
        text = json.dumps([match.producer, match.host, match.settings, match.subject_info, match.sprt, match.verdict,
                           match.progress, match.machines], default=str)
        if match.pk in text:
            plan.notes.append(f"{match.pk}: the id is also written inside the match's own fields, which are left as they are")
    return plan


def pairs(plan):
    """Every (old, new) the files are looked for under: the recorded renames and the ones step 1 would make."""
    both = dict(RenamedMatch.objects.values_list("old_id", "new_id"))
    both.update(plan.matches)
    return sorted(both.items())


# --- steps 1 and 2: the database ---

def rename_in_database(old_id, new_id):
    """One match, in one transaction. Repeating it after it is done does nothing."""
    with transaction.atomic():
        old = Match.objects.filter(pk=old_id).first()
        if old is None:
            return False
        copy = Match(**{f.attname: getattr(old, f.attname) for f in Match._meta.concrete_fields})
        copy.pk = new_id
        copy.save(force_insert=True)
        for relation in Match._meta.related_objects:
            if relation.one_to_many or relation.one_to_one:
                name = relation.field.name
                relation.related_model.objects.filter(**{name: old_id}).update(**{name: new_id})
        old.delete()
        RenamedMatch.objects.create(old_id=old_id, new_id=new_id)
    return True


def datasets_naming(renamed):
    """The datasets whose manifest or selection names a match in `renamed` ({old: new})."""
    found = []
    for dataset in Dataset.objects.all():
        used = {e.get("match") for e in dataset.manifest or [] if isinstance(e, dict)}
        used |= set((dataset.selection or {}).get("matches") or [])
        if used & set(renamed):
            found.append(dataset)
    return found


def fix_datasets(renamed):
    """The datasets that name a renamed match, pointed at the new id. Returns how many changed."""
    changed = datasets_naming(renamed)
    for dataset in changed:
        for entry in dataset.manifest or []:
            if isinstance(entry, dict) and entry.get("match") in renamed:
                entry["match"] = renamed[entry["match"]]
        selection = dataset.selection or {}
        if selection.get("matches"):
            selection["matches"] = [renamed.get(m, m) for m in selection["matches"]]
        dataset.selection = selection
        dataset.save(update_fields=["manifest", "selection"])
    return len(changed)


# --- step 3: the files ---

def directories():
    """(label, directory) of the places a directory is named after a match."""
    return [("games", games.games_dir()), ("batches", catalogue.batches_dir()), ("uploads", uploads.uploads_dir())]


def archive_bytes(old_id, new_id, raw):
    """The archived document with match.id changed and nothing else; ValueError when it isn't the old match's."""
    document = json.loads(raw)
    found = (document.get("match") or {}).get("id")
    if found != old_id:
        raise ValueError(f"{old_id}.json holds match {found!r}")
    expected = json.loads(raw)
    expected["match"]["id"] = new_id
    # the quoted id changed wherever it stands keeps the bytes as they were; if that changed any other value too, only
    # match.id is written, the document dumped again
    replaced = raw.decode("utf-8").replace(json.dumps(old_id), json.dumps(new_id))
    return replaced.encode("utf-8") if json.loads(replaced) == expected else json.dumps(expected).encode("utf-8")


def file_steps(old_id, new_id):
    """What remains under the old name of one match: [(kind, source, target, action)], action "move", "rewrite" (an
    archive document, written under the new name with its id changed) or "clash" (the target exists and is not what
    this would leave). A target that already holds exactly that is not a clash: the run stopped after writing it."""
    steps = []
    folder = ingest.archive_dir()
    for suffix in (".json", ".meta.json"):
        source, target = folder / f"{old_id}{suffix}", folder / f"{new_id}{suffix}"
        if not source.exists():
            continue
        raw = source.read_bytes()
        wanted = archive_bytes(old_id, new_id, raw) if suffix == ".json" else raw
        clash = target.exists() and target.read_bytes() != wanted
        steps.append(("archive", source, target, "clash" if clash else "rewrite" if suffix == ".json" else "move"))
    for label, base in directories():
        source, target = base / old_id, base / new_id
        if source.exists():
            steps.append((label, source, target, "clash" if target.exists() else "move"))
    return steps


def do_file_step(old_id, new_id, step):
    kind, source, target, action = step
    if kind == "archive":
        raw = source.read_bytes()
        atomic_write(target, archive_bytes(old_id, new_id, raw) if action == "rewrite" else raw)
        source.unlink()
    else:
        os.rename(source, target)


@dataclass
class Result:
    renamed: int = 0
    datasets: int = 0
    moved: int = 0


def run(dry_run=False, out=print):
    """Renames what is left to rename, or says what it would. Raises RenameError, having changed nothing, when
    anything stands in the way."""
    plan = plan_database()
    steps = {}
    for old_id, new_id in pairs(plan):
        try:
            found = file_steps(old_id, new_id)
        except ValueError as error:
            plan.problems.append(str(error))
            continue
        if found:
            steps[(old_id, new_id)] = found
    problems = list(plan.problems)
    for (old_id, new_id), found in steps.items():
        problems += [f"{old_id}: {target} exists already and is not what moving {source} would leave"
                     for kind, source, target, action in found if action == "clash"]
    for note in plan.notes:
        out(f"note: {note}")
    if problems:
        raise RenameError(problems)

    result = Result()
    for old_id, new_id in sorted(plan.matches.items()):
        out(f"{old_id} -> {new_id}: database rows {'would move' if dry_run else 'moved'}")
        if not dry_run:
            rename_in_database(old_id, new_id)
        result.renamed += 1
    renamed = dict(pairs(plan))
    if dry_run:
        result.datasets = len(datasets_naming(renamed))
    else:
        result.datasets = fix_datasets(renamed)
    if result.datasets:
        out(f"{result.datasets} dataset(s) {'would be' if dry_run else 'were'} pointed at the new ids")
    for (old_id, new_id), found in steps.items():
        for step in found:
            kind, source, target, action = step
            what = "rewrite its id and move" if action == "rewrite" else "move"
            out(f"{old_id} -> {new_id}: {kind}: would {what} {source}" if dry_run
                else f"{old_id} -> {new_id}: {kind}: {source.name} -> {target.name}")
            if not dry_run:
                do_file_step(old_id, new_id, step)
            result.moved += 1
    return result


def orphans():
    """Entries named like an old id that no match and no recorded rename accounts for: left alone, told about."""
    known = set(RenamedMatch.objects.values_list("old_id", flat=True)) | set(Match.objects.values_list("pk", flat=True))
    found = []
    for folder in [ingest.archive_dir()] + [base for _, base in directories()]:
        if not folder.is_dir():
            continue
        for entry in sorted(folder.iterdir()):
            name = entry.name
            for suffix in (".meta.json", ".json"):
                if name.endswith(suffix):
                    name = name[:-len(suffix)]
                    break
            if OLD_FORM.match(name) and name not in known:
                found.append(entry)
    return found
