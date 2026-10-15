from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models.functions import Coalesce
from django.utils import timezone

from nyayo.matches import games
from nyayo.matches.models import Game


class Command(BaseCommand):
    help = ("Deletes games' PGN files and their index rows, nothing else: a match's document, counts and snapshots "
            "stay. Pick the games with --match, --older-than or both.")

    def add_arguments(self, parser):
        parser.add_argument("--match", help="only this match's games")
        parser.add_argument("--older-than", type=int, metavar="DAYS",
                            help="only games that finished (or, while live, were first seen) more than DAYS ago")
        parser.add_argument("--dry-run", action="store_true", help="list what would be deleted, delete nothing")

    def handle(self, *args, match=None, older_than=None, dry_run=False, **options):
        if match is None and older_than is None:
            raise CommandError("say which games: --match ID, --older-than DAYS, or both")
        queryset = Game.objects.all()
        if match is not None:
            queryset = queryset.filter(match_id=match)
        if older_than is not None:
            cutoff = timezone.now() - timedelta(days=older_than)
            queryset = queryset.annotate(since=Coalesce("finished_at", "received_at")).filter(since__lt=cutoff)
        count = 0
        for game in queryset.order_by("match_id", "game"):
            self.stdout.write(f"{'would delete' if dry_run else 'deleting'} {game.match_id}/{game.game} {game.result}")
            if not dry_run:
                games.delete_game_file(game.match_id, game.game)
                game.delete()
            count += 1
        self.stdout.write(f"{count} game{'s' if count != 1 else ''} {'would be ' if dry_run else ''}deleted")
