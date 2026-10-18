from django.core.management.base import BaseCommand, CommandError

from nyayo.matches import export


class Command(BaseCommand):
    help = ("Writes every stored game matching the filters to a file as one bz2 stream: the stored batches and the "
            "sealed single games, the same as GET /api/games. No filter is every game.")

    def add_arguments(self, parser):
        parser.add_argument("-o", "--output", required=True, help="the file to write, e.g. games.pgn.bz2")
        for name in export.FILTERS:
            parser.add_argument(f"--{name}", help=f"filter on {name} (see docs/match-schema.md, Export)")

    def handle(self, *args, output, **options):
        try:
            filters = export.parse(options)
        except export.BadFilter as error:
            raise CommandError(str(error))
        size = 0
        with open(output, "wb") as out:
            for chunk in export.chunks(filters):
                out.write(chunk)
                size += len(chunk)
        self.stdout.write(f"wrote {size} bytes to {output}")
