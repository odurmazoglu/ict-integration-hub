"""Immutable fixed-asset (capitalization) accounting evidence for one review.

An operator's accepted ``CAPITALIZE_FIXED_ASSET`` accounting resolution selects,
explicitly, the Odoo fixed-asset account and the Odoo depreciation model every line
of the invoice is posted with. This module freezes exactly that selection as
execution evidence so a later Odoo configuration change (for example a changed
``account.account.depreciation_model_id`` default) can never silently alter an
already-decided review.

Responsibility split (see ``docs/FIXED_ASSET_ACCOUNTING.md``): the Hub only records
the intent and the selected ids and writes a *draft* Vendor Bill whose lines carry
``account_id`` + ``depreciation_model_id``. It never computes depreciation, never
creates ``account.asset`` and never posts. Odoo's native asset behaviour applies when
a human posts the bill.

Deliberately free of ``app.application.workbench`` imports so the decision DTOs, the
execution contracts and the Vendor Bill builder can all depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.application.dto.base import ApplicationDTO
from app.application.exceptions.base import ApplicationError
from app.application.expense_mapping.predicates import invoice_is_product_identifier_free
from app.domain.invoice import InternalInvoice
from app.matching import InvoiceProductMatchResult, PartnerMatchResult, PartnerMatchStatus, ProductMatchStatus

FIXED_ASSET_ACCOUNTING_SCHEMA_VERSION = 1
#: ``matched_by``-style provenance of the evidence: always an accepted review-scoped
#: accounting resolution, never a supplier-wide mapping.
FIXED_ASSET_ACCOUNTING_SOURCE = "review_accounting_resolution"


class FixedAssetAccountingContractError(ApplicationError):
    """Safe error for structurally invalid fixed-asset accounting evidence."""

    error_category = "fixed_asset_accounting_contract_error"


@dataclass(frozen=True, slots=True)
class FixedAssetAccounting(ApplicationDTO):
    """The frozen fixed-asset posting selection for every line of one invoice."""

    accounting_resolution_id: int
    asset_account_id: int
    depreciation_model_id: int
    schema_version: int = FIXED_ASSET_ACCOUNTING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("accounting_resolution_id", "asset_account_id", "depreciation_model_id"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise FixedAssetAccountingContractError(f"fixed_asset_accounting.{name} must be a positive integer.")
        if self.schema_version != FIXED_ASSET_ACCOUNTING_SCHEMA_VERSION:
            raise FixedAssetAccountingContractError("Unsupported fixed_asset_accounting schema_version.")


def fixed_asset_accounting_to_data(value: FixedAssetAccounting) -> dict[str, Any]:
    return {
        "schema_version": value.schema_version,
        "source": FIXED_ASSET_ACCOUNTING_SOURCE,
        "accounting_resolution_id": value.accounting_resolution_id,
        "asset_account_id": value.asset_account_id,
        "depreciation_model_id": value.depreciation_model_id,
    }


def fixed_asset_accounting_from_data(data: object) -> FixedAssetAccounting | None:
    """Strict inverse of :func:`fixed_asset_accounting_to_data`; ``None`` stays ``None``."""

    if data is None:
        return None
    expected = {"schema_version", "source", "accounting_resolution_id", "asset_account_id", "depreciation_model_id"}
    if not isinstance(data, dict) or set(data) != expected or data["source"] != FIXED_ASSET_ACCOUNTING_SOURCE:
        raise FixedAssetAccountingContractError("Persisted fixed_asset_accounting evidence is not canonical.")
    return FixedAssetAccounting(
        accounting_resolution_id=data["accounting_resolution_id"],
        asset_account_id=data["asset_account_id"],
        depreciation_model_id=data["depreciation_model_id"],
        schema_version=data["schema_version"],
    )


def fixed_asset_evidence_errors(
    *,
    fixed_asset_accounting: object | None,
    operating_expense_match: object | None,
    invoice: InternalInvoice,
    partner_match: PartnerMatchResult,
    product_match: InvoiceProductMatchResult,
) -> list[str]:
    """Fail-closed contradiction checks for fixed-asset execution evidence.

    ``[]`` when there is no fixed-asset evidence. Otherwise the snapshot must be a
    whole-invoice account-mode invoice exactly like operating-expense mode (matched
    supplier, product-identifier-free invoice, one INVALID_INPUT product result per
    line) and must never coexist with an operating-expense account.
    """

    if fixed_asset_accounting is None:
        return []
    if not isinstance(fixed_asset_accounting, FixedAssetAccounting):
        return ["fixed_asset_accounting must be a FixedAssetAccounting."]
    errors: list[str] = []
    if operating_expense_match is not None:
        errors.append("fixed_asset_accounting cannot coexist with operating_expense_match.")
    if partner_match.status is not PartnerMatchStatus.MATCHED or partner_match.partner_id is None:
        errors.append("fixed-asset evidence requires a matched supplier partner.")
    if not invoice_is_product_identifier_free(invoice):
        errors.append("fixed-asset evidence requires an invoice free of deterministic product identifiers.")
    if product_match.errors:
        errors.append("fixed-asset product evidence carries evaluation errors.")
    elif tuple(r.line_number for r in product_match.line_results) != tuple(line.line_number for line in invoice.lines):
        errors.append("fixed-asset product evidence must cover every invoice line exactly once.")
    elif any(r.result.status is not ProductMatchStatus.INVALID_INPUT for r in product_match.line_results):
        errors.append("fixed-asset product evidence must be INVALID_INPUT for every line.")
    return errors


__all__ = [
    "FIXED_ASSET_ACCOUNTING_SCHEMA_VERSION",
    "FIXED_ASSET_ACCOUNTING_SOURCE",
    "FixedAssetAccounting",
    "FixedAssetAccountingContractError",
    "fixed_asset_accounting_from_data",
    "fixed_asset_accounting_to_data",
    "fixed_asset_evidence_errors",
]
