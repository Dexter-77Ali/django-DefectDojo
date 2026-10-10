"""
company: provision directory users before their first login, and catch leavers.

    manage.py company_ldap_sync

Reads the members of the directory groups DefectDojo uses (the admin group, dojo-pt-<product type>
and dojo-team-<team>, prefixes from the settings) with the django-auth-ldap connection settings, then
for each member creates or updates the DefectDojo user (CompanyLDAPBackend.populate_user) and applies
the same product-type and team sync as a login. Run it at rollout and nightly (cron), so people can be
picked as assignees before they have logged in. Local accounts (with a usable password) are never
touched: the backend refuses them. Only direct group members are read, like the login sync.

Directory users (Django group company:directory) the member read did not reach are then rechecked:
someone still in the directory gets the login sync (outside every group, that removes their product
types, teams and admin flags); someone no longer in the directory, or disabled in Active Directory,
is deactivated, which also ends their sessions and API token.
"""
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from dojo.company.ldap import AD_ENABLED, DEFAULT_PREFIX, DEFAULT_TEAM_PREFIX, DIRECTORY

logger = logging.getLogger(__name__)

# The attribute holding the username on a member's entry, and a filter that keeps enabled people only
# (an AD group nested in a group also has a sAMAccountName; it must not become a DefectDojo user).
PROFILES = {
    "ad": ("sAMAccountName", f"(&(objectCategory=person)(objectClass=user){AD_ENABLED})"),
    "openldap": ("uid", "(uid=*)"),
}


def bind(backend):
    """A connection bound with AUTH_LDAP_BIND_DN, set up as django-auth-ldap sets up its own."""
    ldap_settings = backend.settings
    connection = backend.ldap.initialize(ldap_settings.SERVER_URI, bytes_mode=False)  # backend.ldap applies GLOBAL_OPTIONS
    for option, value in ldap_settings.CONNECTION_OPTIONS.items():
        connection.set_option(option, value)
    if ldap_settings.START_TLS:
        connection.start_tls_s()
    connection.simple_bind_s(ldap_settings.BIND_DN, ldap_settings.BIND_PASSWORD)
    return connection


def _values(attrs, name):
    return [value.decode() if isinstance(value, bytes) else value for value in attrs.get(name, [])]


def group_member_dns(connection, group_search, prefixes, admin_group_dn=None) -> set[str]:
    """Member DNs of the groups whose cn starts with one of the prefixes, and of the admin group."""
    import ldap  # noqa: PLC0415 -- python-ldap is only in the company image
    from ldap.filter import escape_filter_chars  # noqa: PLC0415

    names = "".join(f"(cn={escape_filter_chars(prefix)}*)" for prefix in prefixes)
    results = connection.search_s(group_search.base_dn, group_search.scope, f"(&{group_search.filterstr}(|{names}))", ["member"])
    if admin_group_dn:
        try:
            results += connection.search_s(admin_group_dn, ldap.SCOPE_BASE, "(objectClass=*)", ["member"])
        except ldap.NO_SUCH_OBJECT:
            logger.warning("company ldap sync: admin group %s does not exist", admin_group_dn)
    members = set()
    for dn, attrs in results:
        if dn is None:  # Active Directory search referral
            continue
        for name in attrs:
            # Active Directory returns the members of a large group as member;range=0-1499 instead of member
            if name.lower() == "member":
                members.update(_values(attrs, name))
            elif name.lower().startswith("member;range="):
                # ponytail: first page only; members past it are provisioned at first login. Add ranged reads if needed.
                logger.warning("company ldap sync: %s has more members than one read returns; only the first page is read", dn)
                members.update(_values(attrs, name))
    return members


def usernames_for(connection, member_dns, profile) -> set[str]:
    """Usernames of the people among the DNs; groups, computers, disabled AD accounts and dangling DNs are skipped."""
    import ldap  # noqa: PLC0415 -- python-ldap is only in the company image

    attribute, people = PROFILES[profile]
    usernames = set()
    # ponytail: one read per member; fine for thousands of members on a nightly run
    for dn in sorted(member_dns):
        try:
            results = connection.search_s(dn, ldap.SCOPE_BASE, people, [attribute])
        except ldap.NO_SUCH_OBJECT:
            continue
        for _dn, attrs in results:
            usernames.update(_values(attrs, attribute)[:1])
    return usernames


