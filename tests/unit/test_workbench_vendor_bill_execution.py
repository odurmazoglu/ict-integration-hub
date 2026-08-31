from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.execution import (
    AcceptedDecisionExecutionResult,
    AcceptedDecisionExecutionStatus,
    AcceptedReviewDecision,
    ExecutionApproval,
    ExecutionArtifact,
    ExecutionArtifactType,
    ExecutionMode,
    ExecutionModeNotEnabledError,
    ExecutionSourceInvoiceNotFoundError,
    ExecutionState,
    ExecutionStepResult,
    ExecutionStepStatus,
    ExecutionStepType,
    RunAcceptedDecisionExecutionCommand,
    WorkbenchVendorBillExecutionStatus,
    WorkbenchVendorBillExecutionWorkflow,
    accepted_decision_execution_id,
)
from app.application.workbench import (
    AllocationCompleteness,
    BusinessContextAllocation,
    BusinessContextAllocationSet,
    BusinessContextAllocationType,
    ReviewDecisionType,
)
from app.application.workbench.exceptions import ReviewNotFoundError
from app.application.workflow import WorkflowType


def test_dry_run_uses_persisted_company_scoped_decision_and_source_evidence() -> None:
    decision_reader = FakeDecisionReader(_decision())
    source_reader = FakeSourceReader()
    execution = FakeExecutionUseCase(
        AcceptedDecisionExecutionResult(
            review_id="review-1",
            company_id=7,
            decision_version=3,
            status=AcceptedDecisionExecutionStatus.DRY_RUN_COMPLETED,
            execution_id="execution-1",
            runtime_state=ExecutionState.COMPLETED,
        )
    )

    result = _workflow(decision_reader, source_reader, execution).execute(
        review_id="review-1",
        company_id=7,
        decision_version=3,
        mode=ExecutionMode.DRY_RUN,
    )

    assert result.status is WorkbenchVendorBillExecutionStatus.DRY_RUN_COMPLETED
    assert result.execution_id == "execution-1"
    assert decision_reader.calls == [{"review_id": "review-1", "company_id": 7, "decision_version": 3}]
    assert source_reader.calls == [{"review_id": "review-1", "company_id": 7, "decision_version": 3}]
    assert execution.commands == [
        RunAcceptedDecisionExecutionCommand(
            review_id="review-1",
            company_id=7,
            decision_version=3,
            mode=ExecutionMode.DRY_RUN,
        )
    ]


def test_execute_without_approval_is_rejected_before_source_or_runtime() -> None:
    decision_reader = FakeDecisionReader(_decision())
    source_reader = FakeSourceReader()
    execution = FakeExecutionUseCase(AssertionError("runtime must not be called"))

    result = _workflow(decision_reader, source_reader, execution).execute(
        review_id="review-1",
        company_id=7,
        decision_version=3,
        mode=ExecutionMode.EXECUTE,
    )

    assert result.status is WorkbenchVendorBillExecutionStatus.APPROVAL_REQUIRED
    assert source_reader.calls == []
    assert execution.commands == []


def test_execute_disabled_is_reported_without_swallowing_gate_message() -> None:
    result = _workflow(
        FakeDecisionReader(_decision()),
        FakeSourceReader(),
        FakeExecutionUseCase(ExecutionModeNotEnabledError("Execute mode is not enabled.")),
    ).execute(
        review_id="review-1",
        company_id=7,
        decision_version=3,
        mode=ExecutionMode.EXECUTE,
        approval=ExecutionApproval(approved_by="controller"),
    )

    assert result.status is WorkbenchVendorBillExecutionStatus.EXECUTION_DISABLED
    assert result.message == "Execute mode is not enabled."


def test_completed_execute_replay_returns_existing_execution_without_second_runtime_call() -> None:
    decision = _decision()
    command = RunAcceptedDecisionExecutionCommand(
        review_id="review-1",
        company_id=7,
        decision_version=3,
        mode=ExecutionMode.EXECUTE,
        approval=ExecutionApproval(approved_by="controller"),
    )
    existing_snapshot = Snapshot(
        execution_id=accepted_decision_execution_id(command, decision=decision),
        state=ExecutionState.COMPLETED,
        steps=(
            Step(
                last_result=ExecutionStepResult(
                    step_key="vendor-bill",
                    step_type=ExecutionStepType.VENDOR_BILL,
                    status=ExecutionStepStatus.EXECUTED,
                    dry_run=False,
                    produced_artifacts=(
                        ExecutionArtifact(
                            artifact_type=ExecutionArtifactType.VENDOR_BILL,
                            artifact_id="9001",
                            external_identity="vendor-bill-write:key",
                            created=True,
                        ),
                    ),
                )
            ),
        ),
    )
    runtime_repository = FakeRuntimeRepository(snapshot=existing_snapshot)
    execution = FakeExecutionUseCase(AssertionError("runtime must not be called"))

    result = _workflow(
        FakeDecisionReader(decision),
        FakeSourceReader(),
        execution,
        runtime_repository=runtime_repository,
    ).execute(
        review_id="review-1",
        company_id=7,
        decision_version=3,
        mode=ExecutionMode.EXECUTE,
        approval=ExecutionApproval(approved_by="controller"),
    )

    assert result.status is WorkbenchVendorBillExecutionStatus.ALREADY_EXECUTED
    assert result.execution_id == existing_snapshot.execution_id
    assert result.artifacts[0].artifact_id == "9001"
    assert execution.commands == []
    assert runtime_repository.snapshot_reads == [existing_snapshot.execution_id]


