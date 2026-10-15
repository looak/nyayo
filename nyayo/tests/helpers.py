"""Runs for tests, all derived from the real smoke run in fixtures/ rather than written by hand."""

import copy
import gzip
import json
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.test import TestCase, override_settings

from nyayo.runs.models import Machine

FIXTURE = Path(__file__).parent / "fixtures" / "d695c0979c67-smoke-20261014T015748.json"
_REAL = json.loads(FIXTURE.read_bytes())


def real_run():
    return copy.deepcopy(_REAL)


def fake_sha(label):
    return uuid.uuid5(uuid.NAMESPACE_OID, label).hex + "0" * 8


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat()


RECORDED = datetime.fromisoformat(_REAL["run"]["recorded_at"])


def derived(state="committed", commit=None, parents=None, branch=None, minutes=0, base=None):
    """The real run as another run: a new id, on commit (a new sha by default) whose ancestry is commit followed by
    parents (the real run's ancestry by default), recorded minutes after the real one."""
    document = real_run()
    r = document["run"]
    r["id"] = str(uuid.uuid4())
    r["recorded_at"] = iso(RECORDED + timedelta(minutes=minutes))
    parents = r["ancestors"] if parents is None else parents
    if state == "wip":
        r["state"], r["commit"] = "wip", None
        r["base_commit"] = base or parents[0]
        r["tree"] = fake_sha(f"tree {r['id']}")
        r["ancestors"] = [r["base_commit"]] + [p for p in parents if p != r["base_commit"]]
    else:
        commit = commit or fake_sha(f"commit {r['id']}")
        r["state"], r["commit"] = state, commit
        r["ancestors"] = [commit] + [p for p in parents if p != commit]
    if branch:
        r["branch"] = branch
    return document


def body(document):
    return json.dumps(document).encode("utf-8")


def case(document, check_index, case_id):
    return next(c for c in document["checks"][check_index]["cases"] if c["id"] == case_id)


class NyayoTestCase(TestCase):
    """A temporary archive directory and a machine with a token."""

    def setUp(self):
        self.archive = Path(tempfile.mkdtemp(prefix="nyayo-archive-"))
        self.addCleanup(shutil.rmtree, self.archive, ignore_errors=True)
        self.games = Path(tempfile.mkdtemp(prefix="nyayo-games-"))
        self.addCleanup(shutil.rmtree, self.games, ignore_errors=True)
        overrides = override_settings(NYAYO_ARCHIVE_DIR=self.archive, NYAYO_GAMES_DIR=self.games)
        overrides.enable()
        self.addCleanup(overrides.disable)
        self.machine = Machine(name="babylon")
        self.token = self.machine.new_token()
        self.machine.save()

    def put(self, document, token=None, run_id=None, gzipped=False, raw=None):
        data = raw if raw is not None else body(document)
        headers = {"Authorization": f"Bearer {token or self.token}"}
        if gzipped:
            data = gzip.compress(data)
            headers["Content-Encoding"] = "gzip"
        run_id = run_id or document["run"]["id"]
        return self.client.put(f"/api/runs/{run_id}", data=data, content_type="application/json", headers=headers)

    def ingest(self, document):
        response = self.put(document)
        self.assertIn(response.status_code, (200, 201), response.content)
        return document
