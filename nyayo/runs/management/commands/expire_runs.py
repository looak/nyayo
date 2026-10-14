from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from nyayo.runs.ingest import delete_archived
from nyayo.runs.models import Run


class Command(BaseCommand):
    help = ("Deletes runs that never became history: wip runs never promoted within NYAYO_WIP_DAYS of being recorded, "
            "and committed runs not published within NYAYO_COMMITTED_DAYS of being committed. Published runs are kept.")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="list what would be deleted, delete nothing")

    def handle(self, *args, dry_run=False, **options):
        now = timezone.now()
        wip_before = now - timedelta(days=settings.NYAYO_WIP_DAYS)
        committed_before = now - timedelta(days=settings.NYAYO_COMMITTED_DAYS)
        expired = (Run.objects.annotate(committed=Coalesce("committed_at", "recorded_at"))
                   .filter(Q(state="wip", recorded_at__lt=wip_before)
                           | Q(state="committed", committed__lt=committed_before)))
        count = 0
        for run in expired:
            self.stdout.write(f"{'would delete' if dry_run else 'deleting'} {run.state} {run.short_name} {run.branch}")
            if not dry_run:
                delete_archived(run.pk)
                run.delete()
            count += 1
        self.stdout.write(f"{count} run(s) {'would expire' if dry_run else 'expired'}")
