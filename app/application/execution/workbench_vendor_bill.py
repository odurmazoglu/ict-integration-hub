from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol

from app.application.dto import ApplicationDTO
from app.application.execution.accepted_decision_use_cases import (
    AcceptedDecisionExecutionStatus,
    RunAcceptedDecisionExecutionCommand,
    RunAcceptedDecisionExecutionUseCase,
    accepted_decision_execution_id,
)
from app.application.execution.contracts import (
    AcceptedReviewDecision,
    ExecutionApproval,
    ExecutionArtifact,
    ExecutionMode,
)
from app.application.execution.exceptions import (
    ExecutionApprovalError,
    ExecutionError,
    ExecutionModeNotEnabledError,
    ExecutionPlanningError,
    ExecutionSourceInvoiceIntegrityError,
    ExecutionSourceInvoiceNotFoundError,
    ExecutionUnsupportedStepError,
)
from app.application.execution.ports import (
    AcceptedReviewDecisionReader,
    ExecutionRuntimeRepository,
    ExecutionSourceInvoiceReader,
)
from app.application.execution.runtime import ExecutionState
from app.application.workbench.dto import ReviewDecisionType
from app.application.workbench.exceptions import (
    ReviewNotFoundError,
    WorkbenchCandidateAmbiguityError,
    WorkbenchCandidateReadError,
    WorkbenchProjectionPublishError,
)
from app.application.workbench.projection import ProjectionPublishResult
from app.application.workflow import WorkflowType

EXECUTION_PROJECTION_FAILURE_MESSAGE = (
    "Odoo Workbench execution result projection failed; Hub execution remains authoritative."
)


class WorkbenchVendorBillExecutionStatus(StrEnum):
    DRY_RUN_COMPLETED = "dry_run_completed"
    EXECUTED = "executed"
    ALREADY_EXECUTED = "already_executed"
    NOT_EXECUTABLE = "not_executable"
    NOT_FOUND = "not_found"
    APPROVAL_REQUIRED = "approval_required"
    EXECUTION_DISABLED = "execution_disabled"
    MISSING_SOURCE_EVIDENCE = "missing_source_evidence"
    EXECUTION_FAILED = "execution_failed"


class WorkbenchExecutionResultPublisher(Protocol):
    def project_vendor_bill_execution_result(
        self,
        result: WorkbenchVendorBillExecutionResult,
        *,
        trace_id: str | None = None,
    ) -> ProjectionPublishResult:
        pass


@dataclass(frozen=True, slots=True)
class WorkbenchVendorBillExecutionResult(ApplicationDTO):
    review_id: str
    company_id: int
    decision_version: int
    mode: ExecutionMode
    status: WorkbenchVendorBillExecutionStatus
    execution_id: str | None = None
    runtime_state: ExecutionState | None = None
    artifacts: tuple[ExecutionArtifact, ...] = field(default_factory=tuple)
    message: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.decision_version, "decision_version must be positive.")
        if not isinstance(self.mode, ExecutionMode):
            raise ExecutionPlanningError("mode must be a canonical ExecutionMode.")
        if not isinstance(self.status, WorkbenchVendorBillExecutionStatus):
            raise ExecutionPlanningError("status must be a Workbench Vendor Bill execution status.")
        if self.execution_id is not None:
            _require_text(self.execution_id, "execution_id must be non-empty when supplied.")
        if self.runtime_state is not None and not isinstance(self.runtime_state, ExecutionState):
            raise ExecutionPlanningError("runtime_state must be a canonical ExecutionState when supplied.")
        for artifact in self.artifacts:
            if not isinstance(artifact, ExecutionArtifact):
                raise ExecutionPlanningError("artifacts must contain canonical ExecutionArtifact values.")
        if self.message is not None:
            _require_text(self.message, "message must be non-empty when supplied.")


