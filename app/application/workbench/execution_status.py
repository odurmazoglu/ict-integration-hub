"""P0-PROD-12A: read-only operator execution/recovery status for one Workbench review.

Pure composition over already-persisted truth (``workflow_executions``,
``workbench_review_decisions``, Stage-1/Stage-2 execution evidence,
``workbench_review_write_authorizations``). This module introduces no new
persisted state and performs no writes -- it exists only to make the exact
data an engineer previously had to read over SSH/psql (execution id/state,
retry count, prior failure, created artifact, evidence presence, the
authorization actually consumed by the execution) available through the API.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.application.dto.base import ApplicationDTO
from app.application.execution.contracts import ExecutionArtifact, ExecutionMode
from app.application.execution.runtime import ExecutionFailure, ExecutionState
from app.application.workbench.dto import ReviewStatus
from app.application.workbench.write_authorization import WriteAuthorizationOperationType, WriteAuthorizationStatus
from app.application.workflow import WorkflowType


@dataclass(frozen=True, slots=True)
class WorkbenchDecisionStatus(ApplicationDTO):
    """The accepted decision the review's current version was submitted with, if any."""

    decision_id: str | None
    decision_version: int
    selected_workflow: WorkflowType | None


@dataclass(frozen=True, slots=True)
class WorkbenchExecutionSummary(ApplicationDTO):
    """State of the most recent real (EXECUTE-mode) accepted-decision execution."""

    execution_id: str
    mode: ExecutionMode
    state: ExecutionState
    retry_count: int
    max_attempts: int
    remaining_attempts: int
    #: True only when ``state`` is WAITING_RETRY and the runtime's own retry
    #: policy (max_attempts) has not yet been exhausted -- the same structural
    #: fact ``ExecutionCoordinator``/``_should_retry`` already gates a resume
    #: on. This is not business-safety advice; it does not assert a retry
    #: WILL succeed, only that the runtime would still permit one.
    retry_possible: bool


@dataclass(frozen=True, slots=True)
class WorkbenchEvidenceStatus(ApplicationDTO):
    """Presence/version metadata only -- never the underlying immutable evidence payload."""

    stage_one_present: bool
    stage_one_review_version: int | None
    stage_two_present: bool
    stage_two_decision_version: int | None


@dataclass(frozen=True, slots=True)
class WorkbenchExecutionAuthorizationStatus(ApplicationDTO):
    """The narrow write authorization provably consumed by this execution, if any.

    Populated ONLY when a persisted authorization's ``consumed_by_execution_id``
    exactly matches the execution's ``execution_id`` -- the one relationship the
    existing schema can actually prove (``workflow_executions`` carries no
    ``authorization_id`` column of its own). A pending, not-yet-consumed
    authorization for this review is deliberately NOT surfaced here to avoid
    fabricating a relationship the data cannot prove; use the existing
    ``GET .../write-authorizations`` list endpoint for that.
    """

    authorization_id: str
    operation_type: WriteAuthorizationOperationType
    target_version: int
    status: WriteAuthorizationStatus
    use_count: int
    is_expired: bool
    consumed_by_execution_id: str | None


@dataclass(frozen=True, slots=True)
class WorkbenchRecoveryStatus(ApplicationDTO):
    """Factual recovery indicators derived from existing execution state.

    Deliberately does not include any "safe_to_retry" heuristic -- only facts
    already provable from the persisted execution snapshot.
    """

    execution_completed: bool
    waiting_retry: bool
    remaining_attempts: int


@dataclass(frozen=True, slots=True)
class WorkbenchExecutionStatus(ApplicationDTO):
    """Top-level read-only operator execution/recovery status for one review."""

    review_id: str
    company_id: int
    review_version: int
    review_status: ReviewStatus

    decision: WorkbenchDecisionStatus | None
    execution: WorkbenchExecutionSummary | None
    failure: ExecutionFailure | None
    artifacts: tuple[ExecutionArtifact, ...] = field(default_factory=tuple)
    evidence: WorkbenchEvidenceStatus = field(
        default_factory=lambda: WorkbenchEvidenceStatus(
            stage_one_present=False,
            stage_one_review_version=None,
            stage_two_present=False,
            stage_two_decision_version=None,
        )
    )
    authorization: WorkbenchExecutionAuthorizationStatus | None = None
    recovery: WorkbenchRecoveryStatus = field(
        default_factory=lambda: WorkbenchRecoveryStatus(
            execution_completed=False, waiting_retry=False, remaining_attempts=0
        )
    )
