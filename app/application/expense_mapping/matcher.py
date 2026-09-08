from __future__ import annotations

from typing import Protocol

from app.application.expense_mapping.matching import OperatingExpenseMatchResult, OperatingExpenseMatchStatus
from app.matching import PartnerMatchResult

NULL_MATCHER_REASON = "Operating-expense matching is not enabled for this runtime."


class OperatingExpenseMatcher(Protocol):
    """Contract the rule engine depends on for deterministic operating-expense classification."""

    def match_invoice(
        self,
        invoice: object,
        *,
        company_id: int | None,
        partner_match: PartnerMatchResult | None,
    ) -> OperatingExpenseMatchResult:
        pass


class NullOperatingExpenseMatcher:
    """Inert matcher: always ``NOT_FOUND``.

    Used wherever operating-expense classification must stay disabled (the default
    for production composition until the full account-only Vendor Bill chain ships).
    """

    def match_invoice(
        self,
        invoice: object,
        *,
        company_id: int | None,
        partner_match: PartnerMatchResult | None,
    ) -> OperatingExpenseMatchResult:
        return OperatingExpenseMatchResult(
            status=OperatingExpenseMatchStatus.NOT_FOUND,
            reason=NULL_MATCHER_REASON,
        )
