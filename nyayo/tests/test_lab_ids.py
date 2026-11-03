"""The test id rule: the first test is above the highest old match number, rounded up to the next thousand."""

from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings

from nyayo.lab import ids
from nyayo.lab.models import Engine, Test
from nyayo.matches.models import Engine as MatchEngine, Match


def make_test():
    user = User.objects.get_or_create(username="owner")[0]
    engine = Engine.objects.create(name="Elephant", source="s", sha="a" * 40)
    return Test.objects.create(author=user, dev=engine, base=engine, book_name="b", dev_repo="r", base_repo="r",
                               dev_engine="Elephant", base_engine="Elephant", dev_options="", base_options="",
                               dev_time_control="8.0+0.08", base_time_control="8.0+0.08")


@override_settings(NYAYO_INSTANCE="openbench")
class Rule(SimpleTestCase):
    def test_with_no_matches_ids_start_at_one(self):
        self.assertEqual(ids.first_test_id([]), 1)

    def test_it_is_above_the_highest_number_rounded_up_to_the_next_thousand(self):
        self.assertEqual(ids.first_test_id(["openbench-openbench-1"]), 1000)
        self.assertEqual(ids.first_test_id(["openbench-openbench-999"]), 1000)
        self.assertEqual(ids.first_test_id(["openbench-openbench-1000"]), 2000)
        self.assertEqual(ids.first_test_id(["openbench-openbench-5", "openbench-openbench-1234"]), 2000)
        self.assertEqual(ids.first_test_id(["openbench-openbench-1999"]), 2000)

    def test_only_the_ids_of_this_form_count(self):
        self.assertEqual(ids.first_test_id(["gauntlet-7", "openbench-openbench-12-x", "openbench-openbench-40"]), 1000)
        self.assertEqual(ids.first_test_id(["gauntlet-7", "openbench-openbench-12-x"]), 1)

    def test_any_instance_counts_whatever_the_setting_is(self):
        self.assertEqual(ids.first_test_id(["openbench-bobby-2500", "openbench-openbench-40"]), 3000)
        with override_settings(NYAYO_INSTANCE="somethingelse"):
            self.assertEqual(ids.first_test_id(["openbench-openbench-1234"]), 2000)

    def test_a_match_id_is_built_from_the_instance(self):
        self.assertEqual(ids.match_id(1234), "openbench-openbench-1234")
        self.assertEqual(ids.match_id(7, instance="lab"), "openbench-lab-7")
        with override_settings(NYAYO_INSTANCE="bobby"):
            self.assertEqual(ids.match_id(7), "openbench-bobby-7")


class Starting(TestCase):
    def test_a_fresh_database_counts_from_one(self):
        self.assertEqual(make_test().id, 1)

    def test_ids_count_on_from_the_floor(self):
        ids.start_test_ids(3000)
        first, second = make_test(), make_test()
        self.assertEqual((first.id, second.id), (3000, 3001))
        self.assertEqual(first.match_id(), ids.match_id(3000))

    def test_the_floor_never_lowers_an_id_already_passed(self):
        ids.start_test_ids(3000)
        make_test()
        ids.start_test_ids(1000)
        self.assertEqual(make_test().id, 3001)


class Migrated(TransactionTestCase):
    """The data migration on a database that holds old matches."""

    def test_old_match_ids_set_the_first_test_id(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("lab", "0001_models")])
        subject = MatchEngine.objects.create(name="Elephant")
        for number in (1234, 87):
            Match.objects.create(id=f"openbench-openbench-{number}", kind="sprt", state="finished", subject=subject)
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes("lab"))
        self.assertEqual(make_test().id, 2000)
        self.assertEqual(Match.objects.count(), 2)
