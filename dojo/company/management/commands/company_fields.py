"""
company: manage the company product fields from the command line.

    manage.py company_fields list
    manage.py company_fields get <product_id>
    manage.py company_fields set <product_id> <key> <value>
    manage.py company_fields check
"""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from dojo.company import fields
from dojo.models import Product


class Command(BaseCommand):

    """List, read, write and validate company product fields (DojoMeta rows)."""

    help = "Manage company product fields stored as metadata"

    def add_arguments(self, parser):
        sub = parser.add_subparsers(dest="action", required=True)
        sub.add_parser("list", help="show the known fields and their allowed values")
        get = sub.add_parser("get", help="show the fields set on one product")
        get.add_argument("product_id", type=int)
        set_ = sub.add_parser("set", help="set one field on one product")
        set_.add_argument("product_id", type=int)
        set_.add_argument("key")
        set_.add_argument("value")
        sub.add_parser("check", help="report metadata rows that fail validation")

    def handle(self, *args, **options):
        action = options["action"]
        if action == "list":
            for f in fields.COMPANY_FIELDS.values():
                choices = f" ({', '.join(f.choices)})" if f.choices else ""
                self.stdout.write(f"{f.key}: {f.label}{choices}")
            return
        if action == "check":
            problems = fields.check_all()
            for product_id, key, value, problem in problems:
                self.stdout.write(f"product {product_id} {key}={value!r}: {problem}")
            self.stdout.write(f"{len(problems)} invalid row(s)")
            return
        product = Product.objects.filter(pk=options["product_id"]).first()
        if product is None:
            msg = f"product {options['product_id']} does not exist"
            raise CommandError(msg)
        if action == "get":
            for key, value in sorted(fields.get_product_fields(product).items()):
                self.stdout.write(f"{key}={value}")
            return
        try:
            row = fields.set_product_field(product, options["key"], options["value"])
        except ValidationError as e:
            raise CommandError("; ".join(e.messages)) from e
        self.stdout.write(f"{row.name}={row.value} on product {product.id}")
