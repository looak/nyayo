from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from nyayo.matches import demo


class Command(BaseCommand):
    help = ("Makes (or remakes) a handful of simulated SPRTs with a history, a few games and a batch, and gauntlets "
            "with their games, for polishing the pages: SPRTs passed, failed, stopped, running, silent and just "
            "started; gauntlets running, finished, stopped with games cut off, silent and just started. Debug mode "
            "only; the numbers are invented.")

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("seed_demo makes up results; it only runs with NYAYO_DEBUG=true")
        for match_id in demo.seed():
            self.stdout.write(f"/matches/{match_id}")
