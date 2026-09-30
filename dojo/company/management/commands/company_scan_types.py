"""
company: apply the scanner allowlist DD_COMPANY_SCAN_TYPES to the scan type list.

    manage.py company_scan_types

Parser scan types (keys of dojo.tools.factory.PARSERS) that are not listed and have no tests are
deactivated (Test_Type.active = False): upload pages hide them and the API rejects them. Listed
ones are (re)activated. Manual test types such as "Pen Test" and types with existing tests are left
alone, so history stays filterable. An empty list changes nothing. Restart uwsgi afterwards: the
v2 API builds its scan type choices at startup.
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from dojo.models import Test, Test_Type
from dojo.tools.factory import PARSERS


class Command(BaseCommand):

    """Deactivate unused parser scan types that the company allowlist does not name."""

    help = "Apply DD_COMPANY_SCAN_TYPES: deactivate unlisted parser scan types without tests, activate listed ones"

    def handle(self, *args, **options):
        allowed = {name.strip() for name in settings.COMPANY_SCAN_TYPES if name.strip()}
        if not allowed:
            self.stdout.write("DD_COMPANY_SCAN_TYPES is empty: every scanner stays available, nothing changed.")
            return
        for name in sorted(allowed - PARSERS.keys()):
            self.stderr.write(self.style.WARNING(f"Not a parser scan type, ignored: {name!r}"))
        used = set(Test.objects.values_list("test_type__name", flat=True).distinct())
        used |= set(Test.objects.exclude(scan_type=None).values_list("scan_type", flat=True).distinct())
        # a parser without a Test_Type row counts as active (dojo.tools.factory.get_parser creates it on use)
        active = dict(Test_Type.objects.filter(name__in=list(PARSERS)).values_list("name", "active"))
        changed = 0
        for name in sorted(PARSERS):
            if name in allowed:
                wanted = True
            elif name in used:
                continue
            else:
                wanted = False
            if active.get(name, True) != wanted:
                Test_Type.objects.update_or_create(name=name, defaults={"active": wanted})
                self.stdout.write(f"{'activated' if wanted else 'deactivated'}: {name}")
                changed += 1
        self.stdout.write(f"{changed} scan types changed, {len(allowed & PARSERS.keys())} allowed.")