def test_missing_review_or_accepted_decision_fails_closed() -> None:
    result = _workflow(
        FakeDecisionReader(ReviewNotFoundError("Review item was not found.")),
        FakeSourceReader(),
        FakeExecutionUseCase(AssertionError("runtime must not be called")),
    ).execute(review_id="missing", company_id=7, decision_version=3)

    assert result.status is WorkbenchVendorBillExecutionStatus.NOT_FOUND


def test_missing_pinned_source_evidence_fails_closed_before_runtime() -> None:
    source_reader = FakeSourceReader(
        ExecutionSourceInvoiceNotFoundError("Execution source invoice evidence was not found.")
    )
    execution = FakeExecutionUseCase(AssertionError("runtime must not be called"))

    result = _workflow(FakeDecisionReader(_decision()), source_reader, execution).execute(
        review_id="review-1",
        company_id=7,
        decision_version=3,
    )

    assert result.status is WorkbenchVendorBillExecutionStatus.MISSING_SOURCE_EVIDENCE
    assert execution.commands == []


@pytest.mark.parametrize(
    "workflow",
    [
        WorkflowType.RFQ,
        WorkflowType.EXPENSE,
        WorkflowType.ASSET,
        WorkflowType.SUBSCRIPTION,
        WorkflowType.MANUAL_REVIEW,
    ],
)
def test_unsupported_persisted_workflows_are_rejected_before_runtime(workflow: WorkflowType) -> None:
    execution = FakeExecutionUseCase(AssertionError("runtime must not be called"))

    result = _workflow(
        FakeDecisionReader(_decision(selected_workflow=workflow)),
        FakeSourceReader(),
        execution,
    ).execute(review_id="review-1", company_id=7, decision_version=3)

    assert result.status is WorkbenchVendorBillExecutionStatus.NOT_EXECUTABLE
    assert execution.commands == []


@pytest.mark.parametrize(
    "allocation_type",
    [
        BusinessContextAllocationType.SALES_ORDER_COST,
        BusinessContextAllocationType.CUSTOMER_RECHARGE,
        BusinessContextAllocationType.EXISTING_PURCHASE_ORDER,
        BusinessContextAllocationType.NEW_RFQ_PURCHASE,
        BusinessContextAllocationType.PROJECT_COST,
        BusinessContextAllocationType.OPERATING_EXPENSE,
        BusinessContextAllocationType.FIXED_ASSET,
        BusinessContextAllocationType.SUBSCRIPTION_SERVICE,
        BusinessContextAllocationType.INTERNAL_COST,
    ],
)
def test_allocation_driven_semantics_are_rejected_before_runtime(
    allocation_type: BusinessContextAllocationType,
) -> None:
    execution = FakeExecutionUseCase(AssertionError("runtime must not be called"))

    result = _workflow(
        FakeDecisionReader(_decision(business_context_allocations=_allocations(allocation_type))),
        FakeSourceReader(),
        execution,
    ).execute(review_id="review-1", company_id=7, decision_version=3)

    assert result.status is WorkbenchVendorBillExecutionStatus.NOT_EXECUTABLE
    assert "Allocation-driven" in (result.message or "")
    assert execution.commands == []


def test_dismiss_is_rejected_before_source_or_runtime() -> None:
    source_reader = FakeSourceReader()
    execution = FakeExecutionUseCase(AssertionError("runtime must not be called"))

    result = _workflow(
        FakeDecisionReader(_decision(decision_type=ReviewDecisionType.DISMISS, selected_workflow=None)),
        source_reader,
        execution,
    ).execute(review_id="review-1", company_id=7, decision_version=3)

    assert result.status is WorkbenchVendorBillExecutionStatus.NOT_EXECUTABLE
    assert source_reader.calls == []
    assert execution.commands == []


def test_unexpected_programming_errors_propagate() -> None:
    with pytest.raises(TypeError):
        _workflow(
            FakeDecisionReader(_decision()),
            FakeSourceReader(),
            FakeExecutionUseCase(TypeError("invariant broke")),
        ).execute(review_id="review-1", company_id=7, decision_version=3)


