import threading
from unittest import mock

from django.conf import settings
from django.test import Client

from nyayo.matches import games
from nyayo.matches.models import Match

from .test_games import GameTestCase, pgn
from .test_matches_ingest import gauntlet


class GameThreads(GameTestCase):
    """A flood of game requests takes at most NYAYO_GAME_THREADS threads; the rest answer match reports and pages."""

    def setUp(self):
        super().setUp()
        self.entered = threading.Semaphore(0)
        self.release = threading.Event()
        self.answers = []
        store = games.store

        def slow_store(*args):
            if threading.current_thread() is threading.main_thread():
                return store(*args)
            self.entered.release()
            self.release.wait(10)
            return True

        # the held requests run on threads of their own, outside the test's transaction and its database, so they
        # are given the machine and store nothing
        for patcher in (mock.patch("nyayo.matches.views.machine_for", return_value=self.machine),
                        mock.patch("nyayo.matches.games.store", side_effect=slow_store)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.release.set)

    def hold_game_threads(self):
        """Game PUTs on other threads, as many as there are game slots, each held in the request until released."""
        threads = []
        for number in range(settings.NYAYO_GAME_THREADS):
            thread = threading.Thread(target=self.put_from_thread, args=(str(100 + number),))
            thread.start()
            threads.append(thread)
        for _ in threads:
            self.assertTrue(self.entered.acquire(timeout=10), "a game request never reached the store")
        return threads

    def put_from_thread(self, game):
        response = Client().put(f"/api/matches/gauntlet-test-1/games/{game}", data=pgn(),
                                content_type="application/x-chess-pgn",
                                headers={"Authorization": f"Bearer {self.token}"})
        self.answers.append(response.status_code)

    def finish(self, threads):
        self.release.set()
        for thread in threads:
            thread.join(10)
        self.assertEqual(self.answers, [201] * len(threads))

    def test_a_flood_of_games_leaves_the_match_report_answering(self):
        threads = self.hold_game_threads()

        busy = self.put_game(pgn(), game="1")
        self.assertEqual(busy.status_code, 503, busy.content)
        self.assertEqual(busy["Retry-After"], "1")
        self.assertFalse(self.path("1").exists())

        report = self.put_match(gauntlet())
        self.assertEqual(report.status_code, 200, report.content)
        self.assertEqual(self.client.get("/matches").status_code, 200)
        self.assertEqual(self.client.get("/api/matches/gauntlet-test-1/games/100").status_code, 404)

        self.finish(threads)
        self.assertEqual(self.put_game(pgn(), game="1").status_code, 201)

    def test_a_batch_waits_for_a_game_slot_too(self):
        threads = self.hold_game_threads()
        response = self.client.put("/api/matches/gauntlet-test-1/pgns/1", data=pgn(),
                                   content_type="application/x-chess-pgn",
                                   headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(response.status_code, 503, response.content)
        self.finish(threads)

    def test_a_refused_game_gives_its_slot_back(self):
        for _ in range(settings.NYAYO_GAME_THREADS + 1):
            self.assertEqual(self.put_game(pgn("1. e4 e5 2. Ke3")).status_code, 400)
        self.assertEqual(self.put_game(pgn()).status_code, 201)
        self.assertTrue(Match.objects.exists())
