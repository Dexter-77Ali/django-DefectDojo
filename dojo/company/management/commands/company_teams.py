"""
company: teams and risk approvers for local accounts and installs without a directory.

    manage.py company_teams list
    manage.py company_teams create <team>
    manage.py company_teams add <team> <username>
    manage.py company_teams remove <team> <username>
    manage.py company_teams delete <team>
    manage.py company_teams --approver <username>
    manage.py company_teams --no-approver <username>

A team is the Django group team:<team>; findings are assigned to it with the finding tag team:<team>
(dojo/company/teams.py). Team names are lower case: letters, digits, ".", "_" and "-". With LDAP on,
the directory decides the teams of directory users on every login (groups dojo-team-<team>), so add
directory users to the directory group instead; a membership added here would be removed again.

--approver puts the user in the Django group risk-approvers, who approve risk acceptance requests
(dojo/company/risk_requests.py); --no-approver takes them out. With DD_COMPANY_LDAP_RISK_APPROVER_GROUP
set, the directory decides this for directory users too.
"""
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError

from dojo.company.ldap import TEAM, TEAM_NAME
from dojo.company.risk_requests import APPROVER_GROUP, approver_group
from dojo.models import Dojo_User, Finding


def team_group_name(name: str) -> str:
    name = name.strip().lower()
    if not TEAM_NAME.match(name):
        msg = f"{name!r} is not a valid team name: lower-case letters, digits, '.', '_' and '-', at most 64 characters"
        raise CommandError(msg)
    return TEAM + name


def find_user(username: str):
    user = Dojo_User.objects.filter(username__iexact=username).first()
    if user is None:
        msg = f"no user {username!r}"
        raise CommandError(msg)
    return user


class Command(BaseCommand):

    """List, create, delete teams and change their members; grant or revoke the risk approver role."""

    help = "Manage teams (Django groups team:<name>) that findings are assigned to, and risk approvers"

    def add_arguments(self, parser):
        parser.add_argument("--approver", metavar="USERNAME", help="make the user a risk approver (group risk-approvers)")
        parser.add_argument("--no-approver", metavar="USERNAME", help="take the risk approver role from the user")
        actions = parser.add_subparsers(dest="action")
        actions.add_parser("list", help="teams and their members, and the risk approvers")
        actions.add_parser("create", help="create a team").add_argument("team")
        actions.add_parser("delete", help="delete a team; findings keep their team tag").add_argument("team")
        for action in ("add", "remove"):
            sub = actions.add_parser(action, help=f"{action} a user")
            sub.add_argument("team")
            sub.add_argument("username")

    def handle(self, *args, **options):
        if options["approver"] or options["no_approver"]:
            self.approver(options["approver"], options["no_approver"])
            return
        action = options["action"]
        if action is None:
            msg = "name an action (list, create, delete, add, remove) or --approver/--no-approver <username>"
            raise CommandError(msg)
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
        user = find_user(options["username"])
        if action == "add":
            user.groups.add(group)
        else:
            user.groups.remove(group)
        self.stdout.write(f"{'added' if action == 'add' else 'removed'}: {user.username} {'to' if action == 'add' else 'from'} {name}")

    def approver(self, grant, revoke):
        group = approver_group()
        if grant:
            user = find_user(grant)
            user.groups.add(group)
            self.stdout.write(f"approver: {user.username} is in {APPROVER_GROUP}")
        if revoke:
            user = find_user(revoke)
            user.groups.remove(group)
            self.stdout.write(f"not an approver: {user.username} left {APPROVER_GROUP}")

    def list_teams(self):
        groups = Group.objects.filter(name__startswith=TEAM).order_by("name").prefetch_related("user_set")
        for group in groups:
            members = ", ".join(sorted(user.username for user in group.user_set.all())) or "no members"
            self.stdout.write(f"{group.name}: {members}")
        if not groups:
            self.stdout.write("no teams")
        approvers = sorted(Dojo_User.objects.filter(groups__name=APPROVER_GROUP).values_list("username", flat=True))
        self.stdout.write(f"{APPROVER_GROUP}: {', '.join(approvers) or 'no members'}")
