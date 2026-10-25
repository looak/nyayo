"""The games page (/games): every game kept as data, newest first, 64 to a page."""

import bz2
import re
from datetime import datetime, timedelta, timezone as zone
from io import StringIO

from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from nyayo.matches import cards, catalogue
from nyayo.matches.models import Batch, Match, MatchResult, Upload

from .test_index import MATE, OPENBENCH, RESIGNED
from .test_uploads import BatchCase, batch_text, game

BOOK = "rnbqkb1r/pp2pppp/3p1n2/8/3NP3/8/PPP2PPP/RNBQKB1R w KQkq - 0 5"
OTHER_BOOK = "rnbqkbnr/pppp1ppp/8/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R b KQkq - 1 2"
MASK = 2 ** 64 - 1
ROW = re.compile(r"<tr[ >]")


def utc(*when):
    return int(datetime(*when, tzinfo=zone.utc).timestamp())


def start_of(game_id):
    start = catalogue.index().execute("SELECT start FROM card WHERE game = ?", (game_id,)).fetchone()[0]
    return f"{start & MASK:016x}"


class GamesPage(BatchCase):
    def setUp(self):
        super().setUp()
        Match.objects.update(settings=OPENBENCH)
        MatchResult.objects.update(timelosses={}, crashes={}, illegal={})

    def has(self, page, text):
        self.assertTrue(text in page, f"{text!r} is not in the page")

    def lacks(self, page, text):
        self.assertFalse(text in page, f"{text!r} is in the page")

    def page(self, query="", status=200):
        response = self.client.get("/games" + query)
        self.assertEqual(response.status_code, status)
        return response

    def rows(self, query=""):
        """The rows of the table, the header's left out."""
        return ROW.split(self.page(query).content.decode())[2:]

    def test_a_game_with_everything_on_its_row(self):
        self.keep(bz2.compress(batch_text(game("1-0", moves=MATE, Date="2026.10.18",
                                               GameEndTime="2026-10-18T16:29:31 -0700"))))
        page = self.page().content.decode()
        self.has(page, "2026-10-18")
        self.has(page, "23:29")  # the game's own time, in UTC
        self.has(page, '<a href="/matches/gauntlet-test-1/games/1.0.1">1.0.1</a>')
        self.has(page, ">Elephant 1.0.0<")
        self.has(page, ">Barbarossa 0.6.0<")
        self.has(page, 'title="7 plies">4<')  # moves
        self.has(page, "outcome-win")
        self.has(page, "Checkmate")
        self.has(page, 'href="/matches/gauntlet-test-1"')
        self.has(page, "gauntlet")
        self.lacks(page, "same opening")  # a game from the standard position that ends before ply 16 has none

    def test_the_players_are_named_by_colour(self):
        self.keep(bz2.compress(batch_text(game("1-0", moves=MATE), game("0-1", moves=MATE, Subject="black"))))
        rows = self.rows()
        black = [r for r in rows if "1.0.2" in r][0]
        self.assertLess(black.index("Barbarossa 0.6.0"), black.index("Elephant 1.0.0"))
        white = [r for r in rows if "1.0.1" in r][0]
        self.assertLess(white.index("Elephant 1.0.0"), white.index("Barbarossa 0.6.0"))

    def test_how_it_ended_and_what_was_deduced(self):
        self.keep(bz2.compress(batch_text(game("1-0", moves=MATE), game("0-1", moves=RESIGNED),
                                          game("1-0", moves="1. e4 e5", Termination="time forfeit"))))
        page = self.page().content.decode()
        self.has(page, "Checkmate")
        self.has(page, "Resignation")
        self.has(page, "Lost on time")
        self.assertEqual(page.count("(deduced)"), 1)

    def test_newest_first_by_the_games_own_time_then_its_date(self):
        self.keep(bz2.compress(batch_text(
            game("1-0", moves=MATE, Date="2026.10.01"),
            game("1-0", moves=MATE, Date="2026.10.20", GameEndTime="2026-10-20T10:00:00 +0000"),
            game("1-0", moves=MATE, Date="2026.10.10"))))
        page = self.page().content.decode()
        self.assertLess(page.index("1.0.2"), page.index("1.0.3"))
        self.assertLess(page.index("1.0.3"), page.index("1.0.1"))
        self.assertEqual(page.count('class="muted">10:00'), 1)  # only the game with a time shows one

    def test_a_game_with_no_date_is_dated_when_nyayo_got_it(self):
        self.keep(bz2.compress(batch_text(game("1-0", moves=MATE, Date="????.??.??"))))
        self.assertIn(timezone.now().strftime("%Y-%m-%d"), self.page().content.decode())

    def test_64_to_a_page(self):
        self.keep(bz2.compress(batch_text(*[game("1-0", moves=MATE, Date="2026.10.18") for _ in range(70)])))
        first = self.page().content.decode()
        self.assertEqual(len(ROW.findall(first)) - 1, 64)
        self.has(first, "70 games")
        self.has(first, "?page=2")
        self.assertEqual(len(self.rows("?page=2")), 6)
        self.assertEqual(len(self.rows("?page=99")), 6)
        self.assertEqual(len(self.rows()), 64)

    def test_a_game_opens_in_its_own_view(self):
        self.keep(bz2.compress(batch_text(game("1-0", moves=MATE))))
        response = self.client.get("/matches/gauntlet-test-1/games/1.0.1")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Qxf7")

    def test_games_from_the_same_opening(self):
        self.keep(bz2.compress(batch_text(
            game("1-0", moves="1. Nb3 e6", FEN=BOOK, SetUp="1"),
            game("0-1", moves="1. Nb3 e6", FEN=BOOK, SetUp="1", Subject="black"),
            game("1/2-1/2", moves="2... Nc6 3. Nc3", FEN=OTHER_BOOK, SetUp="1"))))
        every = self.page().content.decode()
        self.assertEqual(every.count("same opening"), 3)
        key = start_of("1.0.1")
        self.has(every, f"/games?opening={key}")
        filtered = self.page(f"?opening={key}").content.decode()
        self.has(filtered, "2 games matching")
        self.has(filtered, "1.0.1")
        self.has(filtered, "1.0.2")
        self.lacks(filtered, "1.0.3")
        self.has(filtered, "Show all")

    def test_a_game_from_the_standard_position_is_grouped_by_its_position_after_16_plies(self):
        long = "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 5. O-O Be7 6. Re1 b5 7. Bb3 d6 8. c3 O-O"
        self.keep(bz2.compress(batch_text(game("1-0", moves=long), game("0-1", moves=long, Subject="black"))))
        start, opening = catalogue.index().execute("SELECT start, opening FROM card WHERE game = '1.0.1'").fetchone()
        self.assertEqual(start & MASK, 0x463B96181691FC9C)
        self.assertIn(f"/games?opening={opening & MASK:016x}", self.page().content.decode())
        self.assertIn("2 games matching", self.page(f"?opening={opening & MASK:016x}").content.decode())

    def test_filtered_to_a_match(self):
        self.keep(bz2.compress(batch_text(game("1-0", moves=MATE))))
        page = self.page("?match=gauntlet-test-1").content.decode()
        self.has(page, "1 game matching")
        self.has(page, "1.0.1")
        other = self.page("?match=ghost").content.decode()
        self.has(other, "0 games matching")
        self.has(other, "No games match")

    def test_filtered_by_result_ending_and_length(self):
        self.keep(bz2.compress(batch_text(
            game("1-0", moves=MATE),                                      # 1.0.1 won, checkmate, 4 moves
            game("0-1", moves=RESIGNED),                                  # 1.0.2 lost, resignation
            game("1/2-1/2", moves="1. e4 e5 2. Nf3 Nc6 3. Bb5 a6"),      # 1.0.3 drawn, 3 moves
            game("1-0", moves=MATE, Subject="black"))))                   # 1.0.4 lost, checkmate
        won = self.rows("?result=win")
        self.assertEqual([("1.0.1" in r) for r in won], [True])
        self.assertIn('class="t-win"', self.page("?result=win").content.decode())
        lost = "".join(self.rows("?result=loss"))
        self.has(lost, "1.0.2")
        self.has(lost, "1.0.4")
        self.lacks(lost, "1.0.1")
        self.assertEqual(len(self.rows("?result=draw")), 1)
        mates = "".join(self.rows("?ended=checkmate"))
        self.has(mates, "1.0.1")
        self.has(mates, "1.0.4")
        self.lacks(mates, "1.0.2")
        short = "".join(self.rows("?moves_to=3"))
        self.has(short, "1.0.3")
        self.lacks(short, "1.0.1")
        both = self.page("?result=loss&ended=checkmate").content.decode()
        self.has(both, "1 game matching")
        self.has(both, "Ended by checkmate.")
        self.has(both, '<option value="checkmate" selected>')
        self.has(both, "<details class=\"filterbar\" open>")

    def test_bad_filters_are_400(self):
        self.page("?ended=boredom", status=400)
        self.page("?ended=rule", status=400)
        self.page("?moves_from=ten", status=400)
        self.assertEqual(self.page("?result=maybe").status_code, 200)  # not a result: no filter

    def test_a_bad_opening_is_400(self):
        for bad in ("zz", "123", "g" * 16, "0" * 17):
            self.page(f"?opening={bad}", status=400)

    def test_nothing_kept_yet(self):
        page = self.page().content.decode()
        self.has(page, "No games kept yet")
        self.has(page, 'href="/games"')  # the nav


