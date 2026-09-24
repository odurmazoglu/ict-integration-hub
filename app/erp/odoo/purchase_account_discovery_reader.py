"""P0-PROD-18D: read-only product/category purchase-account configuration reader.

Reads exactly what is needed to answer "which purchase account would Odoo's own
product/category configuration give a product-backed Vendor Bill line?" -- and nothing
else. It is not a generic Odoo browser: every model, domain, and field list is fixed
here, never caller-controlled.

Every field is verified via the sanctioned ``ir.model.fields`` metadata read before it
is requested (the P0-PROD-15J/15K/15P lesson: this production Odoo can reject fields a
generic schema would have). Relational fields are additionally verified to have the
expected ``ttype``/``relation``, so a field that exists with different semantics fails
closed instead of being misread. Built on :class:`OdooReadOnlyAdapter`, which
structurally cannot create, write, unlink, post, pay, or reconcile anything.
"""

from __future__ import annotations

from typing import Any

from app.application.workbench.purchase_account_discovery import (
    MAX_DISCOVERY_CATEGORIES,
    CategoryPurchaseAccountRecord,
    ProductPurchaseAccountRecord,
    PurchaseAccountRecord,
)
from app.erp.exceptions import ErpRepositoryResponseError
from app.erp.odoo.adapter import OdooReadOnlyAdapter

SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR = "Odoo purchase-account discovery returned an unsafe response."

COMPANY_MODEL = "res.company"
CATEGORY_MODEL = "product.category"
PRODUCT_MODEL = "product.product"
TEMPLATE_MODEL = "product.template"
ACCOUNT_MODEL = "account.account"

#: Bounds the visible-company read; more companies than this is refused, not truncated.
MAX_VISIBLE_COMPANIES = 50

#: (field, ttype, relation) -- relation is ``None`` for non-relational fields.
_FieldSpec = tuple[str, str, str | None]

REQUIRED_FIELDS: dict[str, tuple[_FieldSpec, ...]] = {
    CATEGORY_MODEL: (
        ("name", "char", None),
        ("property_account_expense_categ_id", "many2one", ACCOUNT_MODEL),
    ),
    PRODUCT_MODEL: (
        ("product_tmpl_id", "many2one", TEMPLATE_MODEL),
        ("active", "boolean", None),
        ("company_id", "many2one", COMPANY_MODEL),
    ),
    TEMPLATE_MODEL: (
        ("name", "char", None),
        ("categ_id", "many2one", CATEGORY_MODEL),
        ("property_account_expense_id", "many2one", ACCOUNT_MODEL),
        ("type", "selection", None),
        ("company_id", "many2one", COMPANY_MODEL),
    ),
    ACCOUNT_MODEL: (
        ("code", "char", None),
        ("name", "char", None),
        ("account_type", "selection", None),
        ("company_ids", "many2many", COMPANY_MODEL),
    ),
}

#: Read only when present; absence is represented as ``None``, never guessed.
OPTIONAL_FIELDS: dict[str, tuple[_FieldSpec, ...]] = {
    CATEGORY_MODEL: (("complete_name", "char", None),),
    PRODUCT_MODEL: (),
    TEMPLATE_MODEL: (("is_storable", "boolean", None),),
    ACCOUNT_MODEL: (("deprecated", "boolean", None),),
}


