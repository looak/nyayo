"""manage.py seed_demo: simulated SPRTs for polishing the pages."""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from nyayo.matches.models import Batch, Match

from .helpers import NyayoTestCase


class SeedDemo(NyayoTestCase):
    def seed(self):
        with override_settings(DEBUG=True):
            call_command("seed_demo", stdout=StringIO())

    def test_it_refuses_outside_debug(self):
        with override_settings(DEBUG=False), self.assertRaises(CommandError):
            call_command("seed_demo", stdout=StringIO())

    def test_the_scenarios_pages_games_and_batch(self):
        self.seed()
        # every scenario is there in its state
        states = {m.pk: (m.state, (m.sprt or {}).get("result")) for m in Match.objects.all()}
        self.assertEqual(states["demo-sprt-passed"], ("finished", "passed"))
        self.assertEqual(states["demo-sprt-failed"], ("finished", "failed"))
        self.assertEqual(states["demo-sprt-stopped"], ("stopped", None))
        self.assertEqual(states["demo-sprt-running"], ("running", None))
        # the finished ones end at a bound
        passed, failed = Match.objects.get(pk="demo-sprt-passed"), Match.objects.get(pk="demo-sprt-failed")
        self.assertGreaterEqual(passed.sprt["llr"], passed.sprt["upper"])
        self.assertLessEqual(failed.sprt["llr"], failed.sprt["lower"])
        # the pages draw a history
        running = self.client.get("/matches/demo-sprt-running").content.decode()
        self.assertIn('aria-label="Elo over games"', running)
        self.assertIn('aria-label="LLR over games"', running)
        self.assertIn("Games to go", running)
        self.assertNotIn("Games to go", self.client.get("/matches/demo-sprt-passed").content.decode())
        self.assertIn("too few", self.client.get("/matches/demo-sprt-new").content.decode())
        # one is silent and one is not
        self.assertContains(self.client.get("/matches/demo-sprt-silent"), "badge silent")
        self.assertNotContains(self.client.get("/matches/demo-sprt-running"), "badge silent")
        # games and a batch are there and the game page loads
        self.assertEqual(Batch.objects.get().games, 8)
        self.assertEqual(Batch.objects.get().state, "sealed")
        self.assertEqual(self.client.get("/matches/demo-sprt-passed/games/demo.0.3").status_code, 200)
        self.assertEqual(self.client.get("/matches/demo-sprt-running/games/7").status_code, 200)
        self.assertEqual(self.client.get("/matches/demo-sprt-running/games/1").status_code, 200)

    def test_the_gauntlets(self):
        self.seed()
        states = {m.pk: m.state for m in Match.objects.filter(kind="gauntlet")}
        self.assertEqual(states, {"demo-gauntlet-running": "running", "demo-gauntlet-finished": "finished",
                                  "demo-gauntlet-stopped": "stopped", "demo-gauntlet-silent": "running",
                                  "demo-gauntlet-new": "running"})
        # the counts are the finished games', and every game started has its PGN
        finished = Match.objects.get(pk="demo-gauntlet-finished")
        self.assertEqual(sum(r.games for r in finished.results.all()), 96)
        self.assertEqual(finished.games.exclude(result="*").count(), 96)
        running = Match.objects.get(pk="demo-gauntlet-running")
        self.assertEqual(running.games.filter(result="*").count(), 6)
        # the pages: boards in play, cut off games, a silent one, and the list's cards
        page = self.client.get("/matches/demo-gauntlet-running").content.decode()
        self.assertEqual(page.count('class="mini live-board"'), 6)
        self.assertIn(">Performance<", page)
        self.assertIn(">Cut off <", self.client.get("/matches/demo-gauntlet-stopped").content.decode())
        self.assertContains(self.client.get("/matches/demo-gauntlet-silent"), "badge silent")
        self.assertIn("mini live-board", self.client.get("/matches").content.decode())
        self.assertEqual(self.client.get("/matches/demo-gauntlet-running/games/41").status_code, 200)

    def test_seeding_again_starts_over(self):
        self.seed()
        self.seed()
        self.assertEqual(Match.objects.filter(pk__startswith="demo-").count(), 11)
        self.assertEqual(Batch.objects.count(), 1)
