from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from app.domain.invoice import InternalInvoice, InvoiceLine
from app.erp.models import Product
from app.erp.provider import RepositoryProvider
from app.matching.result import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    ProductMatchResult,
    ProductMatchStatus,
)

EXACT_MATCH_CONFIDENCE = Decimal("1.00")


class ProductMatchingEngine:
    def __init__(self, provider: RepositoryProvider) -> None:
        self._provider = provider

    def match_invoice(self, invoice: object, *, company_id: int | None = None) -> InvoiceProductMatchResult:
        if not isinstance(invoice, InternalInvoice):
            return InvoiceProductMatchResult(errors=("InternalInvoice DTO is required for product matching.",))
        if not invoice.lines:
            return InvoiceProductMatchResult(errors=("Invoice has no lines to match.",))

        line_results: list[InvoiceProductLineResult] = []
        for line in invoice.lines:
            result = self._match_line(line, company_id=company_id)
            line_results.append(InvoiceProductLineResult(line_number=line.line_number, result=result))
        return InvoiceProductMatchResult(line_results=tuple(line_results))

    def _match_line(self, line: InvoiceLine, *, company_id: int | None) -> ProductMatchResult:
        if line.line_number is None or not line.line_number.strip():
            return _result(
                status=ProductMatchStatus.INVALID_INPUT,
                line=line,
                product_id=None,
                matched_by=None,
                reason="Line identifier is required for product matching.",
                candidate_count=0,
                confidence=None,
            )

        lookup_plan = (
            ("default_code", _clean(line.buyer_item_code), self._provider.product_repository.find_by_default_code),
            ("barcode", _clean(line.barcode), self._provider.product_repository.find_by_barcode),
            ("seller_item_code", _clean(line.seller_item_code), self._provider.product_repository.find_by_default_code),
        )
        if all(identifier is None for _, identifier, _ in lookup_plan):
            return _result(
                status=ProductMatchStatus.INVALID_INPUT,
                line=line,
                product_id=None,
                matched_by=None,
                reason="At least one deterministic product identifier is required.",
                candidate_count=0,
                confidence=None,
            )

        for matched_by, identifier, lookup in lookup_plan:
            if identifier is None:
                continue
            try:
                candidates = lookup(identifier, company_id=company_id)
            except Exception:
                return _result(
                    status=ProductMatchStatus.INVALID_INPUT,
                    line=line,
                    product_id=None,
                    matched_by=None,
                    reason="Product repository lookup failed.",
                    candidate_count=0,
                    confidence=None,
                )

            active_candidates = _active_candidates(candidates)
            if len(active_candidates) == 1:
                return _result(
                    status=ProductMatchStatus.MATCHED,
                    line=line,
                    product_id=active_candidates[0].id,
                    matched_by=matched_by,
                    reason=f"Unique product match by {matched_by}.",
                    candidate_count=1,
                    confidence=EXACT_MATCH_CONFIDENCE,
                )
            if len(active_candidates) > 1:
                return _result(
                    status=ProductMatchStatus.MULTIPLE_MATCHES,
                    line=line,
                    product_id=None,
                    matched_by=None,
                    reason=f"Multiple active product candidates found by {matched_by}.",
                    candidate_count=len(active_candidates),
                    confidence=None,
                )

        return _result(
            status=ProductMatchStatus.NOT_FOUND,
            line=line,
            product_id=None,
            matched_by=None,
            reason="No active deterministic product candidate found.",
            candidate_count=0,
            confidence=None,
        )


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _active_candidates(candidates: Sequence[Product]) -> tuple[Product, ...]:
    return tuple(candidate for candidate in candidates if candidate.active)


def _result(
    *,
    status: ProductMatchStatus,
    line: InvoiceLine,
    product_id: int | None,
    matched_by: str | None,
    reason: str,
    candidate_count: int,
    confidence: Decimal | None,
) -> ProductMatchResult:
    return ProductMatchResult(
        status=status,
        line_number=line.line_number,
        product_id=product_id if status == ProductMatchStatus.MATCHED else None,
        default_code=_clean(line.buyer_item_code),
        barcode=_clean(line.barcode),
        seller_item_code=_clean(line.seller_item_code),
        matched_by=matched_by,
        reason=reason,
        candidate_count=candidate_count,
        confidence=confidence,
    )
