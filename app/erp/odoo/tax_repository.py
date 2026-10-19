from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from app.erp.exceptions import ErpRepositoryResponseError
from app.erp.odoo.adapter import OdooReadOnlyAdapter, many2one_id
from app.tax_mapping.exemption import EXEMPTION_TAX_FIELDS
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
        return tuple(candidate for record in records if (candidate := _candidate(record)) is not None)


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


class OdooExemptionTaxReader:
    """PR T T2-A: read-only ``account.tax`` lookup by exact, configured id.

    Inactive records are read too, so an archived tax is reported as inactive rather than
    missing. Never searches by name or rate. Only ``account.tax``'s own fields are read --
    repartition lines, tags and the tax group are outside the Hub's Odoo allowlist.
    """

    def __init__(self, *, adapter: OdooReadOnlyAdapter) -> None:
        self._adapter = adapter

    def read_exemption_tax(self, tax_id: int) -> dict[str, Any] | None:
        if type(tax_id) is not int or tax_id <= 0:
            raise ErpRepositoryResponseError("Exemption tax id must be a positive integer.")
        records = self._adapter.search_read(
            model="account.tax",
            domain=[["id", "=", tax_id], ["active", "in", [True, False]]],
            fields=list(EXEMPTION_TAX_FIELDS),
            limit=2,
        )
        if not records:
            return None
        if len(records) != 1 or not isinstance(records[0], dict):
            raise ErpRepositoryResponseError("Odoo exemption tax read returned an unsafe response.")
        return dict(records[0])
