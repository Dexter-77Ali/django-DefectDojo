"""company: LDAP login, directory-driven product-type access and teams, manage.py company_ldap_sync."""

import importlib.util
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import PropertyMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
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


class TestTeamSync(DojoTestCase):

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user("ldap.dev", "ldap.dev@example.com")

    def _groups(self):
        return set(self.user.groups.values_list("name", flat=True))

    def test_group_names_to_team_names(self):
        names = company_ldap.team_names_from_groups(
            ["dojo-team-Wallet", "DOJO-TEAM-Merchant Services", "dojo-team-", "dojo-team-:/", "dojo-pt-Wallet", "developers"],
            prefix="dojo-team-",
        )
        self.assertEqual(names, {"wallet", "merchant-services"})

    def test_sync_is_authoritative_for_team_groups_only(self):
        other = Group.objects.create(name="auditors")
        self.user.groups.add(other, Group.objects.create(name="team:legacy"))

        teams = company_ldap.sync_team_membership(self.user, {"dojo-team-Wallet", "dojo-team-api", "dojo-pt-x"}, prefix="dojo-team-")

        self.assertEqual(teams, {"wallet", "api"})
        self.assertEqual(self._groups(), {"auditors", "team:wallet", "team:api"})
        self.assertTrue(Group.objects.filter(name="team:legacy").exists())  # the group stays, only the membership goes

        self.assertEqual(company_ldap.sync_team_membership(self.user, set(), prefix="dojo-team-"), set())
        self.assertEqual(self._groups(), {"auditors"})

    @override_settings(COMPANY_LDAP_TEAM_GROUP_PREFIX="grp-team-")
    def test_prefix_comes_from_the_settings(self):
        self.assertEqual(company_ldap.sync_team_membership(self.user, {"grp-team-wallet", "dojo-team-api"}), {"wallet"})


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
        self.assertIn(company_ldap.AD_ENABLED, target["AUTH_LDAP_USER_SEARCH"].filterstr)  # disabled accounts are not found
        self.assertEqual(type(target["AUTH_LDAP_GROUP_TYPE"]).__name__, "ActiveDirectoryGroupType")
        self.assertEqual(target["AUTH_LDAP_USER_FLAGS_BY_GROUP"]["is_superuser"], "cn=dojo-admins,ou=groups,dc=company,dc=test")
        self.assertEqual(target["COMPANY_LDAP_PRODUCT_TYPE_GROUP_PREFIX"], "dojo-pt-")
        self.assertEqual(target["COMPANY_LDAP_TEAM_GROUP_PREFIX"], "dojo-team-")
        self.assertEqual(target["COMPANY_LDAP_PROFILE"], "ad")

    def test_openldap_profile(self):
        target = self._configure("openldap", DD_COMPANY_LDAP_TEAM_GROUP_PREFIX="grp-team-")
        self.assertIn("uid=", target["AUTH_LDAP_USER_SEARCH"].filterstr)
        self.assertEqual(type(target["AUTH_LDAP_GROUP_TYPE"]).__name__, "GroupOfNamesType")
        self.assertEqual(target["COMPANY_LDAP_TEAM_GROUP_PREFIX"], "grp-team-")
        self.assertEqual(target["COMPANY_LDAP_PROFILE"], "openldap")

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
@override_settings(COMPANY_LDAP_PRODUCT_TYPE_GROUP_PREFIX="dojo-pt-", COMPANY_LDAP_TEAM_GROUP_PREFIX="dojo-team-")
class TestBackend(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def test_login_syncs_product_types_and_teams(self):
        from dojo.company.ldap_backend import CompanyLDAPBackend  # noqa: PLC0415 -- needs django-auth-ldap

        user = get_user_model().objects.create_user("bob", "bob@company.test")
        product_type = Product_Type.objects.order_by("id").first()

        class FakeLdapUser:
            group_names = {f"dojo-pt-{product_type.name}", "dojo-team-wallet"}

        with patch("django_auth_ldap.backend.LDAPBackend.authenticate_ldap_user", return_value=user):
            result = CompanyLDAPBackend().authenticate_ldap_user(FakeLdapUser(), "bob123")
        self.assertIs(result, user)
        self.assertIn("bob", set(product_type.authorized_users.values_list("username", flat=True)))
        self.assertEqual(set(user.groups.values_list("name", flat=True)), {"team:wallet", company_ldap.DIRECTORY})

    @override_settings(COMPANY_LDAP_RISK_APPROVER_GROUP="cn=dojo-security,ou=groups,dc=company,dc=test")
    def test_login_syncs_the_risk_approver_role_without_staff(self):
        from dojo.company.ldap_backend import CompanyLDAPBackend  # noqa: PLC0415 -- needs django-auth-ldap
        from dojo.company.risk_requests import APPROVER_GROUP, is_approver  # noqa: PLC0415

        user = get_user_model().objects.create_user("sam", "sam@company.test")

        class FakeLdapUser:
            group_names = {"dojo-security"}
            group_dns = {"cn=dojo-security,ou=groups,dc=company,dc=test"}

        with patch("django_auth_ldap.backend.LDAPBackend.authenticate_ldap_user", return_value=user):
            CompanyLDAPBackend().authenticate_ldap_user(FakeLdapUser(), "sam123")
        user = get_user_model().objects.get(pk=user.pk)
        self.assertIn(APPROVER_GROUP, set(user.groups.values_list("name", flat=True)))
        self.assertTrue(is_approver(user))
        self.assertFalse(user.is_staff)
        FakeLdapUser.group_dns = set()  # left the security team
        with patch("django_auth_ldap.backend.LDAPBackend.authenticate_ldap_user", return_value=user):
            CompanyLDAPBackend().authenticate_ldap_user(FakeLdapUser(), "sam123")
        self.assertFalse(is_approver(get_user_model().objects.get(pk=user.pk)))

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


class FakeConnection:

    """
    search_s over canned entries: {dn: (attrs, is_person)}. Subtree searches return `groups` as given
    under the group base, and the people whose uid matches a (uid=<name>) filter under the user base.
    """

    def __init__(self, entries, groups):
        self.entries, self.groups, self.filters = entries, groups, []

    def search_s(self, base, scope, filterstr, attrlist=None):
        import ldap  # noqa: PLC0415 -- company image only

        self.filters.append(filterstr)
        if scope != ldap.SCOPE_BASE and base == USERS:
            return [(dn, attrs) for dn, (attrs, is_person) in self.entries.items()
                    if is_person and filterstr == f"(uid={attrs['uid'][0].decode()})"]
        if scope != ldap.SCOPE_BASE:
            return list(self.groups)
        if base not in self.entries:
            raise ldap.NO_SUCH_OBJECT
        attrs, is_person = self.entries[base]
        return [(base, attrs)] if is_person or filterstr == "(objectClass=*)" else []

    def unbind_s(self):
        pass


GROUPS = "ou=groups,dc=company,dc=test"
USERS = "ou=users,dc=company,dc=test"
DAVE = f"uid=dave,{USERS}"
ADMIN = f"uid=admin,{USERS}"
HANK = f"uid=hank,{USERS}"  # in the directory, in no group
IVY = f"uid=ivy,{USERS}"  # only on the first page of a large Active Directory group
NESTED = f"cn=dojo-pt-nested,{GROUPS}"


@unittest.skipUnless(HAS_AUTH_LDAP, "django-auth-ldap is only installed in the company image")
@versioned_fixtures
class TestLdapSyncCommand(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def connection(self):
        return FakeConnection(
            entries={
                DAVE: ({"uid": [b"dave"]}, True),
                ADMIN: ({"uid": [b"admin"]}, True),
                HANK: ({"uid": [b"hank"]}, True),
                NESTED: ({"uid": [b"not-a-person"], "member": [DAVE.encode()]}, False),
                f"cn=dojo-admins,{GROUPS}": ({"member": [ADMIN.encode()]}, False),
            },
            groups=[
                (f"cn=dojo-pt-books,{GROUPS}", {"member": [DAVE.encode(), ADMIN.encode(), NESTED.encode()]}),
                (f"cn=dojo-team-wallet,{GROUPS}", {"member": [DAVE.encode(), b"uid=gone,ou=users,dc=company,dc=test"]}),
                (None, ["ldap://other.company.test/dc=company,dc=test"]),  # Active Directory referral
            ],
        )

    def test_member_dns_and_usernames(self):
        import ldap  # noqa: PLC0415
        from django_auth_ldap.config import LDAPSearch  # noqa: PLC0415

        from dojo.company.management.commands import company_ldap_sync  # noqa: PLC0415

        connection = self.connection()
        # Active Directory: a group of more than 1500 members comes back under member;range=0-1499
        connection.groups.append((f"cn=dojo-team-big,{GROUPS}", {"member;range=0-1499": [IVY.encode()]}))
        search = LDAPSearch(GROUPS, ldap.SCOPE_SUBTREE, "(objectClass=groupOfNames)")
        dns = company_ldap_sync.group_member_dns(connection, search, ("dojo-pt-", "dojo*team-"), f"cn=dojo-admins,{GROUPS}")
        self.assertEqual(dns, {DAVE, ADMIN, NESTED, IVY, "uid=gone,ou=users,dc=company,dc=test"})
        # prefixes are escaped, the group type filter is kept
        self.assertEqual(connection.filters[0], r"(&(objectClass=groupOfNames)(|(cn=dojo-pt-*)(cn=dojo\2ateam-*)))")
        # the nested group and the dangling DN are not people
        self.assertEqual(company_ldap_sync.usernames_for(connection, dns, "openldap"), {"dave", "admin"})
        self.assertEqual(connection.filters[-1], "(uid=*)")
        company_ldap_sync.usernames_for(connection, {DAVE}, "ad")
        self.assertEqual(connection.filters[-1], f"(&(objectCategory=person)(objectClass=user){company_ldap.AD_ENABLED})")

    def test_member_read_includes_the_risk_approver_group(self):
        import ldap  # noqa: PLC0415
        from django_auth_ldap.config import LDAPSearch  # noqa: PLC0415

        from dojo.company.management.commands import company_ldap_sync  # noqa: PLC0415

        connection = self.connection()
        connection.entries[f"cn=dojo-security,{GROUPS}"] = ({"member": [HANK.encode()]}, False)
        search = LDAPSearch(GROUPS, ldap.SCOPE_SUBTREE, "(objectClass=groupOfNames)")
        dns = company_ldap_sync.group_member_dns(connection, search, ("dojo-pt-",), f"cn=dojo-admins,{GROUPS}",
                                                 f"cn=dojo-security,{GROUPS}", f"cn=missing,{GROUPS}", "")
        self.assertIn(HANK, dns)
        self.assertIn(ADMIN, dns)

    def run_sync(self, out, err=None, *, found=True):
        """
        manage.py company_ldap_sync with canned directory reads; populate_user runs for real (with the company
        backend's local-account guard). Dave is in dojo-pt-books and dojo-team-wallet, everyone else in no group.
        found=False makes every user lookup of populate_user fail, as a wrong user search base would.
        """
        import ldap  # noqa: PLC0415
        from django_auth_ldap.backend import _LDAPUser  # noqa: PLC0415, PLC2701 -- company image only
        from django_auth_ldap.config import LDAPSearch  # noqa: PLC0415

        settings = {
            "COMPANY_LDAP_ENABLED": True,
            "COMPANY_LDAP_PROFILE": "openldap",
            "COMPANY_LDAP_PRODUCT_TYPE_GROUP_PREFIX": "dojo-pt-",
            "COMPANY_LDAP_TEAM_GROUP_PREFIX": "dojo-team-",
            "AUTH_LDAP_USER_SEARCH": LDAPSearch(USERS, ldap.SCOPE_SUBTREE, "(uid=%(user)s)"),
            "AUTH_LDAP_GROUP_SEARCH": LDAPSearch(GROUPS, ldap.SCOPE_SUBTREE, "(objectClass=groupOfNames)"),
            "AUTH_LDAP_USER_FLAGS_BY_GROUP": {},
        }
        groups = property(lambda ldap_user: {"dojo-pt-books", "dojo-team-wallet"} if ldap_user._username == "dave" else set())
        with self.settings(**settings), \
                patch("dojo.company.management.commands.company_ldap_sync.bind", side_effect=lambda _backend: self.connection()), \
                patch.object(_LDAPUser, "attrs", new_callable=PropertyMock, return_value={"uid": ["x"]} if found else None), \
                patch.object(_LDAPUser, "group_names", new=groups):
            call_command("company_ldap_sync", stdout=out, stderr=err or StringIO())

    def test_creates_directory_users_and_never_local_accounts(self):
        product_type = Product_Type.objects.get(name="books")
        admin = self.get_test_admin()
        out = StringIO()
        self.run_sync(out)
        self.assertIn("2 directory group members: 1 created, 0 updated, 0 inactive, 1 skipped", out.getvalue())
        self.run_sync(out)
        self.assertIn("2 directory group members: 0 created, 1 updated, 0 inactive, 1 skipped", out.getvalue())

        dave = get_user_model().objects.get(username="dave")
        self.assertFalse(dave.has_usable_password())
        self.assertIn(dave, product_type.authorized_users.all())
        self.assertEqual(set(dave.groups.values_list("name", flat=True)), {"team:wallet", company_ldap.DIRECTORY})
        self.assertIn("skipped: admin", out.getvalue())
        self.assertFalse(admin.groups.exists())
        self.assertNotIn(admin, product_type.authorized_users.all())

    def _former_member(self, username, *, directory=True):
        """A user without a password, with product type books and team wallet, as a directory user who has since left."""
        user = get_user_model().objects.create_user(username, f"{username}@company.test")
        Product_Type.objects.get(name="books").authorized_users.add(user.pk)
        user.groups.add(Group.objects.get_or_create(name="team:wallet")[0])
        if directory:
            user.groups.add(Group.objects.get_or_create(name=company_ldap.DIRECTORY)[0])
        return user

    def test_leavers_lose_access_and_gone_users_are_deactivated(self):
        hank = self._former_member("hank")  # still in the directory, in no group any more
        gina = self._former_member("gina")  # deleted from the directory (or disabled in AD)
        local = self._former_member("ci-bot", directory=False)  # a local account created without a password
        converted = self._former_member("lou")  # a directory user an administrator gave a local password
        converted.set_password("Local-pass-123")
        converted.save()
        out = StringIO()
        self.run_sync(out)
        self.assertIn("2 directory group members: 1 created, 0 updated, 0 inactive, 1 skipped; 1 rechecked, 1 deactivated", out.getvalue())

        books = Product_Type.objects.get(name="books").authorized_users
        for user in (hank, gina, local, converted):
            user.refresh_from_db()
        self.assertTrue(hank.is_active)
        self.assertNotIn(hank, books.all())
        self.assertEqual(set(hank.groups.values_list("name", flat=True)), {company_ldap.DIRECTORY})
        self.assertFalse(gina.is_active)
        for untouched in (local, converted):
            self.assertTrue(untouched.is_active)
            self.assertIn(untouched, books.all())
            self.assertIn("team:wallet", set(untouched.groups.values_list("name", flat=True)))

    def test_no_leaver_check_when_no_member_can_be_synced(self):
        gina = self._former_member("gina")
        out, err = StringIO(), StringIO()
        self.run_sync(out, err, found=False)
        self.assertIn("0 created, 0 updated, 0 inactive, 2 skipped; 0 rechecked, 0 deactivated", out.getvalue())
        self.assertIn("leaver check is skipped", err.getvalue())
        gina.refresh_from_db()
        self.assertTrue(gina.is_active)

    def test_refuses_to_run_without_ldap(self):
        with self.settings(COMPANY_LDAP_ENABLED=False), self.assertRaises(CommandError):
            call_command("company_ldap_sync")
