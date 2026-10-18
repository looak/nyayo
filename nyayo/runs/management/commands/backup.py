from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from nyayo import backup


class Command(BaseCommand):
    help = ("Backs nyayo up: an online copy of the database in the data directory's backup/, and, with "
            "NYAYO_BACKUP_REPO set, the archive, the games and an SQL dump of the database committed and pushed to "
            "that git checkout. games/batches is left to the share it lives on.")

    def add_arguments(self, parser):
        parser.add_argument("--no-push", action="store_true", help="commit but don't push")

    def handle(self, *args, no_push=False, **options):
        try:
            for line in backup.backup(settings.DATA_DIR, settings.DATABASES["default"]["NAME"],
                                      repo=settings.NYAYO_BACKUP_REPO or None, push=not no_push):
                self.stdout.write(line)
        except backup.BackupError as error:
            raise CommandError(str(error))
        if not settings.NYAYO_BACKUP_REPO:
            self.stderr.write("NYAYO_BACKUP_REPO isn't set: only the local database copy was made, which is not a "
                              "backup off this machine")
