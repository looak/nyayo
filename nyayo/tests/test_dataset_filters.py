"""Position datasets limited to players and a date: the selection's filters, the API's checks, a rebuild, the pages."""

import bz2
import datetime

from django.test import override_settings

from nyayo.matches import catalogue, datasets, selection
from nyayo.matches.models import Dataset, Match, MatchResult

from .test_datasets import Datasets, stand_in
from .test_index import MATE, OPENBENCH, RESIGNED
from .test_uploads import BatchCase, batch_text, game

UTC = datetime.timezone.utc
FOUR = batch_text(
    # Elephant 1.4.0 white against Stash, begun 5 October
    game("1-0", moves=MATE, White="Elephant 1.4.0", Black="Stash 36", GameStartTime="2026-10-05T10:00:00 +0000",
         GameEndTime="2026-10-05T10:05:00 +0000"),
    # Stash white against Elephant 1.4.0, with only a finish, 20 October
    game("0-1", moves=RESIGNED, GameEndTime="2026-10-20T10:00:00 +0000", White="Stash 36", Black="Elephant 1.4.0"),
    # Elephant 1.3.0 against Barbarossa, begun on 30 September and finished on 2 October: the start counts
    game("1-0", moves=MATE, White="Elephant 1.3.0", Black="Barbarossa 0.6.0",
         GameStartTime="2026-09-30T23:00:00 +0000", GameEndTime="2026-10-02T10:00:00 +0000"),
    # Stash against Barbarossa, no time at all: dated by its match
    game("1-0", moves=MATE, White="Stash 36", Black="Barbarossa 0.6.0"))


