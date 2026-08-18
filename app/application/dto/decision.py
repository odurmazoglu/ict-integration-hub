from __future__ import annotations

from dataclasses import dataclass, field

from app.application.dto.base import ApplicationDTO
from app.application.workflow import ManualReviewReason, WorkflowDecision, WorkflowType
from app.matching import InvoiceProductMatchResult, PartnerMatchResult
from app.tax_mapping import InvoiceTaxMappingResult


@dataclass(frozen=True, slots=True)
class RuleEvaluationResult(ApplicationDTO):
    """Rule Engine output consumed by the Decision Engine."""

    workflow_decision: WorkflowDecision
    partner_match: PartnerMatchResult | None = None
    product_match: InvoiceProductMatchResult | None = None
    tax_match: InvoiceTaxMappingResult | None = None
    classification_result: object | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def workflow(self) -> WorkflowType:
        return self.workflow_decision.workflow

    @property
    def matched_rule(self) -> str | None:
        return self.workflow_decision.matched_rule

    @property
    def explanation(self) -> str | None:
        return self.workflow_decision.explanation


@dataclass(frozen=True, slots=True)
class DecisionResult(ApplicationDTO):
    """Decision Engine result for a selected invoice workflow."""

    success: bool
    invoice_id: str
    workflow: WorkflowType
    strategy: str
    status: str
    vendor_bill_id: int | None = None
    review_id: str | None = None
    review_required: bool = False
    review_reasons: tuple[ManualReviewReason, ...] = field(default_factory=tuple)
    classification_result: object | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)
    duration: float = 0.0
