from django.core.management.base import BaseCommand, CommandError

from nyayo.matches import catalogue
from nyayo.matches.models import Batch, Upload


class Command(BaseCommand):
    help = ("Deletes the games a match keeps as data: its batches (files, rows and cards in the index) and its "
            "uploads. They are data to keep, so this is only ever run by hand and needs --match; purge_games never "
            "touches them.")

    def add_arguments(self, parser):
        parser.add_argument("--match", required=True, help="the match whose batches go")
        parser.add_argument("--dry-run", action="store_true", help="list what would be deleted, delete nothing")

    def handle(self, *args, match, dry_run=False, **options):
        batches = list(Batch.objects.filter(match_id=match))
        uploads = list(Upload.objects.filter(match_id=match))
        if not batches and not uploads:
            raise CommandError(f"{match} has no batches")
        verb = "would delete" if dry_run else "deleting"
        for row in batches:
            self.stdout.write(f"{verb} batch {row.match_id}/{row.number} ({row.state}) {row.games} games")
        for row in uploads:
            self.stdout.write(f"{verb} upload {row.match_id}/{row.name} ({row.state}) {row.games} games")
        if not dry_run:
            catalogue.purge(match)
        self.stdout.write(f"{len(batches)} batch{'es' if len(batches) != 1 else ''} and {len(uploads)} "
                          f"upload{'s' if len(uploads) != 1 else ''} {'would be ' if dry_run else ''}deleted")
