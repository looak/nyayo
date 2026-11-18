"""The engine catalogue: filling rows from reports, finding them by alias, the backfill, the pages, the merge."""

import io
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management import call_command
from django.urls import reverse

from nyayo.matches import engines
from nyayo.matches.models import Engine, Match, MatchResult

from .test_matches_ingest import MatchTestCase, gauntlet

LEORIK = {"id": "leorik", "name": "Leorik", "version": "3.0",
          "url": "https://github.com/lithander/Leorik", "ref": "3.0",
          "rating": {"value": 2900, "list": "CCRL Blitz", "as_of": "2026-10-18",
                     "source": "https://www.computerchess.org.uk/ccrl/404/leorik"}}


def played(opponent, **changes):
    """A gauntlet whose first opponent (the one with results) is this one."""
    return gauntlet(**{"opponents__0": opponent, "results__0__opponent": opponent["id"], **changes})


class Filling(MatchTestCase):
    def test_a_report_fills_url_ref_and_rating(self):
        self.put_match(played({**LEORIK}))
        row = Engine.objects.get(name="Leorik")
        self.assertEqual((row.url, row.ref), ("https://github.com/lithander/Leorik", "3.0"))
        self.assertEqual(row.rating["value"], 2900)
        self.assertEqual(row.rating["source"], "https://www.computerchess.org.uk/ccrl/404/leorik")

    def test_a_report_never_overwrites_what_the_row_has(self):
        Engine.objects.create(name="Leorik", version="3.0", url="https://github.com/me/leorik", ref="v3",
                              rating={"value": 2800, "list": "CEGT"})
        self.put_match(played({**LEORIK}))
        row = Engine.objects.get(name="Leorik")
        self.assertEqual((row.url, row.ref, row.rating["value"]), ("https://github.com/me/leorik", "v3", 2800))

    def test_another_match_fills_what_the_first_lacked(self):
        bare = {"id": "leorik", "name": "Leorik", "version": "3.0"}
        self.put_match(played(bare))
        self.assertEqual(Engine.objects.get(name="Leorik").url, "")
        self.put_match(played({**LEORIK}, match__id="gauntlet-test-2"))
        self.assertEqual(Engine.objects.get(name="Leorik").ref, "3.0")

    def test_the_subject_takes_repo_as_its_url(self):
        self.put_match(gauntlet(**{"subject__repo": "https://github.com/looak/elephant"}))
        self.assertEqual(Engine.objects.get(name="Elephant").url, "https://github.com/looak/elephant")

    def test_config_fills_an_opponent_that_names_nothing_itself(self):
        entry = {"id": "belette", "url": "https://github.com/vincentbab/Belette", "ref": "v1.0", "asset": "*linux*",
                 "options": {"Hash": 64}}
        self.put_match(gauntlet(config={"format": "uwanja.gauntlet/1", "gauntlet": {"opponent": [entry]}}))
        row = Engine.objects.get(name="Belette")
        self.assertEqual((row.url, row.ref, row.asset, row.options), (entry["url"], "v1.0", "*linux*", {"Hash": 64}))

    def test_nothing_is_filled_when_the_document_says_nothing(self):
        self.put_match(gauntlet())
        row = Engine.objects.get(name="Belette")
        self.assertEqual((row.url, row.ref, row.rating), ("", "", None))

    def test_a_rating_source_must_be_a_string(self):
        bad = {**LEORIK, "rating": {"value": 1, "source": 5}}
        self.assertRefused(self.put_match(played(bad)), 400, "rating.source")


class Aliases(MatchTestCase):
    def test_an_alias_finds_the_row_instead_of_making_another(self):
        row = Engine.objects.create(name="Leorik", version="3.0", aliases=["leorik"])
        self.put_match(played({"id": "leorik", "name": "leorik"}))
        self.assertEqual(Engine.objects.filter(name__iexact="leorik").count(), 1)
        self.assertEqual(MatchResult.objects.get(opponent="leorik").engine, row)

    def test_an_alias_does_not_stand_for_another_version(self):
        Engine.objects.create(name="Leorik", version="3.0", aliases=["leorik"])
        self.put_match(played({"id": "leorik", "name": "leorik", "version": "2.0"}))
        self.assertEqual(Engine.objects.filter(name="leorik").count(), 1)

    def test_the_exact_identity_wins_over_an_alias(self):
        Engine.objects.create(name="Leorik", version="3.0", aliases=["leorik"])
        exact = Engine.objects.create(name="leorik", version="")
        self.assertEqual(engines.find({"name": "leorik"}), exact)

    def test_no_row_is_none(self):
        self.assertIsNone(engines.find({"name": "Nobody"}))


