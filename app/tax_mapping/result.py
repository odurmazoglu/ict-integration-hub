from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


class TaxType(StrEnum):
    VAT = "VAT"
    WITHHOLDING = "WITHHOLDING"
    EXEMPTION = "EXEMPTION"
    UNKNOWN = "UNKNOWN"


class TaxMatchStatus(StrEnum):
    MATCHED = "MATCHED"
    NOT_FOUND = "NOT_FOUND"
    MULTIPLE_MATCHES = "MULTIPLE_MATCHES"
    INVALID_INPUT = "INVALID_INPUT"


@dataclass(frozen=True, slots=True)
class TaxMatchResult:
    status: TaxMatchStatus
    tax_id: int | None
    company_id: int | None
    tax_type: TaxType | None
    tax_rate: Decimal | None
    matched_by: str | None
    confidence: Decimal | None
    reason: str
    candidate_count: int


@dataclass(frozen=True, slots=True)
class InvoiceTaxLineResult:
    line_number: str | None
    tax_index: int
    result: TaxMatchResult


@dataclass(frozen=True, slots=True)
class InvoiceTaxMappingResult:
    line_results: tuple[InvoiceTaxLineResult, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)
