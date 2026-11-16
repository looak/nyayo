"""A running tune's card on the dashboard and the lists, in place of the match card: no Elo or score of its pairs."""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from nyayo.lab import demo
from nyayo.lab.models import Test
from nyayo.matches import dashboard, pages
from nyayo.matches.models import Match


class TuneCard(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user("owner", password="pw", is_staff=True, is_superuser=True))
        demo.seed_tune(name="t-card", iterations=2000, now_iteration=1100)
        self.test = Test.objects.get(name="t-card")
        self.match = Match.objects.get(pk=self.test.match_id())
        Match.objects.filter(pk=self.match.pk).update(state="running", updated_at=timezone.now())

    def cards(self, **kw):
        now = timezone.now()
        return pages.running_cards(pages.silent_cutoff(now), now, **kw)

    def test_a_running_tune_has_a_tune_card_not_a_match_card(self):
        (card,) = self.cards()
        self.assertEqual(card["match"].pk, self.test.match_id())
        self.assertNotIn("elo", card)
        self.assertNotIn("score", card)
        tune = card["tune"]
        self.assertEqual((tune["iteration"], tune["iterations"], tune["label"]), (1100, 2000, "Tune %d" % self.test.id))
        self.assertEqual(tune["check"]["iteration"], 1000)

    def test_the_card_shows_progress_the_parameters_that_moved_most_and_the_check(self):
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("tune-card", page)
        self.assertIn("Tune %d" % self.test.id, page)
        self.assertIn("1,100", page)
        self.assertIn("iterations", page)
        self.assertIn("against the start", page)
        self.assertIn("then an SPRT", page) if self.test.spsa_run.then_sprt else None
        for row in self.cards()[0]["tune"]["parameters"]:
            self.assertIn(row["name"], page)
        self.assertNotIn("score-label muted\">score", page)
        self.assertNotIn("LLR", page)
        self.assertIn('href="/matches/%s"' % self.test.match_id(), page)

    def test_only_the_few_that_moved_most_are_listed(self):
        tune = self.cards()[0]["tune"]
        self.assertLessEqual(len(tune["parameters"]), 6)
        moved = [p["moved"] for p in tune["parameters"]]
        self.assertEqual(moved, sorted(moved, reverse=True))
        self.assertEqual(tune["parameter_count"], len(tune["parameters"]) + tune["more"])

    def test_the_tests_list_uses_the_card_too(self):
        page = self.client.get(reverse("tests")).content.decode()
        self.assertIn("tune-card", page)

    def test_a_tune_match_with_no_lab_test_keeps_the_match_card(self):
        Match.objects.create(pk="tune-999999", kind="games", state="running", name="elsewhere", subject=self.match.subject)
        ids = [c["match"].pk for c in self.cards() if "tune" not in c]
        self.assertEqual(ids, ["tune-999999"])

    def test_the_header_tile_and_recent_matches_call_it_a_tune_and_leave_its_checks_out(self):
        check_ids = [m for m in pages.hidden_checks()]
        self.assertTrue(check_ids)
        Match.objects.filter(pk__in=check_ids).update(state="running")
        playing = dashboard.playing()
        self.assertEqual(playing["kinds"], [("tune", 1)])
        recent = dashboard.recent_matches(pages.silent_cutoff(timezone.now()))
        self.assertEqual([m.pk for m in recent], [self.test.match_id()])
        self.assertEqual(recent[0].job_kind, "tune")
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("iteration 1,100/2,000", page)
        self.assertIn('<span class="tier">tune</span>', page)
