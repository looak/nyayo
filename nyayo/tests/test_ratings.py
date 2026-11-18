"""Our own ratings (matches/ratings.py): a synthetic pool whose true ratings the fit recovers, the prior, the flag,
refitting, the refit when a gauntlet finishes, the command and the page."""

import io
import math
import random
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from nyayo.matches import ratings
from nyayo.matches.models import Engine, Match, MatchResult

from .test_matches_ingest import MatchTestCase, gauntlet

DRAW = 90  # the true draw width of the synthetic pool, in Elo


def play(rng, diff, games):
    """Wins, draws and losses of `games` games between engines `diff` Elo apart, in the model the fit assumes."""
    k = math.log(10) / 400
    win = 1 / (1 + math.exp(-k * (diff - DRAW)))
    not_loss = 1 / (1 + math.exp(-k * (diff + DRAW)))
    counts = [0, 0, 0]
    for _ in range(games):
        u = rng.random()
        counts[0 if u < win else 1 if u < not_loss else 2] += 1
    return tuple(counts)


def store(subject, results, name, state="finished", hidden=False):
    """A gauntlet by `subject` with `results` ({ engine: (w, d, l) }) stored directly."""
    match = Match.objects.create(id=name, kind="gauntlet", state=state, producer={"name": "t"}, subject=subject,
                                 hidden=hidden)
    for ordinal, (engine, (w, d, l)) in enumerate(results.items()):
        MatchResult.objects.create(match=match, ordinal=ordinal, opponent=engine.name.lower(), engine=engine,
                                   games=w + d + l, wins=w, draws=d, losses=l)
    return match


def opponent(name, rating=None):
    return Engine.objects.create(name=name, rating={"value": rating, "list": "CCRL Blitz"} if rating is not None else None)


class Pool(TestCase):
    """Six listed opponents, 2300 to 2800, and two Elephant versions, 150 games a pair, drawn from known ratings."""

    TRUE = {"A": 2300, "B": 2400, "C": 2500, "D": 2600, "E": 2700, "F": 2800}
    ELEPHANT = {"1": 2450, "2": 2550}

    def setUp(self):
        rng = random.Random(7)
        self.opponents = {n: opponent(n, r) for n, r in self.TRUE.items()}
        self.versions = {v: Engine.objects.create(name="Elephant", version=v) for v in self.ELEPHANT}
        for v, rating in self.ELEPHANT.items():
            results = {self.opponents[n]: play(rng, rating - r, 150) for n, r in self.TRUE.items()}
            store(self.versions[v], results, f"gauntlet-{v}")

    def test_the_true_ratings_are_recovered_within_error(self):
        fit = {f.engine: f for f in ratings.refit().engines}
        for v, true in self.ELEPHANT.items():
            f = fit[self.versions[v]]
            self.assertLess(abs(f.value - true), 3 * f.error, f)
            self.assertTrue(5 < f.error < 40, f)
        for n, true in self.TRUE.items():
            f = fit[self.opponents[n]]
            self.assertLess(abs(f.value - true), 3 * f.error, f)
            self.assertLessEqual(f.error, 20)  # the prior's own width bounds it
        self.assertFalse([f for f in fit.values() if f.flagged])

    def test_the_draw_width_is_recovered(self):
        self.assertLess(abs(ratings.refit().draw - DRAW), 15)

    def test_ours_is_stored_and_refitting_the_same_games_writes_nothing(self):
        ratings.refit()
        stored = {e.pk: e.ours for e in Engine.objects.all()}
        self.assertEqual(set(stored[self.versions["1"].pk]), {"value", "error", "games", "as_of", "flagged", "z"})
        self.assertEqual(stored[self.versions["1"].pk]["games"], 900)
        self.assertEqual(stored[self.opponents["A"].pk]["games"], 300)  # both versions played it
        with CaptureQueriesContext(connection) as queries:
            ratings.refit()
        self.assertEqual([q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("UPDATE", "INSERT"))], [])
        self.assertEqual({e.pk: e.ours for e in Engine.objects.all()}, stored)

    def test_a_hidden_gauntlet_leaves_the_fit(self):
        ratings.refit()
        Match.objects.filter(pk="gauntlet-2").update(hidden=True)
        fit = {f.engine: f for f in ratings.refit().engines}
        self.assertNotIn(self.versions["2"], fit)
        self.assertIsNone(Engine.objects.get(pk=self.versions["2"].pk).ours)
        self.assertEqual(fit[self.opponents["A"]].games, 150)


