"""The frame around every page: the sidebar's status block and its links."""

import bz2
from unittest import mock

from django.contrib.staticfiles import finders
from django.test import override_settings
from django.utils import timezone

from nyayo import shell
from nyayo.matches import worker
from nyayo.matches.models import Match

from .test_games import pgn
from .test_uploads import TWO, BatchCase


class Frame(BatchCase):
    def page(self, url="/matches"):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_the_status_block(self):
        self.keep(bz2.compress(TWO))
        self.put_batch(bz2.compress(TWO), batch="waiting")
        page = self.page()
        self.assertIn("Worker not running", page)
        self.assertIn("python-chess", page)
        self.assertIn("1 upload waiting", page)
        self.assertIn('title="games kept as data">2<', page)
        self.assertNotIn(" live</span>", page)  # the gauntlet fixture is running, but silent: last heard of long ago
        Match.objects.update(updated_at=timezone.now())
        self.assertIn("1 live", self.page())

    def test_a_running_worker_and_the_pinned_reader(self):
        with mock.patch.object(worker, "is_running", return_value=True), \
                override_settings(NYAYO_READER="/home/fischer/dev/nyayo-bin/phant-indexer-1.3.1-40623e8"):
            page = self.page()
        self.assertIn("Worker running", page)
        self.assertIn("phant-indexer-1.3.1-40623e8", page)

    def test_a_refused_upload_is_red(self):
        self.keep(bz2.compress(b"not bzip2"))
        page = self.page()
        self.assertIn("1 upload refused", page)
        self.assertIn('class="count red">1 refused', page)

    def test_the_link_of_the_page_is_marked(self):
        def active(page):
            return page.split('class="active"')[0].rsplit("<a ", 1)[1]

        self.assertIn("/matches", active(self.page("/matches")))
        self.assertIn("/matches", active(self.page("/matches/gauntlet-test-1")))
        self.assertIn('href="/"', active(self.page("/")))  # the dashboard
        self.assertIn('href="/runs"', active(self.page("/runs")))
        self.assertIn("/games", active(self.page("/games")))
        self.assertIn("Catalogue › Games", self.page("/games"))

    def test_is_running_sees_a_held_lock(self):
        self.assertFalse(worker.is_running())
        with worker.only_worker():
            self.assertTrue(worker.is_running())
        self.assertFalse(worker.is_running())

    def test_the_polled_fragment_never_works_the_status_out(self):
        self.put_game(pgn("1. e4 e5"), game="1")
        with mock.patch.object(shell, "status", side_effect=AssertionError("worked out")):
            response = self.client.get("/matches/gauntlet-test-1/games/1/live")
        self.assertEqual(response.status_code, 200)

    def test_the_elephant_is_the_icon(self):
        page = self.page()
        self.assertIn('<link rel="icon" type="image/svg+xml" href="/static/img/elephant.svg">', page)
        self.assertIn('<link rel="icon" type="image/png" sizes="32x32" href="/static/img/elephant-32.png">', page)
        self.assertIn('<link rel="apple-touch-icon" sizes="180x180" href="/static/img/apple-touch-icon.png">', page)
        self.assertIn('<img class="logo" src="/static/img/elephant.svg" alt="">', page)
        # the files are there, with the licence they are used under
        for name in ("elephant.svg", "elephant-32.png", "apple-touch-icon.png", "LICENSE-elephant.txt"):
            self.assertIsNotNone(finders.find(f"img/{name}"), name)
        self.assertIn("BSD 3-Clause", open(finders.find("img/LICENSE-elephant.txt"), encoding="utf-8").read())
