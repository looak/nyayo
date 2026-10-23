"""The game reader behind its interface: python-chess, or a pinned phant-indexer."""

import bz2
import os
import sys
import unittest
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from nyayo.matches import reader
from nyayo.matches.models import Batch, Upload

from .test_uploads import TWO, BatchCase, batch_text, game

FAKE = Path(__file__).with_name("fake_reader.py")


def fake(sha256=None):
    return reader.PhantIndexer(FAKE, sha256 or reader.file_sha256(FAKE), launcher=[sys.executable])


class Pinned(BatchCase):
    def test_the_pinned_binary_reads_and_names_itself(self):
        found = fake()
        self.assertEqual((found.name, found.format), ("phant-indexer 9.9.9", 2))
        records = found.read(TWO)
        self.assertEqual([(r["game"], r["ok"], r["plies"]) for r in records], [(1, True, 4), (2, True, 4)])

    def test_a_binary_that_isnt_the_pinned_one_is_refused(self):
        with self.assertRaises(reader.WrongReader):
            fake("0" * 64)
        with self.assertRaises(reader.WrongReader):
            reader.PhantIndexer(FAKE, "", launcher=[sys.executable])

    def test_the_worker_doesnt_start_on_a_mismatch(self):
        with override_settings(NYAYO_READER=str(FAKE), NYAYO_READER_SHA256="0" * 64):
            with self.assertRaises(CommandError) as raised:
                call_command("worker", "--once")
        self.assertIn("not starting", str(raised.exception))

    def test_without_one_it_is_python_chess(self):
        self.assertIsInstance(reader.default(), reader.PythonChess)

    def test_the_batch_records_the_reader(self):
        self.put_batch(bz2.compress(TWO))
        from nyayo.matches import worker

        worker.run(once=True, reader=fake())
        self.assertEqual(Batch.objects.get().reader, "phant-indexer 9.9.9")
        self.assertEqual(Upload.objects.get().reader, "phant-indexer 9.9.9")

    def test_a_reader_that_fails_refuses_the_upload_with_its_words(self):
        from nyayo.matches import worker

        self.put_batch(b"FAIL", content_type="application/x-chess-pgn")
        worker.run(once=True, reader=fake())
        upload = Upload.objects.get()
        self.assertEqual(upload.state, "refused")
        self.assertIn("can't decompress", upload.reason)


@unittest.skipUnless(os.environ.get("NYAYO_TEST_READER"), "set NYAYO_TEST_READER to a phant-indexer binary")
class TheRealOne(BatchCase):
    """Against a real phant-indexer: it and python-chess agree on what nyayo uses."""

    def test_it_agrees_with_python_chess(self):
        path = os.environ["NYAYO_TEST_READER"]
        real = reader.PhantIndexer(path, reader.file_sha256(path))
        text = batch_text(game("1-0"), game("0-1", Subject="black"), game("1-0", moves="1. e4 e5 2. Ke3"),
                          game("1/2-1/2", moves="1. e4 {+0.31/12 0.1s} e5 {+0.1 3/4 5 6} 2. Nf3"))
        mine, theirs = reader.PythonChess().read(text), real.read(text)
        keys = ("game", "offset", "length", "ok", "result", "plies", "ended", "start", "opening", "promotions",
                "castled", "captures", "ep_captures", "checks", "min_pieces", "material")
        self.assertEqual([{k: r.get(k) for k in keys} for r in mine], [{k: r.get(k) for k in keys} for r in theirs])
        self.assertEqual([r.get("error", {}).get("ply") for r in mine], [r.get("error", {}).get("ply") for r in theirs])
        # python-chess fills in the seven tag roster ("?" for what a PGN lacks); phant-indexer keeps what is written
        self.assertEqual([{k: v for k, v in a["tags"].items() if k in b["tags"]} for a, b in zip(mine, theirs)],
                         [r["tags"] for r in theirs])