class OdooPurchaseAccountDiscoveryReader:
    """Structurally read-only reader behind ``PurchaseAccountDiscoveryReader``."""

    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter
        self._validated_models: set[str] = set()
        self._optional_available: dict[str, frozenset[str]] = {}

    def accessible_company_ids(self) -> tuple[int, ...]:
        records = self._adapter.search_read_all(
            model=COMPANY_MODEL, domain=[], fields=["id"], max_records=MAX_VISIBLE_COMPANIES + 1
        )
        if len(records) > MAX_VISIBLE_COMPANIES:
            raise ErpRepositoryResponseError(SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR)
        ids = [_required_positive_int(_record(record).get("id")) for record in records]
        if len(set(ids)) != len(ids):
            raise ErpRepositoryResponseError(SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR)
        return tuple(sorted(ids))

    def list_categories(self) -> tuple[CategoryPurchaseAccountRecord, ...]:
        fields = self._fields_for(CATEGORY_MODEL)
        records = self._adapter.search_read_all(
            model=CATEGORY_MODEL, domain=[], fields=fields, max_records=MAX_DISCOVERY_CATEGORIES + 1
        )
        if len(records) > MAX_DISCOVERY_CATEGORIES:
            raise ErpRepositoryResponseError(SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR)
        categories = tuple(self._category(record) for record in records)
        _require_unique_ids(category.id for category in categories)
        return categories

    def find_category(self, *, category_id: int) -> CategoryPurchaseAccountRecord | None:
        _require_positive_argument(category_id)
        fields = self._fields_for(CATEGORY_MODEL)
        records = self._adapter.search_read(
            model=CATEGORY_MODEL, domain=[["id", "=", category_id]], fields=fields, limit=2
        )
        if not records:
            return None
        if len(records) > 1:
            raise ErpRepositoryResponseError(SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR)
        category = self._category(records[0])
        if category.id != category_id:
            raise ErpRepositoryResponseError(SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR)
        return category

    def find_product(self, *, company_id: int, product_id: int) -> ProductPurchaseAccountRecord | None:
        _require_positive_argument(company_id)
        _require_positive_argument(product_id)
        company_scope = ["company_id", "in", [company_id, False]]
        # Archived records are read too, so an inactive product is reported as such
        # instead of looking like it does not exist.
        with_archived = ["active", "in", [True, False]]
        variants = self._adapter.search_read(
            model=PRODUCT_MODEL,
            domain=[["id", "=", product_id], company_scope, with_archived],
            fields=self._fields_for(PRODUCT_MODEL),
            limit=2,
        )
        if not variants:
            return None
        if len(variants) > 1:
            raise ErpRepositoryResponseError(SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR)
        variant = _record(variants[0])
        if _required_positive_int(variant.get("id")) != product_id:
            raise ErpRepositoryResponseError(SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR)
        template_id = _required_many2one_id(variant.get("product_tmpl_id"))
        variant_company_id = _optional_many2one_id(variant.get("company_id"))
        variant_active = _required_bool(variant.get("active"))

        templates = self._adapter.search_read(
            model=TEMPLATE_MODEL,
            domain=[["id", "=", template_id], company_scope, with_archived],
            fields=self._fields_for(TEMPLATE_MODEL),
            limit=2,
        )
        if len(templates) != 1:
            raise ErpRepositoryResponseError(SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR)
        template = _record(templates[0])
        if _required_positive_int(template.get("id")) != template_id:
            raise ErpRepositoryResponseError(SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR)
        template_company_id = _optional_many2one_id(template.get("company_id"))
        if template_company_id != variant_company_id or variant_company_id not in (None, company_id):
            raise ErpRepositoryResponseError(SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR)

        is_storable = (
            _required_bool(template.get("is_storable"))
            if "is_storable" in self._optional_available[TEMPLATE_MODEL]
            else None
        )
        return _build(
            ProductPurchaseAccountRecord,
            product_id=product_id,
            product_template_id=template_id,
            name=_required_text(template.get("name")),
            active=variant_active,
            company_id=variant_company_id,
            product_type=_required_text(template.get("type")),
            category_id=_optional_many2one_id(template.get("categ_id")),
            override_account_id=_optional_many2one_id(template.get("property_account_expense_id")),
            is_storable=is_storable,
        )

    def find_accounts(self, *, company_id: int, account_ids: tuple[int, ...]) -> tuple[PurchaseAccountRecord, ...]:
        _require_positive_argument(company_id)
        if not account_ids:
            return ()
        for account_id in account_ids:
            _require_positive_argument(account_id)
        requested = sorted(set(account_ids))
        records = self._adapter.search_read(
            model=ACCOUNT_MODEL,
            domain=[["id", "in", requested], ["company_ids", "in", [company_id]]],
            fields=self._fields_for(ACCOUNT_MODEL),
            limit=len(requested) + 1,
        )
        accounts = tuple(self._account(record) for record in records)
        _require_unique_ids(account.id for account in accounts)
        if any(account.id not in requested for account in accounts):
            raise ErpRepositoryResponseError(SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR)
        return accounts

    # ------------------------------------------------------------------ record mapping

    def _category(self, raw: object) -> CategoryPurchaseAccountRecord:
        record = _record(raw)
        complete_name = (
            _optional_text(record.get("complete_name"))
            if "complete_name" in self._optional_available[CATEGORY_MODEL]
            else None
        )
        return _build(
            CategoryPurchaseAccountRecord,
            id=_required_positive_int(record.get("id")),
            name=_required_text(record.get("name")),
            expense_account_id=_optional_many2one_id(record.get("property_account_expense_categ_id")),
            complete_name=complete_name,
        )

    def _account(self, raw: object) -> PurchaseAccountRecord:
        record = _record(raw)
        deprecated = (
            _required_bool(record.get("deprecated"))
            if "deprecated" in self._optional_available[ACCOUNT_MODEL]
            else None
        )
        return _build(
            PurchaseAccountRecord,
            id=_required_positive_int(record.get("id")),
            code=_required_text(record.get("code")),
            name=_required_text(record.get("name")),
            account_type=_required_text(record.get("account_type")),
            company_ids=_required_id_list(record.get("company_ids")),
            deprecated=deprecated,
        )

    # ------------------------------------------------------------------ metadata validation

    def _fields_for(self, model: str) -> list[str]:
        self._ensure_model_validated(model)
        required = [name for name, _ttype, _relation in REQUIRED_FIELDS[model]]
        available = self._optional_available[model]
        optional = [name for name, _ttype, _relation in OPTIONAL_FIELDS[model] if name in available]
        return ["id", *required, *optional]

    def _ensure_model_validated(self, model: str) -> None:
        if model in self._validated_models:
            return
        for spec in REQUIRED_FIELDS[model]:
            if not self._field_matches(model, spec):
                raise ErpRepositoryResponseError(SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR)
        self._optional_available[model] = frozenset(
            spec[0] for spec in OPTIONAL_FIELDS[model] if self._field_matches(model, spec)
        )
        self._validated_models.add(model)

    def _field_matches(self, model: str, spec: _FieldSpec) -> bool:
        field_name, ttype, relation = spec
        records = self._adapter.read_model_field_metadata(model=model, field_name=field_name)
        if len(records) != 1 or not isinstance(records[0], dict):
            return False
        metadata = records[0]
        if metadata.get("name") != field_name or metadata.get("ttype") != ttype:
            return False
        return relation is None or metadata.get("relation") == relation


