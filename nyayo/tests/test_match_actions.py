"""A match's actions in the sidebar, Unapprove, and taking a match away: Remove (hidden, games kept) and Delete with games."""

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from nyayo.lab import document, modify_test
from nyayo.lab.models import LogEvent, Result, Test
from nyayo.matches.ingest import archive_path
from nyayo.matches.models import Batch, Game, Match, Tombstone, Upload

from .helpers import NyayoTestCase
from .lab_helpers import make_machine, make_test
from .test_games import GameTestCase, pgn
from .test_matches_ingest import gauntlet


def staff():
    return User.objects.create_user("boss", password="pw", is_staff=True, is_superuser=True)


def lab_match(**fields):
    """A lab test and its match, as the shell makes them."""
    test = make_test(**fields)
    document.report_test(test)
    return test, Match.objects.get(pk=test.match_id())


class Sidebar(NyayoTestCase):
    def setUp(self):
        super().setUp()
        self.test, self.match = lab_match(name="lmr-trim")
        self.url = "/matches/%s" % self.match.pk

    def page(self, url=None):
        return self.client.get(url or self.url).content.decode()

    def test_signed_out_there_is_no_group_and_nothing_acts(self):
        self.assertNotIn("Actions for this match", self.page())
        for action in ("stop", "unapprove"):
            self.assertEqual(self.client.post(reverse("test_action", args=[self.test.pk, action])).status_code, 302)
        self.assertEqual(self.client.get(reverse("match_remove"), {"id": self.match.pk}).status_code, 302)
        self.assertEqual(self.client.post(reverse("match_remove"), {"id": self.match.pk, "mode": "remove"}).status_code, 302)
        self.assertFalse(Match.objects.get(pk=self.match.pk).hidden)

    def test_not_staff_gets_a_403(self):
        User.objects.create_user("plain", password="pw")
        self.client.login(username="plain", password="pw")
        self.assertEqual(self.client.get(reverse("match_remove"), {"id": self.match.pk}).status_code, 403)
        self.assertNotIn("Actions for this match", self.page())

    def test_the_group_is_the_last_thing_in_the_nav_and_not_in_the_page(self):
        self.client.force_login(staff())
        page = self.page()
        nav = page.split("<nav>")[1].split("</nav>")[0]
        self.assertGreater(nav.index("Actions for this match"), nav.index("System"))
        self.assertGreater(nav.index("Actions for this match"), nav.index("Admin"))
        main = page.split("<main>")[1]
        self.assertNotIn('class="test-actions"', main)
        self.assertNotIn("Actions for this match", main)
        self.assertIn(reverse("test_action", args=[self.test.pk, "stop"]), nav)
        self.assertIn("Remove…", nav)
        self.assertIn("Delete…", nav)

    def test_the_group_is_on_a_game_page_too(self):
        self.client.force_login(staff())
        self.client.put("/api/matches/x", data=b"{}", content_type="application/json")
        from nyayo.matches import games
        games.store(self.match.pk, "1", pgn())
        page = self.page(self.url + "/games/1")
        self.assertIn("Actions for this match", page)

    def test_a_match_that_is_no_lab_test_still_offers_remove_and_delete_only(self):
        Match.objects.create(id="gauntlet-1", subject=self.match.subject, kind="gauntlet", state="running", producer={"name": "uwanja"})
        self.client.force_login(staff())
        nav = self.page("/matches/gauntlet-1").split("<nav>")[1]
        self.assertIn("Remove…", nav)
        self.assertNotIn("/tests/", nav)

    def test_what_is_offered_follows_the_state(self):
        self.client.force_login(staff())
        self.assertEqual(modify_test.offers(self.test), ["unapprove", "stop"])
        self.client.post(reverse("test_action", args=[self.test.pk, "stop"]))
        self.test.refresh_from_db()
        self.assertEqual(modify_test.offers(self.test), ["restart"])
        nav = self.page().split("<nav>")[1]
        self.assertIn("/restart", nav)
        self.assertNotIn("/stop", nav)
        self.assertEqual(Match.objects.get(pk=self.match.pk).state, "stopped")


