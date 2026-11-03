import copy
import gzip
import json

from nyayo.matches.models import Engine, Match, MatchResult, MatchSnapshot

from .helpers import NyayoTestCase, body

GAUNTLET = {
    "schema_version": 1,
    "match": {
        "id": "gauntlet-test-1", "kind": "gauntlet", "state": "running", "name": "elephant-1.0.0-ccrl-blitz",
        "producer": {"name": "uwanja", "version": "0.1.0"},
        "started_at": "2026-10-15T08:00:00Z", "updated_at": "2026-10-15T08:30:00Z", "finished_at": None,
        "host": {"name": "ryzen", "logical_cores": 32},
        "settings": {"tc": "120+1", "book": "8moves_v3.pgn", "concurrency": 14},
    },
    "subject": {"name": "Elephant", "version": "1.0.0", "commit": None, "bench": 123456,
                "options": {"Threads": 1, "Hash": 64}},
    "opponents": [
        {"id": "barbarossa", "name": "Barbarossa", "version": "0.6.0", "options": {"Threads": 1},
         "rating": {"value": 2465, "list": "CCRL Blitz", "as_of": "2026-09-01"}},
        {"id": "belette", "name": "Belette", "version": "1.0", "rating": None},
    ],
    "results": [
        {"opponent": "barbarossa", "games": 10, "wins": 5, "draws": 3, "losses": 2, "penta": [0, 1, 2, 2, 0],
         "timelosses": {"subject": 0, "opponent": 1}, "crashes": {"subject": 0, "opponent": 0},
         "illegal": {"subject": 0, "opponent": 0}},
    ],
    "sprt": None,
    "machines": [],
}

ID = "openbench-openbench-123"


def v1(**changes):
    """OpenBench's test as match schema v1 (docs/match-schema.md, OpenBench)."""
    document = {
        "schema_version": 1,
        "match": {
            "id": ID, "kind": "sprt", "state": "running", "name": "feature-x vs main",
            "producer": {"name": "OpenBench", "version": "openbench", "url": "http://172.16.100.11:8000/test/123/"},
            "started_at": "2026-10-16T10:00:00Z", "updated_at": "2026-10-16T12:34:56Z", "finished_at": None,
            "host": {"name": "openbench"},
            "settings": {"tc": "8+0.08", "book": "book", "runner": {"name": "cutechess-ob"}, "pgn_options": [],
                         "openbench": {}},
        },
        "subject": {"name": "Elephant", "branch": "feature-x", "commit": "abc123", "bench": 2000,
                    "options": {"Threads": "1", "Hash": "64"}},
        "opponents": [{"id": "base", "name": "Elephant", "branch": "main", "commit": "def456", "bench": 1800,
                       "options": {"Threads": "1"}}],
        "results": [{"opponent": "base", "games": 20, "wins": 10, "draws": 7, "losses": 3, "penta": [0, 1, 6, 2, 1],
                     "timelosses": {"subject": 0, "opponent": 0}, "crashes": {"subject": 1, "opponent": 0},
                     "illegal": {"subject": 0, "opponent": 0}}],
        "sprt": {"elo0": 0.0, "elo1": 5.0, "alpha": 0.05, "beta": 0.05, "model": "penta", "llr": 0.55,
                 "lower": -2.94, "upper": 2.94, "result": None},
        "stats": {"elo": 21.74, "error": 114.47, "model": "penta"},
        "machines": [{"name": "hunt", "games": 20}],
    }
    for path, value in changes.items():
        target = document
        *parents, last = path.split("__")
        for part in parents:
            target = target[part]
        target[last] = value
    return document


def gauntlet(**changes):
    document = copy.deepcopy(GAUNTLET)
    for path, value in changes.items():
        target = document
        *parents, last = path.split("__")
        for part in parents:
            target = target[int(part)] if part.isdigit() else target[part]
        target[int(last) if last.isdigit() else last] = value
    return document


class MatchTestCase(NyayoTestCase):
    def put_match(self, document, token=None, match_id=None, gzipped=False, raw=None):
        data = raw if raw is not None else body(document)
        headers = {"Authorization": f"Bearer {token or self.token}"}
        if gzipped:
            data = gzip.compress(data)
            headers["Content-Encoding"] = "gzip"
        match_id = match_id or document["match"]["id"]
        return self.client.put(f"/api/matches/{match_id}", data=data, content_type="application/json",
                               headers=headers)

    def assertRefused(self, response, status, containing):
        self.assertEqual(response.status_code, status, response.content)
        self.assertIn(containing, response.content.decode())


