"""Position datasets: the worker draws them through the reader, the API and the Exports page start and show them."""

import bz2
import hashlib
import json
import sys

import zstandard
from django.test import override_settings

from nyayo.matches import catalogue, datasets, reader, worker
from nyayo.matches.models import Batch, Dataset, Match, MatchResult, Upload

from .test_index import MATE, OPENBENCH, RESIGNED
from .test_reader import FAKE
from .test_uploads import BatchCase, batch_text, game

# a long won game adjudicated, a lost one by resignation (inferred), one lost on time: two good labels
LONG = ("1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 6. Re1 b5 7. Bb3 d6 8. c3 O-O 9. h3 Nb8 10. d4 Nbd7 "
        "11. Nbd2 Bb7 12. Bc2 Re8")
THREE = batch_text(game("1-0", moves=LONG, Termination="adjudication"), game("0-1", moves=RESIGNED),
                   game("0-1", moves="1. e4 e5 2. Nf3", Termination="time forfeit"))


def stand_in():
    """The tests' phant-indexer: --draw as elephant's contract has it, from python-chess."""
    return reader.PhantIndexer(FAKE, reader.file_sha256(FAKE), launcher=[sys.executable])


class Datasets(BatchCase):
    def setUp(self):
        super().setUp()
        Match.objects.update(settings={**OPENBENCH, "tc": "120+1"})
        MatchResult.objects.update(timelosses={}, crashes={}, illegal={})
        settings = override_settings(NYAYO_DATASETS_DIR=self.data / "datasets")
        settings.enable()
        self.addCleanup(settings.disable)
        self.addCleanup(self.forget_run)
        self.reader = stand_in()
        with override_settings(NYAYO_BATCH_GAMES=3):  # full at once, so sealed
            self.keep(bz2.compress(THREE))

    def forget_run(self):
        if datasets._run is not None:
            datasets._run.stop()
            datasets._run = None

    def post(self, body=None, token=None):
        return self.client.post("/api/exports", data=json.dumps(body or {}), content_type="application/json",
                                headers={"Authorization": f"Bearer {token or self.token}"})

    def draw(self):
        worker.run(once=True, reader=self.reader)

    def lines(self, dataset):
        return zstandard.ZstdDecompressor().stream_reader(open(datasets.file_path(dataset), "rb")).read()

    def test_requested_by_the_api_and_drawn_by_the_worker(self):
        response = self.post({"note": "king table"})
        self.assertEqual(response.status_code, 201)
        record = response.json()
        self.assertEqual((record["state"], record["note"], record["requested_by"]), ("requested", "king table",
                                                                                     self.machine.name))
        self.assertEqual(record["draw"]["per_game"], 10)
        self.draw()
        dataset = Dataset.objects.get()
        self.assertEqual(dataset.state, "done", dataset.error)
        self.assertEqual((dataset.batches_done, dataset.batches_total, dataset.games), (1, 1, 2))
        data = self.lines(dataset)
        lines = data.decode().splitlines()
        self.assertEqual(dataset.positions, len(lines))
        self.assertGreater(len(lines), 5)
        self.assertTrue(all(line.endswith((" [1.0]", " [0.0]", " [0.5]")) for line in lines))
        self.assertEqual(dataset.sha256, hashlib.sha256(data).hexdigest())
        # the record: versions, the reader's sha256, and the manifest with each batch's
        batch = Batch.objects.get()
        self.assertEqual(dataset.reader, "phant-indexer 9.9.9")
        self.assertEqual(dataset.reader_sha256, reader.file_sha256(FAKE))
        self.assertTrue(dataset.nyayo_version)
        self.assertEqual(dataset.manifest[0]["sha256"], batch.sha256)
        self.assertEqual([g[0] for g in dataset.manifest[0]["games"]], [1, 2])  # the time loss is left out
        self.assertEqual(dataset.selection["endings"], ["rule", "resignation", "draw_adjudication", "adjudication"])
        # the API: the list, one with its manifest and its file
        listed = self.client.get("/api/exports").json()["exports"]
        self.assertEqual([d["id"] for d in listed], [dataset.pk])
        one = self.client.get(f"/api/exports/{dataset.pk}").json()
        self.assertEqual(one["progress"], {"batches": 1, "of": 1, "games": 2, "positions": len(lines)})
        self.assertEqual(one["manifest"][0]["id"], batch.pk)
        self.assertTrue(one["file"].endswith(f"/exports/{dataset.pk}/file"))
        self.assertEqual(b"".join(self.client.get(f"/exports/{dataset.pk}/file").streaming_content),
                         datasets.file_path(dataset).read_bytes())

    def test_uploads_go_first(self):
        dataset = datasets.request()
        worker.step(self.reader)  # begins: the manifest, the reader's process
        self.put_batch(bz2.compress(batch_text(game("1-0", moves=MATE))), batch="2.0")
        worker.step(self.reader)  # the upload, not the dataset
        dataset.refresh_from_db()
        self.assertEqual((dataset.state, dataset.batches_done), ("running", 0))
        self.assertEqual(Upload.objects.get(name="2.0").state, "done")
        self.draw()
        dataset.refresh_from_db()
        self.assertEqual(dataset.state, "done")

    def test_a_rebuild_gives_the_same_positions(self):
        dataset = datasets.request()
        self.draw()
        dataset.refresh_from_db()
        first = dataset.sha256
        datasets.delete_file(dataset)
        self.assertFalse(datasets.file_path(dataset).exists())
        self.assertIsNone(self.client.get(f"/api/exports/{dataset.pk}").json()["file"])
        self.assertEqual(self.client.get(f"/exports/{dataset.pk}/file").status_code, 404)
        # from the record alone: the index can go
        catalogue.index().execute("DELETE FROM card")
        datasets.rebuild(dataset)
        self.draw()
        dataset.refresh_from_db()
        self.assertEqual((dataset.state, dataset.sha256, dataset.rebuild_matched), ("done", first, True))
        self.assertIsNone(dataset.file_deleted_at)

    def test_a_batch_that_changed_fails_the_rebuild(self):
        dataset = datasets.request()
        self.draw()
        dataset.refresh_from_db()
        path = catalogue.sealed_path(Batch.objects.get())
        path.write_bytes(path.read_bytes() + b"\0")
        datasets.rebuild(dataset)
        self.draw()
        dataset.refresh_from_db()
        self.assertEqual(dataset.state, "failed")
        self.assertIn("sha256 differs", dataset.error)

    def test_python_chess_cannot_draw(self):
        dataset = datasets.request()
        worker.run(once=True, reader=reader.PythonChess())
        dataset.refresh_from_db()
        self.assertEqual(dataset.state, "failed")
        self.assertIn("needs phant-indexer", dataset.error)

    def test_a_dataset_left_running_starts_over(self):
        dataset = datasets.request()
        worker.step(self.reader)
        self.forget_run()  # the worker stopped
        dataset.refresh_from_db()
        self.assertEqual(dataset.state, "running")
        self.draw()
        dataset.refresh_from_db()
        self.assertEqual((dataset.state, dataset.games), ("done", 2))

    def test_the_api_wants_a_token_to_start_one(self):
        self.assertEqual(self.post(token="wrong").status_code, 401)
        self.assertEqual(self.client.post("/api/exports").status_code, 401)
        self.assertEqual(Dataset.objects.count(), 0)
        self.assertEqual(self.post().status_code, 201)

    def test_the_exports_page(self):
        page = self.client.get("/exports").content.decode()
        self.assertIn("Draw a dataset", page)
        self.assertIn('href="/exports" class="active"', page)
        self.client.post("/exports", {"action": "request", "note": "for 1.4"})
        dataset = Dataset.objects.get()
        self.assertEqual((dataset.requested_by, dataset.note), ("page", "for 1.4"))
        self.assertIn('http-equiv="refresh"', self.client.get("/exports").content.decode())
        self.draw()
        page = self.client.get("/exports").content.decode()
        self.assertIn("Download positions", page)
        self.assertIn("--draw 10 --quiet --min-ply 16 --seed", page)
        self.client.post("/exports", {"action": "note", "id": dataset.pk, "note": "tuned 1.4's king table"})
        self.client.post("/exports", {"action": "delete", "id": dataset.pk})
        dataset.refresh_from_db()
        self.assertEqual(dataset.note, "tuned 1.4's king table")
        self.assertIsNotNone(dataset.file_deleted_at)
        self.client.post("/exports", {"action": "rebuild", "id": dataset.pk})
        self.draw()
        self.assertIn("the same positions", self.client.get("/exports").content.decode())
