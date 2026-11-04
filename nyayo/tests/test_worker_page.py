"""A box's page: the hourly rollup behind its 48-hour graph (nyayo/lab/hours.py), the graph, the tiles, and the list of every test
it has played, paged."""

import datetime
import html

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from nyayo.lab import hours, machines
from nyayo.lab.models import Machine, MachineHour, Result
from nyayo.matches.worker import look_for_expired

from . import test_lab_worker_api as api
from .lab_helpers import make_machine, make_result, make_test

HOUR = datetime.timedelta(hours=1)


def top_of_hour(ago=0):
    return hours.start_of(timezone.now()) - HOUR * ago


def keep(box="hunt", ago=0, **values):
    return MachineHour.objects.update_or_create(box=box, hour=top_of_hour(ago), defaults=values)[0]


def page(client, path="/machines/hunt"):
    reply = client.get(path)
    assert reply.status_code == 200, reply.status_code
    return html.unescape(reply.content.decode())


class Rollup(TestCase):
    def machine(self, **info):
        machine = make_machine("hunt", **info)
        return machine

    def test_a_call_adds_its_games_and_the_time_since_the_last_one_to_the_hour(self):
        machine = self.machine()
        now = hours.start_of(timezone.now()) + datetime.timedelta(minutes=30)
        Machine.objects.filter(pk=machine.pk).update(updated=now - datetime.timedelta(seconds=40))
        machine.refresh_from_db()
        machine.rtt_ms, machine.rtt_noted = 2.5, True
        hours.note(machine, games=8, now=now)
        hours.note(machine, games=4, now=now)
        row = MachineHour.objects.get()
        self.assertEqual((row.box, row.hour, row.games, row.heard, row.rtt_n, row.rtt_sum), ("hunt", hours.start_of(now), 12, 80, 2, 5.0))

    def test_a_long_silence_is_not_counted_as_being_there(self):
        machine = self.machine()
        now = timezone.now()
        Machine.objects.filter(pk=machine.pk).update(updated=now - datetime.timedelta(hours=3))
        machine.refresh_from_db()
        hours.note(machine, now=now)
        self.assertEqual(MachineHour.objects.get().heard, 0)

    def test_no_response_time_is_none_not_zero(self):
        machine = self.machine()
        hours.note(machine)
        row = MachineHour.objects.get()
        self.assertEqual(row.rtt_n, 0)
        self.assertIsNone(hours.last_hours("hunt", timezone.now())[-1]["rtt"])

    def test_a_restart_goes_on_in_the_same_row_and_another_box_has_its_own(self):
        hours.note(self.machine())
        hours.note(make_machine("hunt"))
        hours.note(make_machine("pc-wsl"))
        self.assertEqual(sorted(MachineHour.objects.values_list("box", "games")), [("hunt", 0), ("pc-wsl", 0)])
        self.assertEqual(MachineHour.objects.get(box="hunt").hour, hours.start_of(timezone.now()))

    def test_a_worker_with_no_name_is_kept_by_its_user_and_address(self):
        a = make_machine("anon", machine_name=None, mac_address="aa")
        b = make_machine("anon", machine_name="None", mac_address="bb")
        self.assertNotEqual(hours.box_of(a), hours.box_of(b))
        self.assertTrue(hours.box_of(a).startswith("anon:"))

    def test_a_failure_never_reaches_the_worker(self):
        class Broken:
            id = 1
            user_id = 1
            updated = None
            info = {}
            rtt_ms = None
        hours.note(None)            # nothing to read from
        hours.note(Broken(), now="not a time")

    def test_last_hours_is_always_48_with_the_gaps_filled(self):
        keep(ago=0, games=3, heard=900)
        keep(ago=5, games=7, heard=3600, rtt_sum=6.0, rtt_n=3)
        found = hours.last_hours("hunt", timezone.now())
        self.assertEqual(len(found), 48)
        self.assertEqual((found[-1]["games"], found[-1]["has"]), (3, True))
        self.assertEqual((found[-6]["games"], found[-6]["rtt"]), (7, 2.0))
        self.assertEqual((found[0]["has"], found[0]["games"]), (False, 0))

    def test_hours_older_than_thirty_days_are_pruned_and_nothing_else(self):
        keep(ago=24 * 31, games=1)
        keep(ago=24 * 29, games=1)
        keep(ago=0, games=1)
        self.assertEqual(hours.prune(), 1)
        self.assertEqual(MachineHour.objects.count(), 2)

    def test_the_worker_prunes_with_its_other_housekeeping(self):
        keep(ago=24 * 40, games=1)
        look_for_expired()
        self.assertFalse(MachineHour.objects.exists())


