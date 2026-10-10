"""
company: sidebar shortcuts (company/_sidebar.html) and the non-staff declutter (company/_declutter.html).

The declutter is presentation only: a <style> that hides menu entries for users who are neither
staff nor superuser. Permissions are unchanged, so every hidden page still opens by URL.
"""

import re
from urllib.parse import urlsplit

from django.test import override_settings
from django.urls import resolve, reverse

from dojo.models import Dojo_User, Product
from unittests.dojo_test_case import DojoTestCase, versioned_fixtures

STYLE = re.compile(r'<style id="company-declutter">(.*?)</style>', re.DOTALL)
RULE = re.compile(r"([^{}]+)\{[^}]*\}")
HREF = re.compile(r'href="([^"]*)"')
# Upstream markup a selector relies on right before its a[href]; any other selector only needs the href.
# ponytail: regexes over rendered HTML, not a CSS engine (none is installed); they follow upstream's markup style.
SHAPES = (
    ("li.divider:has(+ li > a", r'<li[^>]*class="divider"[^>]*></li>\s*<li[^>]*>\s*<a href="{}"'),
    ("div[x-data]:has(> a", r'<div x-data="[^"]*">\s*<a href="{}"'),
    ("li:has(> a", r'<li[^>]*>\s*<a href="{}"'),
    (":not([data-testid])", r'<a href="{}"(?![^>]*data-testid)'),
)
SHORTCUTS = ("dashboard", "report_builder")  # the company shortcuts link here, so plain a[href] rules must not
DAILY_WORK = ("open_findings", "product", "engagement", "notifications")


@versioned_fixtures
@override_settings(OS_MESSAGE_ENABLED=False)
class TestCompanyNavigation(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def setUp(self):
        super().setUp()
        self.product = Product.objects.order_by("id").first()
        self.developer = Dojo_User.objects.create(username="company-developer")
        self.product.authorized_users.add(self.developer)
        self.pages = (reverse("dashboard"), reverse("view_product", args=(self.product.id,)))

    def render(self, user, url):
        self.client.force_login(user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def selectors(self, user, url):
        style = STYLE.search(self.render(user, url))
        if style is None:
            return None
        return [s.strip() for rule in RULE.findall(style.group(1)) for s in rule.split(",")
                if s.strip() and s.strip() != "#company-declutter-end"]

    def test_shortcuts_come_before_upstream_items(self):
        sidebar = self.render(self.developer, reverse("dashboard"))
        sidebar = sidebar[sidebar.index('id="dd-sidebar"'):]
        my_work = sidebar.index('data-testid="company-nav-my-work"')
        reports = sidebar.index('data-testid="company-nav-reports"')
        upstream_first = sidebar.index("<!-- Dashboard -->")
        self.assertLess(my_work, reports)
        self.assertLess(reports, upstream_first)
        self.assertIn(f'href="{reverse("report_builder")}" data-testid="company-nav-reports"', sidebar)

    def test_declutter_only_for_non_staff(self):
        self.assertIsNone(self.selectors(self.get_test_admin(), reverse("dashboard")))
        staff = Dojo_User.objects.create(username="company-staff", is_staff=True)
        self.assertIsNone(self.selectors(staff, reverse("dashboard")))
        self.assertTrue(self.selectors(self.developer, reverse("dashboard")))

    def test_selectors_match_upstream_markup_and_spare_daily_work(self):
        # The admin gets no declutter and sees every entry, so each selector must still find its upstream element there.
        keep = {name: f'href="{reverse(name)}"' for name in DAILY_WORK}
        shortcuts = {reverse(name) for name in SHORTCUTS}
        global_count = len(self.selectors(self.developer, self.pages[0]))
        for url in self.pages:
            html = self.render(self.get_test_admin(), url)
            selectors = self.selectors(self.developer, url)
            for selector in selectors:
                with self.subTest(url=url, selector=selector):
                    href = HREF.search(selector).group(1)
                    resolve(urlsplit(href).path)
                    container = selector.split()[0]
                    marker = f'id="{container[1:]}"' if container.startswith("#") else f"{container[1:]}"
                    self.assertIn(marker, html)
                    shape = next((p for key, p in SHAPES if key in selector), r'href="{}"')
                    found = re.compile(shape.format(re.escape(href))).search(html, html.index(marker))
                    self.assertIsNotNone(found, "upstream markup no longer matches this selector")
                    if ":has(" not in selector and ":not([data-testid])" not in selector:
                        self.assertNotIn(href, shortcuts)
                        self.assertNotIn(f'href="{href}"', keep.values())
                    if "div[x-data]:has(" in selector:  # a whole sidebar section: nothing daily in it
                        section = html[found.start():html.find("<div x-data=", found.end())]
                        for name, daily in keep.items():
                            self.assertFalse(daily in section, f"hidden section holds {name}")
        self.assertGreater(len(selectors), global_count)  # product tab entries on product pages only
