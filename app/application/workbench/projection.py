from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from app.application.dto import ApplicationDTO
from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.dto import (
    BusinessContextDecision,
    LineResolution,
    ReviewDecisionType,
    ReviewStatus,
    TaxResolution,
)
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workflow import ManualReviewReason, WorkflowType


@dataclass(frozen=True, slots=True)
class WorkbenchProjection(ApplicationDTO):
    """ERP-neutral projection of one Hub-owned Workbench review item."""

    review_id: str
    company_id: int
    invoice_id: str
    version: int
    status: ReviewStatus
    invoice_number: str | None
    supplier_name: str | None
    supplier_tax_number: str | None
    invoice_date: date | None
    currency: str | None
    total_amount: Decimal | None
    workflow: WorkflowType
    review_summary: str | None = None
    review_reasons: tuple[ManualReviewReason, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    trace_id: str | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_text(self.invoice_id, "invoice_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.version, "version must be positive.")
        _require_enum(self.status, ReviewStatus, "status must be a canonical ReviewStatus.")
        _require_enum(self.workflow, WorkflowType, "workflow must be a canonical WorkflowType.")
        _validate_decimal(self.total_amount, "total_amount must be a finite Decimal value.")
        object.__setattr__(self, "review_reasons", tuple(self.review_reasons))
        object.__setattr__(self, "warnings", tuple(str(warning) for warning in self.warnings))


@dataclass(frozen=True, slots=True)
class OdooWorkbenchDecisionCandidate(ApplicationDTO):
    """ERP-neutral candidate decision read from the future Odoo Studio projection."""

    odoo_record_id: int
    review_id: str
    company_id: int
    expected_version: int
    decision: ReviewDecisionType
    idempotency_key: str
    decided_by_odoo_user_id: int
    decided_at: datetime
    decision_ready: bool
    selected_workflow: WorkflowType | None = None
    selected_partner_id: int | None = None
    line_resolutions: tuple[LineResolution, ...] = field(default_factory=tuple)
    tax_resolutions: tuple[TaxResolution, ...] = field(default_factory=tuple)
    business_context: BusinessContextDecision | None = None
    comment: str | None = None

    def __post_init__(self) -> None:
        _require_positive_int(self.odoo_record_id, "odoo_record_id must be a positive ERP id.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.expected_version, "expected_version must be positive.")
        _require_positive_int(
            self.decided_by_odoo_user_id,
            "decided_by_odoo_user_id must be a positive ERP id.",
        )
        _require_text(self.review_id, "review_id is required.")
        _require_text(self.idempotency_key, "idempotency_key is required.")
        _require_enum(self.decision, ReviewDecisionType, "decision must be a canonical ReviewDecisionType.")
        if self.selected_workflow is not None:
            _require_enum(
                self.selected_workflow,
                WorkflowType,
                "selected_workflow must be a canonical WorkflowType.",
            )
        if self.decision_ready is not True:
            raise WorkbenchContractError("decision_ready must be true for decision candidates.")
        line_resolutions = tuple(self.line_resolutions)
        tax_resolutions = tuple(self.tax_resolutions)
        object.__setattr__(self, "line_resolutions", line_resolutions)
        object.__setattr__(self, "tax_resolutions", tax_resolutions)
        ReviewDecisionCommand(
            review_id=self.review_id,
            company_id=self.company_id,
            expected_version=self.expected_version,
            decision=self.decision,
            decided_by=f"odoo:{self.decided_by_odoo_user_id}",
            idempotency_key=self.idempotency_key,
            selected_workflow=self.selected_workflow,
            selected_partner_id=self.selected_partner_id,
            line_resolutions=line_resolutions,
            tax_resolutions=tax_resolutions,
            business_context=self.business_context,
            comment=self.comment,
        )


@dataclass(frozen=True, slots=True)
class ProjectionPublishResult(ApplicationDTO):
    """Immutable result for publishing or acknowledging one Workbench projection."""

    review_id: str
    odoo_record_id: int
    created: bool
    updated: bool
    version: int
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.odoo_record_id, "odoo_record_id must be a positive ERP id.")
        _require_positive_int(self.version, "version must be positive.")
        object.__setattr__(self, "warnings", tuple(str(warning) for warning in self.warnings))


def _require_text(value: str | None, message: str) -> None:
    if value is None or not value.strip():
        raise WorkbenchContractError(message)


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)


def _require_enum(value: Enum, expected_type: type[Enum], message: str) -> None:
    if not isinstance(value, expected_type):
        raise WorkbenchContractError(message)


def _validate_decimal(value: Decimal | None, message: str) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal) or not value.is_finite():
        raise WorkbenchContractError(message)
