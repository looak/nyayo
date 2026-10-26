"""manage.py export_training: a selection's games as stored, and a manifest that takes the same games again."""

import bz2
import json
import tempfile
from io import StringIO
from pathlib import Path

import zstandard
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from nyayo.matches import catalogue, selection, training
from nyayo.matches.models import Batch, Match, MatchResult

from .test_index import MATE, OPENBENCH, RESIGNED
from .test_uploads import BatchCase, batch_text, game

# a won game ended by mate (a rule), a lost one by resignation (inferred from the scores), one lost on time
THREE = batch_text(game("1-0", moves=MATE), game("0-1", moves=RESIGNED),
                   game("0-1", moves="1. e4 e5 2. Nf3", Termination="time forfeit"))


def unpacked(path):
    return zstandard.ZstdDecompressor().stream_reader(open(path, "rb")).read()


class ExportTraining(BatchCase):
    def setUp(self):
        super().setUp()
        Match.objects.update(settings={**OPENBENCH, "tc": "120+1", "book": "8moves_v3.pgn"})
        MatchResult.objects.update(timelosses={}, crashes={}, illegal={})
        self.out = Path(tempfile.mkdtemp()) / "eg"

    def keep_sealed(self, body=THREE, batch="1.0"):
        with override_settings(NYAYO_BATCH_GAMES=3):  # full at once, so sealed
            self.keep(bz2.compress(body), batch=batch)

    def export(self, *flags):
        call_command("export_training", "--out", str(self.out), *flags, stdout=StringIO())
        manifest = json.loads(Path(f"{self.out}.manifest.json").read_text(encoding="utf-8"))
        return manifest, unpacked(f"{self.out}.pgn.zst").decode()

    def test_the_good_endings_by_default_with_their_comments(self):
        self.keep_sealed()
        manifest, games = self.export()
        self.assertEqual(manifest["games"], 2)
        self.assertEqual(games.count("[Result "), 2)
        self.assertIn("Qxf7#", games)
        self.assertIn("{-4.10 10/12 100 1000}", games)  # the comments come along
        self.assertNotIn("time forfeit", games)
        batch = Batch.objects.get()
        self.assertEqual(manifest["batches"][0]["id"], batch.pk)
        self.assertEqual(manifest["batches"][0]["sha256"], batch.sha256)
        self.assertEqual([g["game"] for g in manifest["batches"][0]["games"]], ["1.0.1", "1.0.2"])
        self.assertEqual(manifest["filters"]["endings"], ["rule", "resignation", "draw_adjudication", "adjudication"])
        self.assertTrue(manifest["reader"].startswith("python-chess"))
        self.assertTrue(manifest["nyayo"])

    def test_the_games_read_again(self):
        # the export is PGN the reader reads, game for game, with each one's result
        self.keep_sealed()
        _, games = self.export()
        from nyayo.matches import reader
        records = reader.PythonChess().read(games.encode())
        self.assertEqual([(r["ok"], r["result"]) for r in records], [(True, "1-0"), (True, "0-1")])

    def test_endings_left_out_and_kept(self):
        self.keep_sealed()
        self.assertEqual(self.export("--exclude", "resignation")[0]["games"], 1)
        self.assertEqual(self.export("--endings", "time")[0]["games"], 1)
        self.assertEqual(self.export("--endings", "rule,resignation,time")[0]["games"], 3)
        with self.assertRaises(CommandError):
            self.export("--endings", "boredom")

    def test_the_manifest_takes_the_same_games_again(self):
        self.keep_sealed()
        manifest, games = self.export()
        # from the batches alone: the index can go
        catalogue.index().execute("DELETE FROM card")
        call_command("export_training", "--out", f"{self.out}-again", "--from-manifest",
                     f"{self.out}.manifest.json", stdout=StringIO())
        self.assertEqual(unpacked(f"{self.out}-again.pgn.zst").decode(), games)

    def test_a_batch_that_changed_is_refused(self):
        self.keep_sealed()
        self.export()
        path = catalogue.sealed_path(Batch.objects.get())
        path.write_bytes(path.read_bytes() + b"\0")
        with self.assertRaisesMessage(CommandError, "sha256 differs"):
            call_command("export_training", "--out", f"{self.out}-again", "--from-manifest",
                         f"{self.out}.manifest.json", stdout=StringIO())

    def test_games_in_an_open_batch_wait(self):
        self.keep(bz2.compress(THREE))  # 64 games to a batch: this one stays open
        manifest, games = self.export()
        self.assertEqual((manifest["games"], manifest["left_out_in_open_batches"]), (0, 2))
        self.assertEqual(games, "")

    def test_the_matches_filters(self):
        self.keep_sealed()
        self.assertEqual(self.export("--match", "gauntlet-test-1")[0]["games"], 2)
        self.assertEqual(self.export("--match", "elsewhere")[0]["games"], 0)
        self.assertEqual(self.export("--kind", "gauntlet", "--tc", "120+1", "--book", "8moves_v3.pgn")[0]["games"], 2)
        self.assertEqual(self.export("--tc", "8+0.08")[0]["games"], 0)
        self.assertEqual(self.export("--subject-version", "1.0.0-")[0]["games"], 2)  # the subject is Elephant 1.0.0
        self.assertEqual(self.export("--subject-version", "0.9-1.0.0")[0]["games"], 2)
        self.assertEqual(self.export("--subject-version", "1.1.0-")[0]["games"], 0)
        self.assertEqual(self.export("--openbench", "63")[0]["games"], 0)
        Match.objects.update(producer={"name": "OpenBench", "url": "http://172.16.100.11:8000/test/63/"})
        self.assertEqual(self.export("--openbench", "63")[0]["games"], 2)


class Versions(BatchCase):
    def test_bounds(self):
        self.assertEqual(selection.parse_versions("1.3.0-"), ((1, 3, 0), None))
        self.assertEqual(selection.parse_versions("1.2.0-1.3.0"), ((1, 2, 0), (1, 3, 0)))
        self.assertEqual(selection.parse_versions("1.3.0"), ((1, 3, 0), (1, 3, 0)))
        self.assertEqual(selection.version_key("v1.10.2"), (1, 10, 2))
        self.assertIsNone(selection.version_key("feature-x"))
        for bad in ("-", "x-", "1.0-y", ""):
            with self.assertRaises(selection.BadSelection):
                selection.parse_versions(bad)
