from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.domain.invoice import InternalInvoice, Tax
from app.tax_mapping.exceptions import TaxMappingError
from app.tax_mapping.repository import TaxCandidate, TaxRepository
from app.tax_mapping.result import (
    InvoiceTaxLineResult,
    InvoiceTaxMappingResult,
    TaxMatchResult,
    TaxMatchStatus,
    TaxType,
)

EXACT_MATCH_CONFIDENCE = Decimal("1.00")


class TaxMappingEngine:
    def __init__(self, repository: TaxRepository) -> None:
        self._repository = repository

    def map_invoice(self, invoice: InternalInvoice, *, company_id: int | None) -> InvoiceTaxMappingResult:
        if not invoice.lines:
            return InvoiceTaxMappingResult(errors=("Invoice has no lines to map.",))

        line_results: list[InvoiceTaxLineResult] = []
        errors: list[str] = []
        cache: dict[tuple[int | None, Decimal, TaxType], TaxMatchResult] = {}

        for line in invoice.lines:
            if line.line_number is None or not line.line_number.strip():
                if line.taxes:
                    for tax_index, tax in enumerate(line.taxes):
                        line_results.append(
                            InvoiceTaxLineResult(
                                line_number=line.line_number,
                                tax_index=tax_index,
                                result=_invalid_result(
                                    tax,
                                    reason="Line identifier is required for tax mapping.",
                                ),
                            )
                        )
                    continue
                errors.append("Line identifier is required for tax mapping.")
                continue

            for tax_index, tax in enumerate(line.taxes):
                result = self._map_tax(tax, company_id=company_id, cache=cache)
                line_results.append(
                    InvoiceTaxLineResult(line_number=line.line_number, tax_index=tax_index, result=result)
                )

        return InvoiceTaxMappingResult(line_results=tuple(line_results), errors=tuple(errors))

    def _map_tax(
        self,
        tax: Tax,
        *,
        company_id: int | None,
        cache: dict[tuple[int | None, Decimal, TaxType], TaxMatchResult],
    ) -> TaxMatchResult:
        tax_type = normalize_tax_type(tax.tax_type)
        rate = normalize_tax_rate(tax.rate)
        invalid_reason = _invalid_reason(tax_type=tax_type, rate=rate)
        if invalid_reason is not None:
            return _invalid_result(tax, reason=invalid_reason, tax_type=tax_type, rate=rate)

        assert rate is not None
        assert tax_type is not None
        cache_key = (company_id, rate, tax_type)
        if cache_key in cache:
            return cache[cache_key]

        try:
            candidates = self._repository.find_candidates(company_id=company_id, rate=rate, tax_type=tax_type)
        except TaxMappingError:
            return _result(
                status=TaxMatchStatus.INVALID_INPUT,
                tax_id=None,
                company_id=company_id,
                tax_type=tax_type,
                tax_rate=rate,
                matched_by=None,
                confidence=None,
                reason="Tax repository lookup failed.",
                candidate_count=0,
            )

        valid_candidates = _valid_candidates(candidates, company_id=company_id, rate=rate, tax_type=tax_type)
        result = _result_from_candidates(valid_candidates, company_id=company_id, rate=rate, tax_type=tax_type)
        cache[cache_key] = result
        return result


def normalize_tax_type(value: object) -> TaxType | None:
    if isinstance(value, TaxType):
        return value
    if value is None:
        return None
    normalized = str(value).strip().replace("-", "_").replace(" ", "_").upper()
    if not normalized:
        return None
    aliases = {
        "VAT": TaxType.VAT,
        "KDV": TaxType.VAT,
        "VALUE_ADDED_TAX": TaxType.VAT,
        "WITHHOLDING": TaxType.WITHHOLDING,
        "TEVKIFAT": TaxType.WITHHOLDING,
        "STOPAJ": TaxType.WITHHOLDING,
        "EXEMPTION": TaxType.EXEMPTION,
        "EXEMPT": TaxType.EXEMPTION,
        "ISTISNA": TaxType.EXEMPTION,
        "İSTİSNA": TaxType.EXEMPTION,
        "UNKNOWN": TaxType.UNKNOWN,
    }
    return aliases.get(normalized)


def normalize_tax_rate(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return rate.normalize()


def _invalid_reason(*, tax_type: TaxType | None, rate: Decimal | None) -> str | None:
    if tax_type is None:
        return "Tax type is required or unsupported."
    if tax_type == TaxType.UNKNOWN:
        return "Tax type is unknown."
    if rate is None:
        return "Tax rate is required or malformed."
    if rate < Decimal("0"):
        return "Tax rate must not be negative."
    return None


def _invalid_result(
    tax: Tax,
    *,
    reason: str,
    tax_type: TaxType | None = None,
    rate: Decimal | None = None,
) -> TaxMatchResult:
    return _result(
        status=TaxMatchStatus.INVALID_INPUT,
        tax_id=None,
        company_id=None,
        tax_type=tax_type if tax_type is not None else normalize_tax_type(tax.tax_type),
        tax_rate=rate if rate is not None else normalize_tax_rate(tax.rate),
        matched_by=None,
        confidence=None,
        reason=reason,
        candidate_count=0,
    )


def _valid_candidates(
    candidates: object,
    *,
    company_id: int | None,
    rate: Decimal,
    tax_type: TaxType,
) -> tuple[TaxCandidate, ...]:
    return tuple(
        candidate
        for candidate in candidates
        if candidate.active
        and candidate.tax_type == tax_type
        and normalize_tax_rate(candidate.rate) == rate
        and (company_id is None or candidate.company_id == company_id)
    )


def _result_from_candidates(
    candidates: tuple[TaxCandidate, ...],
    *,
    company_id: int | None,
    rate: Decimal,
    tax_type: TaxType,
) -> TaxMatchResult:
    if len(candidates) == 1:
        candidate = candidates[0]
        return _result(
            status=TaxMatchStatus.MATCHED,
            tax_id=candidate.tax_id,
            company_id=candidate.company_id,
            tax_type=tax_type,
            tax_rate=rate,
            matched_by="company_type_rate",
            confidence=EXACT_MATCH_CONFIDENCE,
            reason="Exact company, type, and rate match.",
            candidate_count=1,
        )
    if len(candidates) > 1:
        return _result(
            status=TaxMatchStatus.MULTIPLE_MATCHES,
            tax_id=None,
            company_id=company_id,
            tax_type=tax_type,
            tax_rate=rate,
            matched_by=None,
            confidence=None,
            reason="Multiple exact tax candidates found.",
            candidate_count=len(candidates),
        )
    return _result(
        status=TaxMatchStatus.NOT_FOUND,
        tax_id=None,
        company_id=company_id,
        tax_type=tax_type,
        tax_rate=rate,
        matched_by=None,
        confidence=None,
        reason="No active exact tax candidate found.",
        candidate_count=0,
    )


def _result(
    *,
    status: TaxMatchStatus,
    tax_id: int | None,
    company_id: int | None,
    tax_type: TaxType | None,
    tax_rate: Decimal | None,
    matched_by: str | None,
    confidence: Decimal | None,
    reason: str,
    candidate_count: int,
) -> TaxMatchResult:
    return TaxMatchResult(
        status=status,
        tax_id=tax_id if status == TaxMatchStatus.MATCHED else None,
        company_id=company_id,
        tax_type=tax_type,
        tax_rate=tax_rate,
        matched_by=matched_by,
        confidence=confidence,
        reason=reason,
        candidate_count=candidate_count,
    )