class Backfill(MatchTestCase):
    def test_from_a_stored_match_and_a_gauntlet_file(self):
        self.put_match(gauntlet())
        row = Engine.objects.get(name="Barbarossa")
        row.rating = None
        row.save()
        path = Path(self.data) / "g.toml"
        path.write_text('name = "elephant-1.0.0-ccrl-blitz"\n[[opponent]]\nurl = "https://github.com/x/Barbarossa"\n'
                        'ref = "v0.6.0"\nid = "barbarossa"\noptions = { Hash = 64 }\nrating = 2465\n'
                        '[[opponent]]\nurl = "https://github.com/x/Belette"\nref = "v1"\n', encoding="utf-8")
        out = io.StringIO()
        call_command("backfill_engines", str(path), stdout=out)
        row.refresh_from_db()
        self.assertEqual((row.url, row.ref, row.options, row.rating["value"]),
                         ("https://github.com/x/Barbarossa", "v0.6.0", {"Hash": 64}, 2465))
        self.assertEqual(Engine.objects.get(name="Belette").ref, "v1")  # id from the url, as uwanja makes it
        out = io.StringIO()
        call_command("backfill_engines", str(path), stdout=out)  # again: nothing changes
        self.assertIn("0 engine rows filled", out.getvalue().splitlines()[-1])

    def test_a_files_subject_is_filled_too_and_again_changes_nothing(self):
        self.put_match(gauntlet(**{"subject__repo": "https://github.com/looak/elephant"}))
        path = Path(self.data) / "s.toml"
        path.write_text(chr(10).join(['name = "elephant-1.0.0-ccrl-blitz"', "[subject]",
                                    'url = "https://github.com/looak/elephant"', 'ref = "1.0.0"',
                                    "options = { Threads = 1 }"]), encoding="utf-8")
        call_command("backfill_engines", str(path), stdout=io.StringIO())
        row = Engine.objects.get(name="Elephant")
        self.assertEqual((row.url, row.ref, row.options),
                         ("https://github.com/looak/elephant", "1.0.0", {"Threads": 1}))
        out = io.StringIO()
        call_command("backfill_engines", str(path), stdout=out)
        self.assertIn("0 engine rows filled", out.getvalue().splitlines()[-1])

    def test_a_file_for_no_stored_match_is_said(self):
        notes = []
        engines.backfill_file({"name": "nope", "opponent": []}, notes)
        self.assertIn("nope", notes[0])


class Merge(MatchTestCase):
    def setUp(self):
        super().setUp()
        self.put_match(gauntlet(**{"opponents__0": {"id": "leorik", "name": "Leorik", "version": "3.0"},
                                   "results__0__opponent": "leorik"}))
        self.put_match(gauntlet(**{"match__id": "second", "opponents__0": {"id": "leorik", "name": "leorik",
                                                                            "rating": {"value": 2900}},
                                   "results__0__opponent": "leorik"}))
        self.keep = Engine.objects.get(name="Leorik")
        self.drop = Engine.objects.get(name="leorik")

    def test_merge_repoints_matches_and_keeps_the_name_as_an_alias(self):
        engines.merge(self.keep, self.drop)
        self.assertFalse(Engine.objects.filter(pk=self.drop.pk).exists())
        self.assertEqual(MatchResult.objects.filter(engine=self.keep).count(), 2)
        self.keep.refresh_from_db()
        self.assertEqual(self.keep.aliases, ["leorik"])
        self.assertEqual(self.keep.rating["value"], 2900)  # what the kept row lacked
        self.assertEqual(engines.find({"name": "leorik"}), self.keep)

    def test_merge_repoints_a_subject(self):
        other = Engine.objects.create(name="Elephant Gambit", version="1.0.0")
        subject = Match.objects.get(pk="second").subject
        engines.merge(other, subject)
        self.assertEqual(Match.objects.get(pk="second").subject, other)
        self.assertEqual(Match.objects.get(pk="gauntlet-test-1").subject, other)

    def test_an_engine_is_not_merged_into_itself(self):
        with self.assertRaises(engines.MergeError):
            engines.merge(self.keep, self.keep)


