"""company: the authentication backend; imported only when DD_COMPANY_LDAP_ENABLED is on (needs django-auth-ldap)."""

from django_auth_ldap.backend import LDAPBackend

from dojo.company.ldap import sync_product_type_access


class CompanyLDAPBackend(LDAPBackend):

    """django-auth-ldap backend that keeps local accounts local and syncs product-type access from groups."""

    def get_or_build_user(self, username, ldap_user):
        user, built = super().get_or_build_user(username, ldap_user)
        # A local account (one with a usable password: the bootstrap admin, break-glass and
        # service accounts) is never claimed by a directory account of the same name. Without
        # this, a directory user named like a local superuser would log in as that superuser,
        # or demote it and take over its identity. django-auth-ldap creates its own users
        # with an unusable password, which is the convention DefectDojo already relies on.
        if not built and user.has_usable_password():
            msg = f"{username!r} is a local account, not a directory account"
            raise ldap_user.AuthenticationFailed(msg)
        return user, built

    def get_user(self, user_id):
        # Mirror ModelBackend.user_can_authenticate: a deactivated user's session must end on
        # the next request instead of living on until the cookie expires.
        user = super().get_user(user_id)
        return user if user is not None and user.is_active else None

    def authenticate_ldap_user(self, ldap_user, password):
        user = super().authenticate_ldap_user(ldap_user, password)
        if user is None or not user.is_active:
            # also closes /api/v2/api-token-auth/, which relies on authenticate() refusing inactive users
            return None
        sync_product_type_access(user, ldap_user.group_names)
        return user