class CreateAndUpdate(MatchTestCase):
    def test_create_indexes_the_match(self):
        response = self.put_match(gauntlet())
        self.assertEqual(response.status_code, 201, response.content)
        match = Match.objects.get()
        self.assertEqual((match.kind, match.state, match.producer_name), ("gauntlet", "running", "uwanja"))
        self.assertEqual(match.machine, self.machine)
        self.assertEqual(match.subject.name, "Elephant")
        self.assertEqual(match.subject_info, {"bench": 123456, "options": {"Threads": 1, "Hash": 64}})
        rows = list(match.results.all())
        self.assertEqual([r.opponent for r in rows], ["barbarossa", "belette"])
        self.assertEqual((rows[0].games, rows[0].wins, rows[0].penta), (10, 5, [0, 1, 2, 2, 0]))
        self.assertEqual(rows[0].rating["value"], 2465)
        self.assertEqual(rows[0].timelosses, {"subject": 0, "opponent": 1})
        # an opponent that hasn't played yet still has a row, at zero
        self.assertEqual((rows[1].games, rows[1].penta), (0, None))

    def test_archive_holds_the_raw_bytes(self):
        raw = body(gauntlet())
        self.put_match(None, raw=raw, match_id="gauntlet-test-1")
        self.assertEqual((self.archive / "matches" / "gauntlet-test-1.json").read_bytes(), raw)
        meta = json.loads((self.archive / "matches" / "gauntlet-test-1.meta.json").read_text())
        self.assertEqual(meta["machine"], "babylon")

    def test_gzip_body(self):
        self.assertEqual(self.put_match(gauntlet(), gzipped=True).status_code, 201)

    def test_progress_is_an_update(self):
        self.put_match(gauntlet())
        grown = gauntlet()
        grown["match"]["updated_at"] = "2026-10-15T09:00:00Z"
        grown["results"][0].update(games=14, wins=7, draws=4, losses=3, penta=[0, 1, 3, 3, 0])
        grown["results"].append({"opponent": "belette", "games": 2, "wins": 1, "draws": 1, "losses": 0})
        response = self.put_match(grown)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Match.objects.count(), 1)
        row = MatchResult.objects.get(opponent="barbarossa")
        self.assertEqual((row.games, row.wins), (14, 7))
        self.assertEqual(MatchResult.objects.get(opponent="belette").games, 2)
        self.assertEqual(Match.objects.get().updated_at.hour, 9)

    def test_finishing_and_restarting(self):
        self.put_match(gauntlet())
        finished = gauntlet()
        finished["match"].update(state="finished", finished_at="2026-10-15T10:00:00Z")
        self.assertEqual(self.put_match(finished).status_code, 200)
        self.assertEqual(Match.objects.get().state, "finished")
        # no state is final, a restart clears finished_at
        restarted = gauntlet()
        restarted["match"]["updated_at"] = "2026-10-15T11:00:00Z"
        self.assertEqual(self.put_match(restarted).status_code, 200)
        match = Match.objects.get()
        self.assertEqual((match.state, match.finished_at), ("running", None))

    def test_engines_are_shared(self):
        self.put_match(gauntlet())
        other = gauntlet()
        other["match"]["id"] = "gauntlet-test-2"
        self.put_match(other)
        self.assertEqual(Engine.objects.filter(name="Elephant").count(), 1)
        self.assertEqual(Engine.objects.count(), 3)

    def test_openbench_sprt_is_accepted(self):
        response = self.put_match(v1())
        self.assertEqual(response.status_code, 201, response.content)
        match = Match.objects.get(pk=ID)
        self.assertEqual((match.kind, match.state, match.producer_name), ("sprt", "running", "OpenBench"))
        self.assertEqual(match.subject.commit, "abc123")
        self.assertEqual(match.subject_info["branch"], "feature-x")
        self.assertEqual(match.subject_info["options"], {"Threads": "1", "Hash": "64"})
        row = match.results.get()
        self.assertEqual((row.opponent, row.games, row.wins, row.draws, row.losses), ("base", 20, 10, 7, 3))
        self.assertEqual(row.penta, [0, 1, 6, 2, 1])
        self.assertEqual((match.sprt["llr"], match.sprt["lower"], match.sprt["upper"]), (0.55, -2.94, 2.94))

    def test_openbench_sprt_update_grows_counts_and_llr(self):
        self.put_match(v1())
        later = v1()
        later["match"]["updated_at"] = "2026-10-16T12:44:56Z"
        later["results"][0].update(games=22, wins=11, draws=8, losses=3, penta=[0, 1, 7, 2, 1])
        later["sprt"]["llr"] = 0.7
        response = self.put_match(later)
        self.assertEqual(response.status_code, 200, response.content)
        row = MatchResult.objects.get()
        self.assertEqual((row.games, row.wins, row.draws, row.losses), (22, 11, 8, 3))
        self.assertEqual(Match.objects.get().sprt["llr"], 0.7)

    def test_the_old_top_level_shape_is_no_longer_accepted(self):
        response = self.put_match({"id": ID, "kind": "sprt", "state": "running", "updated_at": "2026-10-16T12:34:56Z"},
                                  match_id=ID)
        self.assertRefused(response, 400, "schema_version")


