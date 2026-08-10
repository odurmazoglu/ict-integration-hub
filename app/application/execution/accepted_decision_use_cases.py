from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import NAMESPACE_URL, uuid5

from app.application.commands import Command
from app.application.dto import ApplicationDTO
from app.application.execution.contracts import AcceptedReviewDecision, ExecutionMode, ExecutionRequest, ExecutionStatus
from app.application.execution.exceptions import ExecutionModeNotEnabledError, ExecutionPlanningError
from app.application.execution.planner import ExecutionPlanner
from app.application.execution.ports import (
    AcceptedReviewDecisionReader,
    ExecutionRuntimeRepository,
    RetryPolicyResolver,
)
from app.application.execution.runtime import ExecutionState
from app.application.execution.runtime_service import ExecutionRuntimeCoordinator, ExecutionRuntimeService
from app.application.workbench.dto import ReviewDecisionType
from app.application.workbench.exceptions import ReviewNotFoundError


class AcceptedDecisionExecutionStatus(StrEnum):
    DRY_RUN_COMPLETED = ExecutionStatus.DRY_RUN_COMPLETED.value
    NOT_EXECUTABLE = "not_executable"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class RunAcceptedDecisionExecutionCommand(Command):
    """Run one persisted accepted Workbench decision through the no-write runtime."""

    review_id: str
    company_id: int
    decision_version: int
    mode: ExecutionMode = ExecutionMode.DRY_RUN

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.decision_version, "decision_version must be positive.")
        if not isinstance(self.mode, ExecutionMode):
            raise ExecutionPlanningError("mode must be a canonical ExecutionMode.")


@dataclass(frozen=True, slots=True)
class AcceptedDecisionExecutionResult(ApplicationDTO):
    review_id: str
    company_id: int
    decision_version: int
    status: AcceptedDecisionExecutionStatus
    execution_id: str | None = None
    runtime_state: ExecutionState | None = None

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.decision_version, "decision_version must be positive.")
        if not isinstance(self.status, AcceptedDecisionExecutionStatus):
            raise ExecutionPlanningError("status must be a canonical AcceptedDecisionExecutionStatus.")
        if self.execution_id is not None:
            _require_text(self.execution_id, "execution_id must be non-empty when supplied.")
        if self.runtime_state is not None and not isinstance(self.runtime_state, ExecutionState):
            raise ExecutionPlanningError("runtime_state must be a canonical ExecutionState when supplied.")


class RunAcceptedDecisionExecutionUseCase:
    """Execute a canonical accepted Hub decision through the durable dry-run runtime."""

    def __init__(
        self,
        *,
        accepted_decision_reader: AcceptedReviewDecisionReader,
        execution_planner: ExecutionPlanner,
        runtime_service: ExecutionRuntimeService,
        runtime_coordinator: ExecutionRuntimeCoordinator,
        runtime_repository: ExecutionRuntimeRepository,
        retry_policy_resolver: RetryPolicyResolver,
    ) -> None:
        self._accepted_decision_reader = accepted_decision_reader
        self._execution_planner = execution_planner
        self._runtime_service = runtime_service
        self._runtime_coordinator = runtime_coordinator
        self._runtime_repository = runtime_repository
        self._retry_policy_resolver = retry_policy_resolver

    def execute(self, command: RunAcceptedDecisionExecutionCommand) -> AcceptedDecisionExecutionResult:
        if not isinstance(command, RunAcceptedDecisionExecutionCommand):
            raise ExecutionPlanningError("RunAcceptedDecisionExecutionCommand is required.")
        if command.mode is ExecutionMode.EXECUTE:
            raise ExecutionModeNotEnabledError("EXECUTE mode is not enabled for accepted decision runtime integration.")

        try:
            decision = self._accepted_decision_reader.get_accepted_decision(
                review_id=command.review_id,
                company_id=command.company_id,
                decision_version=command.decision_version,
            )
        except ReviewNotFoundError:
            return AcceptedDecisionExecutionResult(
                review_id=command.review_id,
                company_id=command.company_id,
                decision_version=command.decision_version,
                status=AcceptedDecisionExecutionStatus.NOT_FOUND,
            )

        if decision.decision_type is ReviewDecisionType.DISMISS:
            return AcceptedDecisionExecutionResult(
                review_id=command.review_id,
                company_id=command.company_id,
                decision_version=command.decision_version,
                status=AcceptedDecisionExecutionStatus.NOT_EXECUTABLE,
            )

        request = _execution_request(command, decision=decision)
        plan = self._execution_planner.plan(request)
        runtime = self._runtime_service.create_or_load(
            plan=plan,
            retry_policy=self._retry_policy_resolver.resolve(plan),
        )
        result = self._runtime_coordinator.execute(runtime.snapshot)
        snapshot = self._runtime_repository.get_snapshot(execution_id=result.execution_id)
        return AcceptedDecisionExecutionResult(
            review_id=command.review_id,
            company_id=command.company_id,
            decision_version=command.decision_version,
            status=AcceptedDecisionExecutionStatus(result.status.value),
            execution_id=result.execution_id,
            runtime_state=snapshot.state if snapshot is not None else None,
        )


def _execution_request(
    command: RunAcceptedDecisionExecutionCommand,
    *,
    decision: AcceptedReviewDecision,
) -> ExecutionRequest:
    execution_id = _execution_id(command, decision=decision)
    return ExecutionRequest(
        execution_id=execution_id,
        review_id=decision.review_id,
        company_id=decision.company_id,
        decision_version=decision.decision_version,
        decision_id=decision.decision_id,
        idempotency_key=None,
        mode=command.mode,
        selected_workflow=decision.selected_workflow,
        business_context_allocations=decision.business_context_allocations,
    )


def _execution_id(command: RunAcceptedDecisionExecutionCommand, *, decision: AcceptedReviewDecision) -> str:
    decision_identity = decision.decision_id or "decision-id-absent"
    identity = (
        f"accepted-decision-execution:{command.company_id}:"
        f"{command.review_id}:{command.decision_version}:{decision_identity}:{command.mode.value}"
    )
    return f"accepted-decision-execution:{uuid5(NAMESPACE_URL, identity)}"


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise ExecutionPlanningError(message)


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise ExecutionPlanningError(message)