class Unapprove(NyayoTestCase):
    def setUp(self):
        super().setUp()
        self.test, self.match = lab_match()
        self.client.force_login(staff())

    def act(self, action):
        return self.client.post(reverse("test_action", args=[self.test.pk, action]))

    def test_unapprove_puts_it_back_to_not_approved_and_approve_undoes_it(self):
        self.assertEqual(self.act("unapprove").status_code, 302)
        self.test.refresh_from_db()
        self.assertFalse(self.test.approved)
        self.assertTrue(LogEvent.objects.filter(test_id=self.test.id, summary="UNAPPROVE").exists())
        page = self.client.get("/matches/%s" % self.match.pk).content.decode().split("<nav>")[1]
        self.assertIn("Not approved", page)
        self.assertIn("/approve", page)
        self.assertNotIn("/unapprove", page)
        self.act("approve")
        self.test.refresh_from_db()
        self.assertTrue(self.test.approved)

    def test_a_test_with_games_cannot_be_unapproved(self):
        machine = make_machine("hunt")
        Result.objects.create(test=self.test, machine=machine, workloads=1, games=8)
        self.assertEqual(self.act("unapprove").status_code, 409)
        self.test.refresh_from_db()
        self.assertTrue(self.test.approved)
        nav = self.client.get("/matches/%s" % self.match.pk).content.decode().split("<nav>")[1]
        self.assertNotIn("/unapprove", nav)
        self.assertIn("/stop", nav)

    def test_a_stopped_test_cannot_be_unapproved(self):
        self.act("stop")
        self.assertEqual(self.act("unapprove").status_code, 409)

    def test_only_a_superuser_unapproves(self):
        User.objects.create_user("staff", password="pw", is_staff=True)
        self.client.logout()
        self.client.login(username="staff", password="pw")
        self.assertEqual(self.act("unapprove").status_code, 403)


class Remove(GameTestCase):
    def setUp(self):
        super().setUp()
        self.put_game(pgn("1. e4 e5 2. Nf3 Nc6", "1-0"))
        self.owner = staff()
        self.client.force_login(self.owner)
        self.match = Match.objects.get(pk="gauntlet-test-1")

    def remove(self, *ids, mode="remove", confirm=""):
        return self.client.post(reverse("match_remove"), {"id": list(ids), "mode": mode, "confirm": confirm})

    def test_the_confirm_page_says_what_will_go(self):
        page = self.client.get(reverse("match_remove"), {"id": self.match.pk, "mode": "delete"}).content.decode()
        for expected in ("Remove, keep the games", "Delete with the games", "comes back as a new match", "type <b>%s</b>" % (self.match.name or self.match.pk), "1 game file"):
            self.assertIn(expected, page)

    def test_remove_hides_it_everywhere_but_keeps_its_games(self):
        self.assertEqual(self.remove(self.match.pk).status_code, 302)
        self.assertTrue(Match.objects.get(pk=self.match.pk).hidden)
        listing = self.client.get("/matches").content.decode()
        self.assertNotIn(reverse("match", args=[self.match.pk]), listing)
        self.assertIn("Bring it back", self.client.get(reverse("matches_removed")).content.decode())
        page = self.client.get("/matches/%s" % self.match.pk)
        self.assertEqual(page.status_code, 404)
        self.assertIn("was removed", page.content.decode())
        self.assertEqual(Game.objects.filter(match=self.match).count(), 1)
        self.assertTrue(self.path().exists())
        game = self.client.get("/matches/%s/games/1" % self.match.pk)
        self.assertEqual(game.status_code, 200)
        self.assertIn("from a removed match", game.content.decode())
        self.assertEqual(self.client.get("/api/matches/%s/games/1" % self.match.pk).status_code, 200)

    def test_a_removed_match_is_not_on_the_dashboard_or_the_live_count(self):
        Match.objects.update(updated_at=timezone.now())
        self.assertIn("1 live", self.client.get("/matches").content.decode())
        self.remove(self.match.pk)
        self.assertNotIn("1 live", self.client.get("/matches").content.decode())

    def test_bring_it_back(self):
        self.remove(self.match.pk)
        self.assertEqual(self.client.post(reverse("match_restore"), {"id": self.match.pk}).status_code, 302)
        self.assertFalse(Match.objects.get(pk=self.match.pk).hidden)
        self.assertEqual(self.client.get("/matches/%s" % self.match.pk).status_code, 200)
        self.assertEqual(self.client.post(reverse("match_restore"), {"id": self.match.pk}).status_code, 404)

    def test_the_producer_goes_on_reporting_and_it_stays_removed(self):
        self.remove(self.match.pk)
        self.assertEqual(self.put_match(gauntlet()).status_code, 200)
        self.assertTrue(Match.objects.get(pk=self.match.pk).hidden)

    def test_delete_asks_for_the_name(self):
        wrong = self.remove(self.match.pk, mode="delete", confirm="no")
        self.assertEqual(wrong.status_code, 302)
        self.assertTrue(Match.objects.filter(pk=self.match.pk).exists())
        self.assertIn("mode=delete", wrong["Location"])

    def test_delete_with_games_leaves_nothing_but_a_tombstone(self):
        archive = archive_path(self.match.pk)
        self.assertTrue(archive.exists())
        name = self.match.name or self.match.pk
        self.remove(self.match.pk, mode="delete", confirm=name)
        self.assertFalse(Match.objects.filter(pk="gauntlet-test-1").exists())
        self.assertEqual(Game.objects.count(), 0)
        self.assertFalse(self.path().exists())
        self.assertFalse(archive.exists())
        self.assertFalse(archive.with_name("gauntlet-test-1.meta.json").exists())
        self.assertTrue(Tombstone.objects.filter(pk="gauntlet-test-1").exists())
        page = self.client.get("/matches/gauntlet-test-1")
        self.assertEqual(page.status_code, 404)
        self.assertIn("was deleted", page.content.decode())
        self.assertEqual(self.client.get("/matches/gauntlet-test-1/games/1").status_code, 404)

    def test_a_rebuild_does_not_bring_it_back(self):
        self.remove(self.match.pk, mode="delete", confirm=self.match.name or self.match.pk)
        self.assertFalse(list(self.archive.rglob("gauntlet-test-1*")))

    def test_the_producer_sending_it_again_makes_it_anew(self):
        self.remove(self.match.pk, mode="delete", confirm=self.match.name or self.match.pk)
        self.assertEqual(self.put_match(gauntlet()).status_code, 201)
        self.assertFalse(Tombstone.objects.exists())
        self.assertEqual(Game.objects.count(), 0)
        self.assertEqual(self.client.get("/matches/gauntlet-test-1").status_code, 200)

    def test_batches_and_uploads_go_with_a_delete_but_stay_with_a_remove(self):
        Batch.objects.create(match=self.match, number=1, games=3, state="sealed")
        Upload.objects.create(match=self.match, name="1.0", bytes=1, sha256="a" * 64, state="done")
        self.remove(self.match.pk)
        self.assertEqual((Batch.objects.count(), Upload.objects.count()), (1, 1))
        self.remove(self.match.pk, mode="delete", confirm=self.match.name or self.match.pk)
        self.assertEqual((Batch.objects.count(), Upload.objects.count()), (0, 0))

    def test_an_upload_still_being_read_makes_it_wait(self):
        Upload.objects.create(match=self.match, name="1.0", bytes=1, sha256="b" * 64, state="pending")
        response = self.remove(self.match.pk, mode="delete", confirm=self.match.name or self.match.pk, )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Match.objects.filter(pk=self.match.pk).exists())
        page = self.client.get(reverse("match_remove"), {"id": self.match.pk, "mode": "delete"}).content.decode()
        self.assertIn("still being processed", page)

    def test_several_at_once(self):
        other = Match.objects.create(id="gauntlet-2", subject=self.match.subject, kind="gauntlet", state="finished", producer={"name": "uwanja"})
        self.remove(self.match.pk, other.pk)
        self.assertEqual(Match.objects.filter(hidden=True).count(), 2)
        self.remove(self.match.pk, other.pk, mode="delete", confirm="2 matches")
        self.assertEqual(Match.objects.count(), 0)

    def test_the_list_has_boxes_only_for_someone_signed_in(self):
        page = self.client.get("/matches").content.decode()
        self.assertIn('name="id" value="gauntlet-test-1"', page)
        self.assertIn("Delete with games", page)
        self.client.logout()
        page = self.client.get("/matches").content.decode()
        self.assertNotIn('class="pick"', page)
        self.assertNotIn("Delete with games", page)


