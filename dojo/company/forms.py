"""
company: one-page scan upload form (company/upload, dojo.company.views.quick_upload).

Three fields for a first upload (product, scanner, file). Uploads land in the product's
settings.COMPANY_UPLOAD_ENGAGEMENT_NAME engagement, which CI re-imports share, and re-import into the
test of the same scanner and component, so fixed findings close. File checks are upstream's
(dojo/forms.py ImportScanForm.clean).
"""
from django import forms
from django.conf import settings
from django.db.models import Max
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from dojo.importers.auto_create_context import AutoCreateContextManager
from dojo.models import Product, Test
from dojo.product.queries import get_authorized_products
from dojo.risk_acceptance.helper import expiration_days
from dojo.tools.factory import get_choices_sorted, requires_file
from dojo.utils import is_scan_file_too_large
from dojo.validators import ImporterFileExtensionValidator

RECENT_SCANNERS = 8
# Changing the product or the scanner re-renders the page (GET), which recomputes the product's default
# scanner and the component choices. The file is chosen last, so nothing typed is lost.
RELOAD_ON_PRODUCT = "location.search = new URLSearchParams({product: this.value})"
RELOAD_ON_SCANNER = "location.search = new URLSearchParams({product: this.form.product.value, scan_type: this.value})"
# Component value of the "No component" choice: the scanner's one untitled test, next to titled ones
NO_COMPONENT = "__none__"


def upload_engagement(product):
    """The product's shared upload engagement, resolved exactly as auto_import_scan resolves it."""
    if product is None:
        return None
    return AutoCreateContextManager().get_target_engagement_if_exists(
        engagement_name=settings.COMPANY_UPLOAD_ENGAGEMENT_NAME, product=product,
    )


class QuickUploadForm(forms.Form):

    """Product, scanner and file; component and "close fixed findings" under More options."""

    product = forms.ModelChoiceField(
        queryset=Product.objects.none(), label=_("Product"), empty_label=_("Choose a product…"),
        widget=forms.Select(attrs={"class": "form-control", "onchange": RELOAD_ON_PRODUCT}),
    )
    scan_type = forms.ChoiceField(
        label=_("Scanner"),
        widget=forms.Select(attrs={"class": "form-control", "onchange": RELOAD_ON_SCANNER}),
    )
    file = forms.FileField(
        label=_("Report file"), required=False, allow_empty_file=True,
        validators=[ImporterFileExtensionValidator()],
        widget=forms.FileInput(attrs={"accept": ", ".join(settings.FILE_IMPORT_TYPES), "class": "company-dropzone"}),
    )
    component = forms.CharField(
        label=_("Component"), max_length=255, required=False,
        help_text=_("Repository or service this report covers. Each component keeps its own findings."),
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    close_old_findings = forms.BooleanField(
        label=_("Close fixed findings"), required=False, initial=True,
        help_text=_("Findings of the previous report that this report no longer contains are closed."),
    )

    def __init__(self, *args, user, test=None, product=None, scan_type=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.test = test
        self.target_test = None  # the untitled test, when the upload picks NO_COMPONENT
        self.component_titles, self.untitled_test, self.component_required = [], None, False
        if test is not None:
            # ?test=<id>: a new report for that test; its product, scanner and component are fixed
            for name in ("product", "scan_type", "component"):
                del self.fields[name]
            return
        products = get_authorized_products("import", user)
        self.fields["product"].queryset = products
        scanners = dict(get_choices_sorted())
        recent = [
            (row["scan_type"], scanners[row["scan_type"]])
            for row in Test.objects.filter(engagement__product__in=products, scan_type__in=list(scanners))
            .values("scan_type").annotate(last=Max("id")).order_by("-last")[:RECENT_SCANNERS]
        ]
        self.fields["scan_type"].choices = [
            ("", _("Choose a scanner…")),
            *([(_("Used before"), recent)] if recent else []),
            (_("All scanners"), list(scanners.items())),
        ]
        if self.is_bound:
            return
        if product is None and len(only := list(products[:2])) == 1:
            product = only[0]
        if scan_type not in scanners:
            scan_type = product and (Test.objects.filter(engagement__product=product, scan_type__in=list(scanners))
                                     .order_by("-id").values_list("scan_type", flat=True).first())
        titles = self.load_components(product, scan_type)
        # every test of the scanner has the same title: pre-pick it, a blank upload would re-import into it anyway
        component = next(iter(titles)) if len(titles) == 1 and "" not in titles else None
        self.initial.update(product=product, scan_type=scan_type, component=component)

    def load_components(self, product, scan_type):
        """
        Set component_titles, untitled_test (when there is exactly one) and component_required from the
        upload engagement's tests of this scanner, and return the set of their titles ("" for untitled).

        A blank component re-imports into the engagement's LAST test of the scanner
        (AutoCreateContextManager.get_target_test_if_exists) and closes whatever that report lacks, so
        once a scanner has several tests, or named ones, the uploader has to say which one this is. The
        untitled test is then picked as NO_COMPONENT and targeted by id.
        """
        engagement = upload_engagement(product)
        tests = list(engagement.test_set.filter(scan_type=scan_type).only("id", "title")) if engagement and scan_type else []
        titles = {test.title or "" for test in tests}
        untitled = [test for test in tests if not test.title]
        self.component_titles = sorted(titles - {""})
        self.untitled_test = untitled[0] if len(untitled) == 1 else None
        self.component_required = len(tests) > 1 or bool(self.component_titles)
        return titles

    @property
    def scan_type_value(self):
        """The scanner of this upload (the test's own in ?test mode)."""
        if self.test is not None:
            return self.test.scan_type or self.test.test_type.name
        return self.cleaned_data.get("scan_type")

    def clean(self):
        cleaned = super().clean()
        scan_type = self.scan_type_value
        if self.test is None:
            self.load_components(cleaned.get("product"), scan_type)
            component = cleaned.get("component")
            if component == NO_COMPONENT:
                cleaned["component"] = ""
                self.target_test = self.untitled_test
            if (component == NO_COMPONENT and self.target_test is None) or (self.component_required and not component):
                self.add_error("component", _("This product already has several reports of this scanner. "
                                              "Pick the component this report belongs to, or name a new one."))
        file = cleaned.get("file")
        if scan_type and requires_file(scan_type) and not file:
            self.add_error("file", _("Choose the report file."))
        if file and is_scan_file_too_large(file):
            self.add_error("file", _("Report file is too large. Maximum supported size is %d MB") % settings.SCAN_FILE_MAX_SIZE)
        return cleaned


class RiskRequestForm(forms.Form):

    """Risk acceptance request (company/risk/request, dojo.company.views.risk_request): a justification and a duration."""

    justification = forms.CharField(
        label=_("Justification"),
        max_length=5000,
        widget=forms.Textarea(attrs={"class": "form-control", "rows": 5}),
        help_text=_("Why the risk can be accepted: compensating controls, the planned fix and its date, the business reason."),
    )
    days = forms.TypedChoiceField(label=_("Accept for"), coerce=int, widget=forms.Select(attrs={"class": "form-control"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        days = settings.COMPANY_RA_DAYS
        self.fields["days"].choices = [(value, ngettext("%(days)d day", "%(days)d days", value) % {"days": value}) for value in days]
        default = expiration_days()
        self.fields["days"].initial = default if default in days else days[0]
