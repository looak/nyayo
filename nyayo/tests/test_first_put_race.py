"""Two producers sending a new match's first document at the same moment: one creates it, the other is an update."""

import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from django.db import IntegrityError


from nyayo.matches import ingest
from nyayo.matches.models import Match, MatchSnapshot

from .test_matches_ingest import GAUNTLET, MatchTestCase

REPO = Path(__file__).resolve().parents[2]


def misses_once():
    """A find_match that says there is no match the first time it is asked, as it did for the producer that lost."""
    real, asked = ingest.find_match, []

    def find(match_id):
        if not asked:
            asked.append(match_id)
            return None
        return real(match_id)

    return find


class LostTheLookup(MatchTestCase):
    """The lookup said there was no match, and by the time this document is stored there is one."""

    def test_the_second_document_is_applied_as_an_update(self):
        self.assertEqual(self.put_match(GAUNTLET).status_code, 201)
        with mock.patch.object(ingest, "find_match", side_effect=misses_once()):
            response = self.put_match(GAUNTLET)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual((Match.objects.count(), MatchSnapshot.objects.count()), (1, 1))
        self.assertEqual(Match.objects.get().results.count(), 2)

    def test_it_is_checked_against_what_the_other_one_wrote(self):
        self.assertEqual(self.put_match(GAUNTLET).status_code, 201)
        shrunk = copy.deepcopy(GAUNTLET)
        shrunk["results"][0].update(games=8, wins=4, draws=2, losses=2)
        with mock.patch.object(ingest, "find_match", side_effect=misses_once()):
            response = self.put_match(shrunk)
        self.assertRefused(response, 409, "counts only grow")
        self.assertEqual(Match.objects.get().results.get(opponent="barbarossa").games, 10)

    def test_another_integrity_error_is_still_an_error(self):
        with mock.patch.object(ingest, "create_match", side_effect=IntegrityError("x")):
            with self.assertRaises(IntegrityError):
                self.put_match(GAUNTLET)


CHILD = textwrap.dedent("""
    import json, os, sys, time
    sys.path.insert(0, {repo!r})
    os.environ["DJANGO_SETTINGS_MODULE"] = "nyayo.settings"
    import django
    django.setup()
    from nyayo.matches import ingest

    folder, name = {folder!r}, sys.argv[1]
    real, met = ingest.find_match, []

    def meet(match_id):
        found = real(match_id)
        if not met:  # both have looked, and found nothing, before either goes on to store
            met.append(1)
            open(os.path.join(folder, "looked." + name), "w").close()
            start = time.time()
            while not all(os.path.exists(os.path.join(folder, "looked." + n)) for n in "12"):
                if time.time() - start > 60:
                    raise SystemExit("the other never looked")
                time.sleep(0.01)
        return found

    ingest.find_match = meet
    document = json.load(open(os.path.join(folder, "document.json")))
    result = ingest.ingest(json.dumps(document).encode(), None, document["match"]["id"])
    print("created" if result.created else "updated")
""")


class TwoProcesses(unittest.TestCase):
    """The real thing: two processes on one SQLite file, made to look before either stores."""

    def test_two_concurrent_first_puts_give_one_match(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {**os.environ, "NYAYO_DEBUG": "1", "NYAYO_DATA_DIR": folder, "PYTHONIOENCODING": "utf-8"}
            migrate = subprocess.run([sys.executable, "manage.py", "migrate", "-v0"], cwd=REPO, env=env,
                                     capture_output=True, text=True)
            self.assertEqual(migrate.returncode, 0, migrate.stderr)
            Path(folder, "document.json").write_text(json.dumps(GAUNTLET))
            script = Path(folder, "child.py")
            script.write_text(CHILD.format(repo=str(REPO), folder=folder))
            children = [subprocess.Popen([sys.executable, str(script), name], cwd=REPO, env=env,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                        for name in "12"]
            outputs = [child.communicate(timeout=120) for child in children]
            for child, (out, err) in zip(children, outputs):
                self.assertEqual(child.returncode, 0, err[-2000:])
            self.assertEqual(sorted(out.strip() for out, _ in outputs), ["created", "updated"])

            db = sqlite3.connect(Path(folder, "nyayo.sqlite3"))
            counts = [db.execute(f"select count(*) from {table}").fetchone()[0]
                      for table in ("matches_match", "matches_matchresult", "matches_matchsnapshot")]
            db.close()
            self.assertEqual(counts, [1, 2, 1])
