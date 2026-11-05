"""Matches named <mode>-<test id>, and `manage.py rename_matches`, which gives the matches taken from OpenBench the same
form (nyayo/matches/renaming.py), with the redirects from the old ids (nyayo/matches/oldids.py)."""

import bz2
import json
import uuid
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from nyayo.lab import document, modify_test
from nyayo.lab.models import LogEvent, Result, Test
from nyayo.matches import catalogue, renaming
from nyayo.matches.ingest import archive_dir, archive_path
from nyayo.matches.models import (Batch, Dataset, Game, Match, MatchResult, MatchSnapshot, RenamedMatch, Tombstone,
                                  Upload)

from .lab_helpers import make_machine, make_result, make_test
from .test_games import pgn
from .test_matches_ingest import gauntlet, v1
from .test_uploads import BatchCase, batch_text, game

OLD = "openbench-openbench-%d"
PLAYED = batch_text(game("1-0", Opponent="base"), game("0-1", Subject="black", Opponent="base"))


def run(*args, expect_error=False):
    out, err = StringIO(), StringIO()
    if expect_error:
        try:
            call_command("rename_matches", *args, stdout=out, stderr=err)
        except CommandError as error:
            return out.getvalue(), err.getvalue(), str(error)
        raise AssertionError("expected the command to refuse")
    call_command("rename_matches", *args, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue(), None


class NewIds(TestCase):
    def test_each_mode_has_its_prefix(self):
        expected = {"SPRT": "sprt", "GAMES": "games", "SPSA": "tune", "DATAGEN": "datagen"}
        for mode, prefix in expected.items():
            test = make_test(test_mode=mode)
            self.assertEqual(test.match_id(), f"{prefix}-{test.id}")

    def test_the_number_is_one_sequence_across_modes(self):
        numbers = [make_test(test_mode=mode).id for mode in ("SPRT", "GAMES", "SPSA", "DATAGEN")]
        self.assertEqual(numbers, sorted(set(numbers)))

    def test_a_report_makes_the_match_under_the_new_id(self):
        for mode, prefix, kind in (("SPRT", "sprt", "sprt"), ("GAMES", "games", "games")):
            test = make_test(test_mode=mode)
            document.report_test(test)
            match = Match.objects.get(pk=f"{prefix}-{test.id}")
            self.assertEqual((match.kind, match.producer["version"], match.host["name"]), (kind, "openbench", "openbench"))

    def test_a_match_id_reads_back_to_its_test_only_with_the_right_mode(self):
        test = make_test(test_mode="GAMES")
        right = Match(pk=f"games-{test.id}")
        wrong = Match(pk=f"sprt-{test.id}")
        self.assertEqual(modify_test.test_of(right), test)
        self.assertIsNone(modify_test.test_of(wrong))
        self.assertIsNone(modify_test.test_of(Match(pk=f"games-{test.id + 50}")))
        self.assertIsNone(modify_test.test_of(Match(pk=OLD % test.id)))
        self.assertIsNone(modify_test.test_of(Match(pk="gauntlet-1")))


class MachinesLinks(TestCase):
    def test_the_pages_link_to_the_match_of_each_mode_without_a_query_per_row(self):
        machine = make_machine("hunt")
        for mode in ("SPRT", "GAMES", "SPSA", "DATAGEN"):
            make_result(make_test(test_mode=mode, name="t-" + mode.lower()), machine)
        last = Test.objects.order_by("id").last()
        machine.workload = last.id
        machine.save()
        counts = []
        for _ in range(2):
            from django.db import connection
            from django.test.utils import CaptureQueriesContext
            with CaptureQueriesContext(connection) as queries:
                detail = self.client.get("/machines/hunt").content.decode()
            counts.append(len(queries))
            make_result(make_test(test_mode="SPRT"), machine)
        for test in Test.objects.filter(name__startswith="t-"):
            self.assertIn('href="/matches/%s"' % test.match_id(), detail)
        self.assertIn("/matches/tune-", detail)
        self.assertIn("/matches/datagen-", detail)
        self.assertEqual(counts[0], counts[1])
        listing = self.client.get("/machines").content.decode()
        self.assertIn('href="/matches/%s"' % last.match_id(), listing)


class Reserved(BatchCase):
    def test_an_outside_producer_cannot_use_a_tests_id(self):
        for name in ("sprt-5", "games-1", "tune-77", "datagen-1000"):
            response = self.put_match(v1(match__id=name), match_id=name)
            self.assertEqual(response.status_code, 400, name)
            self.assertIn("reserved", response.content.decode())
        self.assertFalse(Match.objects.filter(pk="sprt-5").exists())

    def test_nor_can_it_write_games_or_batches_under_one(self):
        self.assertEqual(self.put_game(pgn(), match_id="sprt-5").status_code, 400)
        self.assertEqual(self.put_batch(b"x", match_id="sprt-5").status_code, 400)

    def test_ids_that_only_resemble_them_are_fine(self):
        for name in ("sprt-5x", "mysprt-5", "sprt5", "sprt-5-b"):
            self.assertEqual(self.put_match(v1(match__id=name), match_id=name).status_code, 201, name)

    def test_the_lab_still_makes_its_own(self):
        test = make_test()
        self.assertIsNotNone(document.report_test(test))
        self.assertTrue(Match.objects.filter(pk=f"sprt-{test.id}").exists())


class Fixture(BatchCase):
    """Matches as the live data has them: lab tests' under openbench-openbench-N, an OpenBench server's with no test,
    five gauntlets with uuids, all with their archive, games, batches and uploads."""

    def setUp(self):
        super().setUp()
        self.uuids = [str(uuid.uuid4()) for _ in range(5)]
        for name in self.uuids:
            self.assertEqual(self.put_match(gauntlet(match__id=name)).status_code, 201)
            self.assertEqual(self.put_game(pgn("1. e4 e5"), match_id=name).status_code, 201)
        self.untouched = self.snapshot_of(self.uuids + ["gauntlet-test-1"])
        # a lab test of mode GAMES whose match the document said was kind sprt, one of SPSA, and two of the OpenBench server's
        self.games_test = make_test(id=1001, test_mode="GAMES", name="games")
        self.tune_test = make_test(id=1002, test_mode="SPSA", name="tune")
        self.sprt_test = make_test(id=1000, name="sprt")
        self.old = [OLD % n for n in (1000, 1001, 1002, 12, 40)]
        self.expected = {OLD % 1000: "sprt-1000", OLD % 1001: "games-1001", OLD % 1002: "tune-1002",
                         OLD % 12: "sprt-12", OLD % 40: "sprt-40"}
        for name in self.old:
            number = name.rsplit("-", 1)[1]
            self.assertEqual(self.put_match(self.document_of(name, int(number))).status_code, 201, name)
            self.assertEqual(self.put_game(pgn("1. e4 e5"), game="3", match_id=name).status_code, 201)
            self.assertEqual(self.put_game(pgn("1. e4 e5 2. Nf3 Nc6", result="1-0"), game="4", match_id=name).status_code, 201)
            self.keep(bz2.compress(PLAYED), match_id=name, batch="1.0")
        for name in self.old:
            self.put_batch(bz2.compress(PLAYED), batch="9.9", match_id=name)    # one still waiting for the worker
        Dataset.objects.create(state="done", selection={"matches": [OLD % 12, "gauntlet-test-1"], "openbench": ["12"]},
                               manifest=[{"id": 1, "match": OLD % 12, "number": 1, "sha256": "x", "games": []},
                                         {"id": 2, "match": "gauntlet-test-1", "number": 1, "sha256": "y", "games": []}])
        Tombstone.objects.create(pk=OLD % 7, name="deleted earlier")

    def document_of(self, name, number):
        """The match a lab test's document would have been under its old id; the OpenBench server's own are v1's."""
        test = Test.objects.filter(pk=number).first()
        if test is None:
            return v1(match__id=name, match__producer={"name": "OpenBench", "version": "openbench", "url": None})
        built = document.build_document(test)
        built["match"]["id"] = name
        return built

    def snapshot_of(self, ids):
        out = {}
        for name in ids:
            match = Match.objects.get(pk=name)
            out[name] = (match.updated_at, archive_path(name).read_bytes(), sorted(p.name for p in (self.games / name).glob("*")),
                         list(MatchResult.objects.filter(match=match).values_list("pk", "games")))
        return out

    def names_left(self, old):
        return [p for base in (self.games, self.batches, self.data / "uploads") for p in base.glob(old + "*")] + \
               list(archive_dir().glob(old + "*"))

    def state(self):
        files = sorted(str(p.relative_to(base)) for base in (self.archive, self.games, self.batches, self.data / "uploads")
                       for p in base.rglob("*"))
        rows = (sorted(Match.objects.values_list("pk", flat=True)), sorted(Game.objects.values_list("match_id", "game")),
                list(Dataset.objects.values_list("selection", "manifest")))
        return files, rows


class Rename(Fixture):
    def test_everything_that_holds_the_id_moves(self):
        counts = {name: (Game.objects.filter(match_id=name).count(), Batch.objects.filter(match_id=name).count(),
                         Upload.objects.filter(match_id=name).count(), MatchResult.objects.filter(match_id=name).count(),
                         MatchSnapshot.objects.filter(match_id=name).count()) for name in self.old}
        self.assertTrue(all(c[0] and c[1] and c[2] and c[3] and c[4] for c in counts.values()), counts)
        cards = {name: [c["game"] for c in catalogue.cards(name)] for name in self.old}
        out, _, _ = run()
        self.assertIn("5 match(es) renamed", out)
        for old, new in self.expected.items():
            self.assertFalse(Match.objects.filter(pk=old).exists())
            match = Match.objects.get(pk=new)
            self.assertEqual((Game.objects.filter(match=match).count(), Batch.objects.filter(match=match).count(),
                              Upload.objects.filter(match=match).count(), MatchResult.objects.filter(match=match).count(),
                              MatchSnapshot.objects.filter(match=match).count()), counts[old])
            self.assertEqual(self.names_left(old), [])
            self.assertEqual(json.loads(archive_path(new).read_bytes())["match"]["id"], new)
            self.assertTrue((archive_dir() / f"{new}.meta.json").exists())
            self.assertTrue((self.games / new / "3.pgn").exists() and (self.games / new / "4.pgn").exists())
            self.assertTrue((self.data / "uploads" / new / "9.9").exists())
            batch = Batch.objects.filter(match=match).first()
            self.assertTrue(catalogue.open_path(batch).exists() or catalogue.sealed_path(batch).exists())
            self.assertEqual(catalogue.sealed_path(batch).parent.name, new)
            self.assertEqual([c["game"] for c in catalogue.cards(new)], cards[old])
            self.assertEqual(RenamedMatch.objects.get(pk=old).new_id, new)
        self.assertEqual(self.snapshot_of(self.uuids + ["gauntlet-test-1"]), self.untouched)
        self.assertEqual(Match.objects.filter(pk__in=self.uuids).count(), 5)
        dataset = Dataset.objects.get()
        self.assertEqual(dataset.selection["matches"], ["sprt-12", "gauntlet-test-1"])
        self.assertEqual([e["match"] for e in dataset.manifest], ["sprt-12", "gauntlet-test-1"])
        self.assertTrue(Tombstone.objects.filter(pk=OLD % 7).exists())

    def test_the_archive_is_the_same_document_with_the_new_id(self):
        before = json.loads(archive_path(OLD % 12).read_bytes())
        raw = archive_path(OLD % 12).read_bytes()
        run()
        after = archive_path("sprt-12").read_bytes()
        before["match"]["id"] = "sprt-12"
        self.assertEqual(json.loads(after), before)
        self.assertEqual(after, raw.replace(OLD.encode() % 12, b"sprt-12"))

    def test_a_report_after_the_rename_is_an_update_not_a_conflict(self):
        run()
        test = Test.objects.get(pk=1000)
        first = document.report_test(test)    # the worker's next report: an update of the renamed match, not a conflict
        self.assertIsNotNone(first)
        self.assertFalse(first.created)
        self.assertEqual(Match.objects.filter(pk="sprt-1000").count(), 1)
        self.assertFalse(Match.objects.filter(pk=OLD % 1000).exists())

    def test_a_resend_of_the_renamed_document_is_accepted_and_the_old_id_is_told_where_to_go(self):
        run()
        again = json.loads(archive_path("sprt-12").read_bytes())
        # the id is reserved for the lab's own: an outside producer is refused, and an old id says where it went
        self.assertEqual(self.put_match(again, match_id="sprt-12").status_code, 400)
        old = v1(match__id=OLD % 12)
        response = self.put_match(old, match_id=OLD % 12)
        self.assertEqual(response.status_code, 409)
        self.assertIn("sprt-12", response.content.decode())
        self.assertFalse(Match.objects.filter(pk=OLD % 12).exists())

    def test_the_dry_run_changes_nothing(self):
        before = self.state()
        out, _, _ = run("--dry-run")
        self.assertIn("5 match(es) would be renamed", out)
        self.assertIn("dry run: nothing was changed", out)
        for old, new in self.expected.items():
            self.assertIn(f"{old} -> {new}", out)
        self.assertEqual(self.state(), before)
        self.assertFalse(RenamedMatch.objects.exists())

    def test_it_can_be_run_again_and_does_nothing(self):
        run()
        after = self.state()
        out, _, _ = run()
        self.assertIn("0 match(es) renamed, 0 file(s)", out)
        self.assertEqual(self.state(), after)

    def test_stopped_in_the_middle_it_finishes_when_run_again(self):
        real = renaming.do_file_step
        calls = []

        def stop_after_a_few(old_id, new_id, step):
            if len(calls) == 7:
                raise KeyboardInterrupt
            calls.append(step)
            return real(old_id, new_id, step)

        with mock.patch.object(renaming, "do_file_step", stop_after_a_few):
            with self.assertRaises(KeyboardInterrupt):
                run()
        # the database was done first; some files have moved and some have not
        self.assertEqual(RenamedMatch.objects.count(), 5)
        self.assertTrue(any(self.names_left(old) for old in self.old))
        out, _, _ = run("--dry-run")
        self.assertIn("0 match(es) would be renamed", out)
        self.assertNotIn("would be renamed, 0 file", out)
        run()
        for old, new in self.expected.items():
            self.assertEqual(self.names_left(old), [])
            self.assertEqual(json.loads(archive_path(new).read_bytes())["match"]["id"], new)
            self.assertTrue((self.games / new / "3.pgn").exists())
        self.assertEqual(Dataset.objects.get().selection["matches"], ["sprt-12", "gauntlet-test-1"])

    def test_stopped_between_the_database_and_the_datasets_it_still_updates_them(self):
        with mock.patch.object(renaming, "fix_datasets", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                run()
        self.assertEqual(Dataset.objects.get().selection["matches"], [OLD % 12, "gauntlet-test-1"])
        run()
        self.assertEqual(Dataset.objects.get().selection["matches"], ["sprt-12", "gauntlet-test-1"])

    def test_stopped_after_writing_an_archive_but_before_removing_the_old_one_it_carries_on(self):
        new = archive_dir() / "sprt-12.json"
        new.write_bytes(renaming.archive_bytes(OLD % 12, "sprt-12", archive_path(OLD % 12).read_bytes()))
        run()
        self.assertFalse(archive_path(OLD % 12).exists())
        self.assertEqual(json.loads(new.read_bytes())["match"]["id"], "sprt-12")

    def test_a_match_that_has_the_new_id_already_is_refused_with_nothing_changed(self):
        self.put_match(v1(match__id="other-12"), match_id="other-12")
        Match.objects.filter(pk="other-12").update(kind="sprt")
        # a match under sprt-12 that arrived some other way
        copy = Match.objects.get(pk="other-12")
        copy.pk = "sprt-12"
        copy.save(force_insert=True)
        before = self.state()
        _, err, error = run(expect_error=True)
        self.assertIn("a match sprt-12 exists already", err)
        self.assertIn("nothing was changed", error)
        self.assertEqual(self.state(), before)
        self.assertFalse(RenamedMatch.objects.exists())

    def test_two_old_ids_wanting_one_new_id_are_refused(self):
        self.put_match(v1(match__id="openbench-bobby-12"))
        _, err, _ = run(expect_error=True)
        self.assertIn("would all become sprt-12", err)
        self.assertFalse(RenamedMatch.objects.exists())

    def test_a_deleted_matchs_tombstone_blocks_its_id(self):
        Tombstone.objects.create(pk="sprt-12")
        _, err, _ = run(expect_error=True)
        self.assertIn("sprt-12 is the id of a match that was deleted", err)

    def test_a_file_that_would_be_overwritten_is_refused(self):
        (self.games / "sprt-12").mkdir()
        before = self.state()
        _, err, _ = run(expect_error=True)
        self.assertIn("exists already", err)
        self.assertEqual(self.state(), before)
        self.assertFalse(RenamedMatch.objects.exists())

    def test_a_kind_with_no_rule_and_no_test_is_refused(self):
        self.put_match(v1(match__id=OLD % 99, match__kind="selfplay"))
        _, err, _ = run(expect_error=True)
        self.assertIn("kind selfplay has no rule", err)

    def test_files_named_like_an_old_id_that_no_match_has_are_told_not_touched(self):
        (self.games / "openbench-openbench-5000").mkdir()
        out, _, _ = run()
        self.assertIn("openbench-openbench-5000", out)
        self.assertTrue((self.games / "openbench-openbench-5000").exists())

    def test_every_table_that_refers_to_a_match_is_covered(self):
        # the rename moves rows by what the model says refers to Match; this is what it knows today
        tables = {r.related_model.__name__ for r in Match._meta.related_objects}
        self.assertEqual(tables, {"ScheduleEntry", "MatchResult", "MatchSnapshot", "Upload", "Batch", "Game"})


class Links(Fixture):
    """After the rename: old links redirect, a lookalike is a 404."""

    def setUp(self):
        super().setUp()
        run()

    def test_a_match_and_every_page_under_it_redirect_for_a_get(self):
        for tail in ("", "/workers", "/games/3", "/games/3/live"):
            response = self.client.get(f"/matches/{OLD % 12}{tail}")
            self.assertEqual(response.status_code, 301, tail)
            self.assertEqual(response["Location"], f"/matches/sprt-12{tail}")
            self.assertEqual(self.client.get(response["Location"]).status_code, 200, tail)
        self.assertEqual(self.client.head(f"/matches/{OLD % 12}")["Location"], "/matches/sprt-12")

    def test_the_query_string_is_kept(self):
        response = self.client.get(f"/matches/{OLD % 1001}/games/3/live?since=4&x=a%20b")
        self.assertEqual(response["Location"], "/matches/games-1001/games/3/live?since=4&x=a%20b")

    def test_each_old_id_goes_to_its_own(self):
        for old, new in self.expected.items():
            self.assertEqual(self.client.get(f"/matches/{old}")["Location"], f"/matches/{new}")

    def test_a_viewer_polling_the_api_is_redirected_too(self):
        response = self.client.get(f"/api/matches/{OLD % 12}/games/3")
        self.assertEqual((response.status_code, response["Location"]), (301, "/api/matches/sprt-12/games/3"))
        self.assertEqual(self.client.get(response["Location"]).status_code, 200)

    def test_a_write_to_an_old_id_is_not_redirected(self):
        for method in (self.client.put, self.client.patch):
            response = method(f"/api/matches/{OLD % 12}/games/3", data=b"x", content_type="application/x-chess-pgn",
                              headers={"Authorization": f"Bearer {self.token}"})
            self.assertEqual(response.status_code, 409)
            self.assertIn("sprt-12", response.content.decode())
        self.assertEqual(self.client.post(f"/matches/{OLD % 12}").status_code, 404)

    def test_an_id_that_only_resembles_the_old_form_is_a_404(self):
        for name in ("openbench-openbench-77", "openbench-nothing-12", "openbench-12", "openbench-openbench-12x"):
            self.assertEqual(self.client.get(f"/matches/{name}").status_code, 404, name)
            self.assertEqual(self.client.get(f"/matches/{name}/workers").status_code, 404, name)

    def test_an_old_id_an_outside_match_holds_is_not_redirected(self):
        self.assertEqual(self.put_match(v1(match__id="openbench-other-12")).status_code, 201)
        self.assertEqual(self.client.get("/matches/openbench-other-12").status_code, 200)

    def test_a_saved_export_selection_by_old_id_finds_the_renamed_match(self):
        from nyayo.matches.selection import Selection
        chosen = Selection(matches=[OLD % 12]).matches()
        self.assertEqual([m.pk for m in chosen], ["sprt-12"])
        self.assertEqual([m.pk for m in Selection(openbench=["12"]).matches()], ["sprt-12"])

    def test_the_uuid_matches_are_where_they_were(self):
        for name in self.uuids:
            self.assertEqual(self.client.get(f"/matches/{name}").status_code, 200)
            self.assertEqual(self.client.get(f"/matches/{name}/games/1").status_code, 200)

    def test_under_a_script_prefix_the_prefix_stays(self):
        response = self.client.get(f"/matches/{OLD % 12}", SCRIPT_NAME="/nyayo")
        self.assertEqual(response["Location"], "/nyayo/matches/sprt-12")


class AfterTheRename(Fixture):
    """What the pages do with a renamed match: the buttons, modify, delete."""

    def setUp(self):
        super().setUp()
        run()
        self.owner = User.objects.create_user("boss", password="pw", is_staff=True, is_superuser=True)
        self.client.force_login(self.owner)

    def test_a_lab_test_found_from_its_renamed_match(self):
        self.assertEqual(modify_test.test_of(Match.objects.get(pk="sprt-1000")), self.sprt_test)
        self.assertEqual(modify_test.test_of(Match.objects.get(pk="games-1001")), self.games_test)
        self.assertEqual(modify_test.test_of(Match.objects.get(pk="tune-1002")), self.tune_test)
        self.assertIsNone(modify_test.test_of(Match.objects.get(pk="sprt-12")))

    def test_the_page_offers_the_buttons_for_a_test_and_only_delete_for_the_servers(self):
        page = self.client.get("/matches/sprt-1000").content.decode()
        self.assertIn(reverse("test_action", args=[1000, "stop"]), page)
        page = self.client.get("/matches/sprt-12").content.decode()
        self.assertNotIn("/action/", page)
        self.assertIn("Delete", page)

    def test_stop_and_restart_a_renamed_test(self):
        response = self.client.post(reverse("test_action", args=[1000, "stop"]))
        self.assertRedirects(response, "/matches/sprt-1000", fetch_redirect_response=False)
        self.assertTrue(Test.objects.get(pk=1000).finished)
        self.assertEqual(Match.objects.get(pk="sprt-1000").state, "stopped")
        self.assertTrue(LogEvent.objects.filter(test_id=1000, summary="STOP").exists())
        self.assertFalse(Match.objects.filter(pk=OLD % 1000).exists())
        self.assertEqual(Match.objects.filter(pk__startswith="sprt-").count(), 3)

    def test_delete_with_games_removes_everything_under_the_new_name(self):
        self.work()    # the upload that waited in uploads/<new id>/ is read under the new name; a delete waits for that
        self.assertFalse(list((self.data / "uploads").glob("sprt-12/9.9")))
        response = self.client.post(reverse("match_remove"), {"id": ["games-1001", "sprt-12"], "mode": "delete", "confirm": ""})
        self.assertLess(response.status_code, 400, response.content)
        for name in ("games-1001", "sprt-12"):
            self.assertFalse(Match.objects.filter(pk=name).exists())
            self.assertTrue(Tombstone.objects.filter(pk=name).exists())
            self.assertFalse(archive_path(name).exists())
            self.assertFalse((self.games / name).exists())
            self.assertFalse((self.batches / name).exists())
            self.assertFalse(Batch.objects.filter(match_id=name).exists())
        self.assertFalse(Test.objects.filter(pk=1001).exists())
        # the old link now lands on the deleted match's page, which says so
        page = self.client.get(self.client.get(f"/matches/{OLD % 12}")["Location"])
        self.assertEqual(page.status_code, 404)
        self.assertIn("was deleted", page.content.decode())
        self.assertTrue(Match.objects.filter(pk="sprt-40").exists())