class DeleteALabTest(NyayoTestCase):
    def test_the_test_its_results_steps_and_events_go_and_its_id_is_not_reused(self):
        test, match = lab_match(name="doomed")
        machine = make_machine("hunt")
        result = Result.objects.create(test=test, machine=machine, workloads=1, games=0)
        self.client.force_login(staff())
        self.client.post(reverse("match_remove"), {"id": match.pk, "mode": "delete", "confirm": "doomed"})
        self.assertFalse(Test.objects.filter(pk=test.pk).exists())
        self.assertFalse(Result.objects.filter(test_id=test.pk).exists())
        self.assertFalse(LogEvent.objects.filter(test_id=test.pk).exists())
        self.assertFalse(Match.objects.filter(pk=match.pk).exists())
        again, _ = lab_match(name="next")
        self.assertGreater(again.pk, test.pk)

    def test_a_running_test_is_stopped_first_by_remove(self):
        test, match = lab_match(name="live")
        self.client.force_login(staff())
        self.client.post(reverse("match_remove"), {"id": match.pk, "mode": "remove"})
        test.refresh_from_db()
        self.assertTrue(test.finished)
        self.assertTrue(Match.objects.get(pk=match.pk).hidden)
        self.assertEqual(Match.objects.get(pk=match.pk).state, "stopped")
