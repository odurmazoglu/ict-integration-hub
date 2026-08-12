from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from app.application.dto import ApplicationDTO
from app.application.rules import InvoiceClassificationStatus
from app.application.workbench.allocations import BusinessContextAllocationSet
from app.application.workbench.commands import ReviewDecisionCommand
from app.application.workbench.dto import (
    LineResolution,
    ReviewDecisionType,
    ReviewStatus,
    TaxResolution,
)
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workflow import ManualReviewReason, WorkflowType

CLASSIFICATION_STATUS_BADGES: dict[str, str] = {
    InvoiceClassificationStatus.MATCHED.value: "success",
    InvoiceClassificationStatus.REVIEW_REQUIRED.value: "warning",
    InvoiceClassificationStatus.NO_MATCH.value: "muted",
    InvoiceClassificationStatus.CONFLICT.value: "danger",
    "UNAVAILABLE": "muted",
}

BUSINESS_CONTEXT_BADGES: dict[bool, str] = {
    True: "info",
    False: "muted",
}

REVIEW_REQUIRED_BADGES: dict[bool, str] = {
    True: "warning",
    False: "muted",
}

WORKFLOW_DISPLAY_NAMES: dict[WorkflowType, str] = {
    WorkflowType.VENDOR_BILL: "Vendor Bill",
    WorkflowType.RFQ: "RFQ",
    WorkflowType.EXPENSE: "Expense",
    WorkflowType.ASSET: "Asset",
    WorkflowType.SUBSCRIPTION: "Subscription",
    WorkflowType.MANUAL_REVIEW: "Manual Review",
}


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
        _require_optional_aware_datetime(
            self.updated_at,
            "updated_at must be a timezone-aware datetime when supplied.",
        )
        object.__setattr__(self, "review_reasons", tuple(self.review_reasons))
        object.__setattr__(self, "warnings", tuple(str(warning) for warning in self.warnings))


@dataclass(frozen=True, slots=True)
class WorkbenchClassificationConflictRuleProjection(ApplicationDTO):
    """User-safe summary of one conflicting classification rule."""

    rule_name: str
    rule_code: str
    rule_version: int
    workflow: WorkflowType | None
    workflow_display: str | None
    classification_code: str | None

    def __post_init__(self) -> None:
        _require_text(self.rule_name, "rule_name is required.")
        _require_text(self.rule_code, "rule_code is required.")
        _require_positive_int(self.rule_version, "rule_version must be positive.")
        if self.workflow is not None:
            _require_enum(self.workflow, WorkflowType, "workflow must be a canonical WorkflowType.")
        expected_display = _workflow_display(self.workflow)
        if self.workflow_display != expected_display:
            raise WorkbenchContractError("workflow_display must match the canonical workflow display.")
        if self.classification_code is not None:
            _require_text(self.classification_code, "classification_code must be non-empty when supplied.")


