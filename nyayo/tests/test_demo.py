"""manage.py seed_demo: simulated SPRTs for polishing the pages."""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from nyayo.matches.models import GameBatch, Match

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
        self.assertIn("Heading for", running)
        self.assertNotIn("Heading for", self.client.get("/matches/demo-sprt-passed").content.decode())
        self.assertIn("too few", self.client.get("/matches/demo-sprt-new").content.decode())
        # one is silent and one is not
        self.assertContains(self.client.get("/matches/demo-sprt-silent"), "badge silent")
        self.assertNotContains(self.client.get("/matches/demo-sprt-running"), "badge silent")
        # games and a batch are there and the game page loads
        self.assertEqual(GameBatch.objects.get().games, 8)
        self.assertEqual(self.client.get("/matches/demo-sprt-running/games/7").status_code, 200)
        self.assertEqual(self.client.get("/matches/demo-sprt-running/games/1").status_code, 200)

    def test_seeding_again_starts_over(self):
        self.seed()
        self.seed()
        self.assertEqual(Match.objects.filter(pk__startswith="demo-").count(), 6)
        self.assertEqual(GameBatch.objects.count(), 1)