def _build(dto_type: type, **values: Any) -> Any:
    try:
        return dto_type(**values)
    except Exception as exc:
        raise ErpRepositoryResponseError(SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR) from exc


def _record(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ErpRepositoryResponseError(SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR)
    return value


def _require_positive_argument(value: object) -> None:
    if type(value) is not int or value <= 0:
        raise ErpRepositoryResponseError(SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR)


def _require_unique_ids(ids: Any) -> None:
    seen: set[int] = set()
    for record_id in ids:
        if record_id in seen:
            raise ErpRepositoryResponseError(SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR)
        seen.add(record_id)


def _required_positive_int(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ErpRepositoryResponseError(SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR)
    return value


def _required_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ErpRepositoryResponseError(SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR)
    return value


def _required_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ErpRepositoryResponseError(SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR)
    return value


def _optional_text(value: object) -> str | None:
    # Odoo represents an empty char field as the literal ``False``.
    if value is False:
        return None
    return _required_text(value)


def _optional_many2one_id(value: object) -> int | None:
    # Odoo's json/2 search_read represents an empty many2one as the literal ``False`` --
    # the only falsy shape treated as absence. Any other unrecognized shape fails closed.
    if value is False:
        return None
    return _required_many2one_id(value)


def _required_many2one_id(value: object) -> int:
    if isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[1], str):
        return _required_positive_int(value[0])
    if type(value) is int and value > 0:
        return value
    raise ErpRepositoryResponseError(SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR)


def _required_id_list(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise ErpRepositoryResponseError(SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR)
    return tuple(_required_positive_int(item) for item in value)


__all__ = [
    "ACCOUNT_MODEL",
    "CATEGORY_MODEL",
    "COMPANY_MODEL",
    "MAX_VISIBLE_COMPANIES",
    "OPTIONAL_FIELDS",
    "PRODUCT_MODEL",
    "REQUIRED_FIELDS",
    "SAFE_PURCHASE_ACCOUNT_DISCOVERY_ERROR",
    "TEMPLATE_MODEL",
    "OdooPurchaseAccountDiscoveryReader",
]
