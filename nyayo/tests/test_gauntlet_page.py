"""The gauntlet page: the file it builds (uwanja's format, escaped, every rule of uwanja's loader met), picking opponents
by hand, by name and by a rating range, the download, and what is wrong with a form."""

import tomllib

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from nyayo.matches import gauntlet_file as gf
from nyayo.matches.models import Engine

from . import uwanja_config


def rating(value, source=None):
    out = {"value": value, "list": "CCRL Blitz", "as_of": "2026-10-18"}
    if source:
        out["source"] = source
    return out


class Writer(SimpleTestCase):
    def roundtrip(self, item):
        return tomllib.loads(f"x = {gf.value(item)}")["x"]

    def test_strings_survive_whatever_they_hold(self):
        for text in ["plain", 'a "quote"', "back\\slash", "line\nbreak", "tab\tand\r\nreturn", "del\x7fete", "nul\x00",
                     "ünïcödé ♞", "it's", "# not a comment", "[[opponent]]", "x = 1\n[evil]", " ", ""]:
            self.assertEqual(self.roundtrip(text), text, repr(text))

    def test_keys_are_quoted_when_they_need_to_be(self):
        options = {"Hash": 64, "Move Overhead": 10, "a.b": 1, 'q"': 2}
        data = tomllib.loads(f"options = {gf.table(options)}")
        self.assertEqual(data["options"], options)

    def test_numbers_booleans_and_lists(self):
        self.assertEqual(self.roundtrip({"a": 1, "b": 2.5, "c": 3.0, "d": True, "e": False, "f": [1, "x"]}),
                         {"a": 1, "b": 2.5, "c": 3, "d": True, "e": False, "f": [1, "x"]})

    def test_what_toml_cant_say_is_refused(self):
        with self.assertRaises(ValueError):
            gf.value(float("nan"))


