"""The worker cards, the verdict, the list and the clock of a test that reports steps (docs/spec.md, SPRT viewer)."""

from datetime import datetime, timedelta, timezone

from django.test import SimpleTestCase, override_settings

from nyayo.matches import stepsview

from .test_matches_ingest import ID, MatchTestCase, v1

# the pages read the real clock, so the tests' reports are dated from it
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def at(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


def walk():
    return [
        {"step": "picked_up", "state": "passed", "at": at(300)},
        {"step": "compile", "side": "dev", "state": "failed", "at": at(200), "seconds": 14, "blame": "engine",
         "detail": "attacks_bb was not declared", "log": "mobility.cpp:212: error",
         "evidence": [{"points_at": "engine", "text": "Base compiled beside it."}], "effect": "The test stops."},
        {"step": "compile", "side": "base", "state": "passed", "at": at(190), "seconds": 0, "cached": True},
        {"step": "bench", "side": "dev", "state": "pending"},
        {"step": "running", "state": "pending"},
    ]


def unstarted(state="failed_prechecks", steps=None, **match):
    document = v1()
    document["match"].update(state=state, finished_at=NOW.isoformat() if state == "failed_prechecks" else None, **match)
    document["results"][0].update(games=0, wins=0, draws=0, losses=0, penta=[0] * 5)
    document["sprt"].update(llr=0.0)
    document["machines"] = [{"name": "hunt", "games": 0, "workloads": 1, "active": True, "updated_at": at(10),
                             "steps": walk() if steps is None else steps}]
    return document


class Walk(SimpleTestCase):
    def test_columns_follow_the_steps_as_sent(self):
        columns = stepsview.columns(walk(), NOW, {})
        self.assertEqual([c["head"] for c in columns], ["Picked up", "Compile", "Bench", "Running"])
        self.assertEqual([n["label"] for n in columns[1]["nodes"]], ["dev", "base"])
        self.assertEqual([n["look"] for n in columns[1]["nodes"]], ["engine", "passed"])
        # what follows a failure was not reached
        self.assertEqual(columns[2]["nodes"][0]["sub"], "not reached")

    def test_a_failure_is_coloured_by_its_blame(self):
        for blame in ("engine", "worker", "unclear"):
            self.assertEqual(stepsview.look({"state": "failed", "blame": blame}), blame)
        self.assertEqual(stepsview.look({"state": "failed"}), "failed")

    def test_a_failure_without_a_blame_counts_once(self):
        steps = [{"step": "compile", "side": "dev", "state": "failed", "at": at(10)}]
        self.assertEqual(stepsview.tally([{"steps": steps}])["engines"][0]["text"], "failed to compile on 1")

    def test_tally_counts_what_was_sent_and_decides_nothing(self):
        rows = [{"steps": walk(), "kind": "engine"}, {"steps": walk()[:3], "kind": "checks"}]
        tally = stepsview.tally(rows)
        self.assertEqual(tally["engines"][0]["text"], "failed to compile on 2")
        self.assertEqual(tally["engines"][1]["text"], "compiled on 2 workers")
        self.assertEqual(tally["health"]["look"], "engine")
        self.assertEqual([p["text"] for p in tally["workers"]], ["1 on their checks", "1 stopped by the engine"])

    def test_the_clock_starts_at_the_first_running_step(self):
        running = {"steps": [{"step": "running", "state": "active", "at": at(60)}]}
        self.assertEqual(stepsview.running_since([running, {"steps": walk()}]), NOW - timedelta(seconds=60))
        self.assertIsNone(stepsview.running_since([{"steps": walk()}]))


class OnThePages(MatchTestCase):
    def page(self, document, path=""):
        self.assertIn(self.put_match(document).status_code, (200, 201))
        return self.client.get(f"/matches/{ID}{path}").content.decode()

    def test_a_failed_start_has_its_verdict_cards_and_no_tiles(self):
        page = self.page(unstarted(verdict={"blame": "engine", "summary": "Dev broke it", "detail": "Base was fine.",
                                            "where": "src/a.cpp:1", "seen_on": ["hunt"]}))
        self.assertIn("Stopped: dev broke it", page)
        self.assertIn("Deletes itself", page)
        self.assertIn('class="node st-engine"', page)
        self.assertIn("mobility.cpp:212: error", page)
        self.assertIn("Base compiled beside it.", page)
        self.assertIn("The engines", page)
        self.assertNotIn('class="tiles"', page)

    @override_settings(NYAYO_FAILED_PRECHECKS_HOURS=10)
    def test_when_it_deletes_itself_follows_the_setting(self):
        document = unstarted()
        document["match"]["finished_at"] = datetime.now(timezone.utc).isoformat()
        self.assertRegex(self.page(document), r"in (9 h 59|10 h)")

    def test_a_waiting_test_with_no_worker_says_so(self):
        page = self.page(unstarted("waiting", steps=[]))
        self.assertIn("hunt", page)
        document = unstarted("waiting")
        document["machines"] = []
        self.assertIn("No worker has picked it up yet", self.page(document))

    def test_a_match_without_steps_keeps_the_table_and_the_tiles(self):
        document = v1()
        document["machines"] = [{"name": "hunt", "games": 5}]
        page = self.page(document)
        self.assertIn('<table class="workers">', page)
        self.assertIn('class="tiles"', page)
        self.assertNotIn('<section class="card wcard', page)

    def test_elapsed_counts_from_the_first_running_step(self):
        document = v1()
        document["match"]["updated_at"] = datetime.now(timezone.utc).isoformat()
        document["machines"] = [{"name": "hunt", "games": 5, "steps": [
            {"step": "picked_up", "state": "passed", "at": at(7200)},
            {"step": "running", "state": "active",
             "at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()}]}]
        page = self.page(document)
        self.assertIn("Playing since", page)
        self.assertRegex(page, r"Elapsed</dt><dd>(9|10) m")

    def test_the_fragment_redraws_the_area(self):
        self.page(unstarted("waiting", steps=[{"step": "compile", "side": "dev", "state": "active", "at": at(5)}]))
        data = self.client.get(f"/matches/{ID}/workers").json()
        self.assertTrue(data["open"])
        self.assertEqual(data["state"], "waiting")
        self.assertIn("Compiling dev", data["html"])

    def test_a_waiting_or_held_test_has_not_stopped(self):
        for state in ("waiting", "held"):
            page = self.page(unstarted(state))
            self.assertNotIn("<dt>Stopped</dt>", page)
            self.assertNotIn("<dt>Ended</dt>", page)
            self.assertIn("<dt>Last report</dt>", page)

    def test_commit_links(self):
        document = v1()
        document["subject"]["repo"] = "looak/elephant"
        page = self.page(document)
        self.assertIn('href="https://github.com/looak/elephant/commit/abc123"', page)

    def test_the_list_shows_the_three_apart_and_the_live_count_leaves_them_out(self):
        for number, state in enumerate(("waiting", "held", "failed_prechecks")):
            document = unstarted(state)
            document["match"]["id"] = f"unstarted-{number}"
            self.assertIn(self.put_match(document).status_code, (200, 201))
        page = self.client.get("/tests").content.decode()
        self.assertIn("Waiting for a worker", page)
        self.assertIn("Failed their start", page)
        self.assertIn("Held, they need a look", page)
        self.assertEqual(page.count('<div class="walk-line">'), 3)
        self.assertNotIn(" live</span>", page)
        for state in ("waiting", "held", "failed_prechecks"):
            self.assertEqual(self.client.get("/tests", {"state": state}).status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)
