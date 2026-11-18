"""A match's `config` (docs/match-schema.md, The config): validated, stored, archived, fixed once received, offered as
a gauntlet.toml on the match page, and read by the catalogue."""

import json
import tomllib

from django.urls import reverse

from nyayo.matches import gauntlet_file
from nyayo.matches.models import Engine, Match

from . import uwanja_config
from .test_matches_ingest import MatchTestCase, gauntlet

GAUNTLET_JSON = {
    "name": "elephant-1.2.0-ccrl-blitz", "tc": "120+1", "book": "../books/UHO_Lichess_4852_v1.epd", "rounds": 50,
    "concurrency": 10, "book_order": "random", "adjudication": {}, "pgn_options": ["nodes", "nps", "pv"],
    "recover": True,
    "subject": {"cmd": "../engines/elephant-1.2.0", "name": "Elephant Gambit", "options": {"Threads": 1, "Hash": 64},
                "id": "elephant-gambit", "bench": None, "rating": None},
    "opponents": [
        {"id": "barbarossa", "url": "https://github.com/o/Barbarossa", "ref": "v0.6.0", "binary": "*/barb\"x",
         "options": {"Hash": 64}, "role": "opponent",
         "rating": {"value": 2465, "list": "CCRL Blitz", "as_of": "2026-09-01", "source": "https://ccrl.example/b"}},
    ],
}


def config(**changes):
    return {"format": "uwanja.gauntlet/1", "gauntlet": GAUNTLET_JSON, "resolved": {"runner": "v1"}, "portable": False,
            **changes}


class Storing(MatchTestCase):
    def test_it_is_stored_and_archived_whole(self):
        document = gauntlet(config=config())
        self.assertEqual(self.put_match(document).status_code, 201)
        self.assertEqual(Match.objects.get().config, config())
        archived = json.loads((self.archive / "matches" / "gauntlet-test-1.json").read_text())
        self.assertEqual(archived["config"], config())

    def test_a_match_without_one_has_none(self):
        self.put_match(gauntlet())
        self.assertIsNone(Match.objects.get().config)

    def test_the_same_config_again_is_an_update(self):
        self.put_match(gauntlet(config=config()))
        self.assertEqual(self.put_match(gauntlet(config=config(), **{"match__state": "finished"})).status_code, 200)

    def test_it_is_validated(self):
        for bad, words in [("text", "config must be an object"), ({"gauntlet": {}}, "config.format"),
                           ({"format": 1}, "config.format"), ({"format": ""}, "config.format"),
                           ([1], "config must be an object")]:
            self.assertRefused(self.put_match(gauntlet(config=bad)), 400, words)

    def test_it_is_at_most_64_kb(self):
        big = {"format": "x", "filler": "a" * (64 * 1024)}
        self.assertRefused(self.put_match(gauntlet(config=big)), 400, "64 KB")
        edge = {"format": "x", "filler": "a" * (64 * 1024 - 40)}
        self.assertEqual(self.put_match(gauntlet(config=edge)).status_code, 201)

    def test_a_changed_config_is_a_409_naming_it(self):
        self.put_match(gauntlet(config=config()))
        self.assertRefused(self.put_match(gauntlet(config=config(portable=True))), 409, "config")
        self.assertRefused(self.put_match(gauntlet()), 409, "config")
        self.assertEqual(Match.objects.get().config["portable"], False)

    def test_a_changed_config_is_a_409_without_the_archive_too(self):
        self.put_match(gauntlet(config=config()))
        (self.archive / "matches" / "gauntlet-test-1.json").unlink()
        self.assertRefused(self.put_match(gauntlet(config=config(portable=True))), 409, "config")

    def test_it_may_arrive_later_once(self):
        self.put_match(gauntlet())
        self.assertEqual(self.put_match(gauntlet(config=config())).status_code, 200)
        self.assertEqual(Match.objects.get().config, config())
        self.assertRefused(self.put_match(gauntlet(config=config(portable=True))), 409, "config")


class Offering(MatchTestCase):
    def page(self, cfg):
        self.put_match(gauntlet(config=cfg) if cfg is not None else gauntlet())
        return self.client.get(reverse("match", args=["gauntlet-test-1"]))

    def test_the_file_parses_and_meets_uwanjas_rules(self):
        text = gauntlet_file.from_config(config())
        data = tomllib.loads(text)
        uwanja_config.parse(data)
        self.assertEqual(data["subject"], {"cmd": "../engines/elephant-1.2.0", "name": "Elephant Gambit",
                                           "options": {"Threads": 1, "Hash": 64}, "id": "elephant-gambit"})
        self.assertEqual(data["opponent"][0]["binary"], '*/barb"x')
        self.assertEqual(data["opponent"][0]["rating"]["source"], "https://ccrl.example/b")
        self.assertNotIn("role", data["opponent"][0])
        self.assertEqual((data["name"], data["rounds"], data["pgn_options"]), ("elephant-1.2.0-ccrl-blitz", 50, ["nodes", "nps", "pv"]))

    def test_opponent_or_opponents_both_read(self):
        gj = {**GAUNTLET_JSON, "opponent": GAUNTLET_JSON["opponents"]}
        del gj["opponents"]
        self.assertEqual(tomllib.loads(gauntlet_file.from_config(config(gauntlet=gj)))["opponent"][0]["id"], "barbarossa")

    def test_what_isnt_a_gauntlet_gives_no_file(self):
        for cfg in (None, {"format": "openbench/1"}, {"format": "uwanja.gauntlet/1"},
                    {"format": "uwanja.gauntlet/1", "gauntlet": {"subject": {"url": "u"}}},
                    config(gauntlet={**GAUNTLET_JSON, "opponents": []})):
            self.assertIsNone(gauntlet_file.from_config(cfg))

    def test_the_page_offers_it_with_a_note_when_not_portable(self):
        response = self.page(config())
        self.assertContains(response, "Copy as gauntlet.toml")
        self.assertContains(response, "Not portable")
        self.assertIn("[[opponent]]", response.context["config_toml"])

    def test_a_portable_one_has_no_note(self):
        response = self.page(config(portable=True))
        self.assertContains(response, "Copy as gauntlet.toml")
        self.assertNotContains(response, "Not portable")

    def test_a_match_without_a_config_offers_nothing(self):
        self.assertNotContains(self.page(None), "Copy as gauntlet.toml")


class Catalogue(MatchTestCase):
    def test_the_backfill_reads_the_subject_and_opponents_from_the_config(self):
        gj = {**GAUNTLET_JSON, "subject": {"url": "https://github.com/looak/elephant", "ref": "1.2.0",
                                           "options": {"Hash": 32}}}
        self.put_match(gauntlet(config=config(gauntlet=gj), **{"opponents__0__id": "barbarossa"}))
        subject = Engine.objects.get(name="Elephant")
        self.assertEqual((subject.url, subject.ref, subject.options), ("https://github.com/looak/elephant", "1.2.0",
                                                                      {"Hash": 32}))
        opponent = Engine.objects.get(name="Barbarossa")
        self.assertEqual((opponent.url, opponent.ref), ("https://github.com/o/Barbarossa", "v0.6.0"))
        self.assertEqual(opponent.rating["value"], 2465)
