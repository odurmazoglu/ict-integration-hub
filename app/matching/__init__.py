"""Deterministic matching helpers for internal invoice domain models."""

from app.matching.exceptions import ProductMatchingError
from app.matching.product import ProductMatchingEngine
from app.matching.result import (
    InvoiceProductLineResult,
    InvoiceProductMatchResult,
    ProductMatchResult,
    ProductMatchStatus,
)

__all__ = [
    "InvoiceProductLineResult",
    "InvoiceProductMatchResult",
    "ProductMatchResult",
    "ProductMatchStatus",
    "ProductMatchingEngine",
    "ProductMatchingError",
]
