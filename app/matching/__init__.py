"""Deterministic matching helpers for internal invoice domain models."""

from app.matching.exceptions import MatchingError, PartnerMatchingError, ProductMatchingError
from app.matching.partner import (
    CommercialPartnerGroup,
    PartnerMatchingEngine,
    canonical_partner_id,
    group_by_commercial_partner,
)
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
    "CommercialPartnerGroup",
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
    "canonical_partner_id",
    "group_by_commercial_partner",
]
