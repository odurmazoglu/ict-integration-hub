from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.application.dto import ApplicationDTO
from app.application.workbench.evidence import ReviewClassificationEvidence, ReviewExecutionEvidence
from app.application.workbench.exceptions import WorkbenchContractError
from app.application.workflow import ManualReviewReason, WorkflowType


class ReviewReclassificationTrigger(StrEnum):
    """Sanctioned reasons a persisted review may be deterministically reclassified.

    Reclassification is a machine recalculation of an existing review against
    *current* master data using its immutable source invoice -- never a human
    decision. Only explicit remediation checkpoints are accepted; arbitrary
    free-text triggers are not.
    """

    MASTER_DATA_CHANGED = "master_data_changed"
    SUPPLIER_RESOLUTION = "supplier_resolution"


@dataclass(frozen=True, slots=True)
class ReclassifyReviewCommand(ApplicationDTO):
    """Request to non-destructively reclassify one pending Workbench review."""

    review_id: str
    company_id: int
    expected_version: int
    trigger: ReviewReclassificationTrigger
    note: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.expected_version, "expected_version must be positive.")
        if not isinstance(self.trigger, ReviewReclassificationTrigger):
            raise WorkbenchContractError("A sanctioned reclassification trigger is required.")
        if self.note is not None and (not isinstance(self.note, str) or not self.note.strip()):
            raise WorkbenchContractError("note must be non-empty text when provided.")
        if self.note is not None and len(self.note) > 1024:
            raise WorkbenchContractError("note must be 1024 characters or fewer.")


@dataclass(frozen=True, slots=True)
class ReviewReclassificationProposal(ApplicationDTO):
    """Deterministic outcome the repository must apply atomically to a review.

    Built by the use case from the reconstructed immutable source invoice and the
    current normal DecisionEngine result. It carries no supplier/account payloads
    and no Odoo write results -- only the recalculated classification state and
    optional version-pinned evidence for ``to_version``.
    """

    review_id: str
    company_id: int
    expected_version: int
    trigger: ReviewReclassificationTrigger
    note: str | None
    source_invoice_id: str
    new_workflow: WorkflowType
    new_review_reasons: tuple[ManualReviewReason, ...] = field(default_factory=tuple)
    new_warnings: tuple[str, ...] = field(default_factory=tuple)
    matched_rule_code: str | None = None
    matched_rule_id: str | None = None
    new_classification_evidence: ReviewClassificationEvidence | None = None
    new_execution_evidence: ReviewExecutionEvidence | None = None

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.expected_version, "expected_version must be positive.")
        if not isinstance(self.trigger, ReviewReclassificationTrigger):
            raise WorkbenchContractError("A sanctioned reclassification trigger is required.")
        _require_text(self.source_invoice_id, "source_invoice_id is required.")
        if not isinstance(self.new_workflow, WorkflowType):
            raise WorkbenchContractError("new_workflow must be canonical.")
        object.__setattr__(self, "new_review_reasons", tuple(self.new_review_reasons))
        object.__setattr__(self, "new_warnings", tuple(self.new_warnings))
        to_version = self.expected_version + 1
        classification = self.new_classification_evidence
        if classification is not None and (
            classification.review_id != self.review_id
            or classification.company_id != self.company_id
            or classification.review_version != to_version
        ):
            raise WorkbenchContractError("Classification evidence must be pinned to (review, to_version).")
        execution = self.new_execution_evidence
        if execution is not None and (
            execution.review_id != self.review_id
            or execution.company_id != self.company_id
            or execution.review_version != to_version
            or execution.source_invoice_id != self.source_invoice_id
        ):
            raise WorkbenchContractError("Execution evidence must be pinned to (review, to_version, source invoice).")

    @property
    def to_version(self) -> int:
        return self.expected_version + 1

    @property
    def executable(self) -> bool:
        return self.new_execution_evidence is not None


@dataclass(frozen=True, slots=True)
class ReviewReclassificationResult(ApplicationDTO):
    """Outcome of a reclassification request."""

    review_id: str
    company_id: int
    changed: bool
    from_version: int
    to_version: int
    previous_workflow: WorkflowType
    new_workflow: WorkflowType
    previous_review_reasons: tuple[ManualReviewReason, ...]
    new_review_reasons: tuple[ManualReviewReason, ...]
    trigger: ReviewReclassificationTrigger
    executable: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "previous_review_reasons", tuple(self.previous_review_reasons))
        object.__setattr__(self, "new_review_reasons", tuple(self.new_review_reasons))


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise WorkbenchContractError(message)


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise WorkbenchContractError(message)
