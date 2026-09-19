"""company: the authentication backend; imported only when DD_COMPANY_LDAP_ENABLED is on (needs django-auth-ldap)."""

from django_auth_ldap.backend import LDAPBackend

from dojo.company.ldap import sync_product_type_access


class CompanyLDAPBackend(LDAPBackend):

    """django-auth-ldap backend that also syncs product-type access from the user's groups."""

    def authenticate_ldap_user(self, ldap_user, password):
        user = super().authenticate_ldap_user(ldap_user, password)
        if user is not None:
            sync_product_type_access(user, ldap_user.group_names)
        return user