class Snapshots(MatchTestCase):
    def test_first_report_starts_the_graph(self):
        self.put_match(gauntlet())
        snapshot = MatchSnapshot.objects.get()
        self.assertEqual((snapshot.games, snapshot.wins, snapshot.draws, snapshot.losses), (10, 5, 3, 2))
        self.assertEqual(snapshot.counts["barbarossa"], [5, 3, 2])

    def test_a_resend_with_the_same_counts_adds_none(self):
        self.put_match(gauntlet())
        again = gauntlet()
        again["match"]["updated_at"] = "2026-10-15T08:45:00Z"
        self.put_match(again)
        self.assertEqual(MatchSnapshot.objects.count(), 1)

    def test_changed_counts_add_one(self):
        self.put_match(gauntlet())
        grown = gauntlet()
        grown["results"][0].update(games=11, wins=6)
        self.put_match(grown)
        self.assertEqual(MatchSnapshot.objects.count(), 2)
        self.assertEqual(MatchSnapshot.objects.order_by("-pk").first().games, 11)

    def test_llr_is_the_producers(self):
        document = gauntlet()
        document["match"]["kind"] = "sprt"
        document["sprt"] = {"elo0": 0, "elo1": 5, "alpha": 0.05, "beta": 0.05, "model": "normalized",
                            "llr": 1.25, "lower": -2.94, "upper": 2.94, "result": None}
        self.assertEqual(self.put_match(document).status_code, 201)
        self.assertEqual(MatchSnapshot.objects.get().llr, 1.25)


class Refusals(MatchTestCase):
    def test_bad_token(self):
        response = self.put_match(gauntlet(), token="nope")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(Match.objects.count(), 0)

    def test_not_json(self):
        self.assertRefused(self.put_match(None, raw=b"{", match_id="x"), 400, "not JSON")

    def test_id_must_match_the_url(self):
        self.assertRefused(self.put_match(gauntlet(), match_id="other"), 400, "doesn't match the url")

    def test_id_alphabet(self):
        document = gauntlet()
        document["match"]["id"] = "has space"
        self.assertRefused(self.put_match(document, match_id="has%20space"), 400, "match.id")

    def test_id_length(self):
        document = gauntlet()
        document["match"]["id"] = "a" * 129
        self.assertRefused(self.put_match(document), 400, "match.id")

    def test_schema_version(self):
        self.assertRefused(self.put_match(gauntlet(schema_version=2)), 400, "schema_version")

    def test_unknown_state_and_kind(self):
        self.assertRefused(self.put_match(gauntlet(match__state="done")), 400, "match.state")
        self.assertRefused(self.put_match(gauntlet(match__kind="league")), 400, "match.kind")

    def test_result_for_an_unknown_opponent(self):
        document = gauntlet()
        document["results"][0]["opponent"] = "ghost"
        self.assertRefused(self.put_match(document), 400, "isn't in opponents")

    def test_counts_must_add_up(self):
        self.assertRefused(self.put_match(gauntlet(results__0__games=11)), 400, "wins + draws + losses")

    def test_penta_shape(self):
        self.assertRefused(self.put_match(gauntlet(results__0__penta=[1, 2])), 400, "penta")

    def test_duplicate_opponent_ids(self):
        document = gauntlet()
        document["opponents"][1]["id"] = "barbarossa"
        self.assertRefused(self.put_match(document), 400, "used twice")

    def test_timestamp_needs_a_timezone(self):
        document = gauntlet()
        document["match"]["updated_at"] = "2026-10-15T08:30:00"
        self.assertRefused(self.put_match(document), 400, "no timezone")

    def test_refused_documents_are_not_archived(self):
        self.put_match(gauntlet(schema_version=2))
        self.assertFalse((self.archive / "matches").exists())


