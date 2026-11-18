import tomllib

from django.core.management.base import BaseCommand, CommandError

from nyayo.matches import engines


class Command(BaseCommand):
    help = ("Fills the engine catalogue (url, ref, asset, binary, options, rating) from the gauntlets already stored: "
            "their archived documents and index first, then each gauntlet file given (uwanja's gauntlets/*.toml, "
            "matched to its stored match by name and its opponents by id). Fills blanks only; run again any time.")

    def add_arguments(self, parser):
        parser.add_argument("files", nargs="*", help="gauntlet files to read, e.g. ../uwanja/gauntlets/*.toml")

    def handle(self, *args, files=(), **options):
        total = engines.backfill_matches()
        self.stdout.write(f"stored matches: {total} engine row{'s' if total != 1 else ''} filled")
        for path in files:
            try:
                with open(path, "rb") as handle:
                    data = tomllib.load(handle)
            except (OSError, tomllib.TOMLDecodeError) as error:
                raise CommandError(f"{path}: {error}")
            notes = []
            n = engines.backfill_file(data, notes)
            self.stdout.write(f"{path}: {n} engine row{'s' if n != 1 else ''} filled")
            for note in notes:
                self.stdout.write(self.style.WARNING(f"  {note}"))
