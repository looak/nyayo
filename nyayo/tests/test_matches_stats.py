"""The producer's own `stats`."""

from nyayo.matches.models import Match, MatchSnapshot

from .test_matches_ingest import ID, MatchTestCase, v1


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
        self.assertIn("OpenBench's figure, 95% interval", page)
        self.assertIn("+21.7", page)
        self.assertIn("114.47", page)

    def test_the_list_shows_them_in_place_of_the_calculation(self):
        self.put_match(v1())
        self.assertContains(self.client.get("/matches"), "+21.7")
