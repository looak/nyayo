import os

from django.core.management.base import BaseCommand, CommandError

from nyayo.matches import cards, catalogue, reader, reindex


class Command(BaseCommand):
    help = ("Writes the index's cards again from the sealed batches, one batch at a time, visiting only those an "
            "extractor (reader, ending, eval) has a newer version for, so it can be stopped and run again. Never "
            "changes a batch.")

    def add_arguments(self, parser):
        parser.add_argument("--match", help="only this match's batches")
        parser.add_argument("--only", help="only these extractors, comma separated: "
                                           f"{', '.join(catalogue.EXTRACTOR_COLUMNS)}")
        parser.add_argument("--rebuild", action="store_true",
                            help="every batch, its cards written whole, whatever filled them")
        parser.add_argument("--win-adj", metavar="SETTINGS",
                            help='the resign rule for matches whose document has none, "movecount=3 score=400"')
        parser.add_argument("--draw-adj", metavar="SETTINGS",
                            help='the draw rule for matches whose document has none, '
                                 '"movenumber=40 movecount=8 score=10"')
        parser.add_argument("--dry-run", action="store_true", help="count what would be done, do nothing")

    def handle(self, *args, match=None, only=None, rebuild=False, win_adj=None, draw_adj=None, dry_run=False,
               **options):
        extractors = [name.strip() for name in only.split(",")] if only else None
        unknown = set(extractors or ()) - set(catalogue.EXTRACTOR_COLUMNS)
        if unknown:
            raise CommandError(f"no extractor {', '.join(sorted(unknown))}")
        try:
            chosen = reader.default()
        except (reader.WrongReader, OSError) as error:
            raise CommandError(f"not starting: {error}")
        if hasattr(os, "nice"):
            os.nice(10)  # a background job, never ahead of the site
        self.stdout.write(f"reading with {chosen.name}; versions {cards.versions(chosen)}")
        batches, games, mismatched = reindex.run(
            chosen, extractors, match, rebuild, cards.parse_adjudication(win_adj), cards.parse_adjudication(draw_adj),
            dry_run, out=self.stdout.write)
        self.stdout.write(f"{batches} batch{'es' if batches != 1 else ''}, {games} games "
                          f"{'would be ' if dry_run else ''}indexed")
        if mismatched:
            raise CommandError(f"{len(mismatched)} batches don't read as their rows say: {', '.join(mismatched)}")