class FromTheWorkersCalls(api.Api):
    def test_a_poll_a_heartbeat_and_results_each_add_to_the_hour(self):
        test = make_test()
        workload = self.handed_out(test)
        self.assertEqual(MachineHour.objects.get().games, 0)
        self.worker("clientSubmitResults", test_id=workload["test"]["id"], result_id=workload["result"]["id"], crashes=0, timelosses=0,
                    illegals=0, trinomial="2 4 2", pentanomial="0 1 2 1 0")
        self.worker("clientHeartbeat", test_id=test.id)
        row = MachineHour.objects.get()
        self.assertEqual((row.box, row.games), ("hunt", 8))

    def test_the_hour_is_one_small_write_and_the_page_costs_no_more_queries_for_more_hours(self):
        self.sign_in()
        self.worker("clientGetWorkload")
        for ago in range(1, 40):
            keep(ago=ago, games=ago)
        with CaptureQueriesContext(connection) as few:
            self.client.get("/machines/hunt")
        for ago in range(40, 700):
            keep(ago=ago, games=1)
        with CaptureQueriesContext(connection) as many:
            self.client.get("/machines/hunt")
        self.assertEqual(len(few), len(many))


class Graph(TestCase):
    def build(self, rows, up=True):
        return machines.graph(rows, timezone.now(), up)

    def hours(self, **by_ago):
        rows = hours.last_hours("nobody", timezone.now())
        for ago, values in by_ago.items():
            rows[47 - int(ago[1:])].update(has=True, **values)
        return rows

    def test_a_new_box_shows_only_what_it_has_and_says_so(self):
        g = self.build(self.hours(a0=dict(games=5, heard=600, rtt=1.5)))
        self.assertTrue(g["little"])
        self.assertEqual(g["recorded"], 1)
        self.assertEqual([c["look"] for c in g["cols"]].count("none"), 47)
        self.assertEqual(g["cols"][-1]["look"], "part")

    def test_no_history_at_all_is_said_not_drawn_as_offline(self):
        g = self.build(self.hours())
        self.assertTrue(g["empty"])
        self.assertEqual({c["look"] for c in g["cols"]}, {"none"})
        self.assertEqual(g["up_pct"], None)

    def test_after_the_first_record_a_missing_hour_is_offline_and_a_quiet_one_is_online(self):
        g = self.build(self.hours(a3=dict(games=0, heard=3600, rtt=None), a0=dict(games=1, heard=3600, rtt=None)))
        looks = [c["look"] for c in g["cols"]][-4:]
        self.assertEqual(looks, ["on", "off", "off", "on"])
        self.assertIn("offline", g["cols"][-2]["tip"])

    def test_the_response_time_line_is_there_only_when_it_was_measured_and_breaks_at_a_gap(self):
        none = self.build(self.hours(a1=dict(games=3, heard=3600, rtt=None)))
        self.assertFalse(none["has_rtt"])
        self.assertEqual(none["lines"], [])
        some = self.build(self.hours(a4=dict(games=3, heard=3600, rtt=1.0), a3=dict(games=3, heard=3600, rtt=2.0),
                                     a0=dict(games=3, heard=3600, rtt=1.0)))
        self.assertTrue(some["has_rtt"])
        self.assertEqual(len(some["lines"]), 2)

    def test_the_axes_are_round_numbers_over_the_highest_hour(self):
        g = self.build(self.hours(a2=dict(games=101, heard=3600, rtt=3.2)))
        self.assertEqual((g["top"], g["half"], g["rtt_top"]), (120, 60, 5))

    def test_the_figures(self):
        rows = self.hours(**{"a%d" % i: dict(games=10, heard=3600, rtt=1.0) for i in range(24)})
        g = self.build(rows)
        self.assertEqual(g["per_hour"], 10)
        self.assertEqual(g["games_48"], 240)
        self.assertEqual(g["up_for"], "24 h 0 min")


