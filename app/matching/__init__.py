"""Deterministic matching helpers for internal invoice domain models."""

from app.matching.exceptions import MatchingError, PartnerMatchingError, ProductMatchingError
from app.matching.partner import PartnerMatchingEngine
from app.matching.product import ProductMatchingEngine
from app.matching.result import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    PartnerMatchResult,
    PartnerMatchStatus,
    ProductMatchResult,
    ProductMatchStatus,
)

__all__ = [
    "InvoiceProductLineResult",
    "InvoiceProductMatchResult",
    "MatchingError",
    "PartnerMatchResult",
    "PartnerMatchStatus",
    "PartnerMatchingError",
    "PartnerMatchingEngine",
    "ProductMatchResult",
    "ProductMatchStatus",
    "ProductMatchingEngine",
    "ProductMatchingError",
]
