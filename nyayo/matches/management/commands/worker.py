from django.core.management.base import BaseCommand

from nyayo.matches import worker


class Command(BaseCommand):
    help = ("Puts away the games kept as data: reads pending uploads into the matches' batches and seals the batches "
            "of matches that have ended. Runs until stopped, as nyayo-worker.service does; --once drains the queue "
            "and exits.")

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="do what there is to do, then exit")
        parser.add_argument("--interval", type=float, default=1.0, metavar="SECONDS",
                            help="how often to look for work when there is none (default 1)")

    def handle(self, *args, once=False, interval=1.0, **options):
        done = worker.run(once=once, interval=interval)
        if once:
            self.stdout.write(f"{done} thing{'s' if done != 1 else ''} done")