class FixedAfterReceived(MatchTestCase):
    def setUp(self):
        super().setUp()
        self.put_match(gauntlet())

    def test_settings_cant_change(self):
        document = gauntlet()
        document["match"]["settings"]["tc"] = "60+1"
        self.assertRefused(self.put_match(document), 409, "match.settings.tc changed")

    def test_subject_cant_change(self):
        document = gauntlet()
        document["subject"]["version"] = "1.0.1"
        self.assertRefused(self.put_match(document), 409, "subject.version changed")

    def test_opponents_cant_change(self):
        document = gauntlet()
        document["opponents"][0]["rating"]["value"] = 2500
        self.assertRefused(self.put_match(document), 409, "opponents[0].rating.value changed")

    def test_producer_cant_change(self):
        document = gauntlet()
        document["match"]["producer"]["version"] = "0.2.0"
        self.assertRefused(self.put_match(document), 409, "match.producer.version changed")

    def test_producer_url_may_change(self):
        # A link back, set once a producer knows its own address; OpenBench sent null before NYAYO_OPENBENCH_URL
        document = gauntlet()
        document["match"]["producer"]["url"] = "http://172.16.100.11:8000/test/1/"
        self.assertEqual(self.put_match(document).status_code, 200)
        self.assertEqual(Match.objects.get().producer["url"], "http://172.16.100.11:8000/test/1/")

    def test_kind_cant_change(self):
        document = gauntlet()
        document["match"]["kind"] = "games"
        self.assertRefused(self.put_match(document), 409, "match.kind changed")

    def test_counts_only_grow(self):
        document = gauntlet(results__0__games=9, results__0__wins=4)
        self.assertRefused(self.put_match(document), 409, "results[barbarossa].games went from 10 to 9")

    def test_a_row_cant_vanish(self):
        document = gauntlet()
        document["results"] = []
        self.assertRefused(self.put_match(document), 409, "results for barbarossa are missing")

    def test_side_counts_only_grow(self):
        document = gauntlet(results__0__timelosses={"subject": 0, "opponent": 0})
        self.assertRefused(self.put_match(document), 409, "timelosses.opponent")

    def test_penta_cant_shrink(self):
        document = gauntlet(results__0__penta=[0, 1, 2, 1, 0])
        self.assertRefused(self.put_match(document), 409, "penta")

    def test_refused_update_changes_nothing(self):
        self.put_match(gauntlet(results__0__games=9, results__0__wins=4))
        self.assertEqual(MatchResult.objects.get(opponent="barbarossa").games, 10)
        self.assertEqual(MatchSnapshot.objects.count(), 1)

    def test_counts_are_checked_without_the_archive(self):
        (self.archive / "matches" / "gauntlet-test-1.json").unlink()
        self.assertRefused(self.put_match(gauntlet(results__0__wins=4, results__0__losses=3)), 409, "went from 5 to 4")
        grown = gauntlet(results__0__games=11, results__0__wins=6)
        response = self.put_match(grown)
        self.assertEqual(response.status_code, 200, response.content)


