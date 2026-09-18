from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import NAMESPACE_URL, uuid5

from app.application.commands import Command
from app.application.dto import ApplicationDTO
from app.application.exceptions import ApplicationError
from app.application.execution.contracts import (
    AcceptedReviewDecision,
    ExecutionApproval,
    ExecutionMode,
    ExecutionRequest,
    ExecutionStatus,
    ExecutionStepType,
)
from app.application.execution.exceptions import ExecutionPlanningError
from app.application.execution.planner import ExecutionPlanner
from app.application.execution.ports import (
    AcceptedBillingEvidenceReader,
    AcceptedReviewDecisionReader,
    ExecutionPreflight,
    ExecutionRuntimeRepository,
    RetryPolicyResolver,
)
from app.application.execution.preflight import ExecutionPreflightPolicy
from app.application.execution.runtime import ExecutionState
from app.application.execution.runtime_service import ExecutionRuntimeCoordinator, ExecutionRuntimeService
from app.application.services.unit_of_work import UnitOfWork
from app.application.workbench.allocations import BusinessContextAllocationType
from app.application.workbench.dto import ReviewDecisionType
from app.application.workbench.exceptions import ReviewNotFoundError
from app.application.workbench.one_off_vendor_use_cases import OneOffVendorRetirementTrigger
from app.application.workbench.write_authorization import (
    WriteAuthorizationError,
    WriteAuthorizationOperationType,
    WriteAuthorizationRecord,
    WriteAuthorizationRepository,
    WriteAuthorizationScopeMismatchError,
)
from app.application.workflow import WorkflowType


