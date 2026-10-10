"""
company: findings assigned to a team and, optionally, a person (company_assign, "My work").

The assignment is stored as finding tags, with no model: team:<name> and assignee:<username in lower
case> (Finding.tags is force_lowercase). Tags survive close, reopen and re-import, filter exactly on the
findings lists (?tags=team:wallet) and show in lists and the API. Teams are Django groups named
team:<name>, synced from the directory (dojo.company.ldap.sync_team_membership) or managed with
`manage.py company_teams` for local accounts.
"""
from urllib.parse import urlencode

import pghistory
from django.conf import settings
from django.contrib.auth.models import Group
from django.db import transaction
from django.db.models import Prefetch, Q
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django_tagulous.utils import render_tags

from dojo.company.ldap import TEAM, TEAM_NAME
from dojo.filters import FindingSLAFilter
from dojo.finding.helper import OPEN_FINDINGS_QUERY
from dojo.finding.queries import get_authorized_findings
from dojo.models import Dojo_User, Finding, Notes, Product
from dojo.notifications.helper import create_notification
from dojo.tags.utils import bulk_add_tags_to_instances, bulk_remove_tags_from_instances

ASSIGNEE = "assignee:"
EVENT = "company_finding_assigned"  # in NOTIFICATIONS_SYSTEM_LEVEL_TRUMP (local_settings.py)
MAX_FINDINGS = 500


def instance_language():
    """
    Notes and notifications other people read are written in the instance language (LANGUAGE_CODE), never in the
    language of the user who triggers them: upstream keeps stored values in English whatever the UI language.
    """
    return translation.override(settings.LANGUAGE_CODE)


def assignee_tag(user) -> str:
    return ASSIGNEE + user.username.lower()


def findings_url(*tags, **params) -> str:
    """
    Open findings carrying any of the tags. The list's tags filter is a tagulous field: ONE parameter
    holding a tag string ("team:a, team:b"), so repeated ?tags= parameters would keep only the last.
    """
    return reverse("open_findings") + "?" + urlencode([("tags", render_tags(tags)), *params.items()])


def user_team_names(user) -> list[str]:
    """The user's teams, in one query."""
    return sorted(name[len(TEAM):] for name in user.groups.filter(name__startswith=TEAM).values_list("name", flat=True))


def assignment(finding) -> tuple[str | None, str | None]:
    """(team, username) read from the finding's tags; None when unset."""
    team = person = None
    for tag in finding.tags.all():
        if tag.name.startswith(TEAM):
            team = tag.name[len(TEAM):]
        elif tag.name.startswith(ASSIGNEE):
            person = tag.name[len(ASSIGNEE):]
    return team, person


def members_of_every_product(product_ids) -> set[int]:
    """Ids of the users who are members of every one of the products, directly or through the product type. Two queries."""
    per_product = {product_id: set() for product_id in product_ids}
    for field in ("authorized_users", "prod_type__authorized_users"):
        for product_id, user_id in Product.objects.filter(id__in=list(per_product)).values_list("id", field):
            if user_id is not None:
                per_product[product_id].add(user_id)
    return set.intersection(*per_product.values()) if per_product else set()


def assignable_teams(findings) -> dict[str, list]:
    """
    Every team, with the members who may be given all these findings: active, and superuser, staff, or a
    member of every affected product (directly or through its product type). Not upstream's
    get_authorized_users_for_product_and_product_type, which returns every user to a staff requester.
    """
    members_of_all = members_of_every_product({finding.test.engagement.product_id for finding in findings})
    teams = {
        name[len(TEAM):]: []
        for name in Group.objects.filter(name__startswith=TEAM).order_by("name").values_list("name", flat=True)
        if TEAM_NAME.match(name[len(TEAM):])
    }
    members = (
        Dojo_User.objects.filter(is_active=True, groups__name__startswith=TEAM)
        .filter(Q(is_superuser=True) | Q(is_staff=True) | Q(id__in=members_of_all))
        .prefetch_related(Prefetch("groups", queryset=Group.objects.filter(name__startswith=TEAM), to_attr="teams"))
        .distinct()
        .order_by("first_name", "last_name", "username")
    )
    for user in members:
        for group in user.teams:
            teams.get(group.name[len(TEAM):], []).append(user)
    return teams


