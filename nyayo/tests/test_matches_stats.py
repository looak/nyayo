"""The producer's own `stats`, and the move of a match stored in OpenBench's old shape to version 1."""

import copy

from nyayo.matches import ingest
from nyayo.matches.models import Game, Match, MatchResult, MatchSnapshot

from .test_matches_ingest import OPENBENCH_SPRT, MatchTestCase

ID = "openbench-openbench-123"


def v1(**changes):
    """OpenBench's test as match schema v1 (docs/match-schema.md, OpenBench)."""
    document = {
        "schema_version": 1,
        "match": {
            "id": ID, "kind": "sprt", "state": "running", "name": "feature-x vs main",
            "producer": {"name": "OpenBench", "version": "openbench", "url": "http://172.16.100.11:8000/test/123/"},
            "started_at": "2026-10-16T10:00:00Z", "updated_at": "2026-10-16T12:34:56Z", "finished_at": None,
            "host": {"name": "openbench"},
            "settings": {"tc": "8+0.08", "book": "book", "runner": {"name": "cutechess-ob"}, "pgn_options": [],
                         "openbench": {}},
        },
        "subject": {"name": "Elephant", "branch": "feature-x", "commit": "abc123", "bench": 2000,
                    "options": {"Threads": "1", "Hash": "64"}},
        "opponents": [{"id": "base", "name": "Elephant", "branch": "main", "commit": "def456", "bench": 1800,
                       "options": {"Threads": "1"}}],
        "results": [{"opponent": "base", "games": 20, "wins": 10, "draws": 7, "losses": 3, "penta": [0, 1, 6, 2, 1],
                     "timelosses": {"subject": 0, "opponent": 0}, "crashes": {"subject": 1, "opponent": 0},
                     "illegal": {"subject": 0, "opponent": 0}}],
        "sprt": {"elo0": 0.0, "elo1": 5.0, "alpha": 0.05, "beta": 0.05, "model": "penta", "llr": 0.55,
                 "lower": -2.94, "upper": 2.94, "result": None},
        "stats": {"elo": 21.74, "error": 114.47, "model": "penta"},
        "machines": [{"name": "hunt", "games": 20}],
    }
    for path, value in changes.items():
        target = document
        *parents, last = path.split("__")
        for part in parents:
            target = target[part]
        target[last] = value
    return document


class Stats(MatchTestCase):
    def test_the_producers_figures_are_kept_and_may_change(self):
        self.assertEqual(self.put_match(v1()).status_code, 201)
        self.assertEqual(Match.objects.get().stats, {"elo": 21.74, "error": 114.47, "model": "penta"})
        later = v1(stats={"elo": 18.0, "error": 90.0, "model": "penta"})
        later["match"]["updated_at"] = "2026-10-16T12:44:56Z"
        later["results"][0].update(games=21, wins=11)
        self.assertEqual(self.put_match(later).status_code, 200)
        self.assertEqual(Match.objects.get().stats["elo"], 18.0)

    def test_a_match_may_carry_none(self):
        self.assertEqual(self.put_match(v1(stats=None)).status_code, 201)
        self.assertIsNone(Match.objects.get().stats)

    def test_a_snapshot_keeps_them_next_to_the_llr(self):
        self.put_match(v1())
        snapshot = MatchSnapshot.objects.get()
        self.assertEqual((snapshot.llr, snapshot.stats["elo"]), (0.55, 21.74))

    def test_what_is_refused(self):
        self.assertRefused(self.put_match(v1(stats={"elo": 1, "error": 2, "model": "gsprt"})), 400, "stats.model")
        self.assertRefused(self.put_match(v1(stats={"elo": "high", "error": 2, "model": "tri"})), 400, "stats.elo")
        self.assertRefused(self.put_match(v1(stats=[1, 2])), 400, "stats must be an object")

    def test_the_sprt_card_labels_them_as_the_producers(self):
        self.put_match(v1())
        page = self.client.get(f"/matches/{ID}").content.decode()
        self.assertIn("OpenBench's, penta", page)
        self.assertIn("+21.7", page)
        self.assertIn("± 114.5", page)

    def test_the_list_shows_them_in_place_of_the_calculation(self):
        self.put_match(v1())
        self.assertContains(self.client.get("/matches"), "+21.7")


class MovingToV1(MatchTestCase):
    def setUp(self):
        super().setUp()
        self.old = copy.deepcopy(OPENBENCH_SPRT)
        self.old["id"] = ID
        self.assertEqual(self.put_match(self.old, match_id=ID).status_code, 201)

    def new(self, **changes):
        document = v1(**changes)
        document["results"][0].update(games=18, wins=9, draws=6, losses=3, penta=[12, 8, 5, 4, 3],
                                 timelosses={"subject": 2, "opponent": 0})
        return document

    def test_the_first_v1_document_replaces_the_fixed_part(self):
        old = Match.objects.get()
        self.assertEqual(old.results.get().opponent, "Base-Engine")
        self.assertEqual(self.put_match(self.new()).status_code, 200)
        match = Match.objects.get()
        self.assertEqual((match.name, match.settings["tc"], match.subject.name), ("feature-x vs main", "8+0.08",
                                                                                   "Elephant"))
        self.assertEqual(match.subject_info["branch"], "feature-x")
        self.assertEqual(match.sprt["elo1"], 5.0)
        row = match.results.get()
        self.assertEqual((row.opponent, row.info["branch"], row.games), ("base", "main", 18))

    def test_the_opponents_old_id_is_carried_to_snapshots_and_games(self):
        old_id = MatchResult.objects.get().opponent
        Game.objects.create(match=Match.objects.get(), game="1", opponent=old_id, result="1-0")
        self.assertIn(old_id, MatchSnapshot.objects.get().counts)
        document = self.new()
        document["results"][0].update(games=20, wins=10, draws=7, losses=3)
        self.put_match(document)
        self.assertEqual(Game.objects.get().opponent, "base")
        self.assertEqual([set(s.counts) for s in MatchSnapshot.objects.all()], [{"base"}, {"base"}])

    def test_it_happens_once(self):
        self.put_match(self.new())
        later = self.new(**{"subject__branch": "feature-y"})
        later["match"]["updated_at"] = "2026-10-16T13:00:00Z"
        self.assertRefused(self.put_match(later), 409, "subject")

    def test_counts_still_only_grow_across_the_move(self):
        shrunk = self.new()
        shrunk["results"][0].update(games=10, wins=5, draws=3, losses=2)
        self.assertRefused(self.put_match(shrunk), 409, "counts only grow")
        self.assertEqual(MatchResult.objects.get().opponent, "Base-Engine")

    def test_a_different_number_of_opponents_is_refused(self):
        document = self.new()
        document["opponents"].append({"id": "second", "name": "Other"})
        self.assertRefused(self.put_match(document), 409, "opponents changed")

    def test_the_old_shape_alone_is_still_checked_as_before(self):
        changed = copy.deepcopy(self.old)
        changed["subject"]["sha"] = "different"
        changed["updated_at"] = "2026-10-16T13:00:00Z"
        self.assertRefused(self.put_match(changed, match_id=ID), 409, "changed")


class Mapping(MatchTestCase):
    def test_opponent_map_is_by_position(self):
        self.put_match(v1())
        match = Match.objects.get()
        self.assertEqual(ingest.opponent_map(match, v1()), {"base": "base"})