def scheduled():
    document = gauntlet()
    document["progress"] = {"total": 4, "played": 1, "playing": 1}
    document["games"] = [
        {"number": 1, "round": 1, "opponent": "barbarossa", "subject_colour": "white", "state": "finished",
         "result": "1-0", "termination": "adjudication", "fault": None},
        {"number": 2, "round": 1, "opponent": "barbarossa", "subject_colour": "black", "state": "playing",
         "result": None, "termination": None, "fault": None},
        {"number": 3, "round": 2, "opponent": "belette", "subject_colour": "white", "state": "scheduled",
         "result": None, "termination": None, "fault": None},
        {"number": 4, "round": 2, "opponent": "belette", "subject_colour": "black", "state": "scheduled",
         "result": None, "termination": None, "fault": None},
    ]
    return document


class Schedule(MatchTestCase):
    def test_the_schedule_is_indexed(self):
        self.assertEqual(self.put_match(scheduled()).status_code, 201)
        match = Match.objects.get()
        self.assertEqual(len(match.schedule), 4)
        self.assertEqual(match.progress, {"total": 4, "played": 1, "playing": 1})

    def test_games_move_forward(self):
        self.put_match(scheduled())
        later = scheduled()
        later["progress"] = {"total": 4, "played": 2, "playing": 1}
        later["games"][1].update(state="finished", result="1/2-1/2", termination="repetition")
        later["games"][2]["state"] = "playing"
        response = self.put_match(later)
        self.assertEqual(response.status_code, 200, response.content)
        match = Match.objects.get()
        self.assertEqual([g["state"] for g in match.schedule], ["finished", "finished", "playing", "scheduled"])
        self.assertEqual(match.progress["played"], 2)

    def test_a_scheduled_game_may_be_corrected_and_the_list_extended(self):
        self.put_match(scheduled())
        later = scheduled()
        later["games"][3]["subject_colour"] = "white"
        later["games"].append({"number": 5, "round": 3, "opponent": "belette", "subject_colour": "black",
                               "state": "scheduled", "result": None, "termination": None, "fault": None})
        self.assertEqual(self.put_match(later).status_code, 200)
        self.assertEqual(len(Match.objects.get().schedule), 5)

    def test_a_started_game_keeps_its_colour(self):
        self.put_match(scheduled())
        later = scheduled()
        later["games"][1]["subject_colour"] = "white"
        self.assertRefused(self.put_match(later), 409, "games[2].subject_colour changed")

    def test_a_finished_game_keeps_its_result(self):
        self.put_match(scheduled())
        later = scheduled()
        later["games"][0]["result"] = "0-1"
        self.assertRefused(self.put_match(later), 409, "games[1] finished with 1-0")

    def test_a_started_game_cant_vanish_or_unstart(self):
        self.put_match(scheduled())
        later = scheduled()
        del later["games"][1]
        self.assertRefused(self.put_match(later), 409, "games[2] is gone")

    def test_a_playing_game_may_be_scheduled_again_by_a_resume(self):
        self.put_match(scheduled())
        later = scheduled()
        later["games"][1]["state"] = "scheduled"
        self.assertEqual(self.put_match(later).status_code, 200)

    def test_validation(self):
        document = scheduled()
        document["games"][0]["opponent"] = "ghost"
        self.assertRefused(self.put_match(document), 400, "games[0].opponent")
        document = scheduled()
        document["games"][1]["number"] = 1
        self.assertRefused(self.put_match(document), 400, "used twice")
        document = scheduled()
        document["games"][0]["result"] = None
        self.assertRefused(self.put_match(document), 400, "no result")

    def test_the_schedule_names_a_games_opponent_and_colour(self):
        from nyayo.matches.models import Game
        self.put_match(scheduled())
        pgn = b'[White "?"]\n[Black "?"]\n[Result "*"]\n\n1. e4 *\n'
        response = self.client.put("/api/matches/gauntlet-test-1/games/4", data=pgn,
                                   content_type="application/x-chess-pgn",
                                   headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(response.status_code, 201, response.content)
        game = Game.objects.get()
        self.assertEqual((game.opponent, game.colour), ("belette", "black"))


STEPS = [
    {"step": "picked_up", "state": "passed", "at": "2026-11-03T09:00:04Z"},
    {"step": "compile", "side": "dev", "state": "passed", "at": "2026-11-03T09:00:50Z", "seconds": 41,
     "cached": False},
    {"step": "bench", "side": "dev", "state": "failed", "at": "2026-11-03T09:01:30Z", "seconds": 38,
     "detail": "Wrong bench: 53681270, expected 53681269", "log": None, "tries": 1, "blame": "engine",
     "evidence": [{"points_at": "engine", "text": "Base matched its bench on this worker."}],
     "effect": "The test stops.", "retry_at": "2026-11-03T09:06:30Z", "nodes": 53681270, "nps": 1200000},
    {"step": "running", "state": "pending"},
]
VERDICT = {"blame": "engine", "summary": "Dev doesn't compile", "detail": "Base compiled beside it.",
           "where": "src/eval/mobility.cpp:212", "seen_on": ["hunt", "tor"]}


class StepsAndVerdicts(MatchTestCase):
    def test_each_new_state_is_taken(self):
        for state in ("waiting", "held", "failed_prechecks"):
            response = self.put_match(v1(match__state=state))
            self.assertIn(response.status_code, (200, 201), response.content)
            self.assertEqual(Match.objects.get().state, state)

    def test_an_unknown_state_is_still_refused(self):
        self.assertRefused(self.put_match(v1(match__state="queued")), 400, "match.state must be one of")

    def test_verdict_and_steps_are_stored(self):
        document = v1(match__state="failed_prechecks", match__verdict=VERDICT,
                      machines=[{"name": "hunt", "games": 0, "workloads": 1, "active": True, "steps": STEPS}])
        self.assertEqual(self.put_match(document).status_code, 201)
        match = Match.objects.get()
        self.assertEqual(match.verdict, VERDICT)
        self.assertEqual(match.machines[0]["steps"], STEPS)

    def test_verdict_and_steps_may_change_and_go(self):
        self.put_match(v1(match__state="held", match__verdict=VERDICT,
                          machines=[{"name": "hunt", "games": 0, "steps": STEPS[:1]}]))
        later = v1(match__updated_at="2026-11-03T10:00:00Z",
                   machines=[{"name": "hunt", "games": 0, "workloads": 2, "active": False, "steps": STEPS}])
        self.assertEqual(self.put_match(later).status_code, 200)
        match = Match.objects.get()
        self.assertIsNone(match.verdict)
        self.assertEqual((match.state, len(match.machines[0]["steps"])), ("running", 4))

    def test_a_rename_is_an_update(self):
        self.put_match(v1())
        self.assertEqual(self.put_match(v1(match__name="Add mobility (abc123)")).status_code, 200)
        self.assertEqual(Match.objects.get().name, "Add mobility (abc123)")

    def test_other_fixed_fields_are_still_fixed(self):
        self.put_match(v1())
        self.assertRefused(self.put_match(v1(match__kind="games")), 409, "match.kind changed")

    def test_older_documents_are_unchanged(self):
        self.assertEqual(self.put_match(v1()).status_code, 201)
        match = Match.objects.get()
        self.assertEqual((match.verdict, match.machines), (None, [{"name": "hunt", "games": 20}]))

    def test_malformed_additions_are_refused(self):
        def machine(**fields):
            return v1(machines=[{"name": "hunt", "games": 0, **fields}])

        def step(**fields):
            return machine(steps=[{"step": "compile", "state": "passed", **fields}])

        cases = [
            (v1(match__verdict="engine"), "match.verdict must be an object"),
            (v1(match__verdict={"blame": "me"}), "match.verdict.blame"),
            (v1(match__verdict={"blame": "engine", "seen_on": "hunt"}), "match.verdict.seen_on"),
            (machine(workloads=-1), "machines[0].workloads"),
            (machine(active="yes"), "machines[0].active"),
            (machine(steps={}), "machines[0].steps must be a list"),
            (machine(steps=["compile"]), "machines[0].steps[0] must be an object"),
            (step(state="done"), "machines[0].steps[0].state"),
            (step(side="both"), "machines[0].steps[0].side"),
            (step(at="yesterday"), "machines[0].steps[0].at"),
            (step(blame="me"), "machines[0].steps[0].blame"),
            (step(tries=-1), "machines[0].steps[0].tries"),
            (step(evidence=[{"text": "x"}]), "machines[0].steps[0].evidence[0]"),
            (step(log="x" * 4097), "machines[0].steps[0].log is over"),
        ]
        for document, containing in cases:
            with self.subTest(containing):
                self.assertRefused(self.put_match(document), 400, containing)
        self.assertEqual(Match.objects.count(), 0)