class AcceptedDecisionExecutionStatus(StrEnum):
    PLANNED = ExecutionStatus.PLANNED.value
    DRY_RUN_COMPLETED = ExecutionStatus.DRY_RUN_COMPLETED.value
    EXECUTED = ExecutionStatus.EXECUTED.value
    FAILED = ExecutionStatus.FAILED.value
    NOT_EXECUTABLE = "not_executable"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class RunAcceptedDecisionExecutionCommand(Command):
    """Run one persisted accepted Workbench decision through the no-write runtime."""

    review_id: str
    company_id: int
    decision_version: int
    mode: ExecutionMode = ExecutionMode.DRY_RUN
    approval: ExecutionApproval | None = None
    authorization_id: str | None = None
    trace_id: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.decision_version, "decision_version must be positive.")
        if not isinstance(self.mode, ExecutionMode):
            raise ExecutionPlanningError("mode must be a canonical ExecutionMode.")
        if self.authorization_id is not None:
            _require_text(self.authorization_id, "authorization_id must be non-empty.")
            if self.mode is not ExecutionMode.EXECUTE:
                raise ExecutionPlanningError("Runtime authorization is valid only in EXECUTE mode.")
        if self.approval is not None and not isinstance(self.approval, ExecutionApproval):
            raise ExecutionPlanningError("approval must be a canonical ExecutionApproval when supplied.")


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
        unit_of_work: UnitOfWork,
        accepted_decision_reader: AcceptedReviewDecisionReader,
        execution_planner: ExecutionPlanner,
        runtime_service: ExecutionRuntimeService,
        runtime_coordinator: ExecutionRuntimeCoordinator,
        runtime_repository: ExecutionRuntimeRepository,
        retry_policy_resolver: RetryPolicyResolver,
        execution_preflight: ExecutionPreflight | None = None,
        accepted_billing_evidence_reader: AcceptedBillingEvidenceReader | None = None,
        one_off_vendor_retirement_trigger: OneOffVendorRetirementTrigger | None = None,
        write_authorization_repository: WriteAuthorizationRepository | None = None,
    ) -> None:
        self._unit_of_work = unit_of_work
        self._accepted_decision_reader = accepted_decision_reader
        self._execution_planner = execution_planner
        self._runtime_service = runtime_service
        self._runtime_coordinator = runtime_coordinator
        self._runtime_repository = runtime_repository
        self._retry_policy_resolver = retry_policy_resolver
        self._execution_preflight = execution_preflight or ExecutionPreflightPolicy()
        self._accepted_billing_evidence_reader = accepted_billing_evidence_reader
        # Optional: the P0-PROD-08I post-Vendor-Bill retirement hook. None -> never
        # attempted; every other execution behaves identically to before this existed.
        self._one_off_vendor_retirement_trigger = one_off_vendor_retirement_trigger
        self._write_authorization_repository = write_authorization_repository

    def execute(self, command: RunAcceptedDecisionExecutionCommand) -> AcceptedDecisionExecutionResult:
        """Own the Hub transaction; returned runtime failures are persisted outcomes.

        Repositories flush only. Commit state, steps, events and artifacts together
        before retirement/projection. Pre-commit exceptions (including commit errors)
        roll back pending Hub changes; remote ERP writes cannot be rolled back here.
        """
        try:
            execution_result = self._execute(command)
        except Exception:
            self._unit_of_work.rollback()
            raise
        # P0-PROD-08I: the execution outcome is now committed, including any Vendor Bill
        # artifact -- only now is it safe to attempt
        # retirement. Never for DRY_RUN (no real Vendor Bill exists to retire against).
        # Best-effort and entirely optional: see OneOffVendorRetirementTrigger for why a
        # failure here can never turn this successful result into a failure.
        if (
            command.mode is ExecutionMode.EXECUTE
            and execution_result.status is AcceptedDecisionExecutionStatus.EXECUTED
            and self._one_off_vendor_retirement_trigger is not None
        ):
            self._one_off_vendor_retirement_trigger.try_retire_after_execution(
                review_id=command.review_id,
                company_id=command.company_id,
            )
        return execution_result

    def _execute(self, command: RunAcceptedDecisionExecutionCommand) -> AcceptedDecisionExecutionResult:
        if not isinstance(command, RunAcceptedDecisionExecutionCommand):
            raise ExecutionPlanningError("RunAcceptedDecisionExecutionCommand is required.")

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

        accepted_billing_instructions = ()
        if _has_customer_invoice_creation_allocations(decision) and self._accepted_billing_evidence_reader is not None:
            try:
                accepted_billing_instructions = self._accepted_billing_evidence_reader.get_billing_instructions(
                    review_id=decision.review_id,
                    company_id=decision.company_id,
                    decision_version=decision.decision_version,
                    decision_id=decision.decision_id,
                )
            except ReviewNotFoundError:
                accepted_billing_instructions = ()

        request = _execution_request(
            command,
            decision=decision,
            accepted_billing_instructions=accepted_billing_instructions,
        )
        plan = self._execution_planner.plan(request)
        claimed_authorization: WriteAuthorizationRecord | None = None
        if command.mode is ExecutionMode.EXECUTE:
            if command.authorization_id is not None:
                if self._write_authorization_repository is None:
                    raise WriteAuthorizationScopeMismatchError(
                        "Runtime authorization is not supported by this workflow."
                    )
                if command.approval is None or command.approval.authorization is not None:
                    raise WriteAuthorizationScopeMismatchError(
                        "Named approval and a persisted authorization ID are required."
                    )
                if (
                    decision.selected_workflow is not WorkflowType.VENDOR_BILL
                    or len(plan.steps) != 1
                    or plan.steps[0].step_type is not ExecutionStepType.VENDOR_BILL
                ):
                    raise WriteAuthorizationScopeMismatchError(
                        "Authorization is limited to one direct Vendor Bill execution."
                    )
                claimed_authorization = self._write_authorization_repository.claim_and_consume(
                    company_id=command.company_id,
                    review_id=command.review_id,
                    operation_type=WriteAuthorizationOperationType.EXECUTE_VENDOR_BILL,
                    target_version=command.decision_version,
                    authorization_id=command.authorization_id,
                    trace_id=command.trace_id,
                    execution_id=request.execution_id,
                )
            elif command.approval is not None and command.approval.authorization is not None:
                raise WriteAuthorizationScopeMismatchError(
                    "Execution authorization must be claimed by the application."
                )

            effective_approval = command.approval
            if claimed_authorization is not None and command.approval is not None:
                effective_approval = ExecutionApproval(
                    approved_by=command.approval.approved_by,
                    authorization=claimed_authorization,
                )

            try:
                self._execution_preflight.ensure_execute_allowed(plan=plan, approval=effective_approval)
            except ApplicationError as exc:
                if claimed_authorization is None or exc.error_category != "production_safety_gate_failure":
                    raise
                raise WriteAuthorizationError(exc.safe_message) from exc
            self._runtime_coordinator.ensure_plan_supports_mode(plan=plan, mode=command.mode)
        else:
            effective_approval = command.approval

        runtime = self._runtime_service.create_or_load(
            plan=plan,
            retry_policy=self._retry_policy_resolver.resolve(plan),
        )
        result = self._runtime_coordinator.execute(runtime.snapshot, approval=effective_approval)
        snapshot = self._runtime_repository.get_snapshot(execution_id=result.execution_id)
        execution_result = AcceptedDecisionExecutionResult(
            review_id=command.review_id,
            company_id=command.company_id,
            decision_version=command.decision_version,
            status=AcceptedDecisionExecutionStatus(result.status.value),
            execution_id=result.execution_id,
            runtime_state=snapshot.state if snapshot is not None else None,
        )
        # Both success and a returned FAILED/waiting_retry outcome are durable.
        # This must precede every best-effort remote post-execution action.
        self._unit_of_work.commit()
        return execution_result


def _execution_request(
    command: RunAcceptedDecisionExecutionCommand,
    *,
    decision: AcceptedReviewDecision,
    accepted_billing_instructions=(),
) -> ExecutionRequest:
    execution_id = accepted_decision_execution_id(command, decision=decision)
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
        accepted_billing_instructions=accepted_billing_instructions,
        selected_quotation_scenario_ids=decision.selected_quotation_scenario_ids,
    )


def accepted_decision_execution_id(
    command: RunAcceptedDecisionExecutionCommand,
    *,
    decision: AcceptedReviewDecision,
) -> str:
    decision_identity = decision.decision_id or "decision-id-absent"
    identity = (
        f"accepted-decision-execution:{command.company_id}:"
        f"{command.review_id}:{command.decision_version}:{decision_identity}:{command.mode.value}"
    )
    return f"accepted-decision-execution:{uuid5(NAMESPACE_URL, identity)}"


def _has_customer_invoice_creation_allocations(decision: AcceptedReviewDecision) -> bool:
    allocations = decision.business_context_allocations
    if allocations is None:
        return False
    return any(
        allocation.allocation_type is BusinessContextAllocationType.CUSTOMER_RECHARGE
        and allocation.customer_invoice_id is None
        for allocation in allocations.allocations
    )


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise ExecutionPlanningError(message)


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise ExecutionPlanningError(message)
