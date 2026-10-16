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

OPENBENCH_SPRT = {
    "schema_version": 1,
    "id": "openbench-bobby-123",
    "kind": "sprt",
    "state": "running",
    "updated_at": "2026-10-16T12:34:56Z",
    "producer": {"kind": "OpenBench", "url": "/test/123/", "instance": "bobby"},
    "subject": {"engine": "Dev Engine", "name": "Dev Engine", "repo": "https://example.com/dev.git", "sha": "abc123",
                "bench": 2000, "options": "Threads=1 Hash=64", "time_control": "40/20", "network": "dev-net",
                "book": "book"},
    "opponents": [{"engine": "Base Engine", "name": "Base Engine", "repo": "https://example.com/base.git",
                   "sha": "def456", "bench": 1800, "options": "Threads=1", "time_control": "40/20",
                   "network": "base-net"}],
    "results": [{"games": 18, "wins": 9, "losses": 3, "draws": 6, "LL": 12, "LD": 8, "DD": 5, "DW": 4, "WW": 3,
                 "crashes": 1, "timeloss": 2}],
    "sprt": {"lower": -5.0, "upper": 5.0, "alpha": 0.05, "beta": 0.05, "current_llr": 0.4, "lower_llr": -1.0,
             "upper_llr": 1.0, "result": None},
    "settings": {"dev_time_control": "40/20", "base_time_control": "40/20", "book_name": "book",
                 "adjudication": "movecount=3 score=400", "scale_method": "BASE", "scale_nps": 0},
    "machines": [{"machine_id": 7, "user": "alice", "games": 18, "wins": 9, "losses": 3, "draws": 6, "crashes": 1,
                  "timeloss": 2, "updated_at": "2026-10-16T12:34:56Z"}],
    "error": False,
    "finished": False,
}


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

    def test_openbench_sprt_payload_is_accepted(self):
        response = self.put_match(OPENBENCH_SPRT, match_id="openbench-bobby-123")
        self.assertEqual(response.status_code, 201, response.content)
        match = Match.objects.get(pk="openbench-bobby-123")
        self.assertEqual((match.kind, match.state, match.producer_name), ("sprt", "running", "OpenBench"))
        self.assertEqual(match.subject.commit, "abc123")
        self.assertEqual(match.subject_info["options"], {"Threads": "1", "Hash": "64"})
        row = match.results.get()
        self.assertEqual((row.games, row.wins, row.draws, row.losses), (18, 9, 6, 3))
        self.assertEqual(row.penta, [12, 8, 5, 4, 3])
        self.assertEqual((match.sprt["llr"], match.sprt["lower"], match.sprt["upper"]), (0.4, -1.0, 1.0))

    def test_openbench_sprt_update_grows_counts_and_llr(self):
        self.put_match(OPENBENCH_SPRT, match_id="openbench-bobby-123")
        later = copy.deepcopy(OPENBENCH_SPRT)
        later["updated_at"] = "2026-10-16T12:44:56Z"
        later["results"][0].update(games=20, wins=10, draws=7, losses=3, LL=12, LD=9, DD=6, DW=4, WW=3)
        later["sprt"]["current_llr"] = 0.55
        response = self.put_match(later, match_id="openbench-bobby-123")
        self.assertEqual(response.status_code, 200, response.content)
        row = MatchResult.objects.get()
        self.assertEqual((row.games, row.wins, row.draws, row.losses), (20, 10, 7, 3))
        self.assertEqual(Match.objects.get().sprt["llr"], 0.55)


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