class Command(BaseCommand):

    """Create and sync DefectDojo users for every member of the directory groups DefectDojo uses."""

    help = "Create or update directory users and their product types and teams from the directory groups"

    def handle(self, *args, **options):
        if not getattr(settings, "COMPANY_LDAP_ENABLED", False):
            msg = "DD_COMPANY_LDAP_ENABLED is off"
            raise CommandError(msg)
        from dojo.company import ldap_backend  # noqa: PLC0415 -- needs django-auth-ldap

        self.sync = ldap_backend.sync_directory_access
        backend = ldap_backend.CompanyLDAPBackend()
        prefixes = (
            getattr(settings, "COMPANY_LDAP_PRODUCT_TYPE_GROUP_PREFIX", DEFAULT_PREFIX),
            getattr(settings, "COMPANY_LDAP_TEAM_GROUP_PREFIX", DEFAULT_TEAM_PREFIX),
        )
        admin_group = getattr(settings, "AUTH_LDAP_USER_FLAGS_BY_GROUP", {}).get("is_superuser")
        counts = {"created": 0, "updated": 0, "inactive": 0, "skipped": 0, "rechecked": 0, "deactivated": 0}
        connection = bind(backend)
        try:
            dns = group_member_dns(connection, backend.settings.GROUP_SEARCH, prefixes, admin_group)
            usernames = usernames_for(connection, dns, getattr(settings, "COMPANY_LDAP_PROFILE", "ad"))
            seen = self.sync_members(backend, usernames, counts)
            if counts["created"] + counts["updated"]:
                self.check_leavers(backend, connection, seen, counts)
            else:
                # a wrong user search base finds nobody: skip rather than deactivate every directory user
                self.stderr.write("no directory group member could be synced; the leaver check is skipped")
        finally:
            connection.unbind_s()
        self.stdout.write(
            "{members} directory group members: {created} created, {updated} updated, {inactive} inactive, "
            "{skipped} skipped; {rechecked} rechecked, {deactivated} deactivated".format(members=len(usernames), **counts),
        )

    def sync_members(self, backend, usernames, counts) -> set[int]:
        """Create or update every group member; returns the ids of the users reached."""
        users = get_user_model().objects
        seen = set()
        for username in sorted(usernames, key=str.lower):
            existed = users.filter(username__iexact=username).exists()
            user = backend.populate_user(username)  # None for a local account or a name outside the user search base
            if user is None:
                counts["skipped"] += 1
                self.stdout.write(f"skipped: {username} (local account, or not found under the user search base)")
                continue
            seen.add(user.pk)
            if not user.is_active:  # a deactivated user stays as the administrator left it, like at login
                counts["inactive"] += 1
                continue
            product_types, teams = self.sync(user, user.ldap_user.group_names)
            counts["updated" if existed else "created"] += 1
            self.stdout.write(f"{'updated' if existed else 'created'}: {user.username} "
                              f"product types {sorted(product_types) or '-'}, teams {sorted(teams) or '-'}")
        return seen

    def check_leavers(self, backend, connection, seen, counts):
        """Active directory users outside the groups read: resynced while still in the directory, else deactivated."""
        leavers = get_user_model().objects.filter(is_active=True, groups__name=DIRECTORY).exclude(pk__in=seen)
        for user in leavers.order_by("username"):
            if user.has_usable_password():  # made a local account by an administrator: never touched
                continue
            # Our own read, not populate_user: that also returns None on a directory error, which must not deactivate anyone.
            if not backend.settings.USER_SEARCH.execute(connection, {"user": user.username}):
                user.is_active = False
                user.save(update_fields=["is_active"])
                counts["deactivated"] += 1
                self.stdout.write(f"deactivated: {user.username} (no longer in the directory, or disabled)")
                continue
            directory_user = backend.populate_user(user.username)  # also resets the admin flags
            if directory_user is None:
                continue
            product_types, teams = self.sync(directory_user, directory_user.ldap_user.group_names)
            counts["rechecked"] += 1
            self.stdout.write(f"rechecked: {user.username} "
                              f"product types {sorted(product_types) or '-'}, teams {sorted(teams) or '-'}")
