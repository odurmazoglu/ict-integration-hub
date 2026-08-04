from __future__ import annotations

from dataclasses import dataclass, field

from app.application.commands import Command
from app.application.workbench.allocations import BusinessContextAllocationSet
from app.application.workbench.dto import LineResolution, ReviewDecisionType, TaxResolution
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workflow import WorkflowType

MAX_REVIEW_COMMENT_LENGTH = 1000


@dataclass(frozen=True, slots=True)
class ReviewDecisionCommand(Command):
    """Explicit user decision command for a future Workbench handler."""

    review_id: str
    company_id: int
    expected_version: int
    decision: ReviewDecisionType
    decided_by: str
    idempotency_key: str
    selected_workflow: WorkflowType | None = None
    selected_partner_id: int | None = None
    line_resolutions: tuple[LineResolution, ...] = field(default_factory=tuple)
    tax_resolutions: tuple[TaxResolution, ...] = field(default_factory=tuple)
    business_context_allocations: BusinessContextAllocationSet | None = None
    comment: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.expected_version, "expected_version must be positive.")
        _require_text(self.decided_by, "decided_by is required.")
        _require_text(self.idempotency_key, "idempotency_key is required.")
        if self.selected_partner_id is not None:
            _require_positive_int(self.selected_partner_id, "selected_partner_id must be a positive ERP id.")
        if self.comment is not None and len(self.comment) > MAX_REVIEW_COMMENT_LENGTH:
            raise WorkbenchContractError("comment exceeds maximum length.")

        line_resolutions = tuple(self.line_resolutions)
        tax_resolutions = tuple(self.tax_resolutions)
        object.__setattr__(self, "line_resolutions", line_resolutions)
        object.__setattr__(self, "tax_resolutions", tax_resolutions)
        _reject_duplicate_line_resolutions(line_resolutions)
        _reject_duplicate_tax_resolutions(tax_resolutions)
        _validate_decision_combination(self)


def _validate_decision_combination(command: ReviewDecisionCommand) -> None:
    if command.decision is ReviewDecisionType.SELECT_WORKFLOW:
        if command.selected_workflow is None:
            raise WorkbenchContractError("selected_workflow is required for SELECT_WORKFLOW.")
        if command.selected_workflow is WorkflowType.MANUAL_REVIEW:
            raise WorkbenchContractError("MANUAL_REVIEW cannot be selected as a resolution.")
        return

    if command.decision is ReviewDecisionType.DISMISS:
        if (
            command.selected_workflow is not None
            or command.selected_partner_id is not None
            or command.line_resolutions
            or command.tax_resolutions
            or command.business_context_allocations is not None
        ):
            raise WorkbenchContractError("DISMISS cannot include workflow-specific selections.")
        return

    raise WorkbenchContractError("Unsupported review decision.")


def _reject_duplicate_line_resolutions(resolutions: tuple[LineResolution, ...]) -> None:
    seen: set[str] = set()
    for resolution in resolutions:
        if resolution.line_number in seen:
            raise WorkbenchContractError("line_resolutions must have unique line_number values.")
        seen.add(resolution.line_number)


def _reject_duplicate_tax_resolutions(resolutions: tuple[TaxResolution, ...]) -> None:
    seen: set[tuple[str, int]] = set()
    for resolution in resolutions:
        key = (resolution.line_number, resolution.tax_index)
        if key in seen:
            raise WorkbenchContractError("tax_resolutions must have unique line_number and tax_index pairs.")
        seen.add(key)


def _require_text(value: str | None, message: str) -> None:
    if value is None or not value.strip():
        raise WorkbenchContractError(message)


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)