class PlayedExtractor(BatchCase):
    def test_the_tags_the_formats_and_the_exactness(self):
        self.assertEqual(cards.parse_time("2026-10-15T16:29:31 -0700"), utc(2026, 10, 15, 23, 29, 31))
        self.assertEqual(cards.parse_time("2026-10-15T16:29:31+0000"), utc(2026, 10, 15, 16, 29, 31))
        self.assertIsNone(cards.parse_time("yesterday"))
        self.assertEqual(cards.parse_date("2026.10.18"), utc(2026, 10, 18))
        self.assertIsNone(cards.parse_date("????.??.??"))
        both = cards.played_fields({"tags": {"Date": "2026.10.18", "GameEndTime": "2026-10-15T16:29:31 -0700"}})
        self.assertEqual((both["played_at"], both["played_flags"]),
                         (utc(2026, 10, 15, 23, 29, 31), cards.PLAYED_TIME))
        start = cards.played_fields({"tags": {"GameStartTime": "2026-10-15T16:29:28 -0700"}})
        self.assertEqual(start["played_at"], utc(2026, 10, 15, 23, 29, 28))
        day = cards.played_fields({"tags": {"Date": "2026.10.18"}})
        self.assertEqual((day["played_at"], day["played_flags"]), (utc(2026, 10, 18), 0))
        self.assertIsNone(cards.played_fields({"tags": {}})["played_at"])

    def test_reindex_fills_it_for_batches_made_before(self):
        with override_settings(NYAYO_BATCH_GAMES=1):
            self.keep(bz2.compress(batch_text(game("1-0", moves=MATE, Date="2026.10.18"))), batch="2.0")
        sealed = Batch.objects.filter(state="sealed").first()
        index = catalogue.index()
        index.execute("UPDATE card SET played_at = NULL")
        index.execute("DELETE FROM filled WHERE extractor = 'played'")
        call_command("reindex", "--only", "played", stdout=StringIO())
        self.assertEqual(index.execute("SELECT played_at FROM card WHERE game = '2.0.1'").fetchone()[0], utc(2026, 10, 18))
        self.assertEqual(catalogue.filled(sealed)["played"], cards.PLAYED_VERSION)

    def test_a_game_with_only_a_date_is_placed_at_its_arrival_that_day(self):
        def arrival(*when):
            return datetime(*when, tzinfo=zone.utc)

        day = {"tags": {"Date": "2026.10.24"}}
        # an upload that came in that evening, or the next morning (UTC) for a worker west of it: when it came
        for received in (arrival(2026, 10, 24, 19, 5), arrival(2026, 10, 25, 9, 0)):
            fields = cards.played_fields(day, received)
            self.assertEqual((fields["played_at"], fields["played_flags"]), (int(received.timestamp()), 0))
        # one far from the date (resent later, or reindexed without its upload): the date's midnight
        self.assertEqual(cards.played_fields(day, arrival(2026, 11, 3))["played_at"], utc(2026, 10, 24))
        # no date at all: the arrival
        self.assertEqual(cards.played_fields({"tags": {}}, arrival(2026, 11, 3))["played_at"], utc(2026, 11, 3))

    def test_reindex_dates_a_batch_by_its_uploads_arrival(self):
        today = timezone.now()
        with override_settings(NYAYO_BATCH_GAMES=1):  # full at once, so sealed
            self.keep(bz2.compress(batch_text(game("1-0", moves=MATE, Date=today.strftime("%Y.%m.%d")))))
        arrived = Upload.objects.get().received_at
        Batch.objects.update(created_at=today + timedelta(hours=5))  # put away well after it arrived
        index = catalogue.index()
        index.execute("UPDATE card SET played_at = 0")
        index.execute("DELETE FROM filled WHERE extractor = 'played'")
        call_command("reindex", "--only", "played", stdout=StringIO())
        self.assertEqual(index.execute("SELECT played_at FROM card").fetchone()[0], int(arrived.timestamp()))

    def test_times_are_shown_in_the_sites_zone_and_say_so(self):
        self.keep(bz2.compress(batch_text(game("1-0", moves=MATE, GameEndTime="2026-10-18T16:29:31 -0700"))))
        page = self.client.get("/games").content.decode()
        self.assertIn("23:29", page)
        self.assertIn("2026-10-18 23:29 UTC", page)
        with override_settings(TIME_ZONE="America/Vancouver"):
            page = self.client.get("/games").content.decode()
        self.assertIn("2026-10-18 16:29 PDT", page)
        self.assertIn('<span class="muted">16:29</span>', page)
        self.assertIn('times in <code title="America/Vancouver">PDT</code>', page)
