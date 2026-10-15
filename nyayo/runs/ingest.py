"""The one way results get into nyayo. The push endpoint calls ingest() now, the CI artifact poller will later.

A run is archived first, unchanged, as archive/<id>.json, and then indexed into the database. The archive is the
source of truth; the database can be rebuilt from it.

A run id is uploaded again whenever the run changes, which may only be a promotion: state forward (wip, committed,
published), commit from null to a sha once, the state timestamps and the ancestry recomputed from the new commit.
Anything else is a different run under the same id and is refused.
"""

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import STATES, Case, Check, Metric, Run

SCHEMA_VERSION = 1

# what may differ between two uploads of one run id, everything else is fixed once a run is recorded
PROMOTION_FIELDS = {"state", "commit", "committed_at", "published_at", "ancestors"}

SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
CHECK_STATUSES = {"pass", "error", "skipped"}
CASE_STATUSES = {"pass", "fail", "error"}
BUILD_KEYS = ("compiler", "config", "assertions", "exceptions", "bits")


class IngestError(Exception):
    """A run nyayo refuses. status is the HTTP status, the message a short reason for the client to show."""

    def __init__(self, status, reason):
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass
class Ingested:
    run: Run
    created: bool


def archive_dir():
    return Path(settings.NYAYO_ARCHIVE_DIR)


def archive_path(run_id):
    return archive_dir() / f"{run_id}.json"


def check_key(check, run_build):
    """Checks with equal keys searched the same way: the same check, limit, threads, hash, experiment and build.
    per_file only picks positions, every position is searched on its own, so it isn't part of the key. Matches
    Elephant's test/results.py check_key()."""
    params = check.get("params") or {}
    build = check.get("build") or run_build or {}
    key = {"id": check["id"], "version": check.get("version"), "limit": params.get("limit"),
           "threads": params.get("threads"), "hash_mb": params.get("hash_mb"),
           "experiment": params.get("experiment", False),
           "build": {k: build.get(k) for k in BUILD_KEYS}}
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode("utf-8")).hexdigest()


def ingest(raw, machine=None, run_id=None):
    """Stores one run from its raw JSON bytes. machine is who sent it, run_id the id the request was addressed to.
    Returns Ingested(run, created); raises IngestError for a run that's invalid (400) or an update that isn't a
    promotion (409)."""
    document = parse(raw)
    validate(document, run_id)
    r = document["run"]

    existing = Run.objects.filter(pk=r["id"]).first()
    if existing is not None:
        previous = read_archived(existing.pk)
        if previous is not None:
            check_promotion(previous, document)
        else:
            check_promotion_against(existing, r)

    write_archive(r["id"], raw, machine)
    with transaction.atomic():
        if existing is None:
            run = create_run(document, machine)
        else:
            run = promote_run(existing, r)
    return Ingested(run, existing is None)


# --- parsing & validation ---

def parse(raw):
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise IngestError(400, f"not JSON: {error}")
    if not isinstance(document, dict):
        raise IngestError(400, "the body must be a JSON object")
    return document


def invalid(reason):
    return IngestError(400, reason)


def timestamp(r, name, required=False, where="run"):
    value = r.get(name)
    if value is None:
        if required:
            raise invalid(f"{where}.{name} is missing")
        return None
    parsed = parse_datetime(value) if isinstance(value, str) else None
    if parsed is None:
        raise invalid(f"{where}.{name} isn't an ISO 8601 timestamp: {value!r}")
    if timezone.is_naive(parsed):
        raise invalid(f"{where}.{name} has no timezone: {value!r}")
    return parsed


