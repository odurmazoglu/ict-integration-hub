from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.erp.models import Partner
from app.erp.odoo.adapter import OdooReadOnlyAdapter, many2one_id

PARTNER_FIELDS = ["id", "name", "vat", "active", "company_id"]


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
    )


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None
