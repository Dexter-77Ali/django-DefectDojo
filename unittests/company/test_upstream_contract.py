"""
company: upstream contract, the canary for weekly upstream merges.

Each test names the company feature that builds on the upstream piece it checks. A failure after a
merge means upstream moved something a company feature depends on: adapt the company code (and
this file) before releasing. Phases refer to the easier-UI plan (upload, assign, risk acceptance).
"""

import inspect
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template import engines
from django.template.loader_tags import BlockNode
from django.test import override_settings
from django.urls import reverse

import dojo
from dojo.authorization.url_permissions import URL_PERMISSIONS
from dojo.importers.services import ImportResult, auto_import_scan
from dojo.models import Dojo_User
from dojo.risk_acceptance import helper as ra_helper
from unittests.dojo_test_case import DojoTestCase, get_unit_tests_scans_path, versioned_fixtures

# URL names the company layer links to, hides or builds on, with sample arguments.
URL_NAMES = {
    # Phase 1 sidebar shortcuts (company/_sidebar.html)
    "dashboard": (),
    "report_builder": (),
    # Phase 1 non-staff declutter (company/_declutter.html), sidebar and user menu
    "components": (),
    "endpoint": (),
    "test_type": (),
    "dev_env": (),
    "critical_product_metrics": (),
    "metrics_product_type": (),
    "product_type_counts": (),
    "product_tag_counts": (),
    "simple_metrics": (),
    "engineer_metrics": (),
    "regulations": (),
    # Phase 1 non-staff declutter, product tab bar
    "view_product_components": (1,),
    "product_endpoint_report": (1,),
    "add_api_scan_configuration": (1,),
    "view_api_scan_configurations": (1,),
    "new_tool_product": (1,),
    "all_tool_product": (1,),
    "add_engagement_presets": (1,),
    "engagement_presets": (1,),
    # Phase 2 quick upload: "All options…" links and the success redirect
    "import_scan_results_prod": (1,),
    "import_scan_results": (1,),
    "re_import_scan_results": (1,),
    "view_product": (1,),
    "view_test": (1,),
    # Phase 3 assign and "My work": finding pages, list deep links, bulk form target
    "view_finding": (1,),
    "open_findings": (),
    "finding_bulk_update_all": (),
    "finding_bulk_update_all_product": (1,),
    # Phase 4 risk acceptance: upstream pages the inbox links to and enforcement guards
    "view_engagement": (1,),
    "add_risk_acceptance": (1,),
    "view_risk_acceptance": (1, 1),
    "edit_risk_acceptance": (1, 1),
    "expire_risk_acceptance": (1, 1),
    "reinstate_risk_acceptance": (1, 1),
    "delete_risk_acceptance": (1, 1),
    "simple_risk_accept_finding": (1,),
    # Phase 5 "Mark fixed" falls back to the upstream close page
    "close_finding": (1,),
}

# Blocks of upstream dojo/templates/base.html that company/base.html overrides now (header_logo,
# sidebar-items, support-tab, dojo_css, footer) or later phases will (tab_bar: upload and assign
# buttons; modals and extra_javascript: company.js; settings-dropdown-items: language switch).
BASE_BLOCKS = {
    "header_logo", "sidebar-items", "support-tab", "dojo_css", "footer",
    "tab_bar", "modals", "extra_javascript", "settings-dropdown-items",
}


def _block_names(template_path):
    template = engines["django"].engine.from_string(Path(template_path).read_text(encoding="utf-8"))
    return {node.name for node in template.nodelist.get_nodes_by_type(BlockNode)}