def validate(document, run_id=None):
    if document.get("schema_version") != SCHEMA_VERSION:
        raise invalid(f"schema_version must be {SCHEMA_VERSION}, got {document.get('schema_version')!r}")
    r = document.get("run")
    if not isinstance(r, dict):
        raise invalid("run is missing")

    try:
        uuid.UUID(str(r.get("id")))
    except ValueError:
        raise invalid(f"run.id isn't a uuid: {r.get('id')!r}")
    if run_id is not None and str(r["id"]).lower() != str(run_id).lower():
        raise invalid(f"run.id {r['id']} doesn't match the url's {run_id}")
    for name in ("kind", "source"):
        if not isinstance(r.get(name), str) or not r[name]:
            raise invalid(f"run.{name} is missing")

    state = r.get("state")
    if state not in STATES:
        raise invalid(f"run.state must be one of {', '.join(STATES)}, got {state!r}")
    commit = r.get("commit")
    if state == "wip":
        if commit is not None:
            raise invalid("a wip run has no commit, run.commit must be null")
        if not r.get("tree"):
            raise invalid("a wip run needs run.tree")
    elif not isinstance(commit, str) or not SHA.match(commit):
        raise invalid(f"a {state} run needs run.commit, a full sha, got {commit!r}")
    for name in ("base_commit", "tree"):
        value = r.get(name)
        if value is not None and not (isinstance(value, str) and SHA.match(value)):
            raise invalid(f"run.{name} isn't a full sha: {value!r}")
    ancestors = r.get("ancestors", [])
    if not isinstance(ancestors, list) or not all(isinstance(a, str) and SHA.match(a) for a in ancestors):
        raise invalid("run.ancestors must be a list of full shas")

    timestamp(r, "recorded_at", required=True)
    for name in ("started_at", "finished_at", "committed_at", "published_at"):
        timestamp(r, name)

    checks = document.get("checks")
    if not isinstance(checks, list):
        raise invalid("checks must be a list")
    keys = {}
    for i, check in enumerate(checks):
        where = f"checks[{i}]"
        if not isinstance(check, dict) or not isinstance(check.get("id"), str) or not check["id"]:
            raise invalid(f"{where}.id is missing")
        if check.get("status") not in CHECK_STATUSES:
            raise invalid(f"{where}.status must be one of {', '.join(sorted(CHECK_STATUSES))}")
        if not isinstance(check.get("params", {}), dict) or not isinstance(check.get("metrics", {}), dict):
            raise invalid(f"{where}.params and .metrics must be objects")
        for name, metric in check.get("metrics", {}).items():
            if not isinstance(metric, dict) or not isinstance(metric.get("value"), (int, float)) \
                    or isinstance(metric.get("value"), bool):
                raise invalid(f"{where}.metrics.{name} needs a numeric value")
        for name in ("started_at", "finished_at"):
            timestamp(check, name, where=where)
        cases = check.get("cases", [])
        if not isinstance(cases, list):
            raise invalid(f"{where}.cases must be a list")
        for j, case in enumerate(cases):
            if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"]:
                raise invalid(f"{where}.cases[{j}].id is missing")
            if case.get("status") not in CASE_STATUSES:
                raise invalid(f"{where}.cases[{j}].status must be one of {', '.join(sorted(CASE_STATUSES))}")
        key = check_key(check, r.get("build"))
        if key in keys:
            raise invalid(f"{where} ({check.get('name') or check['id']}) searched exactly like "
                          f"checks[{keys[key]}], two checks with the same key can't be told apart")
        keys[key] = i


# --- promotion rules ---

def first_difference(old, new, path):
    """The path of the first place old and new differ, or None."""
    if isinstance(old, dict) and isinstance(new, dict):
        for name in sorted(set(old) | set(new)):
            if name not in old or name not in new:
                return f"{path}.{name}"
            found = first_difference(old[name], new[name], f"{path}.{name}")
            if found:
                return found
        return None
    if isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            return f"{path} (length {len(old)} -> {len(new)})"
        for i, (a, b) in enumerate(zip(old, new)):
            found = first_difference(a, b, f"{path}[{i}]")
            if found:
                return found
        return None
    return None if old == new and type(old) is type(new) else path


def check_promotion(previous, document):
    """Refuses with 409 anything but a promotion of the archived version."""
    old_run, new_run = previous.get("run", {}), document["run"]
    check_promotion_against_values(old_run.get("state"), old_run.get("commit"), new_run)
    fixed_old = {**previous, "run": {k: v for k, v in old_run.items() if k not in PROMOTION_FIELDS}}
    fixed_new = {**document, "run": {k: v for k, v in new_run.items() if k not in PROMOTION_FIELDS}}
    changed = first_difference(fixed_old, fixed_new, "")
    if changed:
        raise IngestError(409, f"{changed.lstrip('.')} changed, only a promotion may change a recorded run")


def check_promotion_against(existing, new_run):
    """Without the archived version, at least hold the state and commit rules."""
    check_promotion_against_values(existing.state, existing.commit, new_run)


