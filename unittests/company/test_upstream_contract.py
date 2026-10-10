"""
company: upstream contract, the canary for weekly upstream merges.

Each test names the company feature that builds on the upstream piece it checks. A failure after a
merge means upstream moved something a company feature depends on: adapt the company code (and
this file) before releasing. Phases refer to the easier-UI plan (upload, assign, risk acceptance).
"""

import inspect
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import Q
from django.template import engines
from django.template.loader_tags import BlockNode
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

import dojo
from dojo.authorization.authorization import user_has_configuration_permission
from dojo.authorization.middleware import AuthorizationMiddleware
from dojo.authorization.url_permissions import URL_PERMISSIONS
from dojo.filters import DojoFilter, FindingSLAFilter, FindingTagFilter
from dojo.finding.helper import OPEN_FINDINGS_QUERY
from dojo.finding.queries import get_authorized_findings_for_queryset
from dojo.finding.ui.filters import FindingFilter
from dojo.importers.auto_create_context import AutoCreateContextManager
from dojo.importers.options import ImporterOptions
from dojo.importers.services import ImportResult, auto_import_scan
from dojo.jira import services as jira_services
from dojo.models import Dojo_User, Engagement, Finding, JIRA_Project, Notes, Product, Risk_Acceptance, Test, Test_Type
from dojo.notifications.helper import NotificationManager, create_notification
from dojo.risk_acceptance import helper as ra_helper
from dojo.risk_acceptance.api.serializer import RiskAcceptanceSerializer
from dojo.risk_acceptance.queries import get_authorized_risk_acceptances
from dojo.tags.utils import bulk_add_tags_to_instances, bulk_remove_tags_from_instances
from dojo.utils import get_return_url, grade_debounce_cache_key, redirect_to_return_url_or_else
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
    "endpoints_status_bulk": (1,),  # the finding page's endpoint bulk form, which must not get "Assign…"
    "product_open_findings": (1,),
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
# sidebar-items, support-tab, dojo_css, footer, tab_bar: upload, assign and risk request actions, modals: the bulk
# "Assign…" and "Request risk acceptance…" forms, extra_javascript: company.js) or a later phase will
# (settings-dropdown-items: language switch).
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
        # Phase 2 quick upload resolves the upload engagement the way auto_import_scan does
        parameters = inspect.signature(AutoCreateContextManager.get_target_engagement_if_exists).parameters
        self.assertLessEqual({"engagement_name", "product"}, set(parameters))
        # ... and pushes to JIRA like the import endpoints: push_to_jira reaches the importer through **kwargs
        self.assertIn("obj", inspect.signature(jira_services.get_project).parameters)
        self.assertIn("push_all_issues", {field.name for field in JIRA_Project._meta.get_fields()})
        self.assertIn('"push_to_jira"', inspect.getsource(ImporterOptions.validate_push_to_jira))

    def test_tab_bar_markers(self):
        # Phase 2 test_quick_upload checks the company tab bar links between these upstream comments
        html = (Path(dojo.__file__).parent / "templates" / "base.html").read_text(encoding="utf-8")
        start, block, end = html.index("<!-- start of tabs -->"), html.index("{% block tab_bar %}"), html.index("<!-- end of tabs -->")
        self.assertLess(start, block)
        self.assertLess(block, end)

    def test_risk_acceptance_helpers(self):
        # Phase 4 approve (add findings, note), Phase 5 extend and let expire, request form default
        expected = {
            "add_findings_to_risk_acceptance": {"user", "risk_acceptance", "findings"},
            "note_on_risk_acceptance": {"risk_acceptance", "user", "action", "reason"},
            "reinstate": {"risk_acceptance", "old_expiration_date", "user"},
            "expire_now": {"risk_acceptance", "user"},
            "default_expiration_date": set(),
            "expiration_days": set(),
            "get_view_risk_acceptance": {"risk_acceptance"},
            "simple_risk_accept": {"user", "finding"},
        }
        for name, parameters in expected.items():
            with self.subTest(name=name):
                function = getattr(ra_helper, name)
                self.assertLessEqual(parameters, set(inspect.signature(function).parameters))

    def test_risk_acceptance_enforcement_hooks(self):
        # Phase 4 enforcement (dojo/company/signals.py) wraps these helpers in ra_helper; the wrapper reaches only
        # callers that look them up on the module, so no upstream module may import them by name
        guarded = {"add_findings_to_risk_acceptance", "simple_risk_accept", "reinstate", "expire_now"}
        for name in guarded:
            self.assertTrue(getattr(getattr(ra_helper, name), "company_guarded", False), name)
        root = Path(dojo.__file__).parent
        for path in root.rglob("*.py"):
            if "company" in path.relative_to(root).parts:
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if "from dojo.risk_acceptance.helper import" in line:
                    imported = {name.strip() for name in line.split("import", 1)[1].split("#")[0].strip(" ()").split(",")}
                    self.assertFalse(imported & guarded, f"{path}: {line}")
        # why the helpers are wrapped instead of guarded at the M2M only: they change findings before saving
        add = inspect.getsource(inspect.unwrap(ra_helper.add_findings_to_risk_acceptance))
        self.assertLess(add.index("finding.save("), add.index("risk_acceptance.accepted_findings.add(finding)"))
        reinstate = inspect.getsource(inspect.unwrap(ra_helper.reinstate))
        self.assertIn("if risk_acceptance.expiration_date_handled:", reinstate)  # the guard's condition
        # the API PATCH saves, then calls the helper even with no findings to add (the guard lets that through)
        update = inspect.getsource(RiskAcceptanceSerializer.update)
        self.assertLess(update.index("super().update("), update.index("ra_helper.add_findings_to_risk_acceptance("))
        # Engagement.copy (wrapped too) copies tests and findings before the risk acceptances
        self.assertTrue(getattr(Engagement.copy, "company_guarded", False))
        copy = inspect.getsource(inspect.unwrap(Engagement.copy))
        self.assertLess(copy.index("test.copy("), copy.index("risk_acceptance.copy("))
        # the fields the pre_save guard compares, the approval writes and the inbox lists
        fields = {field.name for field in Risk_Acceptance._meta.get_fields()}
        self.assertLessEqual({"reactivate_expired", "decision", "decision_details", "accepted_by", "owner", "expiration_date",
                              "expiration_date_handled", "accepted_findings", "engagement", "name"}, fields)
        self.assertEqual(Risk_Acceptance.TREATMENT_ACCEPT, "A")
        self.assertEqual(Engagement._meta.get_field("risk_acceptance").related_model, Risk_Acceptance)
        self.assertLessEqual({"enable_full_risk_acceptance", "enable_simple_risk_acceptance"},
                             {field.name for field in Product._meta.get_fields()})
        # the friendlier URL check ("config", the approver permission): superuser, staff, then Django permissions; inbox scope
        self.assertIn("user.has_perm(permission)", inspect.getsource(user_has_configuration_permission))
        queries = (Path(dojo.__file__).parent / "risk_acceptance" / "queries.py").read_text(encoding="utf-8")  # cache wrapper hides the signature
        self.assertIn("def get_authorized_risk_acceptances(permission):", queries)
        self.assertTrue(callable(get_authorized_risk_acceptances))
        # the hidden "Add Risk Acceptance" links match this path (company base.html, company-ra-restricted)
        for args in ((1,), (1, 1)):
            self.assertIn("/risk_acceptance/add", reverse("add_risk_acceptance", args=args))

    def test_risk_acceptance_notifications(self):
        # Phase 4: company events have no Notifications column; upstream then uses the "other" row and template
        self.assertIn("getattr(notifications, event, notifications.other)", inspect.getsource(NotificationManager._process_notifications))
        self.assertIn('f"notifications/{notification_type}/other.tpl"', inspect.getsource(NotificationManager._create_notification_message))
        self.assertIn("company_risk_requested", settings.NOTIFICATIONS_SYSTEM_LEVEL_TRUMP)
        self.assertIn("company_risk_decided", settings.NOTIFICATIONS_SYSTEM_LEVEL_TRUMP)
        # an empty recipients list sends nothing (the escalation copy still goes out), never a broadcast
        self.assertIn("if not recipients:", inspect.getsource(NotificationManager.create_notification))
        # the owner copy of risk_acceptance_expiration reads the risk_acceptance keyword and upstream's recipient query
        for function in (ra_helper.expire_now, ra_helper.expiration_handler):
            self.assertIn("risk_acceptance=risk_acceptance", inspect.getsource(inspect.unwrap(function)))
        self.assertTrue(callable(NotificationManager._get_user_to_send_notifications_to))

    def test_assign_building_blocks(self):
        # Phase 3 assign view: authorization filter, return_url helpers, tag utilities, notifications
        self.assertLessEqual({"permission", "queryset", "user"}, set(inspect.signature(get_authorized_findings_for_queryset).parameters))
        self.assertEqual(list(inspect.signature(redirect_to_return_url_or_else).parameters), ["request", "or_else"])
        self.assertIn('request.POST.get("return_url"', inspect.getsource(get_return_url))
        for function in (bulk_add_tags_to_instances, bulk_remove_tags_from_instances):
            self.assertEqual(list(inspect.signature(function).parameters)[:2], ["tag_or_tags", "instances"])
        self.assertLessEqual({"event", "title", "recipients", "url"}, set(inspect.signature(create_notification).parameters))
        # assignments reach people on the system channels: upstream merges system settings for these events
        self.assertIn("company_finding_assigned", settings.NOTIFICATIONS_SYSTEM_LEVEL_TRUMP)
        self.assertIn("settings.NOTIFICATIONS_SYSTEM_LEVEL_TRUMP", inspect.getsource(NotificationManager._process_recipients))
        # notes join findings through Finding.notes; tag values are lower case (assignee:<username lower-cased>)
        self.assertEqual(Finding._meta.get_field("notes").related_model, Notes)
        self.assertTrue(Finding.tags.tag_options.force_lowercase)

    def test_open_findings_filters_for_my_work(self):
        # Phase 3 "My findings", team lists and tiles: ?tags=<tag string> filters by exact tag name (a tagulous
        # field, one parameter, comma-separated tags ORed); ?outside_of_sla=1 is "Outside of SLA: True"
        tags = FindingTagFilter.base_filters["tags"]
        self.assertEqual((tags.field_name, tags.extra["to_field_name"]), ("tags__name", "name"))
        self.assertIn('self.form.fields[field] = model._meta.get_field("tags").formfield()', inspect.getsource(DojoFilter.__init__))
        sla = FindingFilter.base_filters["outside_of_sla"]
        self.assertIsInstance(sla, FindingSLAFilter)
        self.assertEqual(sla.options[1][1].__name__, "sla_violated")
        self.assertEqual(OPEN_FINDINGS_QUERY, Q(active=True))
        # "My work" tiles override this dashboard block; the bulk menu hooks company.js relies on
        self.assertIn("finding_stats_tiles", _block_names(Path(dojo.__file__).parent / "templates" / "dojo" / "dashboard.html"))
        templates = Path(dojo.__file__).parent / "templates" / "dojo"
        snippet = (templates / "findings_list_snippet.html").read_text(encoding="utf-8")
        view_test = (templates / "view_test.html").read_text(encoding="utf-8")
        view_finding = (templates / "view_finding.html").read_text(encoding="utf-8")
        self.assertIn("{% url 'finding_bulk_update_all' %}", snippet)
        self.assertIn("{% url 'finding_bulk_update_all_product' product_tab.product.id %}", snippet)
        self.assertIn("{% url 'finding_bulk_update_all_product' product_tab.product.id %}", view_test)
        self.assertIn("{% url 'endpoints_status_bulk' finding.id %}", view_finding)  # must stay a different URL
        for html in (snippet, view_test):
            self.assertIn('id="bulk_edit_menu"', html)
            self.assertIn("$('input[type=checkbox].select_one:checked')", html)
            self.assertIn('name="finding_to_update"', html)

    def test_risk_acceptance_url_permissions(self):
        # Phase 4 enforcement appends the approver permission to these entries (lists) when it is switched on
        for name in ("add_risk_acceptance", "view_risk_acceptance", "edit_risk_acceptance", "expire_risk_acceptance",
                     "reinstate_risk_acceptance", "delete_risk_acceptance", "simple_risk_accept_finding"):
            self.assertIn(name, URL_PERMISSIONS, name)
            self.assertIsInstance(URL_PERMISSIONS[name], list)
        self.assertIn('check_type == "config"', inspect.getsource(AuthorizationMiddleware.process_view))


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
        # drop the grading debounce marker, as test_quick_upload does (it would leak into the next class)
        self.addCleanup(cache.delete, grade_debounce_cache_key(first.test.engagement.product_id))
        finding = first.test.finding_set.order_by("id").first()
        finding.tags.add("team:x")

        second = auto_import_scan(scan_file=SimpleUploadedFile("0_zap_sample.xml", sample), **target)

        self.assertEqual(second.mode_resolved, "reimport")
        self.assertEqual(second.test.id, first.test.id)
        finding.refresh_from_db()
        self.assertIn("team:x", {tag.name for tag in finding.tags.all()})

    def test_blank_test_title_targets_the_last_test_of_the_scanner(self):
        # Phase 2 quick upload requires a component once a scanner has several or named tests, because a
        # blank title re-imports into the engagement's LAST test of that scanner, whatever its title
        engagement = Engagement.objects.order_by("id").first()
        test_type = Test_Type.objects.get_or_create(name="ZAP Scan")[0]
        dates = {"target_start": timezone.now(), "target_end": timezone.now()}
        Test.objects.create(engagement=engagement, test_type=test_type, scan_type="ZAP Scan", title="api", **dates)
        last = Test.objects.create(engagement=engagement, test_type=test_type, scan_type="ZAP Scan", title="web", **dates)
        target = AutoCreateContextManager().get_target_test_if_exists(
            test_title=None, scan_type="ZAP Scan", engagement=engagement,
        )
        self.assertEqual(target, last)