class TestUpstreamContract(DojoTestCase):

    def test_url_names_still_reverse(self):
        for name, args in URL_NAMES.items():
            with self.subTest(name=name):
                reverse(name, args=args)

    def test_base_template_blocks_exist(self):
        upstream = _block_names(Path(dojo.__file__).parent / "templates" / "base.html")
        self.assertLessEqual(BASE_BLOCKS, upstream)
        # a company override of a block upstream removed would silently render nothing
        company = _block_names(Path(dojo.__file__).parent / "company" / "templates" / "base.html")
        self.assertLessEqual(company, upstream)

    def test_auto_import_scan_signature(self):
        # Phase 2 quick upload calls auto_import_scan with these keywords and reports these counts
        parameters = inspect.signature(auto_import_scan).parameters
        for name in ("user", "scan_file", "scan_type", "test", "test_title", "engagement_name", "product_name",
                     "product_type_name", "auto_create_context", "close_old_findings"):
            self.assertIn(name, parameters, name)
        fields = set(inspect.signature(ImportResult).parameters)
        self.assertLessEqual({"test", "new", "closed", "reactivated", "untouched"}, fields)

    def test_risk_acceptance_helpers(self):
        # Phase 4 approve (add findings, note), Phase 5 extend and let expire, request form default
        expected = {
            "add_findings_to_risk_acceptance": {"user", "risk_acceptance", "findings"},
            "note_on_risk_acceptance": {"risk_acceptance", "user", "action", "reason"},
            "reinstate": {"risk_acceptance", "old_expiration_date", "user"},
            "expire_now": {"risk_acceptance", "user"},
            "default_expiration_date": set(),
        }
        for name, parameters in expected.items():
            with self.subTest(name=name):
                function = getattr(ra_helper, name)
                self.assertLessEqual(parameters, set(inspect.signature(function).parameters))

    def test_risk_acceptance_url_permissions(self):
        # Phase 4 enforcement adds the approver permission to these entries when it is switched on
        for name in ("add_risk_acceptance", "view_risk_acceptance", "edit_risk_acceptance", "expire_risk_acceptance",
                     "reinstate_risk_acceptance", "delete_risk_acceptance", "simple_risk_accept_finding"):
            self.assertIn(name, URL_PERMISSIONS, name)


@versioned_fixtures
@override_settings(OS_MESSAGE_ENABLED=False)
class TestUpstreamContractWithData(DojoTestCase):
    fixtures = ["dojo_testdata.json"]

    def test_findings_bulk_form(self):
        # Phases 3 and 4: company.js adds "Assign…" and "Request risk acceptance…" to this menu, only when
        # form#bulk_change_form posts to the findings bulk URL (the endpoint bulk menu reuses the same ids)
        self.client.force_login(self.get_test_admin())
        response = self.client.get(reverse("open_findings"))
        self.assertContains(response, f'<form action="{reverse("finding_bulk_update_all")}" method="post" id="bulk_change_form">')
        self.assertContains(response, 'id="bulk_edit_menu"')
        self.assertContains(response, 'class="select_one ')

    def test_reimport_keeps_existing_finding_tags(self):
        # Phase 3 stores assignments as finding tags (team:<name>, assignee:<user>); a re-import must keep them
        admin = Dojo_User.objects.get(username="admin")
        target = {
            "user": admin,
            "scan_type": "ZAP Scan",
            "auto_create_context": True,
            "product_type_name": "Company contract",
            "product_name": "Company contract",
            "engagement_name": settings.COMPANY_UPLOAD_ENGAGEMENT_NAME,
            "test_title": "contract",
        }
        sample = (get_unit_tests_scans_path("zap") / "0_zap_sample.xml").read_bytes()
        first = auto_import_scan(scan_file=SimpleUploadedFile("0_zap_sample.xml", sample), **target)
        finding = first.test.finding_set.order_by("id").first()
        finding.tags.add("team:x")

        second = auto_import_scan(scan_file=SimpleUploadedFile("0_zap_sample.xml", sample), **target)

        self.assertEqual(second.mode_resolved, "reimport")
        self.assertEqual(second.test.id, first.test.id)
        finding.refresh_from_db()
        self.assertIn("team:x", {tag.name for tag in finding.tags.all()})
