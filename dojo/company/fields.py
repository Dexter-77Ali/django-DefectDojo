"""
company: typed product fields stored as DojoMeta rows, so no schema change.

Keys are namespaced ``company:<name>``. The field set is configuration, so one
build serves several companies: ``COMPANY_FIELDS`` (from ``DD_COMPANY_FIELDS``
JSON) replaces the defaults below, for example

    {"owner-team": {"label": "Owner team"},
     "criticality": {"label": "Business criticality", "choices": ["critical", "high", "medium", "low"]}}

Values are validated here and by the ``company_fields`` management command.
The upstream "Manage Metadata" page and ``/api/v2/metadata/`` accept any
string, so ``manage.py company_fields check`` reports rows that drifted.
"""

from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ValidationError

from dojo.models import DojoMeta, Product

PREFIX = "company:"
MAX_VALUE_LENGTH = 300  # DojoMeta.value is CharField(300)

DEFAULT_FIELDS = {
    "owner-team": {"label": "Owner team"},
    "business-unit": {"label": "Business unit"},
    "criticality": {"label": "Business criticality", "choices": ["critical", "high", "medium", "low"]},
}


@dataclass(frozen=True)
class CompanyField:

    """One company field: a metadata key, a label and optional allowed values."""

    key: str
    label: str
    choices: tuple[str, ...] = ()

    def validate(self, value: str) -> str:
        """Return the normalised value or raise ValidationError."""
        if not isinstance(value, str) or not value.strip():
            msg = f"{self.key}: a non-empty value is required"
            raise ValidationError(msg)
        value = value.strip()
        if self.choices:
            value = value.lower()
            if value not in self.choices:
                msg = f"{self.key}: {value!r} is not one of {', '.join(self.choices)}"
                raise ValidationError(msg)
        if len(value) > MAX_VALUE_LENGTH:
            msg = f"{self.key}: value longer than {MAX_VALUE_LENGTH} characters"
            raise ValidationError(msg)
        return value


def registry() -> dict[str, CompanyField]:
    """The configured fields keyed by full metadata key (settings win over the defaults)."""
    raw = getattr(settings, "COMPANY_FIELDS", None) or DEFAULT_FIELDS
    fields = {}
    for name, raw_spec in raw.items():
        key = name if name.startswith(PREFIX) else f"{PREFIX}{name}"
        spec = raw_spec or {}
        fields[key] = CompanyField(key, str(spec.get("label") or name), tuple(str(c).lower() for c in spec.get("choices", ())))
    return fields


def _field(key: str) -> CompanyField:
    fields = registry()
    try:
        return fields[key]
    except KeyError:
        msg = f"unknown company field {key!r}; known: {', '.join(fields)}"
        raise ValidationError(msg) from None


def get_product_fields(product: Product) -> dict[str, str]:
    """Return every company field set on the product as {key: value}."""
    return dict(
        DojoMeta.objects.filter(product=product, name__startswith=PREFIX).values_list("name", "value"),
    )


def get_product_field(product: Product, key: str, default: str | None = None) -> str | None:
    """Return one company field value, or ``default`` when unset."""
    _field(key)
    row = DojoMeta.objects.filter(product=product, name=key).values_list("value", flat=True).first()
    return default if row is None else row


def set_product_field(product: Product, key: str, value: str) -> DojoMeta:
    """Validate and upsert one company field on the product."""
    value = _field(key).validate(value)
    row, _ = DojoMeta.objects.update_or_create(product=product, name=key, defaults={"value": value})
    return row


def clear_product_field(product: Product, key: str) -> int:
    """Remove one company field from the product; returns the number of rows deleted."""
    _field(key)
    deleted, _ = DojoMeta.objects.filter(product=product, name=key).delete()
    return deleted


def products_with_field(key: str, value: str | None = None):
    """Products carrying the field, optionally with an exact value (case-insensitive)."""
    _field(key)
    products = Product.objects.filter(product_meta__name=key)
    if value is not None:
        products = products.filter(product_meta__value__iexact=value)
    return products.distinct()


def check_all() -> list[tuple[int, str, str, str]]:
    """Return (product_id, key, value, problem) for every company metadata row that fails validation."""
    problems = []
    rows = DojoMeta.objects.filter(name__startswith=PREFIX, product__isnull=False).values_list("product_id", "name", "value")
    for product_id, key, value in rows:
        try:
            _field(key).validate(value)
        except ValidationError as e:
            problems.append((product_id, key, value, "; ".join(e.messages)))
    return problems