def check_promotion_against_values(old_state, old_commit, new_run):
    new_state, new_commit = new_run.get("state"), new_run.get("commit")
    if STATES.index(new_state) < STATES.index(old_state):
        raise IngestError(409, f"run.state can't go back from {old_state} to {new_state}")
    if old_commit is not None and new_commit != old_commit:
        raise IngestError(409, f"run.commit is already {old_commit}, it can't change to {new_commit}")


# --- archive ---

def read_archived(run_id):
    try:
        return json.loads(archive_path(run_id).read_bytes())
    except (OSError, ValueError):
        return None


def write_archive(run_id, raw, machine):
    """The run's raw JSON, replaced on update, and a small sidecar with who sent it so a rebuild keeps that."""
    folder = archive_dir()
    folder.mkdir(parents=True, exist_ok=True)
    atomic_write(archive_path(run_id), raw)
    meta = {"machine": machine.name if machine else None, "received_at": timezone.now().isoformat()}
    atomic_write(folder / f"{run_id}.meta.json", json.dumps(meta).encode("utf-8"))


def atomic_write(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def delete_archived(run_id):
    for path in (archive_path(run_id), archive_dir() / f"{run_id}.meta.json"):
        path.unlink(missing_ok=True)


# --- indexing ---

def create_run(document, machine):
    r = document["run"]
    host = r.get("host") or {}
    now = timezone.now()
    run = Run.objects.create(
        id=r["id"], machine=machine, kind=r["kind"], source=r["source"], trigger=r.get("trigger") or "",
        request_id=r.get("request_id"), state=r["state"], commit=r.get("commit"), base_commit=r.get("base_commit"),
        tree=r.get("tree") or "", branch=r.get("branch") or "", ancestors=r.get("ancestors") or [],
        version=r.get("version") or "", started_at=timestamp(r, "started_at"),
        finished_at=timestamp(r, "finished_at"), recorded_at=timestamp(r, "recorded_at"),
        committed_at=timestamp(r, "committed_at"), published_at=timestamp(r, "published_at"),
        host_name=host.get("name") or "", host=host, build=r.get("build") or {}, ci=r.get("ci"),
        received_at=now, updated_at=now,
    )

    metrics, cases = [], []
    for ordinal, c in enumerate(document["checks"]):
        params = c.get("params") or {}
        build = c.get("build") or run.build
        limit = params.get("limit") or {}
        check = Check.objects.create(
            run=run, ordinal=ordinal, check_id=c["id"], version=c.get("version"), name=c.get("name") or "",
            status=c["status"], deterministic=c.get("deterministic", True) is not False,
            limit_type=limit.get("type") or "", limit_value=limit.get("value"), threads=params.get("threads"),
            hash_mb=params.get("hash_mb"), experiment=bool(params.get("experiment", False)),
            compiler=build.get("compiler") or "", config=build.get("config") or "",
            assertions=build.get("assertions"), exceptions=build.get("exceptions"), bits=build.get("bits"),
            key=check_key(c, run.build), per_file=params.get("per_file"), params=params, build=build,
            engine_verdict=c.get("engine_verdict"), started_at=timestamp(c, "started_at"),
            finished_at=timestamp(c, "finished_at"),
        )
        for name, metric in (c.get("metrics") or {}).items():
            metrics.append(Metric(check_result=check, name=name, value=metric["value"], unit=metric.get("unit") or ""))
        hashes = params.get("file_sha256") or {}
        for i, case in enumerate(c.get("cases") or []):
            cases.append(Case(
                check_result=check, ordinal=i, case_id=case["id"], file=case.get("file") or "",
                file_sha256=hashes.get(case.get("file"), ""), status=case["status"], got=case.get("got") or "",
                points=case.get("points"), max_points=case.get("max_points"), score=case.get("score"),
                mate=case.get("mate"), nodes=case.get("nodes"), time_ms=case.get("time_ms"),
                expected=case.get("expected") or [], avoid=case.get("avoid") or [], pv=case.get("pv") or "",
                fen=case.get("fen") or "",
            ))
    Metric.objects.bulk_create(metrics)
    Case.objects.bulk_create(cases, batch_size=500)
    return run


def promote_run(run, r):
    run.state = r["state"]
    run.commit = r.get("commit")
    run.committed_at = timestamp(r, "committed_at")
    run.published_at = timestamp(r, "published_at")
    run.ancestors = r.get("ancestors") or []
    run.updated_at = timezone.now()
    run.save(update_fields=["state", "commit", "committed_at", "published_at", "ancestors", "updated_at"])
    return run
