"""A server error leaves its traceback in the log, where `journalctl -u nyayo` finds it."""

from unittest import mock

from django.conf import settings
from django.test import Client

from .test_games import GameTestCase


class Errors(GameTestCase):  # a match in the list, so the page has something to summarise
    def test_server_errors_go_to_the_console_handler(self):
        logger = settings.LOGGING["loggers"]["django.request"]
        self.assertEqual(logger["level"], "ERROR")
        self.assertEqual(logger["handlers"], ["console"])
        self.assertEqual(settings.LOGGING["handlers"]["console"]["class"], "logging.StreamHandler")

    def test_a_500_is_logged_with_its_traceback(self):
        client = Client(raise_request_exception=False)
        with mock.patch("nyayo.matches.pages.stats.summarize", side_effect=RuntimeError("the reason")):
            with self.assertLogs("django.request", level="ERROR") as logged:
                response = client.get("/matches")
        self.assertEqual(response.status_code, 500)
        record = logged.records[0]
        self.assertIn("/matches", record.getMessage())
        self.assertEqual(str(record.exc_info[1]), "the reason")

    def test_a_404_is_not_logged_as_an_error(self):
        with self.assertNoLogs("django.request", level="ERROR"):
            self.assertEqual(Client().get("/matches/nothing-here").status_code, 404)