class WorkbenchVendorBillExecutionWorkflow:
    """Run only persisted Vendor Bill decisions through the existing execution runtime."""

    def __init__(
        self,
        *,
        accepted_decision_reader: AcceptedReviewDecisionReader,
        source_invoice_reader: ExecutionSourceInvoiceReader,
        execution_use_case: RunAcceptedDecisionExecutionUseCase,
        runtime_repository: ExecutionRuntimeRepository,
        execution_result_publisher: WorkbenchExecutionResultPublisher | None = None,
    ) -> None:
        self._accepted_decision_reader = accepted_decision_reader
        self._source_invoice_reader = source_invoice_reader
        self._execution_use_case = execution_use_case
        self._runtime_repository = runtime_repository
        self._execution_result_publisher = execution_result_publisher

    def execute(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
        mode: ExecutionMode = ExecutionMode.DRY_RUN,
        approval: ExecutionApproval | None = None,
        trace_id: str | None = None,
    ) -> WorkbenchVendorBillExecutionResult:
        command = RunAcceptedDecisionExecutionCommand(
            review_id=review_id,
            company_id=company_id,
            decision_version=decision_version,
            mode=mode,
            approval=approval,
        )
        try:
            decision = self._accepted_decision_reader.get_accepted_decision(
                review_id=review_id,
                company_id=company_id,
                decision_version=decision_version,
            )
        except ReviewNotFoundError:
            return _result(command, status=WorkbenchVendorBillExecutionStatus.NOT_FOUND)

        eligibility_error = _vendor_bill_eligibility_error(decision)
        if eligibility_error is not None:
            return _result(command, status=WorkbenchVendorBillExecutionStatus.NOT_EXECUTABLE, message=eligibility_error)
        if mode is ExecutionMode.EXECUTE and approval is None:
            return _result(
                command,
                status=WorkbenchVendorBillExecutionStatus.APPROVAL_REQUIRED,
                message="Execution approval is required for execute mode.",
            )

        try:
            self._source_invoice_reader.get_source_invoice(
                review_id=review_id,
                company_id=company_id,
                decision_version=decision_version,
            )
        except ExecutionSourceInvoiceNotFoundError as exc:
            return _result(
                command,
                status=WorkbenchVendorBillExecutionStatus.MISSING_SOURCE_EVIDENCE,
                message=exc.safe_message,
            )
        except ExecutionSourceInvoiceIntegrityError as exc:
            return _result(
                command,
                status=WorkbenchVendorBillExecutionStatus.EXECUTION_FAILED,
                message=exc.safe_message,
            )

        existing_snapshot = self._runtime_repository.get_snapshot(
            execution_id=accepted_decision_execution_id(command, decision=decision)
        )
        if (
            mode is ExecutionMode.EXECUTE
            and existing_snapshot is not None
            and existing_snapshot.state is ExecutionState.COMPLETED
        ):
            replayed = _result(
                command,
                status=WorkbenchVendorBillExecutionStatus.ALREADY_EXECUTED,
                execution_id=existing_snapshot.execution_id,
                runtime_state=existing_snapshot.state,
                artifacts=_artifacts(existing_snapshot),
                message="Execution already completed for this accepted Vendor Bill decision.",
            )
            return self._project_successful_execution(replayed, trace_id=trace_id)

        try:
            execution = self._execution_use_case.execute(command)
        except ExecutionModeNotEnabledError as exc:
            return _result(
                command,
                status=WorkbenchVendorBillExecutionStatus.EXECUTION_DISABLED,
                message=exc.safe_message,
            )
        except ExecutionApprovalError as exc:
            return _result(
                command,
                status=WorkbenchVendorBillExecutionStatus.APPROVAL_REQUIRED,
                message=exc.safe_message,
            )
        except ExecutionUnsupportedStepError as exc:
            return _result(command, status=WorkbenchVendorBillExecutionStatus.NOT_EXECUTABLE, message=exc.safe_message)
        except ExecutionPlanningError as exc:
            return _result(command, status=WorkbenchVendorBillExecutionStatus.NOT_EXECUTABLE, message=exc.safe_message)
        except ExecutionError as exc:
            return _result(
                command,
                status=WorkbenchVendorBillExecutionStatus.EXECUTION_FAILED,
                message=exc.safe_message,
            )

        snapshot = (
            self._runtime_repository.get_snapshot(execution_id=execution.execution_id)
            if execution.execution_id is not None
            else None
        )
        result = _result(
            command,
            status=_status_from_execution(execution.status),
            execution_id=execution.execution_id,
            runtime_state=execution.runtime_state,
            artifacts=_artifacts(snapshot),
        )
        return self._project_successful_execution(result, trace_id=trace_id)

    def _project_successful_execution(
        self,
        result: WorkbenchVendorBillExecutionResult,
        *,
        trace_id: str | None,
    ) -> WorkbenchVendorBillExecutionResult:
        if self._execution_result_publisher is None:
            return result
        if result.mode is not ExecutionMode.EXECUTE:
            return result
        if result.status not in {
            WorkbenchVendorBillExecutionStatus.EXECUTED,
            WorkbenchVendorBillExecutionStatus.ALREADY_EXECUTED,
        }:
            return result
        try:
            self._execution_result_publisher.project_vendor_bill_execution_result(result, trace_id=trace_id)
        except (WorkbenchCandidateReadError, WorkbenchCandidateAmbiguityError, WorkbenchProjectionPublishError):
            return replace(result, message=EXECUTION_PROJECTION_FAILURE_MESSAGE)
        return result


