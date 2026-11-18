from django.core.management.base import BaseCommand

from nyayo.matches import ratings


class Command(BaseCommand):
    help = ("Fits our own ratings from every stored gauntlet game (matches/ratings.py) and stores them as `Engine.ours`: "
            "on the list's scale, under our conditions. Run again any time; the same games give the same ratings and "
            "change nothing. Also done when a gauntlet finishes.")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="fit and print, store nothing")

    def handle(self, *args, dry_run=False, **options):
        result = ratings.fit() if dry_run else ratings.refit()
        if not result.engines:
            self.stdout.write("no gauntlet games with an engine a list rating anchors; nothing fitted")
        else:
            self.stdout.write(f"{len(result.engines)} engines from {result.games} games, "
                              f"draw width {result.draw:.0f} Elo, prior {ratings.prior_sigma():g} Elo"
                              f"{' (dry run, nothing stored)' if dry_run else ''}")
        for f in result.engines:
            listed = f.engine.rating["value"] if f.engine.rating else None
            line = (f"  {str(f.engine):<28} {f.value:7.1f} ± {f.error:4.1f}  {f.games:5d} games  "
                    f"list {listed if listed is not None else '—'}")
            if listed is not None:
                line += f"  ({f.value - listed:+.1f}, z {f.z:+.1f})" if f.z is not None else f"  ({f.value - listed:+.1f})"
            self.stdout.write(self.style.WARNING(line + "  FLAGGED") if f.flagged else line)
        for engine in result.skipped:
            self.stdout.write(self.style.WARNING(f"  {engine}: no list-rated engine in its games, left out"))