class Pages(MatchTestCase):
    def setUp(self):
        super().setUp()
        self.put_match(played({**LEORIK}))
        self.leorik = Engine.objects.get(name="Leorik")
        self.owner = User.objects.create_user("owner", password="x", is_staff=True)
        self.guest = User.objects.create_user("guest", password="x")

    def test_the_list_is_open_and_lists_gauntlet_opponents_only(self):
        Engine.objects.create(name="Elephant", version="", commit="abc")  # an SPRT commit: not in the catalogue
        response = self.client.get(reverse("opponents"))
        self.assertContains(response, "Leorik")
        self.assertContains(response, "2900")
        self.assertNotContains(response, "abc")
        self.assertNotContains(response, ">Edit<")

    def test_a_row_with_url_and_ref_is_listed_before_it_has_played(self):
        Engine.objects.create(name="Ethereal", version="14", url="https://github.com/o/Ethereal", ref="14")
        self.assertContains(self.client.get(reverse("opponents")), "Ethereal")

    def test_editing_and_merging_are_gated(self):
        for name in ("opponent_edit", "opponent_merge"):
            url = reverse(name, args=[self.leorik.pk])
            self.assertEqual(self.client.get(url).status_code, 302, name)
            self.client.force_login(self.guest)
            self.assertEqual(self.client.get(url).status_code, 403, name)
            self.assertEqual(self.client.post(url, {"other": 1}).status_code, 403, name)
            self.client.logout()

    def test_staff_edit_a_row(self):
        self.client.force_login(self.owner)
        url = reverse("opponent_edit", args=[self.leorik.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, {
            "url": "https://github.com/lithander/Leorik/", "ref": "3.0.1", "asset": "", "binary": "leorik",
            "options": "{\"Hash\": 64}", "rating_value": "2901", "rating_list": "CCRL Blitz",
            "rating_as_of": "2026-11-01", "rating_source": "https://example.org/x",
            "aliases": "leorik\nLeorik 3.0, leorik"})
        self.assertRedirects(response, reverse("opponents"), fetch_redirect_response=False)
        self.leorik.refresh_from_db()
        self.assertEqual((self.leorik.url, self.leorik.ref, self.leorik.options),
                         ("https://github.com/lithander/Leorik", "3.0.1", {"Hash": 64}))
        self.assertEqual(self.leorik.rating, {"value": 2901, "list": "CCRL Blitz", "as_of": "2026-11-01",
                                              "source": "https://example.org/x"})
        self.assertEqual(self.leorik.aliases, ["leorik", "Leorik 3.0"])

    def test_a_bad_form_is_refused_and_nothing_changes(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("opponent_edit", args=[self.leorik.pk]), {
            "url": "https://example.com/x", "options": "[1]", "rating_value": "high", "rating_source": ""})
        self.assertContains(response, "GitHub URL")
        self.assertContains(response, "JSON object")
        self.assertContains(response, "must be a number")
        self.leorik.refresh_from_db()
        self.assertEqual(self.leorik.url, "https://github.com/lithander/Leorik")

    def test_merge_asks_then_does_it(self):
        dupe = Engine.objects.create(name="leorik", version="")
        self.client.force_login(self.owner)
        url = reverse("opponent_merge", args=[self.leorik.pk])
        self.assertContains(self.client.get(url, {"other": dupe.pk}), "This is final")
        self.assertTrue(Engine.objects.filter(pk=dupe.pk).exists())
        response = self.client.post(url, {"other": dupe.pk})
        self.assertRedirects(response, reverse("opponents"), fetch_redirect_response=False)
        self.assertFalse(Engine.objects.filter(pk=dupe.pk).exists())
        self.leorik.refresh_from_db()
        self.assertEqual(self.leorik.aliases, ["leorik"])