class Filters(BatchCase):
    post, draw, forget_run = Datasets.post, Datasets.draw, Datasets.forget_run

    def setUp(self):
        super().setUp()
        Match.objects.update(settings={**OPENBENCH, "tc": "120+1"},
                             started_at=datetime.datetime(2026, 10, 10, tzinfo=UTC))
        MatchResult.objects.update(timelosses={}, crashes={}, illegal={})
        settings = override_settings(NYAYO_DATASETS_DIR=self.data / "datasets")
        settings.enable()
        self.addCleanup(settings.disable)
        self.addCleanup(self.forget_run)
        self.reader = stand_in()
        with override_settings(NYAYO_BATCH_GAMES=4):
            self.keep(bz2.compress(FOUR))

    def taken(self, **filters):
        """The games' places a dataset with these filters would take."""
        chosen = selection.Selection(endings=selection.endings_kept(), **filters)
        return [place for entry in datasets.manifest_for(chosen, {"min_ply": 4})
                for place, _, _ in entry["games"]]

    def test_the_cards_hold_names_and_start(self):
        rows = catalogue.index().execute(
            "SELECT white_name, black_name, begun_at FROM card ORDER BY position").fetchall()
        self.assertEqual(rows[0][:2], ("Elephant 1.4.0", "Stash 36"))
        self.assertEqual(rows[2][2], int(datetime.datetime(2026, 9, 30, 23, tzinfo=UTC).timestamp()))
        self.assertIsNone(rows[3][2])

    def test_neither_filter_takes_every_game(self):
        self.assertEqual(self.taken(), [1, 2, 3, 4])

    def test_players_take_a_game_when_either_side_is_picked(self):
        self.assertEqual(self.taken(players=["Elephant 1.4.0"]), [1, 2])
        self.assertEqual(self.taken(players=["Stash 36"]), [1, 2, 4])
        self.assertEqual(self.taken(players=["Elephant 1.3.0", "Elephant 1.4.0"]), [1, 2, 3])
        self.assertEqual(self.taken(players=["Nobody"]), [])

    def test_a_date_uses_the_start_then_the_finish_then_the_match(self):
        day = datetime.date
        self.assertEqual(self.taken(since=day(2026, 10, 5)), [1, 2, 4])  # 3 began on 30 September
        self.assertEqual(self.taken(since=day(2026, 10, 6)), [2, 4])  # 4 by its match, begun 10 October
        self.assertEqual(self.taken(since=day(2026, 10, 11)), [2])
        self.assertEqual(self.taken(since=day(2026, 9, 30)), [1, 2, 3, 4])
        self.assertEqual(self.taken(since=day(2026, 10, 21)), [])

    def test_both_together(self):
        self.assertEqual(self.taken(players=["Stash 36"], since=datetime.date(2026, 10, 6)), [2, 4])
        self.assertEqual(self.taken(players=["Elephant 1.3.0"], since=datetime.date(2026, 10, 6)), [])

    def test_the_players_with_their_games(self):
        found = selection.players()
        self.assertEqual(found[0][:2], ("Stash 36", 3))
        self.assertEqual({name: games for name, games, _ in found},
                         {"Stash 36": 3, "Barbarossa 0.6.0": 2, "Elephant 1.4.0": 2, "Elephant 1.3.0": 1})

    def test_a_date_is_parsed_or_refused(self):
        self.assertIsNone(selection.parse_since(" "))
        self.assertEqual(selection.parse_since("2026-10-01"), datetime.date(2026, 10, 1))
        for bad in ("2026-13-01", "yesterday", "2026-1-1", "01/10/2026"):
            with self.assertRaises(selection.BadSelection):
                selection.parse_since(bad)

    def test_the_api_validates(self):
        for body in ({"players": ["Nobody 1.0"]}, {"from": "soon"}, {"from": "2026-02-30"}, {"players": "Stash 36"},
                     {"players": [3]}, {"from": 5}):
            response = self.post(body)
            self.assertEqual(response.status_code, 400, body)
            self.assertTrue(response.content.strip(), body)
        self.assertIn("Nobody 1.0", self.post({"players": ["Nobody 1.0"]}).content.decode())
        self.assertIn("2026-02-30", self.post({"from": "2026-02-30"}).content.decode())
        self.assertEqual(Dataset.objects.count(), 0)

    def test_the_api_keeps_the_filters_in_the_record(self):
        response = self.post({"players": ["Stash 36", "Stash 36"], "from": "2026-10-06"})
        self.assertEqual(response.status_code, 201)
        record = response.json()
        self.assertEqual((record["selection"]["players"], record["selection"]["since"]),
                         (["Stash 36"], "2026-10-06"))
        self.assertIsNone(self.post({"from": ""}).json()["selection"]["since"])
        self.assertEqual(self.post({}).json()["selection"]["players"], [])

    def test_a_dataset_with_filters_draws_and_rebuilds_the_same(self):
        dataset = datasets.request(players=["Stash 36"], since=datetime.date(2026, 10, 6))
        self.draw()
        dataset.refresh_from_db()
        self.assertEqual((dataset.state, dataset.games), ("done", 2), dataset.error)
        self.assertEqual([g[0] for g in dataset.manifest[0]["games"]], [2, 4])
        first = dataset.sha256
        datasets.rebuild(dataset)
        self.draw()
        dataset.refresh_from_db()
        self.assertEqual((dataset.state, dataset.sha256, dataset.rebuild_matched), ("done", first, True))
        self.assertEqual(dataset.selection["players"], ["Stash 36"])
        self.assertEqual(datasets.selection_of(dataset).label(), "Stash 36 · from 2026-10-06")

    def test_the_pages_show_them(self):
        page = self.client.get("/exports").content.decode()
        self.assertIn('name="player" value="Stash 36"', page)
        self.assertIn("3 games", page)
        self.assertIn('name="from"', page)
        posted = self.client.post("/exports", {"action": "new", "player": ["Elephant 1.4.0", "Stash 36"],
                                               "from": "2026-10-01"})
        self.assertEqual(posted.status_code, 302)
        dataset = Dataset.objects.get()
        self.assertEqual(dataset.selection["players"], ["Elephant 1.4.0", "Stash 36"])
        self.assertIn("Elephant 1.4.0, Stash 36 · from 2026-10-01", self.client.get("/exports").content.decode())
        self.assertIn("Elephant 1.4.0, Stash 36 · from 2026-10-01",
                      self.client.get(f"/exports/{dataset.pk}").content.decode())
        self.assertEqual(self.client.post("/exports", {"action": "new", "player": ["Nobody"]}).status_code, 400)
        self.assertEqual(self.client.post("/exports", {"action": "new", "from": "x"}).status_code, 400)
        self.assertEqual(Dataset.objects.count(), 1)

    def test_the_games_page_takes_the_same_filters(self):
        def count(query):
            response = self.client.get("/games" + query)
            self.assertEqual(response.status_code, 200)
            return response.context["total"]

        self.assertEqual(count(""), 4)
        self.assertEqual(count("?player=Stash+36"), 3)
        self.assertEqual(count("?player=Stash+36&player=Elephant+1.3.0"), 4)
        self.assertEqual(count("?from=2026-10-06"), 2)
        self.assertEqual(count("?player=Stash+36&from=2026-10-06"), 2)
        self.assertEqual(count("?player=Elephant+1.3.0&from=2026-10-06"), 0)
        self.assertEqual(self.client.get("/games?from=soon").status_code, 400)
