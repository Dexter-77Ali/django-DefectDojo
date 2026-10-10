"""company: template tags for the sidebar, the finding page and the dashboard (dojo/company/teams.py does the work)."""
from django import template

from dojo.company import teams

register = template.Library()


@register.simple_tag
def company_work_links(user):
    """Sidebar "My findings" and "My team's findings" URLs; the team URL is None without a team. One query."""
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
