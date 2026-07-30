from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.application.dto.base import ApplicationDTO
from app.matching import InvoiceProductMatchResult, PartnerMatchResult
from app.tax_mapping import InvoiceTaxMappingResult

WorkflowType = Literal["vendor_bill"]


@dataclass(frozen=True, slots=True)
class RuleEvaluationResult(ApplicationDTO):
    """Rule Engine output consumed by the Decision Engine."""

    workflow: str
    partner_match: PartnerMatchResult | None = None
    product_match: InvoiceProductMatchResult | None = None
    tax_match: InvoiceTaxMappingResult | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class DecisionResult(ApplicationDTO):
    """Decision Engine result for a selected invoice workflow."""

    success: bool
    invoice_id: str
    workflow: str
    strategy: str
    status: str
    vendor_bill_id: int | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)
    duration: float = 0.0
