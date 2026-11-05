from django.core.management.base import BaseCommand, CommandError

from nyayo.matches import renaming


class Command(BaseCommand):
    help = ("Renames the matches taken from OpenBench, openbench-<anything>-<N>, to <mode>-<N> (sprt-1000): the mode of "
            "the lab's test N, else the match's kind. Moves everything that holds the id: the database rows, the "
            "datasets that name it, archive/matches/<id>.json (rewritten) and .meta.json, and the games, batches and "
            "uploads directories. Stop nyayo and its worker first, and take a backup. Restartable: run it again after "
            "an interruption and it finishes what is left. --dry-run says what it would do and changes nothing. A "
            "clash is refused before anything changes. Old links redirect to the new ids from the rows it writes.")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="print what would be done, change nothing")

    def handle(self, *args, dry_run=False, **options):
        try:
            result = renaming.run(dry_run=dry_run, out=self.stdout.write)
        except renaming.RenameError as error:
            for problem in error.problems:
                self.stderr.write(f"refused: {problem}")
            raise CommandError(f"{len(error.problems)} problem(s), nothing was changed")
        for entry in renaming.orphans():
            self.stdout.write(f"note: {entry} is named like an old match id and belongs to no match; left as it is")
        would = "would be " if dry_run else ""
        self.stdout.write(f"{result.renamed} match(es) {would}renamed, {result.moved} file(s) or director(ies) "
                          f"{would}moved, {result.datasets} dataset(s) {would}updated")
        if dry_run:
            self.stdout.write("dry run: nothing was changed")
