"""
company: teams for local accounts and installs without a directory.

    manage.py company_teams list
    manage.py company_teams create <team>
    manage.py company_teams add <team> <username>
    manage.py company_teams remove <team> <username>
    manage.py company_teams delete <team>

A team is the Django group team:<team>; findings are assigned to it with the finding tag team:<team>
(dojo/company/teams.py). Team names are lower case: letters, digits, ".", "_" and "-". With LDAP on,
the directory decides the teams of directory users on every login (groups dojo-team-<team>), so add
directory users to the directory group instead; a membership added here would be removed again.
"""
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from dojo.company.ldap import TEAM, TEAM_NAME
from dojo.models import Dojo_User, Finding


def team_group_name(name: str) -> str:
    name = name.strip().lower()
    if not TEAM_NAME.match(name):
        msg = f"{name!r} is not a valid team name: lower-case letters, digits, '.', '_' and '-', at most 64 characters"
        raise CommandError(msg)
    return TEAM + name


class Command(BaseCommand):

    """List, create, delete teams and change their members."""

    help = "Manage teams (Django groups team:<name>) that findings are assigned to"

    def add_arguments(self, parser):
        actions = parser.add_subparsers(dest="action", required=True)
        actions.add_parser("list", help="teams and their members")
        actions.add_parser("create", help="create a team").add_argument("team")
        actions.add_parser("delete", help="delete a team; findings keep their team tag").add_argument("team")
        for action in ("add", "remove"):
            sub = actions.add_parser(action, help=f"{action} a user")
            sub.add_argument("team")
            sub.add_argument("username")

    def handle(self, *args, **options):
        action = options["action"]
        if action == "list":
            self.list_teams()
            return
        name = team_group_name(options["team"])
        if action == "create":
            _group, created = Group.objects.get_or_create(name=name)
            self.stdout.write(f"{'created' if created else 'already exists'}: {name}")
            return
        group = Group.objects.filter(name=name).first()
        if group is None:
            msg = f"no team {name!r}; create it first"
            raise CommandError(msg)
        if action == "delete":
            group.delete()
            tagged = Finding.objects.filter(tags__name=name).count()
            self.stdout.write(f"deleted: {name} ({tagged} findings keep the tag {name})")
            return
        user = Dojo_User.objects.filter(username__iexact=options["username"]).first()
        if user is None:
            msg = f"no user {options['username']!r}"
            raise CommandError(msg)
        if action == "add":
            user.groups.add(group)
        else:
            user.groups.remove(group)
        self.stdout.write(f"{'added' if action == 'add' else 'removed'}: {user.username} {'to' if action == 'add' else 'from'} {name}")

    def list_teams(self):
        groups = Group.objects.filter(name__startswith=TEAM).order_by("name").prefetch_related("user_set")
        for group in groups:
            members = ", ".join(sorted(user.username for user in group.user_set.all())) or "no members"
            self.stdout.write(f"{group.name}: {members}")
        if not groups:
            self.stdout.write("no teams")
