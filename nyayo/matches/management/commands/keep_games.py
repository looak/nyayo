import os
from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from nyayo.matches import export, migrate, reader


class Command(BaseCommand):
    help = ("Migrates the finished single games already stored into batches: each is read, appended as it came to "
            "its match's batches and indexed with how it ended (deduced and marked inferred where the game doesn't "
            "say), then checked: games per match, the export before and after, the reader against python-chess. "
            "Touches nothing in games/ or archive/; purge_games is a separate step. --dry-run reads everything and "
            "counts, writing nothing.")

    def add_arguments(self, parser):
        parser.add_argument("--match", help="only this match")
        parser.add_argument("--dry-run", action="store_true", help="read and count, write nothing")
        parser.add_argument("--skip-export-check", action="store_true", help="don't compare the export before/after")
        parser.add_argument("--skip-reader-check", action="store_true",
                            help="don't compare the reader with python-chess")

    def handle(self, *args, match=None, dry_run=False, skip_export_check=False, skip_reader_check=False, **options):
        try:
            chosen = reader.default()
        except (reader.WrongReader, OSError) as error:
            raise CommandError(f"not starting: {error}")
        if hasattr(os, "nice"):
            os.nice(10)
        say = self.stdout.write
        say(f"reading with {chosen.name}")
        pending = migrate.todo(match)
        say(f"{sum(len(rows) for rows in pending.values())} finished games in {len(pending)} matches to keep")
        filters = {"match": match} if match else {}
        before = None
        if not dry_run and not skip_export_check:
            say("exporting before ...")
            before = migrate.game_hashes(export.chunks(export.parse(filters)))
        kept = migrate.keep(chosen, match, dry_run, say)
        total = sum(count for count, _ in kept.values())
        if dry_run:
            counts = Counter()
            for _, found in kept.values():
                counts.update(found)
            say(f"{total} games would be kept; endings of all of them: {dict(counts)}")
            return
        problems = migrate.check_counts(match)
        if before is not None:
            say("exporting after ...")
            after = migrate.game_hashes(export.chunks(export.parse(filters)))
            if after != before:
                problems.append(f"the export differs: {sum((before - after).values())} games gone, "
                                f"{sum((after - before).values())} new")
            else:
                say(f"export before and after: the same {sum(after.values())} games")
        if not skip_reader_check and chosen.name != reader.PythonChess.name:
            bad = migrate.check_reader(chosen, match)
            say(f"reader against python-chess: {len(bad)} batches differ")
            problems += [f"reader differs on {b}" for b in bad]
        say(f"{total} games kept; endings of OpenBench's finished games: {dict(migrate.ending_report(match))}")
        if problems:
            raise CommandError("; ".join(problems))
        say("all checks passed; purge_games can now remove the single files of sealed batches")
