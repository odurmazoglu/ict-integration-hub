from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from app.domain.invoice import InternalInvoice
from app.erp.models import Partner
from app.erp.provider import RepositoryProvider
from app.matching.exceptions import PartnerMatchingError
from app.matching.result import PartnerMatchResult, PartnerMatchStatus

EXACT_MATCH_CONFIDENCE = Decimal("1.00")


class PartnerMatchingEngine:
    """Deterministic supplier matcher for imported invoices."""

    def __init__(self, provider: RepositoryProvider) -> None:
        self._provider = provider

    def match_invoice(self, invoice: object, *, company_id: int | None = None) -> PartnerMatchResult:
        if not isinstance(invoice, InternalInvoice):
            return _result(
                status=PartnerMatchStatus.INVALID_INPUT,
                partner_id=None,
                matched_by=None,
                reason="InternalInvoice DTO is required for supplier matching.",
                candidate_count=0,
                confidence=None,
            )

        tax_number = _clean(invoice.supplier.tax_number)
        if tax_number is None:
            return _result(
                status=PartnerMatchStatus.INVALID_INPUT,
                partner_id=None,
                matched_by=None,
                reason="Supplier tax number is required for deterministic matching.",
                candidate_count=0,
                confidence=None,
            )

        try:
            candidates = self._provider.partner_repository.find_by_tax_number(tax_number, company_id=company_id)
        except Exception as exc:
            raise PartnerMatchingError("Partner repository lookup failed.") from exc

        active_candidates = _active_candidates(candidates)
        if len(active_candidates) == 1:
            return _result(
                status=PartnerMatchStatus.MATCHED,
                partner_id=active_candidates[0].id,
                matched_by="tax_number",
                reason="Unique supplier partner match by tax number.",
                candidate_count=1,
                confidence=EXACT_MATCH_CONFIDENCE,
            )
        if len(active_candidates) > 1:
            return _result(
                status=PartnerMatchStatus.MULTIPLE_MATCHES,
                partner_id=None,
                matched_by=None,
                reason="Multiple active supplier partner candidates found by tax number.",
                candidate_count=len(active_candidates),
                confidence=None,
            )
        return _result(
            status=PartnerMatchStatus.NOT_FOUND,
            partner_id=None,
            matched_by=None,
            reason="No active deterministic supplier partner candidate found.",
            candidate_count=0,
            confidence=None,
        )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _active_candidates(candidates: Sequence[Partner]) -> tuple[Partner, ...]:
    return tuple(candidate for candidate in candidates if candidate.active)


def _result(
    *,
    status: PartnerMatchStatus,
    partner_id: int | None,
    matched_by: str | None,
    reason: str,
    candidate_count: int,
    confidence: Decimal | None,
) -> PartnerMatchResult:
    return PartnerMatchResult(
        status=status,
        partner_id=partner_id if status == PartnerMatchStatus.MATCHED else None,
        matched_by=matched_by,
        reason=reason,
        candidate_count=candidate_count,
        confidence=confidence,
    )
