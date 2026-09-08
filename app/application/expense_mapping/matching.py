from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.application.expense_mapping.contracts import OperatingExpenseMapping
from app.application.expense_mapping.exceptions import (
    OperatingExpenseMappingContractError,
    OperatingExpenseMappingDataIntegrityError,
    OperatingExpenseMappingError,
)
from app.application.expense_mapping.repository import OperatingExpenseMappingRepository
from app.domain.invoice import InternalInvoice
from app.matching import PartnerMatchResult, PartnerMatchStatus

EXACT_MATCH_CONFIDENCE = Decimal("1.00")
MATCHED_BY_COMPANY_PARTNER = "company_partner"


class OperatingExpenseMatchStatus(StrEnum):
    MATCHED = "MATCHED"
    NOT_FOUND = "NOT_FOUND"
    MULTIPLE_MATCHES = "MULTIPLE_MATCHES"
    INVALID_INPUT = "INVALID_INPUT"


@dataclass(frozen=True, slots=True)
class OperatingExpenseMatchResult:
    """Deterministic operating-expense classification outcome for one invoice.

    Only ``MATCHED`` exposes ``mapping_id``, ``expense_account_id`` and
    ``expense_category``. It is derived purely from the already-resolved
    ``(company_id, vendor_partner_id)`` and the persistent mapping table; no
    supplier VAT/name re-matching and no invoice-description heuristics.
    """

    status: OperatingExpenseMatchStatus
    reason: str
    candidate_count: int = 0
    mapping_id: int | None = None
    company_id: int | None = None
    vendor_partner_id: int | None = None
    expense_account_id: int | None = None
    expense_category: str | None = None
    matched_by: str | None = None
    confidence: Decimal | None = None

    def __post_init__(self) -> None:
        matched = self.status is OperatingExpenseMatchStatus.MATCHED
        if not matched and (
            self.mapping_id is not None or self.expense_account_id is not None or self.expense_category is not None
        ):
            raise OperatingExpenseMappingContractError(
                "Only a MATCHED operating-expense result may expose mapping, account, or category."
            )
        if matched and (self.mapping_id is None or self.expense_account_id is None or self.expense_category is None):
            raise OperatingExpenseMappingContractError(
                "A MATCHED operating-expense result must expose mapping, account, and category."
            )


class OperatingExpenseMatchingEngine:
    """Deterministic operating-expense matcher over the persistent mapping table."""

    def __init__(self, repository: OperatingExpenseMappingRepository) -> None:
        self._repository = repository

    def match_invoice(
        self,
        invoice: object,
        *,
        company_id: int | None,
        partner_match: PartnerMatchResult | None,
    ) -> OperatingExpenseMatchResult:
        if not isinstance(invoice, InternalInvoice):
            return _result(
                OperatingExpenseMatchStatus.INVALID_INPUT,
                "InternalInvoice DTO is required for operating-expense matching.",
            )
        if type(company_id) is not int or company_id <= 0:
            return _result(
                OperatingExpenseMatchStatus.INVALID_INPUT,
                "A positive company_id is required for operating-expense matching.",
            )
        if not isinstance(partner_match, PartnerMatchResult):
            return _result(
                OperatingExpenseMatchStatus.INVALID_INPUT,
                "A supplier PartnerMatchResult is required for operating-expense matching.",
            )
        if partner_match.status is not PartnerMatchStatus.MATCHED or partner_match.partner_id is None:
            return _result(
                OperatingExpenseMatchStatus.NOT_FOUND,
                "Supplier partner is not deterministically matched.",
                company_id=company_id,
            )

        try:
            mapping = self._repository.find_for_supplier(
                company_id=company_id,
                vendor_partner_id=partner_match.partner_id,
            )
        except OperatingExpenseMappingDataIntegrityError:
            return _result(
                OperatingExpenseMatchStatus.MULTIPLE_MATCHES,
                "More than one enabled operating-expense mapping exists for the supplier.",
                candidate_count=2,
                company_id=company_id,
                vendor_partner_id=partner_match.partner_id,
            )
        except OperatingExpenseMappingError:
            return _result(
                OperatingExpenseMatchStatus.INVALID_INPUT,
                "Operating-expense mapping lookup failed.",
                company_id=company_id,
                vendor_partner_id=partner_match.partner_id,
            )

        if mapping is None:
            return _result(
                OperatingExpenseMatchStatus.NOT_FOUND,
                "No enabled operating-expense mapping is configured for this supplier.",
                company_id=company_id,
                vendor_partner_id=partner_match.partner_id,
            )
        return _matched_result(mapping)


def _result(
    status: OperatingExpenseMatchStatus,
    reason: str,
    *,
    candidate_count: int = 0,
    company_id: int | None = None,
    vendor_partner_id: int | None = None,
) -> OperatingExpenseMatchResult:
    return OperatingExpenseMatchResult(
        status=status,
        reason=reason,
        candidate_count=candidate_count,
        company_id=company_id,
        vendor_partner_id=vendor_partner_id,
    )


def _matched_result(mapping: OperatingExpenseMapping) -> OperatingExpenseMatchResult:
    return OperatingExpenseMatchResult(
        status=OperatingExpenseMatchStatus.MATCHED,
        reason="Exact company and supplier partner operating-expense mapping.",
        candidate_count=1,
        mapping_id=mapping.id,
        company_id=mapping.company_id,
        vendor_partner_id=mapping.vendor_partner_id,
        expense_account_id=mapping.expense_account_id,
        expense_category=mapping.expense_category,
        matched_by=MATCHED_BY_COMPANY_PARTNER,
        confidence=EXACT_MATCH_CONFIDENCE,
    )