class Page(TestCase):
    def test_the_graph_comes_first_with_its_legend_and_no_response_time_when_there_is_none(self):
        machine = make_machine("hunt")
        keep(games=12, heard=1800)
        text = page(self.client)
        self.assertLess(text.index("Last 48 hours"), text.index("Connection"))
        self.assertIn("Games each hour", text)
        self.assertNotIn("Response time</span>", text)
        self.assertIn("No response time is shown", text)
        keep(ago=1, games=2, heard=3600, rtt_sum=4.0, rtt_n=2)
        self.assertIn("Response time</span>", page(self.client))

    def test_a_new_worker_with_nothing_on_record(self):
        make_machine("fresh")
        text = page(self.client, "/machines/fresh")
        self.assertIn("Nothing is on record for this machine yet", text)
        self.assertIn("It has not played a test", text)

    def test_the_four_tiles(self):
        test = make_test(name="lmr-trim", test_mode="SPSA")
        machine = make_machine("hunt")
        result = make_result(test, machine)
        Result.objects.filter(pk=result.pk).update(games=40, crashes=2, timeloss=1, workloads=3)
        older = make_result(make_test(name="other"), machine)
        Result.objects.filter(pk=older.pk).update(updated=timezone.now() - datetime.timedelta(days=1))
        text = page(self.client)
        for expected in ("Games, all time", ">40<", "on 2 tests", "2 crashes", "1 time loss", "Last workload", "SPSA against main",
                         "Contributed to", "1 SPSA", "1 SPRT", "Approval", "Not kept yet"):
            self.assertIn(expected, text)
        self.assertEqual(sorted(k for k, n in self.client.get("/machines/hunt").context["kinds"]), ["SPRT", "SPSA"])

    def test_an_offline_worker_says_when_it_was_last_heard(self):
        machine = make_machine("hunt")
        Machine.objects.filter(pk=machine.pk).update(updated=timezone.now() - datetime.timedelta(hours=2))
        text = page(self.client)
        self.assertIn("Offline", text)
        self.assertIn("last heard 2 h ago", text)

    def test_a_worker_in_trouble_says_what_is_wrong_on_its_tile(self):
        make_machine("hunt", supported=[])
        self.assertIn("Needs a look", page(self.client))
        self.assertIn("Plays no engine", page(self.client))

    def test_toolchains_are_what_was_reported_and_say_so_until_registration_keeps_them(self):
        make_machine("hunt", compilers={"Elephant": ["clang++", "21.1.4"]}, python_ver="3.12.3")
        text = page(self.client)
        self.assertIn("clang++", text)
        self.assertIn("21.1.4", text)
        self.assertIn("python3", text)
        self.assertIn("not shown as missing", text)

    def test_toolchains_that_registration_kept_show_the_missing_ones_greyed(self):
        machine = make_machine("hunt")
        machine.toolchains = {"g++": "13.3.0", "rustc": ""}
        rows = machines.toolchains(machine)
        self.assertTrue(rows["complete"])
        self.assertEqual([(r["name"], r["found"], r["cls"]) for r in rows["rows"]], [("g++", True, "cpp"), ("rustc", False, "rust")])

    def test_cpu_flags_are_pills_inside_the_hardware_box_with_tooltips(self):
        make_machine("hunt", cpu_flags=["POPCNT", "NOVEL"], cpu_name="Ryzen 9")
        text = page(self.client)
        hardware = text.split(">Hardware</h3>")[1].split(">Toolchains</h3>")[0]
        self.assertIn('tabindex="0" role="button" aria-label="POPCNT:', hardware)
        self.assertIn('<span class="flagpill">NOVEL</span>', hardware)

    def test_every_test_it_has_played_is_listed_and_paged(self):
        machine = make_machine("hunt")
        for n in range(70):
            make_result(make_test(name="t%02d" % n), machine)
        first = page(self.client)
        self.assertIn("page 1 of 2", first)
        self.assertIn(">t69<", first)
        self.assertNotIn(">t00<", first)
        self.assertIn(">t00<", page(self.client, "/machines/hunt?page=2"))

    def test_the_queries_do_not_grow_with_the_tests_or_the_sessions(self):
        for n in range(3):
            make_result(make_test(name="a%d" % n), make_machine("hunt"))
        with CaptureQueriesContext(connection) as few:
            self.client.get("/machines/hunt")
        for n in range(30):
            make_result(make_test(name="b%d" % n), make_machine("hunt"))
        with CaptureQueriesContext(connection) as many:
            self.client.get("/machines/hunt")
        self.assertEqual(len(few), len(many))

    def test_nothing_credential_like_is_shown_anywhere_on_the_new_page(self):
        machine = make_machine("hunt", tokens={"Elephant": "ghp_THE_GIT_TOKEN"}, password="pw-in-info", mac_address="de:ad:be:ef",
                               secret="info-secret", gpus=[{"name": "RTX", "token": "THE-GPU-TOKEN"}], python_ver="3.12")
        Machine.objects.filter(pk=machine.pk).update(secret="THE-SESSION-SECRET", rtt_ms=5.0)
        keep(games=3, heard=100, rtt_sum=5.0, rtt_n=1)
        make_result(make_test(), machine)
        text = page(self.client)
        for secret in ("THE-SESSION-SECRET", "ghp_THE_GIT_TOKEN", "pw-in-info", "info-secret", "de:ad:be:ef", "THE-GPU-TOKEN"):
            self.assertNotIn(secret, text)
