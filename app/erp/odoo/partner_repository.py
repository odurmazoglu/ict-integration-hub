from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.erp.models import Partner
from app.erp.odoo.adapter import OdooReadOnlyAdapter, many2one_id

PARTNER_FIELDS = [
    "id",
    "name",
    "vat",
    "active",
    # P0-PROD-15J: "company_type" is deliberately NOT requested. It is a valid
    # standard Odoo field in general, but the real production Odoo Online
    # instance's JSON-2 search_read rejects it with an HTTP 500 ("Invalid field
    # 'company_type' on 'res.partner'") -- confirmed against production. Every
    # other field in this list has been read successfully against the same
    # instance. `Partner.company_type`/`SupplierCandidate.company_type` remain
    # `None` (their existing default) rather than a fabricated value: this is
    # read-model robustness against an unavailable field, not corrupt data.
    "parent_id",
    "commercial_partner_id",
    "street",
    "street2",
    "zip",
    "city",
    "state_id",
    "country_id",
    "email",
    "phone",
    "mobile",
    "website",
    "supplier_rank",
    "customer_rank",
    "company_id",
]


class OdooPartnerRepository:
    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def find_by_tax_number(self, tax_number: str, *, company_id: int | None = None) -> Sequence[Partner]:
        domain: list[Any] = [["vat", "=", tax_number]]
        if company_id is not None:
            domain.append(["company_id", "in", [company_id, False]])
        records = self._adapter.search_read_all(model="res.partner", domain=domain, fields=PARTNER_FIELDS)
        return tuple(_partner(record) for record in records)

    def find_by_ids(self, ids: Sequence[int]) -> Sequence[Partner]:
        if not ids:
            return ()
        records = self._adapter.search_read_all(
            model="res.partner",
            domain=[["id", "in", list(ids)]],
            fields=PARTNER_FIELDS,
            max_records=len(ids),
        )
        return tuple(_partner(record) for record in records)


def _partner(record: dict[str, Any]) -> Partner:
    return Partner(
        id=int(record["id"]),
        name=_optional_str(record.get("name")),
        tax_number=_optional_str(record.get("vat")),
        active=bool(record.get("active", True)),
        company_id=many2one_id(record.get("company_id")),
        company_type=_optional_str(record.get("company_type")),
        parent_id=many2one_id(record.get("parent_id")),
        commercial_partner_id=many2one_id(record.get("commercial_partner_id")),
        street=_optional_str(record.get("street")),
        street2=_optional_str(record.get("street2")),
        zip_code=_optional_str(record.get("zip")),
        city=_optional_str(record.get("city")),
        state_id=many2one_id(record.get("state_id")),
        country_id=many2one_id(record.get("country_id")),
        email=_optional_str(record.get("email")),
        phone=_optional_str(record.get("phone")),
        mobile=_optional_str(record.get("mobile")),
        website=_optional_str(record.get("website")),
        supplier_rank=_optional_int(record.get("supplier_rank")),
        customer_rank=_optional_int(record.get("customer_rank")),
    )


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: Any) -> int | None:
    return value if type(value) is int else None