def assign(findings, team: str, person, by) -> None:
    """Replace the findings' team:/assignee: tags and add one note per finding, in one transaction."""
    wanted = [TEAM + team] + ([assignee_tag(person)] if person else [])
    stale = {tag.name for finding in findings for tag in finding.tags.all() if tag.name.startswith((TEAM, ASSIGNEE))}
    stale -= set(wanted)
    who = Dojo_User.generate_full_name(by)
    with instance_language():
        if person:
            entry = _("Assigned to %(team)s / %(person)s by %(user)s") % {"team": team, "person": person, "user": who}
        else:
            entry = _("Assigned to %(team)s by %(user)s") % {"team": team, "user": who}
    with transaction.atomic(), pghistory.context(source="company_assign"):
        if stale:
            bulk_remove_tags_from_instances(sorted(stale), findings)
        bulk_add_tags_to_instances(wanted, findings)
        add_notes(findings, entry, by)


def add_notes(findings, entry: str, author) -> None:
    """
    One public note per finding (the audit trail of assignments and risk requests). The same relation upstream
    fills with finding.notes.add(note) (nothing listens to it), in two queries instead of two per finding.
    """
    now = timezone.now()
    notes = Notes.objects.bulk_create([Notes(entry=entry, author=author, date=now) for _finding in findings])
    Finding.notes.through.objects.bulk_create(
        [Finding.notes.through(finding=finding, notes=note) for finding, note in zip(findings, notes, strict=True)],
    )


def notify(count: int, team: str, person, members, by) -> None:
    """
    One notification to the person, or to each member of the team who may open the findings; never to
    the assigner. It links to the person's "My findings" list, or to the team's list for a team assignment.
    """
    recipients = [user.username for user in ([person] if person else members) if user.pk != by.pk]
    if not recipients:
        return
    who = Dojo_User.generate_full_name(by)
    with instance_language():
        target = str(person) if person else _("team %(team)s") % {"team": team}
        title = ngettext("%(user)s assigned %(count)d finding to %(target)s", "%(user)s assigned %(count)d findings to %(target)s", count)
        title %= {"user": who, "count": count, "target": target}
    create_notification(
        event=EVENT,
        title=title,
        description=title,
        recipients=recipients,
        url=findings_url(assignee_tag(person) if person else TEAM + team),
        icon="user-check",
    )


def work_links(user) -> dict:
    """Sidebar "My findings" and "My team's findings" URLs; the team URL is None without a team. One query."""
    team_tags = [TEAM + name for name in user_team_names(user)]
    return {"mine": findings_url(assignee_tag(user)), "team": findings_url(*team_tags) if team_tags else None}


def my_work(user) -> dict:
    """Dashboard tiles: (count, list URL) of open findings assigned to the user, their teams, and theirs out of SLA."""
    open_findings = get_authorized_findings("view", user=user).filter(OPEN_FINDINGS_QUERY)
    mine_tag = assignee_tag(user)
    mine = open_findings.filter(tags__name=mine_tag)
    team_tags = [TEAM + name for name in user_team_names(user)]
    # the findings list's "Outside of SLA: True" filter (?outside_of_sla=1), so a tile counts what its link shows
    overdue = FindingSLAFilter().filter(mine, 1)
    return {
        "mine": (mine.count(), findings_url(mine_tag)),
        "team": (open_findings.filter(tags__name__in=team_tags).distinct().count(), findings_url(*team_tags)) if team_tags else None,
        "overdue": (overdue.count(), findings_url(mine_tag, outside_of_sla=1)),
    }
