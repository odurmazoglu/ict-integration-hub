from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from app.erp.odoo.adapter import OdooReadOnlyAdapter, many2one_id
from app.tax_mapping.repository import TaxCandidate
from app.tax_mapping.result import TaxType

TAX_FIELDS = ["id", "amount", "active", "type_tax_use", "company_id", "amount_type", "children_tax_ids"]


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
        return tuple(
            candidate for record in records
            if (candidate := _candidate(record)) is not None
        )


def _candidate(record: dict[str, Any]) -> TaxCandidate | None:
    if record.get("amount_type") != "percent":
        return None
    children = record.get("children_tax_ids")
    if not isinstance(children, list) or children:
        return None
    amount = Decimal(str(record.get("amount", "0")))
    if amount < 0 or record.get("type_tax_use") != "purchase":
        return None
    return TaxCandidate(
        tax_id=int(record["id"]),
        company_id=many2one_id(record.get("company_id")),
        tax_type=TaxType.VAT,
        rate=amount,
        active=bool(record.get("active", True)),
        usage_type="purchase",
        amount_type="percent",
        has_children=False,
    )
