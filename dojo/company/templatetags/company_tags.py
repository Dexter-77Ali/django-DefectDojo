"""company: template tags for the sidebar, the finding page and the dashboard (dojo/company/teams.py and risk_requests.py do the work)."""
from django import template

from dojo.company import risk_requests, teams

register = template.Library()


@register.simple_tag
def company_work_links(user):
    """Sidebar "My findings" and "Team findings" URLs; the team URL is None without a team. One query."""
    return teams.work_links(user)


@register.simple_tag
def company_assignment(finding):
    """Finding page: the current team and person, and the teams with the members who may be given the finding."""
    team, person = teams.assignment(finding)
    return {"team": team, "person": person, "teams": teams.assignable_teams([finding])}


@register.simple_tag
def company_my_work(user):
    """Dashboard tiles, computed on the dashboard only."""
    return teams.my_work(user)


@register.simple_tag
def company_risk_pending(user):
    """Sidebar "Risk acceptance": requests waiting for the user; None, without the count query, unless they approve."""
    return risk_requests.pending_count(user)


@register.simple_tag
def company_risk_finding(finding):
    """Finding page: {"pending": a request waits, "can_request": the finding can be requested}."""
    return risk_requests.finding_state(finding)


@register.simple_tag
def company_risk_tiles(user):
    """Dashboard "My risk requests" and, for approvers, "Waiting for my approval"."""
    return risk_requests.tiles(user)


@register.simple_tag
def company_ra_restricted(user):
    """True when approval is required and the user is no approver: upstream's add links are hidden then."""
    return risk_requests.approval_required_for(user)