class Setup(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owner = User.objects.create_user("owner", password="x", is_staff=True)
        cls.elephant = Engine.objects.create(
            name="Elephant Gambit", version="1.2.0", url="https://github.com/looak/elephant", ref="1.2.0",
            ours={"value": 2700.0, "error": 12.0, "games": 500, "as_of": "2026-11-18", "flagged": False, "z": None})

        def make(name, ref, value, **extra):
            return Engine.objects.create(
                name=name, version=ref.lstrip("v"), url=f"https://github.com/o/{name}", ref=ref,
                rating=rating(value, f"https://ccrl.example/{name}") if value else None, **extra)

        cls.halogen = make("Halogen", "v6", 2410, options={"Threads": 1, "Hash": 64})
        cls.zahak = make("Zahak", "3.0", 2640, options={"Hash": 64})
        cls.lynx = make("Lynx", "v1.0.1", 2780)
        cls.far = make("Far", "v1", 3300)
        cls.unrated = make("Unrated", "v1", None)
        cls.no_release = Engine.objects.create(name="Sirius", version="8", rating=rating(2700))

    def setUp(self):
        self.client.force_login(self.owner)

    def get(self, **query):
        return self.client.get(reverse("gauntlet"), query)

    def file(self, response):
        self.assertIsNotNone(response.context["text"], response.context["problems"])
        return tomllib.loads(response.context["text"])


class Page(Setup):
    def test_it_is_for_staff(self):
        self.client.logout()
        self.assertEqual(self.get().status_code, 302)

    def test_the_first_visit_has_the_newest_elephant_and_the_defaults(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="120+1"')
        self.assertContains(response, 'value="../books/UHO_Lichess_4852_v1.epd"')
        self.assertContains(response, 'value="50"')
        self.assertContains(response, 'value="10"')
        self.assertContains(response, f'<option value="{self.elephant.pk}" selected>')
        self.assertIsNone(response.context["text"])
        self.assertEqual(response.context["problems"], [])

    def test_only_rows_with_a_release_are_offered_and_not_the_subject(self):
        names = {r.name for r in self.get(subject="cmd").context["candidates"]}
        self.assertEqual(names, {"Elephant Gambit", "Halogen", "Zahak", "Lynx", "Far", "Unrated"})
        names = {r.name for r in self.get(subject=self.elephant.pk).context["candidates"]}
        self.assertEqual(names, {"Halogen", "Zahak", "Lynx", "Far", "Unrated"})

    def test_the_opponents_show_list_rating_and_ours(self):
        Engine.objects.filter(pk=self.zahak.pk).update(
            ours={"value": 2655.0, "error": 14.0, "games": 100, "as_of": "x", "flagged": False, "z": 0.1})
        page = self.get(subject=self.elephant.pk).content.decode()
        self.assertIn("2410", page)
        self.assertIn("2655", page)
        self.assertIn("https://ccrl.example/Halogen", page)

    def test_a_file_from_picked_opponents(self):
        response = self.get(subject=self.elephant.pk, opp=[self.halogen.pk, self.zahak.pk], name="elephant-test",
                            tc="60+1", rounds="20", concurrency="4", book="b.epd")
        data = self.file(response)
        self.assertEqual({k: data[k] for k in ("name", "tc", "book", "rounds", "concurrency")},
                         {"name": "elephant-test", "tc": "60+1", "book": "b.epd", "rounds": 20, "concurrency": 4})
        self.assertEqual(data["subject"], {"url": "https://github.com/looak/elephant", "ref": "1.2.0",
                                           "options": {"Threads": 1, "Hash": 64}})
        first, second = data["opponent"]
        self.assertEqual(first, {"url": "https://github.com/o/Halogen", "ref": "v6", "id": "halogen",
                                 "options": {"Threads": 1, "Hash": 64},
                                 "rating": {"value": 2410, "list": "CCRL Blitz", "as_of": "2026-10-18",
                                            "source": "https://ccrl.example/Halogen"}})
        self.assertEqual(second["options"], {"Hash": 64})
        self.assertNotIn("rating", data["subject"])

    def test_every_file_meets_uwanjas_rules(self):
        picked = [r.pk for r in (self.halogen, self.zahak, self.lynx, self.unrated)]
        subject, others = uwanja_config.parse(self.file(self.get(subject=self.elephant.pk, opp=picked)))
        self.assertEqual(subject, "elephant")
        self.assertEqual(others, ["halogen", "lynx", "unrated", "zahak"])

    def test_the_fixture_refuses_what_uwanja_does(self):
        data = self.file(self.get(subject=self.elephant.pk, opp=[self.halogen.pk]))
        for change in (lambda d: d["subject"].update(rating=2000), lambda d: d["subject"].update(cmd="x"),
                       lambda d: d["opponent"][0].update(oops=1), lambda d: d.update(rounds=0),
                       lambda d: d["opponent"][0]["rating"].update(oops=1), lambda d: d.update(opponent=[])):
            broken = tomllib.loads(gf_text(data))
            change(broken)
            with self.assertRaises(uwanja_config.ConfigError):
                uwanja_config.parse(broken)

    def test_an_opponent_without_a_rating_has_none_in_the_file(self):
        data = self.file(self.get(subject=self.elephant.pk, opp=[self.unrated.pk]))
        self.assertNotIn("rating", data["opponent"][0])

    def test_ids_stay_unique_for_two_versions_of_one_engine(self):
        other = Engine.objects.create(name="Halogen", version="5", url="https://github.com/o/Halogen", ref="v5")
        data = self.file(self.get(subject=self.elephant.pk, opp=[self.halogen.pk, other.pk]))
        self.assertEqual([o["id"] for o in data["opponent"]], ["halogen", "halogen-v6"])
        uwanja_config.parse(data)

    def test_an_opponent_named_like_the_subject_doesnt_take_its_id(self):
        clone = Engine.objects.create(name="Elephant", version="1.0.0", url="https://github.com/looak/elephant",
                                      ref="1.0.0")
        data = self.file(self.get(subject=self.elephant.pk, opp=[clone.pk]))
        self.assertEqual(data["opponent"][0]["id"], "elephant-1.0.0")
        uwanja_config.parse(data)

    def test_a_local_engine_as_the_subject(self):
        data = self.file(self.get(subject="cmd", cmd="../engines/elephant-1.2.0", subject_name="Elephant Gambit",
                                  opp=[self.halogen.pk]))
        self.assertEqual(data["subject"], {"cmd": "../engines/elephant-1.2.0", "name": "Elephant Gambit",
                                           "options": {"Threads": 1, "Hash": 64}})
        uwanja_config.parse(data)

    def test_a_local_engine_needs_a_command_and_a_name(self):
        self.assertIn("The command", " ".join(self.get(subject="cmd", opp=[self.halogen.pk]).context["problems"]))
        problems = self.get(subject="cmd", cmd="x", opp=[self.halogen.pk]).context["problems"]
        self.assertIn("needs a name", " ".join(problems))

    def test_typed_options_replace_the_defaults(self):
        data = self.file(self.get(subject=self.elephant.pk, opp=[self.halogen.pk],
                                  subject_options='{"Hash": 128, "Move Overhead": "10"}'))
        self.assertEqual(data["subject"]["options"], {"Hash": 128, "Move Overhead": "10"})

    def test_nasty_text_cannot_change_the_file(self):
        evil = Engine.objects.create(
            name='Evil"\nname = "x"', version="1", url="https://github.com/o/evil", ref='v1"\n[x]',
            options={"Hash": 1, 'a"b': "c\nd"}, rating=rating(2500, 'https://a/b"\n[evil]'))
        data = self.file(self.get(subject=self.elephant.pk, opp=[evil.pk], book='a\\b "c"', name="ok"))
        self.assertEqual(set(data), {"name", "tc", "book", "rounds", "concurrency", "subject", "opponent"})
        self.assertEqual(data["opponent"][0]["ref"], 'v1"\n[x]')
        self.assertEqual(data["opponent"][0]["options"], {"Hash": 1, 'a"b': "c\nd"})
        self.assertEqual(data["opponent"][0]["rating"]["source"], 'https://a/b"\n[evil]')
        self.assertEqual(data["book"], 'a\\b "c"')
        uwanja_config.parse(data)

    def test_a_form_that_is_wrong_says_so_and_builds_nothing(self):
        for query, words in [
            (dict(subject=self.elephant.pk), "at least one opponent"),
            (dict(subject=self.elephant.pk, opp=[self.halogen.pk], rounds="0"), "Rounds"),
            (dict(subject=self.elephant.pk, opp=[self.halogen.pk], concurrency="x"), "Concurrency"),
            (dict(subject=self.elephant.pk, opp=[self.halogen.pk], name="a b"), "letters, digits"),
            (dict(subject=self.elephant.pk, opp=[self.halogen.pk], subject_options="[1]"), "JSON object"),
            (dict(subject="999999", opp=[self.halogen.pk]), "Pick the engine"),
        ]:
            response = self.get(**query)
            self.assertIsNone(response.context["text"], query)
            self.assertIn(words, " ".join(response.context["problems"]), query)

    def test_the_subject_is_never_an_opponent(self):
        response = self.get(subject=self.elephant.pk, opp=[self.elephant.pk, self.halogen.pk])
        self.assertEqual(len(self.file(response)["opponent"]), 1)

    def test_the_default_name_comes_from_the_subject(self):
        self.assertEqual(self.file(self.get(subject=self.elephant.pk, opp=[self.halogen.pk]))["name"],
                         "elephant-gambit-1.2.0-gauntlet")

    def test_the_download_is_the_same_file(self):
        query = dict(subject=self.elephant.pk, opp=[self.halogen.pk], name="mine")
        shown = self.get(**query).context["text"]
        response = self.get(download="1", **query)
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="mine.toml"')
        self.assertTrue(response["Content-Type"].startswith("application/toml"))
        self.assertEqual(response.content.decode(), shown)

    def test_the_page_offers_the_copy_and_the_download(self):
        page = self.get(subject=self.elephant.pk, opp=[self.halogen.pk]).content.decode()
        self.assertIn("download=1", page)
        self.assertIn('data-copy-target="copy-gauntlet-file"', page)

    def test_a_download_with_problems_is_the_page(self):
        response = self.get(download="1", subject=self.elephant.pk)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response["Content-Type"])


def gf_text(data):
    """A parsed file back to TOML text, through the page's own writer (only for the fixture's test)."""
    head = {k: v for k, v in data.items() if k not in ("subject", "opponent")}
    from nyayo.matches.gauntlet_file import Engine as Entry, dump
    return dump(head, Entry(data["subject"]), [Entry(o) for o in data["opponent"]])


class Picking(Setup):
    def test_a_range_around_the_subjects_ours(self):
        response = self.get(subject=self.elephant.pk, pick="range", radius="150")
        self.assertEqual(response.context["picked"], {str(self.zahak.pk), str(self.lynx.pk)})
        self.assertIn("2 opponents within 150 of 2700", response.context["notice"])
        self.assertEqual([o["ref"] for o in self.file(response)["opponent"]], ["v1.0.1", "3.0"])

    def test_the_range_is_the_one_typed(self):
        response = self.get(subject=self.elephant.pk, pick="range", radius="400")
        self.assertEqual(response.context["picked"], {str(r.pk) for r in (self.halogen, self.zahak, self.lynx)})
        self.assertEqual(self.get(subject=self.elephant.pk, pick="range", radius="0").context["picked"], set())

    def test_a_subject_without_ours_takes_a_typed_centre(self):
        Engine.objects.filter(pk=self.elephant.pk).update(ours=None)
        response = self.get(subject=self.elephant.pk, pick="range")
        self.assertIn("needs a centre", " ".join(response.context["problems"]))
        response = self.get(subject=self.elephant.pk, pick="range", centre="2400", radius="50")
        self.assertEqual(response.context["picked"], {str(self.halogen.pk)})

    def test_a_local_subject_takes_a_typed_centre(self):
        response = self.get(subject="cmd", cmd="x", subject_name="Mine", pick="range", centre="2780", radius="10")
        self.assertEqual(response.context["picked"], {str(self.lynx.pk)})

    def test_an_opponent_with_no_list_rating_is_placed_by_ours(self):
        Engine.objects.filter(pk=self.unrated.pk).update(
            ours={"value": 2690.0, "error": 30.0, "games": 10, "as_of": "x", "flagged": False, "z": None})
        response = self.get(subject=self.elephant.pk, pick="range", radius="50")
        self.assertEqual(response.context["picked"], {str(self.unrated.pk)})

    def test_by_name_adds_to_the_boxes_ticked(self):
        Engine.objects.filter(pk=self.lynx.pk).update(aliases=["lynx chess"])
        response = self.get(subject=self.elephant.pk, pick="name", q="halo, CHESS", opp=[self.far.pk])
        self.assertEqual(response.context["picked"], {str(r.pk) for r in (self.halogen, self.lynx, self.far)})
        self.assertIn("2 opponents match", response.context["notice"])

    def test_picked_boxes_come_back_ticked(self):
        page = self.get(subject=self.elephant.pk, opp=[self.far.pk]).content.decode()
        self.assertIn(f'id="opp-{self.far.pk}" checked', page)
        self.assertNotIn(f'id="opp-{self.halogen.pk}" checked', page)


class AgainstOurs(TestCase):
    """A gauntlet's page shows its performance against the opponents' ratings of ours, beside the listed one."""

    def setUp(self):
        from nyayo.matches.models import Match, MatchResult
        self.subject = Engine.objects.create(name="Elephant", version="1")
        self.match = Match.objects.create(id="g1", kind="gauntlet", state="finished", producer={"name": "t"},
                                          subject=self.subject)
        self.rows = []
        for ordinal, (name, listed, ours) in enumerate([("A", 2400, 2410.0), ("B", 2600, 2590.0)]):
            engine = Engine.objects.create(
                name=name, rating=rating(listed),
                ours={"value": ours, "error": 10.0, "games": 100, "as_of": "x", "flagged": False, "z": 0} if ours else None)
            MatchResult.objects.create(match=self.match, ordinal=ordinal, opponent=name.lower(), engine=engine, games=100,
                                       wins=50, draws=0, losses=50, rating=rating(listed))

    def page(self):
        return self.client.get(reverse("match", args=["g1"]))

    def test_it_sits_beside_the_listed_performance(self):
        response = self.page()
        # an even score: the performance is the opponents' average rating
        self.assertAlmostEqual(response.context["summary"].performance, 2500)
        self.assertAlmostEqual(response.context["performance_ours"], 2500)
        self.assertContains(response, "2500 against ours")

    def test_it_follows_ours_not_the_listed(self):
        Engine.objects.filter(name="A").update(ours={"value": 2450.0, "error": 10.0, "games": 100, "as_of": "x",
                                                     "flagged": False, "z": 0})
        response = self.page()
        self.assertAlmostEqual(response.context["performance_ours"], 2520)
        self.assertAlmostEqual(response.context["summary"].performance, 2500)

    def test_an_opponent_without_ours_leaves_it_out(self):
        Engine.objects.filter(name="B").update(ours=None)
        response = self.page()
        self.assertIsNone(response.context["performance_ours"])
        self.assertNotContains(response, "against ours")

    def test_an_excluded_opponent_is_left_out_of_it_too(self):
        Engine.objects.filter(name="B").update(ours=None)
        response = self.client.get(reverse("match", args=["g1"]), {"exclude": "b"})
        self.assertAlmostEqual(response.context["performance_ours"], 2410)

    def test_an_sprt_has_none(self):
        from nyayo.matches.models import Match
        Match.objects.filter(pk="g1").update(kind="sprt")
        self.assertIsNone(self.page().context["performance_ours"])
