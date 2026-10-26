"""Position datasets: the worker draws them through the reader, the API and the Exports page start and show them."""

import bz2
import hashlib
import json
import os
import sys

import zstandard
from django.test import override_settings

from nyayo.matches import catalogue, datasets, exports, reader, worker
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
    """The tests' phant-indexer: --draw as elephant's contract has it, from python-chess. NYAYO_TEST_DRAW_READER
    naming a real phant-indexer runs these tests against it instead."""
    real = os.environ.get("NYAYO_TEST_DRAW_READER")
    if real:
        return reader.PhantIndexer(real, reader.file_sha256(real))
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
        self.assertEqual(dataset.reader, self.reader.name)
        self.assertEqual(dataset.reader_sha256, reader.file_sha256(self.reader.path))
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
        self.assertEqual(self.client.delete(f"/api/exports/{dataset.pk}").status_code, 401)
        deleted = self.client.delete(f"/api/exports/{dataset.pk}", headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(deleted.status_code, 200)
        self.assertIsNotNone(deleted.json()["file_deleted_at"])
        self.assertFalse(datasets.file_path(dataset).exists())
        dataset.refresh_from_db()
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
        self.assertIn("New dataset", page)
        self.assertIn("No dataset yet.", page)
        self.assertIn('href="/exports" class="active"', page)
        self.client.post("/exports", {"action": "new"})
        dataset = Dataset.objects.get()
        self.assertEqual(dataset.requested_by, "page")
        page = self.client.get("/exports").content.decode()
        self.assertIn('<span class="badge stopped">queued</span>', page)
        self.assertIn("waiting for the worker", page)  # on top, as the one in progress
        self.assertIn('http-equiv="refresh"', page)
        self.draw()
        dataset.refresh_from_db()
        page = self.client.get("/exports").content.decode()
        self.assertIn('<span class="badge finished">done</span>', page)
        self.assertIn(f'href="/exports/{dataset.pk}/file">download</a>', page)
        self.assertNotIn('http-equiv="refresh"', page)
        self.assertNotIn(">Rebuild</button>", page)
        # its file deleted: the record stays, and the page offers a rebuild
        datasets.delete_file(dataset)
        page = self.client.get("/exports").content.decode()
        self.assertIn("deleted, needs rebuilding", page)
        self.assertIn(">Rebuild</button>", page)
        self.client.post("/exports", {"action": "rebuild", "id": dataset.pk})
        self.draw()
        dataset.refresh_from_db()
        self.assertEqual((dataset.state, dataset.rebuild_matched, dataset.file_deleted_at), ("done", True, None))
        self.assertIn("download</a>", self.client.get("/exports").content.decode())


    def test_each_batch_has_its_books_first_ply_and_the_map_is_kept(self):
        Match.objects.update(settings={**OPENBENCH, "book": "books/8moves_v3.epd"})
        with override_settings(NYAYO_BOOK_MIN_PLY={"8moves_v3": 4, "Endgames": 0}, NYAYO_MIN_PLY_DEFAULT=8):
            dataset = datasets.request()
        self.assertEqual(dataset.draw["min_ply"], {"books": {"8moves_v3": 4, "Endgames": 0}, "default": 8})
        self.draw()
        dataset.refresh_from_db()
        self.assertEqual((dataset.manifest[0]["book"], dataset.manifest[0]["min_ply"]), ("books/8moves_v3.epd", 4))
        # each game goes to the reader tagged with its book's first ply; --min-ply is the default
        self.assertIn("--min-ply 8", " ".join(datasets.draw_arguments(dataset.draw)))
        fed = datasets.batch_games(dataset.manifest[0]).decode()
        self.assertEqual(fed.count('[DrawMinPly "4"]\n[Event '), 2)
        self.assertEqual(dataset.stats["games"]["own_minimum"], 2)
        self.assertNotIn(b"DrawMinPly", catalogue.batch_bytes(Batch.objects.get()))  # the stored games untouched
        # the map kept with the dataset decides, not the settings of the day
        with override_settings(NYAYO_BOOK_MIN_PLY={}, NYAYO_MIN_PLY_DEFAULT=30):
            self.assertEqual(datasets.min_ply_for("8moves_v3.epd", dataset.draw), 4)
            self.assertEqual(datasets.min_ply_for("Endgames.EPD", dataset.draw), 0)
            self.assertEqual(datasets.min_ply_for("UHO_Lichess_4852_v1.epd", dataset.draw), 8)
            self.assertEqual(datasets.min_ply_for(None, dataset.draw), 8)
        # a dataset from before the map had one minimum, for every game
        self.assertEqual(datasets.min_ply_for("8moves_v3", {"min_ply": 16}), 16)
        self.assertIn("--min-ply 16", " ".join(datasets.draw_arguments({**dataset.draw, "min_ply": 16})))

    def test_merge_is_a_choice_kept_with_the_dataset(self):
        self.assertEqual(self.post({"merge": "yes"}).status_code, 400)
        response = self.post({"merge": True})
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["draw"]["merge"])
        dataset = Dataset.objects.get()
        self.assertIn("--merge", datasets.draw_arguments(dataset.draw))
        self.assertNotIn("--merge", datasets.draw_arguments(datasets.request().draw))
        self.client.post("/exports", {"action": "new", "merge": "on"})
        self.assertTrue(Dataset.objects.order_by("-pk").first().draw["merge"])
        self.draw()
        dataset.refresh_from_db()
        self.assertEqual(dataset.state, "done", dataset.error)
        self.assertIsNone(dataset.stats["positions"]["duplicates_dropped"])
        self.assertIsNotNone(dataset.stats["positions"]["drawn"])
        self.assertIn("10/game · merged", self.client.get("/exports").content.decode())
        self.assertEqual(datasets.settings_view({"per_game": 10, "min_ply": 16})["rows"][-1],
                         ("first ply, by book", "16 for every book"))

    def test_the_draws_statistics_are_kept_and_shown(self):
        dataset = datasets.request()
        self.draw()
        dataset.refresh_from_db()
        stats = dataset.stats
        self.assertEqual(stats["stats"], 1)
        self.assertEqual(stats["positions"]["written"], dataset.positions)
        self.assertEqual((stats["games"]["read"], stats["games"]["used"]), (2, 2))
        self.assertEqual(sum(stats["positions"]["results"].values()), dataset.positions)
        self.assertGreaterEqual(stats["positions"]["candidates"], stats["positions"]["quiet"])
        self.assertFalse(list(datasets.directory().glob("*.stats.json")))  # read into the record, then gone
        self.assertEqual(self.client.get(f"/api/exports/{dataset.pk}").json()["stats"], stats)
        view = datasets.stats_view(stats)
        self.assertEqual(view["written"], dataset.positions)
        self.assertTrue(view["quiet_share"].endswith("%"))
        self.assertEqual(len(view["wdl"]), 3)
        self.assertIn(("games read", 2), view["rows"])
        results = stats["positions"]["results"]
        counted = results["white"] + results["draw"] + results["black"]
        self.assertEqual([r["share"] for r in view["wdl"]],
                         [f"{100 * results[k] / counted:.1f}" for k in ("white", "draw", "black")])
        # the list: the settings and quiet share in a line, and a W/D/L bar, each share to one decimal on top
        page = self.client.get("/exports").content.decode()
        self.assertIn(f"10/game · {view['quiet_share']} quiet", page)
        white = view["wdl"][0]
        self.assertIn(f'<span class="pass" style="width: {white["share"]}%" title="white wins: {results["white"]}">'
                      f'{white["share"]}%</span>', page)
        self.assertIn(f'href="/exports/{dataset.pk}">Dataset {dataset.pk}</a>', page)
        self.assertNotIn("<details>", page)
        # its own page: tiles, the three rings, the quiet figures with what they are, every figure, the record
        page = self.client.get(f"/exports/{dataset.pk}").content.decode()
        self.assertIn(">Positions written<", page)
        self.assertIn('aria-label="Results: white wins', page)
        self.assertIn('aria-label="Games: used 2', page)
        self.assertIn('aria-label="Positions: written', page)
        self.assertIn('<dt title="half the games had fewer quiet positions than this, half more">median</dt>', page)
        self.assertIn('<td class="muted">positions written</td>', page)
        self.assertIn('<td class="muted">first ply, by book</td><td>8moves_v3 4, Endgames 0; any other book 8</td>',
                      page)
        self.assertIn(dataset.sha256, page)
        self.assertIn('href="/exports" class="active"', page)
        # the positions ring adds up to the candidates
        ring = exports.rings(view["found"])["positions"]
        self.assertEqual(ring["total"], stats["positions"]["candidates"])
        # a reader that writes none, or another shape: nothing shown, nothing broken
        self.assertIsNone(datasets.stats_view(None))
        self.assertEqual(datasets.stats_view({"other": 1})["rows"], [])
        self.assertEqual(self.client.get(f"/exports/{datasets.request().pk}").status_code, 200)
