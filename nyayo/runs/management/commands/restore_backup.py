from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from nyayo import backup


class Command(BaseCommand):
    help = ("Restores a backup repository into an empty data directory (the one NYAYO_DATA_DIR names): the database "
            "from its SQL dump, the archive and the games. Stop nyayo first; games/batches comes from its share.")

    def add_arguments(self, parser):
        parser.add_argument("repo", help="the checkout of the backup repository")

    def handle(self, *args, repo, **options):
        try:
            for line in backup.restore(repo, settings.DATA_DIR, settings.DATABASES["default"]["NAME"]):
                self.stdout.write(line)
        except backup.BackupError as error:
            raise CommandError(str(error))
