import sys

from nyayo.matches.management.commands.jobs import Command as JobsCommand


class Command(JobsCommand):
    """The old name of `manage.py jobs`, kept until nyayo-worker.service is swapped for nyayo-jobs.service."""

    help = "Deprecated: the old name of `manage.py jobs`, which it runs. Use that."

    def handle(self, *args, **options):
        print("manage.py worker is now manage.py jobs (nyayo-jobs.service); this name will go.", file=sys.stderr)
        return super().handle(*args, **options)
