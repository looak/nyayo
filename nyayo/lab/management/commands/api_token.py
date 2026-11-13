from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from nyayo.lab import api_tokens
from nyayo.lab.models import ApiToken


class Command(BaseCommand):
    help = "Tokens for scripts that make tests: create <name> --user <username> (prints the token once), list, revoke <name>."

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        create = sub.add_parser("create")
        create.add_argument("name")
        create.add_argument("--user", required=True, help="the username the token acts as")
        sub.add_parser("list")
        revoke = sub.add_parser("revoke")
        revoke.add_argument("name")

    def handle(self, *args, action, **options):
        if action == "create":
            user = User.objects.filter(username=options["user"], is_active=True).first()
            if user is None:
                raise CommandError(f"No active user {options['user']}")
            if ApiToken.objects.filter(name=options["name"]).exists():
                raise CommandError(f"A token named {options['name']} exists")
            token, _ = api_tokens.make(options["name"], user)
            self.stdout.write(token)
            self.stderr.write("Shown once; only its hash is kept.")
        elif action == "list":
            for row in ApiToken.objects.select_related("user").order_by("name"):
                state = f"revoked {row.revoked:%Y-%m-%d}" if row.revoked else "active"
                used = f"{row.last_used:%Y-%m-%d %H:%M}" if row.last_used else "never"
                self.stdout.write(f"{row.name}\t{row.user.get_username()}\t{state}\tcreated {row.created:%Y-%m-%d}\tlast used {used}")
        else:
            rows = ApiToken.objects.filter(name=options["name"])
            if not rows.exists():
                raise CommandError(f"No token named {options['name']}")
            self.stdout.write("revoked" if api_tokens.revoke(rows) else "already revoked")
