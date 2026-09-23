"""
company: LDAP / Active Directory login with directory-driven product-type access.

Off unless DD_COMPANY_LDAP_ENABLED=True. dojo/settings/local_settings.py then calls
``configure(globals(), env)`` which fills the django-auth-ldap settings from:

    DD_COMPANY_LDAP_PROFILE        ad (sAMAccountName, memberOf) | openldap (uid, groupOfNames)
    DD_COMPANY_LDAP_SERVER_URI     ldap://host:389 or ldaps://host:636
    DD_COMPANY_LDAP_START_TLS      True to upgrade a plain connection
    DD_COMPANY_LDAP_CA_CERT_PATH   PEM file of the CA that issued the directory certificate
                                   (Active Directory: usually the internal enterprise CA);
                                   unset or an empty file = the system CA store
    DD_COMPANY_LDAP_BIND_DN / DD_COMPANY_LDAP_BIND_PASSWORD   read-only service account
    DD_COMPANY_LDAP_USER_BASE / DD_COMPANY_LDAP_GROUP_BASE    search bases
    DD_COMPANY_LDAP_ADMIN_GROUP    group DN whose members become superuser + staff
    DD_COMPANY_LDAP_PRODUCT_TYPE_GROUP_PREFIX   groups named <prefix><product type name>
                                   grant access to that product type (default dojo-pt-)

Role model of open-source 3.x: superuser, staff (read/write everywhere) and per
product / product-type authorized users. The directory is authoritative for LDAP
users: on every login their product-type memberships are set to exactly the
groups they carry. The local ModelBackend stays last so the admin account and
API token login keep working. Local accounts (usable password) are never matched
by a directory account of the same name (see ldap_backend.py), and a deactivated
directory user's session ends on the next request.
"""

import logging
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

# No model imports at module level: local_settings.py imports this module while
# settings are still loading, before the app registry exists.

logger = logging.getLogger(__name__)

BACKEND = "dojo.company.ldap_backend.CompanyLDAPBackend"
DEFAULT_PREFIX = "dojo-pt-"


def configure(target: dict, env) -> None:
    """Fill django-auth-ldap settings into ``target`` (the settings namespace) from DD_COMPANY_LDAP_* variables."""
    import ldap  # noqa: PLC0415 -- only present in the company image
    from django_auth_ldap.config import ActiveDirectoryGroupType, GroupOfNamesType, LDAPSearch  # noqa: PLC0415

    profile = env("DD_COMPANY_LDAP_PROFILE", default="ad")
    if profile == "ad":
        user_filter, group_filter, group_type = "(sAMAccountName=%(user)s)", "(objectClass=group)", ActiveDirectoryGroupType()
    elif profile == "openldap":
        user_filter, group_filter, group_type = "(uid=%(user)s)", "(objectClass=groupOfNames)", GroupOfNamesType(name_attr="cn")
    else:
        msg = f"DD_COMPANY_LDAP_PROFILE must be 'ad' or 'openldap', got {profile!r}"
        raise ImproperlyConfigured(msg)

    target.update({
        "AUTH_LDAP_SERVER_URI": env("DD_COMPANY_LDAP_SERVER_URI"),
        "AUTH_LDAP_START_TLS": env.bool("DD_COMPANY_LDAP_START_TLS", default=False),
        "AUTH_LDAP_BIND_DN": env("DD_COMPANY_LDAP_BIND_DN", default=""),
        "AUTH_LDAP_BIND_PASSWORD": env("DD_COMPANY_LDAP_BIND_PASSWORD", default=""),
        "AUTH_LDAP_USER_SEARCH": LDAPSearch(env("DD_COMPANY_LDAP_USER_BASE"), ldap.SCOPE_SUBTREE, user_filter),
        "AUTH_LDAP_GROUP_SEARCH": LDAPSearch(env("DD_COMPANY_LDAP_GROUP_BASE"), ldap.SCOPE_SUBTREE, group_filter),
        "AUTH_LDAP_GROUP_TYPE": group_type,
        "AUTH_LDAP_USER_ATTR_MAP": {"first_name": "givenName", "last_name": "sn", "email": "mail"},
        "AUTH_LDAP_ALWAYS_UPDATE_USER": True,
        "AUTH_LDAP_MIRROR_GROUPS": False,
        "COMPANY_LDAP_PRODUCT_TYPE_GROUP_PREFIX": env("DD_COMPANY_LDAP_PRODUCT_TYPE_GROUP_PREFIX", default=DEFAULT_PREFIX),
    })
    admin_group = env("DD_COMPANY_LDAP_ADMIN_GROUP", default="")
    if admin_group:
        target["AUTH_LDAP_USER_FLAGS_BY_GROUP"] = {"is_superuser": admin_group, "is_staff": admin_group}
    ca_path = env("DD_COMPANY_LDAP_CA_CERT_PATH", default="")
    if ca_path:
        if not Path(ca_path).is_file():
            msg = f"DD_COMPANY_LDAP_CA_CERT_PATH {ca_path!r} is not a file"
            raise ImproperlyConfigured(msg)
        if Path(ca_path).stat().st_size:  # an empty placeholder file keeps the system CA store
            target["AUTH_LDAP_GLOBAL_OPTIONS"] = {
                ldap.OPT_X_TLS_CACERTFILE: ca_path,
                ldap.OPT_X_TLS_REQUIRE_CERT: ldap.OPT_X_TLS_DEMAND,
            }
    target["AUTHENTICATION_BACKENDS"] = (BACKEND, *target["AUTHENTICATION_BACKENDS"])


def product_type_names_from_groups(group_names, prefix: str | None = None) -> set[str]:
    """Lower-cased product type names encoded in group names that carry the prefix."""
    prefix = (prefix or getattr(settings, "COMPANY_LDAP_PRODUCT_TYPE_GROUP_PREFIX", DEFAULT_PREFIX)).lower()
    return {
        name[len(prefix):].strip().lower()
        for name in group_names
        if name.lower().startswith(prefix) and name[len(prefix):].strip()
    }


def sync_product_type_access(user, group_names, prefix: str | None = None) -> set[str]:
    """Make the user's product-type memberships equal to the directory groups; returns the granted names."""
    from dojo.models import Product_Type  # noqa: PLC0415 -- see the module-level note

    wanted = product_type_names_from_groups(group_names, prefix)
    granted = set()
    # ponytail: one query per product type; fine for tens of types, revisit with hundreds.
    for product_type in Product_Type.objects.all():
        if product_type.name.strip().lower() in wanted:
            product_type.authorized_users.add(user.pk)
            granted.add(product_type.name)
        else:
            product_type.authorized_users.remove(user.pk)
    logger.info("company ldap: %s authorized for product types %s", user.username, sorted(granted) or "none")
    return granted
