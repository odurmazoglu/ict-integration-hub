from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from app.erp.odoo.adapter import OdooReadOnlyAdapter, many2one_id
from app.tax_mapping.repository import TaxCandidate
from app.tax_mapping.result import TaxType

TAX_FIELDS = ["id", "amount", "active", "type_tax_use", "company_id"]


class OdooTaxRepository:
    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def find_candidates(
        self,
        *,
        company_id: int | None,
        rate: Decimal,
        tax_type: TaxType,
    ) -> Sequence[TaxCandidate]:
        domain: list[Any] = [["amount", "=", float(rate)], ["type_tax_use", "=", "purchase"]]
        if company_id is not None:
            domain.append(["company_id", "in", [company_id, False]])
        records = self._adapter.search_read_all(model="account.tax", domain=domain, fields=TAX_FIELDS)
        return tuple(_candidate(record, tax_type=tax_type) for record in records)


def _candidate(record: dict[str, Any], *, tax_type: TaxType) -> TaxCandidate:
    return TaxCandidate(
        tax_id=int(record["id"]),
        company_id=many2one_id(record.get("company_id")),
        tax_type=tax_type,
        rate=Decimal(str(record.get("amount", "0"))),
        active=bool(record.get("active", True)),
        usage_type=record.get("type_tax_use") if isinstance(record.get("type_tax_use"), str) else None,
    )
