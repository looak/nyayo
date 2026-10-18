"""Every list is 64 to a page, with a pager that keeps the rest of the query."""

import uuid
from datetime import timedelta

from django.contrib.admin.sites import site
from django.template import Context, Template
from django.test import RequestFactory, SimpleTestCase
from django.utils import timezone

from nyayo.matches.models import Engine, Game, Match
from nyayo.paging import PAGE_SIZE, paginate
from nyayo.runs.models import Machine, Run

from .test_games import GameTestCase


class Size(SimpleTestCase):
    def test_it_is_64(self):
        self.assertEqual(PAGE_SIZE, 64)

    def test_a_page_of_a_list(self):
        request = RequestFactory().get("/", {"page": "2"})
        page = paginate(list(range(100)), request)
        self.assertEqual((page.number, len(page.object_list), page.paginator.num_pages), (2, 36, 2))
        self.assertEqual(page.object_list[0], 64)

    def test_nonsense_is_the_first_page_and_too_far_is_the_last(self):
        items = list(range(100))
        self.assertEqual(paginate(items, RequestFactory().get("/", {"page": "x"})).number, 1)
        self.assertEqual(paginate(items, RequestFactory().get("/", {"page": "99"})).number, 2)

    def test_a_key_of_its_own(self):
        request = RequestFactory().get("/", {"page": "2", "newly-failing": "1"})
        self.assertEqual(paginate(list(range(100)), request, "newly-failing").number, 1)


class Tags(SimpleTestCase):
    def render(self, template, query, **context):
        request = RequestFactory().get("/runs", query)
        return Template("{% load runs_extras %}" + template).render(Context({"request": request, **context}))

    def test_the_pager_keeps_the_filters_and_changes_the_page(self):
        html = self.render('{% paged items "page" as p %}{% pager p %}', {"state": "wip", "page": "2"},
                           items=list(range(200)))
        self.assertIn('href="?state=wip&amp;page=1"', html)       # previous
        self.assertIn('href="?state=wip&amp;page=3"', html)       # next
        self.assertIn("page 2 of 4", html)
        self.assertIn("200 in all", html)
        self.assertIn('href="?state=wip&amp;page=4"', html)       # last
        self.assertNotIn(">first<", html)                         # one back is previous enough

    def test_nothing_for_a_list_that_fits(self):
        html = self.render('{% paged items "page" as p %}{% pager p %}', {}, items=list(range(64)))
        self.assertNotIn("pager", html)

    def test_a_nested_list_pages_on_its_own_key(self):
        html = self.render('{% paged items key as p %}{% pager p key %}', {"page": "3", "a-newly-failing": "1"},
                           items=list(range(100)), key="a-newly-failing")
        self.assertIn('href="?page=3&amp;a-newly-failing=2"', html)


class Lists(GameTestCase):
    def matches(self, count):
        subject = Engine.objects.first()
        now = timezone.now()
        Match.objects.bulk_create([
            Match(id=f"bulk-{n:03d}", kind="gauntlet", state="finished", subject=subject, updated_at=now - timedelta(minutes=n))
            for n in range(count)])

    def test_the_match_list_is_64_to_a_page(self):
        self.matches(70)  # and the gauntlet from setUp: 71
        first = self.client.get("/matches").content.decode()
        self.assertEqual(first.count('<a href="/matches/'), 64)
        self.assertIn("page 1 of 2", first)
        second = self.client.get("/matches?page=2").content.decode()
        self.assertEqual(second.count('<a href="/matches/'), 7)

    def test_the_match_list_pager_keeps_the_filter(self):
        self.matches(70)
        page = self.client.get("/matches?kind=gauntlet").content.decode()
        self.assertIn('href="?kind=gauntlet&amp;page=2"', page)

    def test_a_matches_games_are_64_to_a_page_and_still_counted_whole(self):
        match = Match.objects.get()
        Game.objects.bulk_create([
            Game(match=match, game=str(n), opponent="barbarossa", colour="white", result="1-0",
                 finished_at=timezone.now()) for n in range(100, 170)])  # 70 finished games
        first = self.client.get("/matches/gauntlet-test-1").content.decode()
        self.assertEqual(first.count("view</a>"), 64)
        self.assertIn("page 1 of 2", first)
        self.assertIn('<h3>Finished <span class="muted">70</span></h3>', first)  # of all of them, not of the page
        second = self.client.get("/matches/gauntlet-test-1?page=2").content.decode()
        self.assertEqual(second.count("view</a>"), 6)

    def test_a_short_games_list_has_no_pager(self):
        self.put_game(b'[Result "1-0"]\n\n1. e4 e5 1-0\n')
        self.assertNotIn('class="pager"', self.client.get("/matches/gauntlet-test-1").content.decode())

    def test_the_admin_lists_are_64_to_a_page(self):
        for model in (Match, Engine, Machine, Run):
            self.assertEqual(site._registry[model].list_per_page, 64, model)

    def test_the_runs_list_is_64_to_a_page(self):
        now = timezone.now()
        Run.objects.bulk_create([
            Run(id=uuid.uuid4(), kind="smoke", source="local", state="wip", branch="main", tree=f"{n:040d}",
                recorded_at=now - timedelta(minutes=n)) for n in range(70)])
        first = self.client.get("/").content.decode()
        self.assertEqual(first.count('<a href="/runs/'), 64)
        self.assertIn("page 1 of 2", first)
        self.assertEqual(self.client.get("/?page=2").content.decode().count('<a href="/runs/'), 6)
