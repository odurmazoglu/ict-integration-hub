from __future__ import annotations

from typing import Any

from app.erp.models import Company
from app.erp.odoo.adapter import OdooReadOnlyAdapter

COMPANY_FIELDS = ["id", "name", "vat"]


class OdooCompanyRepository:
    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def find_by_id(self, company_id: int) -> Company | None:
        records = self._adapter.search_read(
            model="res.company",
            domain=[["id", "=", company_id]],
            fields=COMPANY_FIELDS,
            limit=1,
        )
        if not records:
            return None
        return _company(records[0])

    def find_by_tax_number(self, tax_number: str) -> tuple[Company, ...]:
        records = self._adapter.search_read(
            model="res.company",
            domain=[["vat", "=", tax_number]],
            fields=COMPANY_FIELDS,
            limit=2,
        )
        return tuple(_company(record) for record in records)

    def find_default(self) -> Company | None:
        records = self._adapter.search_read(model="res.company", domain=[], fields=COMPANY_FIELDS, limit=1)
        if not records:
            return None
        return _company(records[0])


def _company(record: dict[str, Any]) -> Company:
    return Company(
        id=int(record["id"]),
        name=record.get("name") if isinstance(record.get("name"), str) else None,
        tax_number=record.get("vat") if isinstance(record.get("vat"), str) else None,
    )