class Prior(TestCase):
    def test_a_prior_pulls_an_opponent_we_played_little_to_its_list_value(self):
        elephant = Engine.objects.create(name="Elephant", version="1")
        anchor, thin = opponent("Anchor", 2500), opponent("Thin", 2600)
        # 100 games in which Thin looks about 100 Elo weaker than listed
        store(elephant, {anchor: (60, 30, 10), thin: (65, 25, 10)}, "first")
        pulled = {f.engine: f for f in ratings.fit(sigma=20).engines}[thin]
        loose = {f.engine: f for f in ratings.fit(sigma=2000).engines}[thin]
        self.assertLess(abs(pulled.value - 2600), abs(loose.value - 2600))
        self.assertLess(abs(pulled.value - 2600), 20)
        self.assertLessEqual(pulled.error, 20)

    def test_an_engine_with_no_list_rating_has_a_flat_prior_and_a_wider_error(self):
        elephant = Engine.objects.create(name="Elephant", version="1")
        anchor = opponent("Anchor", 2500)
        store(elephant, {anchor: (50, 40, 10)}, "one")
        fit = {f.engine: f for f in ratings.fit().engines}
        self.assertGreater(fit[elephant].error, fit[anchor].error)
        self.assertGreater(fit[elephant].value, 2500)  # it scored 70%

    def test_engines_no_prior_reaches_are_left_out(self):
        a, b = Engine.objects.create(name="a"), Engine.objects.create(name="b")
        store(a, {b: (5, 5, 5)}, "island")
        result = ratings.fit()
        self.assertEqual(result.engines, [])
        self.assertEqual(sorted(e.name for e in result.skipped), ["a", "b"])


class Flagging(TestCase):
    def setUp(self):
        self.elephant = Engine.objects.create(name="Elephant", version="1")
        self.anchor, self.fine, self.off = opponent("Anchor", 2500), opponent("Fine", 2500), opponent("Off", 2500)
        self.more = [opponent(f"More{i}", 2500) for i in range(4)]  # so that one outlier can't move Elephant

    def normal(self, **extra):
        return {self.anchor: (240, 120, 140), self.fine: (240, 120, 140), **{m: (240, 120, 140) for m in self.more}, **extra}

    def fit(self, results):
        store(self.elephant, results, "g")
        return {f.engine: f for f in ratings.fit(sigma=20).engines}

    def test_an_opponent_our_games_contradict_is_flagged_and_one_they_agree_with_is_not(self):
        # Elephant scores 52% against six engines and 90% against Off, which the list rates the same as them
        fit = self.fit(self.normal() | {self.off: (84, 12, 4)})
        self.assertTrue(fit[self.off].flagged)
        self.assertLess(fit[self.off].z, -2)
        self.assertFalse(fit[self.fine].flagged)
        self.assertFalse(fit[self.anchor].flagged)

    def test_a_modest_disagreement_on_a_few_games_is_not_flagged(self):
        fit = self.fit(self.normal() | {self.off: (13, 4, 3)})
        self.assertFalse(fit[self.off].flagged)

    def test_an_elephant_version_is_never_flagged(self):
        fit = self.fit(self.normal() | {self.off: (240, 120, 140)})
        self.assertFalse(fit[self.elephant].flagged)
        self.assertIsNone(fit[self.elephant].z)


class Triggers(MatchTestCase):
    FINISHED = {"match__state": "finished", "match__finished_at": "2026-10-15T09:00:00Z",
                "match__updated_at": "2026-10-15T09:00:00Z"}

    def test_a_gauntlet_becoming_finished_refits_and_a_running_one_does_not(self):
        self.assertEqual(self.put_match(gauntlet()).status_code, 201)
        self.assertEqual(Engine.objects.filter(ours__isnull=False).count(), 0)
        self.assertEqual(self.put_match(gauntlet(**self.FINISHED)).status_code, 200)
        self.assertEqual(Engine.objects.get(name="Barbarossa").ours["games"], 10)
        self.assertIsNotNone(Engine.objects.get(name="Elephant").ours)

    def test_a_failing_fit_does_not_fail_the_report(self):
        with mock.patch.object(ratings, "refit", side_effect=RuntimeError("boom")):
            with self.assertLogs("nyayo.ratings", "ERROR"):
                self.assertEqual(self.put_match(gauntlet(**self.FINISHED)).status_code, 201)

    def test_the_command_fits_and_a_dry_run_stores_nothing(self):
        self.put_match(gauntlet())
        out = io.StringIO()
        call_command("fit_ratings", "--dry-run", stdout=out)
        self.assertIn("Barbarossa", out.getvalue())
        self.assertEqual(Engine.objects.filter(ours__isnull=False).count(), 0)
        out = io.StringIO()
        call_command("fit_ratings", stdout=out)
        self.assertEqual(Engine.objects.filter(ours__isnull=False).count(), 2)
        self.assertIn("draw width", out.getvalue())

    def test_the_command_with_no_gauntlets_says_so(self):
        out = io.StringIO()
        call_command("fit_ratings", stdout=out)
        self.assertIn("nothing fitted", out.getvalue())


class Page(TestCase):
    def test_opponents_shows_ours_beside_the_list_and_the_flag_and_the_versions(self):
        elephant = Engine.objects.create(name="Elephant", version="1.0.0")
        off = opponent("Off", 2500)
        results = {opponent(f"Fine{i}", 2500): (240, 120, 140) for i in range(5)} | {off: (84, 12, 4)}
        store(elephant, results, "g")
        ratings.refit()
        body = self.client.get(reverse("opponents")).content.decode()
        self.assertIn("1.0.0", body)  # the Elephant version has a row
        self.assertIn("under our conditions", body)
        self.assertEqual(body.count("⚠ flagged"), 1)
