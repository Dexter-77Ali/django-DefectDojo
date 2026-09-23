"""company: LDAP login and directory-driven product-type access (Phase 4b)."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from dojo.company import ldap as company_ldap
from dojo.models import Product_Type
from unittests.dojo_test_case import DojoTestCase, versioned_fixtures

HAS_AUTH_LDAP = importlib.util.find_spec("django_auth_ldap") is not None


class FakeEnv:

    """Minimal stand-in for django-environ: values from a dict, ImproperlyConfigured-like KeyError when missing."""

    def __init__(self, values):
        self.values = values

    def __call__(self, name, default=None, **_):
        if name in self.values:
            return self.values[name]
        if default is None:
            raise KeyError(name)
        return default

    def bool(self, name, *, default=False):
        return self.values.get(name, default)


@versioned_fixtures
class TestProductTypeSync(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user("ldap.user", "ldap.user@example.com")
        types = list(Product_Type.objects.order_by("id")[:2])
        self.first, self.second = types[0], types[1]

    def _members(self, product_type):
        return set(product_type.authorized_users.values_list("username", flat=True))

    def test_group_names_to_product_types(self):
        names = company_ldap.product_type_names_from_groups(
            ["dojo-pt-Payments", "DOJO-PT- Retail ", "dojo-pt-", "developers"], prefix="dojo-pt-",
        )
        self.assertEqual(names, {"payments", "retail"})

    def test_sync_is_authoritative(self):
        granted = company_ldap.sync_product_type_access(
            self.user, {f"dojo-pt-{self.first.name.upper()}", "developers"}, prefix="dojo-pt-",
        )
        self.assertEqual(granted, {self.first.name})
        self.assertIn(self.user.username, self._members(self.first))
        self.assertNotIn(self.user.username, self._members(self.second))

        granted = company_ldap.sync_product_type_access(self.user, {f"dojo-pt-{self.second.name}"}, prefix="dojo-pt-")
        self.assertEqual(granted, {self.second.name})
        self.assertNotIn(self.user.username, self._members(self.first))
        self.assertIn(self.user.username, self._members(self.second))

        self.assertEqual(company_ldap.sync_product_type_access(self.user, set(), prefix="dojo-pt-"), set())
        self.assertNotIn(self.user.username, self._members(self.second))


@unittest.skipUnless(HAS_AUTH_LDAP, "django-auth-ldap is only installed in the company image")
class TestConfigure(DojoTestCase):

    def _configure(self, profile, **extra):
        env = FakeEnv({
            "DD_COMPANY_LDAP_PROFILE": profile,
            "DD_COMPANY_LDAP_SERVER_URI": "ldap://ldap:389",
            "DD_COMPANY_LDAP_USER_BASE": "ou=users,dc=company,dc=test",
            "DD_COMPANY_LDAP_GROUP_BASE": "ou=groups,dc=company,dc=test",
            "DD_COMPANY_LDAP_ADMIN_GROUP": "cn=dojo-admins,ou=groups,dc=company,dc=test",
            **extra,
        })
        target = {"AUTHENTICATION_BACKENDS": ("django.contrib.auth.backends.ModelBackend",)}
        company_ldap.configure(target, env)
        return target

    def test_active_directory_profile(self):
        target = self._configure("ad")
        self.assertEqual(target["AUTHENTICATION_BACKENDS"][0], company_ldap.BACKEND)
        self.assertEqual(target["AUTHENTICATION_BACKENDS"][-1], "django.contrib.auth.backends.ModelBackend")
        self.assertIn("sAMAccountName", target["AUTH_LDAP_USER_SEARCH"].filterstr)
        self.assertEqual(type(target["AUTH_LDAP_GROUP_TYPE"]).__name__, "ActiveDirectoryGroupType")
        self.assertEqual(target["AUTH_LDAP_USER_FLAGS_BY_GROUP"]["is_superuser"], "cn=dojo-admins,ou=groups,dc=company,dc=test")
        self.assertEqual(target["COMPANY_LDAP_PRODUCT_TYPE_GROUP_PREFIX"], "dojo-pt-")

    def test_openldap_profile(self):
        target = self._configure("openldap")
        self.assertIn("uid=", target["AUTH_LDAP_USER_SEARCH"].filterstr)
        self.assertEqual(type(target["AUTH_LDAP_GROUP_TYPE"]).__name__, "GroupOfNamesType")

    def test_unknown_profile_fails_loudly(self):
        with self.assertRaises(ImproperlyConfigured):
            self._configure("nope")

    def test_internal_ca_certificate(self):
        import ldap  # noqa: PLC0415 -- company image only

        with tempfile.TemporaryDirectory() as tmp:
            ca = Path(tmp) / "ca.pem"
            ca.write_text("-----BEGIN CERTIFICATE-----", encoding="utf-8")
            options = self._configure("ad", DD_COMPANY_LDAP_CA_CERT_PATH=str(ca))["AUTH_LDAP_GLOBAL_OPTIONS"]
            self.assertEqual(options, {ldap.OPT_X_TLS_CACERTFILE: str(ca), ldap.OPT_X_TLS_REQUIRE_CERT: ldap.OPT_X_TLS_DEMAND})

            ca.write_text("", encoding="utf-8")  # the empty placeholder from secrets.example: system CA store
            self.assertNotIn("AUTH_LDAP_GLOBAL_OPTIONS", self._configure("ad", DD_COMPANY_LDAP_CA_CERT_PATH=str(ca)))
            self.assertNotIn("AUTH_LDAP_GLOBAL_OPTIONS", self._configure("ad"))

            with self.assertRaises(ImproperlyConfigured):  # a typo must not fall back silently
                self._configure("ad", DD_COMPANY_LDAP_CA_CERT_PATH=str(Path(tmp) / "missing.pem"))


@unittest.skipUnless(HAS_AUTH_LDAP, "django-auth-ldap is only installed in the company image")
@versioned_fixtures
@override_settings(COMPANY_LDAP_PRODUCT_TYPE_GROUP_PREFIX="dojo-pt-")
class TestBackend(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def test_login_syncs_product_types(self):
        from dojo.company.ldap_backend import CompanyLDAPBackend  # noqa: PLC0415 -- needs django-auth-ldap

        user = get_user_model().objects.create_user("bob", "bob@company.test")
        product_type = Product_Type.objects.order_by("id").first()

        class FakeLdapUser:
            group_names = {f"dojo-pt-{product_type.name}"}

        with patch("django_auth_ldap.backend.LDAPBackend.authenticate_ldap_user", return_value=user):
            result = CompanyLDAPBackend().authenticate_ldap_user(FakeLdapUser(), "bob123")
        self.assertIs(result, user)
        self.assertIn("bob", set(product_type.authorized_users.values_list("username", flat=True)))

    def test_directory_account_cannot_claim_a_local_account(self):
        from django_auth_ldap.backend import _LDAPUser  # noqa: PLC0415, PLC2701 -- company image only

        from dojo.company.ldap_backend import CompanyLDAPBackend  # noqa: PLC0415

        local_admin = self.get_test_admin()
        self.assertTrue(local_admin.has_usable_password())
        backend = CompanyLDAPBackend()
        ldap_user = _LDAPUser(backend, username=local_admin.username)
        with self.assertRaises(ldap_user.AuthenticationFailed):
            backend.get_or_build_user(local_admin.username, ldap_user)
        local_admin.refresh_from_db()
        self.assertTrue(local_admin.is_superuser)

        directory_user = get_user_model().objects.create_user("dave", "dave@company.test")
        directory_user.set_unusable_password()
        directory_user.save()
        user, built = backend.get_or_build_user("dave", _LDAPUser(backend, username="dave"))
        self.assertEqual(user.pk, directory_user.pk)
        self.assertFalse(built)

    def test_inactive_directory_user_cannot_log_in(self):
        from dojo.company.ldap_backend import CompanyLDAPBackend  # noqa: PLC0415

        user = get_user_model().objects.create_user("frank", "frank@company.test", is_active=False)

        class FakeLdapUser:
            group_names = set()

        with patch("django_auth_ldap.backend.LDAPBackend.authenticate_ldap_user", return_value=user):
            self.assertIsNone(CompanyLDAPBackend().authenticate_ldap_user(FakeLdapUser(), "x"))

    def test_deactivated_directory_user_loses_the_session(self):
        from dojo.company.ldap_backend import CompanyLDAPBackend  # noqa: PLC0415

        user = get_user_model().objects.create_user("erin", "erin@company.test")
        backend = CompanyLDAPBackend()
        self.assertEqual(backend.get_user(user.pk).pk, user.pk)
        user.is_active = False
        user.save()
        self.assertIsNone(backend.get_user(user.pk))
