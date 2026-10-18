from django.core.management.base import BaseCommand, CommandError

from nyayo.matches import batches
from nyayo.matches.models import GameBatch


class Command(BaseCommand):
    help = ("Deletes a match's batches of games, files and rows. Batches are data to keep, so this is only ever run by "
            "hand and needs --match; purge_games never touches them.")

    def add_arguments(self, parser):
        parser.add_argument("--match", required=True, help="the match whose batches go")
        parser.add_argument("--dry-run", action="store_true", help="list what would be deleted, delete nothing")

    def handle(self, *args, match, dry_run=False, **options):
        rows = list(GameBatch.objects.filter(match_id=match))
        if not rows:
            raise CommandError(f"{match} has no batches")
        for row in rows:
            self.stdout.write(f"{'would delete' if dry_run else 'deleting'} {row.match_id}/{row.batch} "
                              f"{row.games} games {row.bytes} bytes")
        if not dry_run:
            batches.delete_batches(match)
        self.stdout.write(f"{len(rows)} batch{'es' if len(rows) != 1 else ''} {'would be ' if dry_run else ''}deleted")