def test_workbench_vendor_bill_execution_uses_existing_strategy_and_no_external_authority() -> None:
    source = Path("app/application/execution/workbench_vendor_bill.py").read_text(encoding="utf-8").lower()
    composition = Path("app/composition/execution.py").read_text(encoding="utf-8")

    assert "runaccepteddecisionexecutionusecase" in source
    assert "SqlAlchemyExecutionSourceInvoiceReader" in composition
    assert "VendorBillExecutionStrategy" in composition
    assert "OdooVendorBillWriter" in composition
    for token in (
        "x_studio",
        "uyumsoft",
        "ruleengine",
        "decisionengine",
        "fuzzy",
        "partner_matcher",
        "product_matcher",
        "scheduler",
        "worker",
    ):
        assert token not in source


class FakeDecisionReader:
    def __init__(self, result: AcceptedReviewDecision | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def get_accepted_decision(
        self,
        *,
        review_id: str,
        company_id: int,
        decision_version: int,
    ) -> AcceptedReviewDecision:
        self.calls.append({"review_id": review_id, "company_id": company_id, "decision_version": decision_version})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


SOURCE_SENTINEL = object()


class FakeSourceReader:
    def __init__(self, result: object | Exception = SOURCE_SENTINEL) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def get_source_invoice(self, *, review_id: str, company_id: int, decision_version: int) -> object:
        self.calls.append({"review_id": review_id, "company_id": company_id, "decision_version": decision_version})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeExecutionUseCase:
    def __init__(self, result: AcceptedDecisionExecutionResult | Exception) -> None:
        self.result = result
        self.commands: list[RunAcceptedDecisionExecutionCommand] = []

    def execute(self, command: RunAcceptedDecisionExecutionCommand) -> AcceptedDecisionExecutionResult:
        self.commands.append(command)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeRuntimeRepository:
    def __init__(self, *, snapshot: Snapshot | None = None) -> None:
        self.snapshot = snapshot
        self.snapshot_reads: list[str] = []

    def get_snapshot(self, *, execution_id: str):
        self.snapshot_reads.append(execution_id)
        if self.snapshot is not None and self.snapshot.execution_id == execution_id:
            return self.snapshot
        return None


@dataclass(frozen=True, slots=True)
class Step:
    last_result: ExecutionStepResult | None = None


@dataclass(frozen=True, slots=True)
class Snapshot:
    execution_id: str
    state: ExecutionState
    steps: tuple[Step, ...]


def _workflow(
    decision_reader: FakeDecisionReader,
    source_reader: FakeSourceReader,
    execution: FakeExecutionUseCase,
    *,
    runtime_repository: FakeRuntimeRepository | None = None,
) -> WorkbenchVendorBillExecutionWorkflow:
    return WorkbenchVendorBillExecutionWorkflow(
        accepted_decision_reader=decision_reader,  # type: ignore[arg-type]
        source_invoice_reader=source_reader,  # type: ignore[arg-type]
        execution_use_case=execution,  # type: ignore[arg-type]
        runtime_repository=runtime_repository or FakeRuntimeRepository(),  # type: ignore[arg-type]
    )


def _decision(
    *,
    selected_workflow: WorkflowType | None = WorkflowType.VENDOR_BILL,
    decision_type: ReviewDecisionType = ReviewDecisionType.SELECT_WORKFLOW,
    business_context_allocations: BusinessContextAllocationSet | None = None,
) -> AcceptedReviewDecision:
    return AcceptedReviewDecision(
        review_id="review-1",
        company_id=7,
        decision_version=3,
        decision_id="decision-1",
        selected_workflow=selected_workflow,
        decision_type=decision_type,
        business_context_allocations=business_context_allocations,
    )


def _allocations(allocation_type: BusinessContextAllocationType) -> BusinessContextAllocationSet:
    return BusinessContextAllocationSet(
        allocations=(
            BusinessContextAllocation(
                allocation_key="alloc-1",
                allocation_type=allocation_type,
                amount=Decimal("10.00"),
                currency="TRY",
                sales_order_id=100 if allocation_type is BusinessContextAllocationType.SALES_ORDER_COST else None,
                recharge_partner_id=101 if allocation_type is BusinessContextAllocationType.CUSTOMER_RECHARGE else None,
                purchase_order_id=102
                if allocation_type is BusinessContextAllocationType.EXISTING_PURCHASE_ORDER
                else None,
                project_id=103 if allocation_type is BusinessContextAllocationType.PROJECT_COST else None,
            ),
        ),
        completeness=AllocationCompleteness.COMPLETE,
        invoice_total=Decimal("10.00"),
        currency="TRY",
    )
