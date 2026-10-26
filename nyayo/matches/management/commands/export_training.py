from django.core.management.base import BaseCommand, CommandError

from nyayo.matches import reader, selection, training


def names(text):
    return [part.strip() for part in (text or "").split(",") if part.strip()]


class Command(BaseCommand):
    help = ("Writes the games a training selection chooses, as stored with their comments, to NAME.pgn.zst, and "
            "NAME.manifest.json: the filters, the count, the versions and every batch's id and sha256 with the games "
            "taken from it. With --from-manifest, the same games again from the batches alone. Only games in sealed "
            "batches and with a result are taken.")

    def add_arguments(self, parser):
        parser.add_argument("--out", required=True, metavar="NAME", help="NAME.pgn.zst and NAME.manifest.json")
        parser.add_argument("--match", action="append", default=[], metavar="ID",
                            help="a match's id; repeat, or separate with commas")
        parser.add_argument("--openbench", action="append", default=[], metavar="TEST",
                            help="an OpenBench test's id; repeat, or separate with commas")
        parser.add_argument("--kind", choices=["sprt", "gauntlet", "selfplay", "games"], help="a kind of match")
        parser.add_argument("--subject-version", metavar="VERSIONS",
                            help="the engine under test's version: 1.3.0- (at least), 1.2.0-1.3.0 (between), 1.3.0")
        parser.add_argument("--book", help="the match's book, as its settings name it")
        parser.add_argument("--tc", help="the match's time control, as its settings say it, e.g. 8+0.08")
        parser.add_argument("--endings", metavar="LIST",
                            help=f"the endings kept, of {', '.join(selection.ENDING_NAMES)} "
                                 f"(default {','.join(selection.GOOD_ENDINGS)}; inferred ones included)")
        parser.add_argument("--exclude", metavar="LIST", help="endings left out of the kept ones, e.g. time,crash")
        parser.add_argument("--from-manifest", metavar="FILE", help="take a manifest's games again; no filters")

    def handle(self, *args, out, from_manifest=None, **options):
        if from_manifest:
            try:
                count = training.again(from_manifest, out)
            except training.Mismatch as error:
                raise CommandError(str(error))
            self.stdout.write(f"{count} games, as {from_manifest} names them, to {out}.pgn.zst")
            return
        try:
            chosen = selection.Selection(
                matches=[n for given in options["match"] for n in names(given)],
                openbench=[n for given in options["openbench"] for n in names(given)],
                kind=options["kind"], book=options["book"], tc=options["tc"],
                versions=selection.parse_versions(options["subject_version"]) if options["subject_version"] else None,
                endings=selection.endings_kept(names(options["endings"]) if options["endings"] else None,
                                               names(options["exclude"])))
        except selection.BadSelection as error:
            raise CommandError(str(error))
        manifest = training.export(chosen, out, reader.default().name)
        self.stdout.write(f"{manifest['games']} games from {len(manifest['batches'])} batches to {out}.pgn.zst, "
                          f"manifest {out}.manifest.json")
        if manifest["left_out_in_open_batches"]:
            self.stdout.write(f"{manifest['left_out_in_open_batches']} more are in batches still open; they are "
                              "taken once sealed")