def _vendor_bill_eligibility_error(decision: AcceptedReviewDecision) -> str | None:
    if decision.decision_type is not ReviewDecisionType.SELECT_WORKFLOW:
        return "Only selected Vendor Bill workflow decisions are executable."
    if decision.selected_workflow is not WorkflowType.VENDOR_BILL:
        return "Only Vendor Bill workflow decisions are supported by this endpoint."
    if decision.business_context_allocations is not None and decision.business_context_allocations.allocations:
        return "Allocation-driven execution steps are not supported by this Vendor Bill endpoint."
    return None


def _status_from_execution(status: AcceptedDecisionExecutionStatus) -> WorkbenchVendorBillExecutionStatus:
    if status is AcceptedDecisionExecutionStatus.DRY_RUN_COMPLETED:
        return WorkbenchVendorBillExecutionStatus.DRY_RUN_COMPLETED
    if status is AcceptedDecisionExecutionStatus.EXECUTED:
        return WorkbenchVendorBillExecutionStatus.EXECUTED
    if status is AcceptedDecisionExecutionStatus.NOT_EXECUTABLE:
        return WorkbenchVendorBillExecutionStatus.NOT_EXECUTABLE
    if status is AcceptedDecisionExecutionStatus.NOT_FOUND:
        return WorkbenchVendorBillExecutionStatus.NOT_FOUND
    return WorkbenchVendorBillExecutionStatus.EXECUTION_FAILED


def _artifacts(snapshot) -> tuple[ExecutionArtifact, ...]:
    if snapshot is None:
        return ()
    artifacts: list[ExecutionArtifact] = []
    for step in snapshot.steps:
        if step.last_result is not None:
            artifacts.extend(step.last_result.produced_artifacts)
    return tuple(artifacts)


def _result(
    command: RunAcceptedDecisionExecutionCommand,
    *,
    status: WorkbenchVendorBillExecutionStatus,
    execution_id: str | None = None,
    runtime_state: ExecutionState | None = None,
    artifacts: tuple[ExecutionArtifact, ...] = (),
    message: str | None = None,
) -> WorkbenchVendorBillExecutionResult:
    return WorkbenchVendorBillExecutionResult(
        review_id=command.review_id,
        company_id=command.company_id,
        decision_version=command.decision_version,
        mode=command.mode,
        status=status,
        execution_id=execution_id,
        runtime_state=runtime_state,
        artifacts=artifacts,
        message=message,
    )


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise ExecutionPlanningError(message)


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise ExecutionPlanningError(message)
