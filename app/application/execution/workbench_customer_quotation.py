from __future__ import annotations

from typing import Protocol

from app.application.execution.accepted_decision_use_cases import (
    RunAcceptedDecisionExecutionCommand,
    RunAcceptedDecisionExecutionUseCase,
    accepted_decision_execution_id,
)
from app.application.execution.contracts import AcceptedReviewDecision, ExecutionApproval, ExecutionMode
from app.application.execution.exceptions import (
    ExecutionApprovalError,
    ExecutionError,
    ExecutionModeNotEnabledError,
    ExecutionPlanningError,
    ExecutionUnsupportedStepError,
)
from app.application.execution.ports import AcceptedReviewDecisionReader, ExecutionRuntimeRepository
from app.application.execution.runtime import ExecutionState
from app.application.execution.workbench_vendor_bill import (
    WorkbenchVendorBillExecutionResult,
    WorkbenchVendorBillExecutionStatus,
    _artifacts,
    _result,
    _status_from_execution,
)
from app.application.quotation.evidence import QuotationScenarioEvidenceRepository
from app.application.quotation.exceptions import QuotationEvidenceError, QuotationEvidenceNotFoundError
from app.application.workbench.dto import ReviewDecisionType
from app.application.workbench.exceptions import ReviewNotFoundError
from app.application.workflow import WorkflowType


class WorkbenchAcceptedDecisionExecutionWorkflow(Protocol):
    """Common shape shared by the accepted-decision execution sub-workflows."""

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
        pass


class WorkbenchCustomerQuotationExecutionWorkflow:
    """Run an accepted CUSTOMER_QUOTATION decision through the shared execution runtime.

    Mirrors :class:`WorkbenchVendorBillExecutionWorkflow`: it reads the durable
    accepted decision, then runs the generic
    :class:`RunAcceptedDecisionExecutionUseCase`. Before any runtime mutation it
    fails closed unless immutable quotation evidence exists for *every* selected
    scenario — a partial evidence set never executes the available subset. It
    never reads Odoo Proposal Scenario records and never auto-captures.
    """

    def __init__(
        self,
        *,
        accepted_decision_reader: AcceptedReviewDecisionReader,
        quotation_evidence_reader: QuotationScenarioEvidenceRepository,
        execution_use_case: RunAcceptedDecisionExecutionUseCase,
        runtime_repository: ExecutionRuntimeRepository,
    ) -> None:
        self._accepted_decision_reader = accepted_decision_reader
        self._quotation_evidence_reader = quotation_evidence_reader
        self._execution_use_case = execution_use_case
        self._runtime_repository = runtime_repository

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

        eligibility_error = _quotation_eligibility_error(decision)
        if eligibility_error is not None:
            return _result(
                command,
                status=WorkbenchVendorBillExecutionStatus.NOT_EXECUTABLE,
                message=eligibility_error,
            )
        if mode is ExecutionMode.EXECUTE and approval is None:
            return _result(
                command,
                status=WorkbenchVendorBillExecutionStatus.APPROVAL_REQUIRED,
                message="Execution approval is required for execute mode.",
            )

        missing = _missing_evidence_scenarios(self._quotation_evidence_reader, decision)
        if missing:
            return _result(
                command,
                status=WorkbenchVendorBillExecutionStatus.MISSING_QUOTATION_EVIDENCE,
                message=(
                    f"Immutable quotation scenario evidence is missing for {len(missing)} selected scenario(s). "
                    "Capture evidence via the quotation-scenarios endpoint before executing."
                ),
            )

        existing_snapshot = self._runtime_repository.get_snapshot(
            execution_id=accepted_decision_execution_id(command, decision=decision)
        )
        if (
            mode is ExecutionMode.EXECUTE
            and existing_snapshot is not None
            and existing_snapshot.state is ExecutionState.COMPLETED
        ):
            return _result(
                command,
                status=WorkbenchVendorBillExecutionStatus.ALREADY_EXECUTED,
                execution_id=existing_snapshot.execution_id,
                runtime_state=existing_snapshot.state,
                artifacts=_artifacts(existing_snapshot),
                message="Execution already completed for this accepted customer quotation decision.",
            )

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
        except (ExecutionUnsupportedStepError, ExecutionPlanningError) as exc:
            return _result(
                command,
                status=WorkbenchVendorBillExecutionStatus.NOT_EXECUTABLE,
                message=exc.safe_message,
            )
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
        return _result(
            command,
            status=_status_from_execution(execution.status),
            execution_id=execution.execution_id,
            runtime_state=execution.runtime_state,
            artifacts=_artifacts(snapshot),
        )


class WorkbenchAcceptedDecisionExecutionDispatcher:
    """Route ``POST /reviews/{review_id}/execute`` to the sub-workflow for the accepted decision.

    ``VENDOR_BILL`` decisions delegate to the unchanged
    :class:`WorkbenchVendorBillExecutionWorkflow`; ``CUSTOMER_QUOTATION`` decisions
    delegate to :class:`WorkbenchCustomerQuotationExecutionWorkflow`. Both share
    the same runtime framework and the same result contract.
    """

    def __init__(
        self,
        *,
        accepted_decision_reader: AcceptedReviewDecisionReader,
        vendor_bill_workflow: WorkbenchAcceptedDecisionExecutionWorkflow,
        customer_quotation_workflow: WorkbenchAcceptedDecisionExecutionWorkflow,
    ) -> None:
        self._accepted_decision_reader = accepted_decision_reader
        self._vendor_bill_workflow = vendor_bill_workflow
        self._customer_quotation_workflow = customer_quotation_workflow

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

        workflow = (
            self._customer_quotation_workflow
            if decision.selected_workflow is WorkflowType.CUSTOMER_QUOTATION
            else self._vendor_bill_workflow
        )
        return workflow.execute(
            review_id=review_id,
            company_id=company_id,
            decision_version=decision_version,
            mode=mode,
            approval=approval,
            trace_id=trace_id,
        )


def _quotation_eligibility_error(decision: AcceptedReviewDecision) -> str | None:
    if decision.decision_type is not ReviewDecisionType.SELECT_WORKFLOW:
        return "Only selected customer quotation workflow decisions are executable."
    if decision.selected_workflow is not WorkflowType.CUSTOMER_QUOTATION:
        return "Only customer quotation workflow decisions are supported by this execution path."
    if not decision.selected_quotation_scenario_ids:
        return "Accepted customer quotation decision has no selected scenario ids."
    if decision.decision_id is None:
        return "Accepted customer quotation decision is missing its durable decision_id."
    return None


def _missing_evidence_scenarios(
    reader: QuotationScenarioEvidenceRepository,
    decision: AcceptedReviewDecision,
) -> tuple[str, ...]:
    missing: list[str] = []
    for scenario_id in decision.selected_quotation_scenario_ids:
        try:
            reader.get(
                company_id=decision.company_id,
                review_id=decision.review_id,
                decision_id=decision.decision_id or "",
                decision_version=decision.decision_version,
                scenario_id=scenario_id,
            )
        except QuotationEvidenceNotFoundError:
            missing.append(scenario_id)
        except QuotationEvidenceError:
            missing.append(scenario_id)
    return tuple(missing)
