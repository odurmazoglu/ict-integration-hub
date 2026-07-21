from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum


class ProductMatchStatus(StrEnum):
    MATCHED = "MATCHED"
    NOT_FOUND = "NOT_FOUND"
    MULTIPLE_MATCHES = "MULTIPLE_MATCHES"
    INVALID_INPUT = "INVALID_INPUT"


class PartnerMatchStatus(StrEnum):
    MATCHED = "MATCHED"
    NOT_FOUND = "NOT_FOUND"
    MULTIPLE_MATCHES = "MULTIPLE_MATCHES"
    INVALID_INPUT = "INVALID_INPUT"


@dataclass(frozen=True, slots=True)
class PartnerMatchResult:
    status: PartnerMatchStatus
    partner_id: int | None
    matched_by: str | None
    reason: str
    candidate_count: int
    confidence: Decimal | None


@dataclass(frozen=True, slots=True)
class ProductMatchResult:
    status: ProductMatchStatus
    line_number: str | None
    product_id: int | None
    default_code: str | None
    barcode: str | None
    seller_item_code: str | None
    matched_by: str | None
    reason: str
    candidate_count: int
    confidence: Decimal | None


@dataclass(frozen=True, slots=True)
class InvoiceProductLineResult:
    line_number: str | None
    result: ProductMatchResult


@dataclass(frozen=True, slots=True)
class InvoiceProductMatchResult:
    line_results: tuple[InvoiceProductLineResult, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)