@dataclass(frozen=True, slots=True)
class WorkbenchClassificationProjection(ApplicationDTO):
    """Read-only UI projection of pinned review classification evidence."""

    status: str
    status_label: str
    status_badge: str
    workflow: WorkflowType | None = None
    workflow_display: str | None = None
    classification_code: str | None = None
    matched_rule_name: str | None = None
    matched_rule_code: str | None = None
    matched_rule_version: int | None = None
    require_review: bool = False
    require_review_label: str = "No"
    require_review_badge: str = "muted"
    require_business_context: bool = False
    require_business_context_label: str = "Not Required"
    require_business_context_badge: str = "muted"
    conflict: bool = False
    conflict_label: str | None = None
    conflict_summary: str | None = None
    conflicting_rules_summary: tuple[WorkbenchClassificationConflictRuleProjection, ...] = field(default_factory=tuple)
    placeholder: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.status, "classification status is required.")
        if self.status not in CLASSIFICATION_STATUS_BADGES:
            raise WorkbenchContractError("classification status must be a supported projection status.")
        _require_text(self.status_label, "status_label is required.")
        if self.status_badge != CLASSIFICATION_STATUS_BADGES[self.status]:
            raise WorkbenchContractError("status_badge must match the canonical classification badge.")
        if self.workflow is not None:
            _require_enum(self.workflow, WorkflowType, "workflow must be a canonical WorkflowType.")
        if self.workflow_display != _workflow_display(self.workflow):
            raise WorkbenchContractError("workflow_display must match the canonical workflow display.")
        if self.classification_code is not None:
            _require_text(self.classification_code, "classification_code must be non-empty when supplied.")
        if self.matched_rule_version is not None:
            _require_positive_int(self.matched_rule_version, "matched_rule_version must be positive.")
        _validate_optional_text(self.matched_rule_name, "matched_rule_name must be non-empty when supplied.")
        _validate_optional_text(self.matched_rule_code, "matched_rule_code must be non-empty when supplied.")
        _require_exact_bool(self.require_review, "require_review must be boolean.")
        _require_exact_bool(self.require_business_context, "require_business_context must be boolean.")
        _require_exact_bool(self.conflict, "conflict must be boolean.")
        if self.require_review_label != _yes_no(self.require_review):
            raise WorkbenchContractError("require_review_label must match require_review.")
        if self.require_review_badge != REVIEW_REQUIRED_BADGES[self.require_review]:
            raise WorkbenchContractError("require_review_badge must match require_review.")
        if self.require_business_context_label != _required_label(self.require_business_context):
            raise WorkbenchContractError("require_business_context_label must match require_business_context.")
        if self.require_business_context_badge != BUSINESS_CONTEXT_BADGES[self.require_business_context]:
            raise WorkbenchContractError("require_business_context_badge must match require_business_context.")
        rules = tuple(self.conflicting_rules_summary)
        for rule in rules:
            if not isinstance(rule, WorkbenchClassificationConflictRuleProjection):
                raise WorkbenchContractError("conflicting_rules_summary must contain conflict rule projections.")
        object.__setattr__(self, "conflicting_rules_summary", rules)
        if self.status == InvoiceClassificationStatus.CONFLICT.value:
            if not self.conflict or len(rules) < 2:
                raise WorkbenchContractError("CONFLICT projection requires conflict rule summaries.")
            _require_text(self.conflict_summary, "conflict_summary is required for conflicts.")
            _require_text(self.conflict_label, "conflict_label is required for conflicts.")
        if self.status == InvoiceClassificationStatus.NO_MATCH.value:
            _require_text(self.placeholder, "NO_MATCH projection requires a safe placeholder.")
        if self.status == "UNAVAILABLE":
            _require_text(self.placeholder, "Missing evidence projection requires a safe placeholder.")


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
    business_context_allocations: BusinessContextAllocationSet | None = None
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
        _require_aware_datetime(self.decided_at, "decided_at must be a timezone-aware datetime.")
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
            business_context_allocations=self.business_context_allocations,
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
        _require_exactly_one_operation(created=self.created, updated=self.updated)
        object.__setattr__(self, "warnings", tuple(str(warning) for warning in self.warnings))


def _require_text(value: str | None, message: str) -> None:
    if value is None or not value.strip():
        raise WorkbenchContractError(message)


def _validate_optional_text(value: str | None, message: str) -> None:
    if value is not None:
        _require_text(value, message)


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)


def _require_exact_bool(value: bool, message: str) -> None:
    if type(value) is not bool:
        raise WorkbenchContractError(message)


def _require_enum(value: Enum, expected_type: type[Enum], message: str) -> None:
    if not isinstance(value, expected_type):
        raise WorkbenchContractError(message)


def _require_optional_aware_datetime(value: datetime | None, message: str) -> None:
    if value is None:
        return
    _require_aware_datetime(value, message)


def _require_aware_datetime(value: datetime, message: str) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise WorkbenchContractError(message)


def _require_exactly_one_operation(*, created: bool, updated: bool) -> None:
    if type(created) is not bool:
        raise WorkbenchContractError("created must be a boolean value.")
    if type(updated) is not bool:
        raise WorkbenchContractError("updated must be a boolean value.")
    if created == updated:
        raise WorkbenchContractError("Projection publish result must represent exactly one create or update.")


def _validate_decimal(value: Decimal | None, message: str) -> None:
    if value is None:
        return
    if not isinstance(value, Decimal) or not value.is_finite():
        raise WorkbenchContractError(message)


def _workflow_display(workflow: WorkflowType | None) -> str | None:
    if workflow is None:
        return None
    return WORKFLOW_DISPLAY_NAMES[workflow]


def _yes_no(value: bool) -> str:
    return "Yes" if value else "No"


def _required_label(value: bool) -> str:
    return "Required" if value else "Not Required"
