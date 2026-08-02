from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from app.application.dto import ApplicationDTO
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workflow import ManualReviewReason, WorkflowType


class ReviewStatus(StrEnum):
    """Canonical lifecycle states for Import Workbench review items."""

    PENDING_REVIEW = "pending_review"
    DECISION_SUBMITTED = "decision_submitted"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class ReviewDecisionType(StrEnum):
    """Canonical explicit user decisions accepted by the Workbench contract."""

    SELECT_WORKFLOW = "select_workflow"
    DISMISS = "dismiss"


@dataclass(frozen=True, slots=True)
class LineResolution(ApplicationDTO):
    """Explicit user-selected product resolution for one invoice line."""

    line_number: str
    selected_product_id: int

    def __post_init__(self) -> None:
        _require_text(self.line_number, "line_number is required.")
        _require_positive_int(self.selected_product_id, "selected_product_id must be a positive ERP id.")


@dataclass(frozen=True, slots=True)
class TaxResolution(ApplicationDTO):
    """Explicit user-selected tax resolution for one invoice tax."""

    line_number: str
    tax_index: int
    selected_tax_id: int

    def __post_init__(self) -> None:
        _require_text(self.line_number, "line_number is required.")
        if type(self.tax_index) is not int or self.tax_index < 0:
            raise WorkbenchContractError("tax_index must be zero or greater.")
        _require_positive_int(self.selected_tax_id, "selected_tax_id must be a positive ERP id.")


@dataclass(frozen=True, slots=True)
class BusinessContextDecision(ApplicationDTO):
    """Explicit procurement traceability identifiers selected by a user."""

    opportunity_id: int | None = None
    sales_order_id: int | None = None
    proposal_scenario_id: int | None = None
    purchase_order_id: int | None = None
    project_id: int | None = None
    analytic_account_id: int | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("opportunity_id", self.opportunity_id),
            ("sales_order_id", self.sales_order_id),
            ("proposal_scenario_id", self.proposal_scenario_id),
            ("purchase_order_id", self.purchase_order_id),
            ("project_id", self.project_id),
            ("analytic_account_id", self.analytic_account_id),
        ):
            if value is not None:
                _require_positive_int(value, f"{field_name} must be a positive ERP id.")


@dataclass(frozen=True, slots=True)
class ReviewItem(ApplicationDTO):
    """Safe Workbench list/detail item for one review-required invoice."""

    review_id: str
    invoice_id: str
    invoice_number: str | None
    supplier_tax_number: str | None
    supplier_name: str | None
    invoice_date: date | None
    currency: str | None
    total_amount: Decimal | None
    workflow: WorkflowType
    status: ReviewStatus
    review_reasons: tuple[ManualReviewReason, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int = 1

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_text(self.invoice_id, "invoice_id is required.")
        _require_positive_int(self.version, "version must be positive.")
        object.__setattr__(self, "review_reasons", tuple(self.review_reasons))
        object.__setattr__(self, "warnings", tuple(self.warnings))


@dataclass(frozen=True, slots=True)
class ReviewQueueResult(ApplicationDTO):
    """Immutable paginated Workbench queue result."""

    items: tuple[ReviewItem, ...] = field(default_factory=tuple)
    total_count: int = 0
    limit: int = 50
    offset: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        if type(self.total_count) is not int or self.total_count < 0:
            raise WorkbenchContractError("total_count must be zero or greater.")
        _require_positive_int(self.limit, "limit must be positive.")
        if type(self.offset) is not int or self.offset < 0:
            raise WorkbenchContractError("offset must be zero or greater.")


@dataclass(frozen=True, slots=True)
class ReviewDecisionAcknowledgement(ApplicationDTO):
    """Safe acknowledgement that a future decision handler accepted a command."""

    accepted: bool
    review_id: str
    status: ReviewStatus
    version: int
    decision: ReviewDecisionType
    selected_workflow: WorkflowType | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.version, "version must be positive.")
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "errors", tuple(self.errors))


def _require_text(value: str | None, message: str) -> None:
    if value is None or not value.strip():
        raise WorkbenchContractError(message)


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)
