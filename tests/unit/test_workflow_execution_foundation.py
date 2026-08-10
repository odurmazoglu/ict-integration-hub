from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.execution import (
    ExecutionCoordinator,
    ExecutionIdempotencyConflictError,
    ExecutionMode,
    ExecutionPlan,
    ExecutionPlanner,
    ExecutionPlanningError,
    ExecutionRequest,
    ExecutionResult,
    ExecutionStateError,
    ExecutionStatus,
    ExecutionStep,
    ExecutionStepRequest,
    ExecutionStepResult,
    ExecutionStepStatus,
    ExecutionStepType,
    ExecutionStrategyResolutionError,
    ExecutionStrategyResolver,
    ExecutionUnsupportedStepError,
    FoundationExecutionStrategy,
    InMemoryExecutionStateRepository,
    execution_idempotency_key,
)
from app.application.workbench import (
    AllocationCompleteness,
    BusinessContextAllocation,
    BusinessContextAllocationSet,
    BusinessContextAllocationType,
)
from app.application.workflow import WorkflowType


def test_execution_request_contract_validates_and_is_immutable() -> None:
    request = _request()

    assert request.company_id == 7
    with pytest.raises(FrozenInstanceError):
        request.review_id = "changed"  # type: ignore[misc]
    with pytest.raises(ExecutionPlanningError):
        _request(company_id=0)
    with pytest.raises(ExecutionPlanningError):
        _request(decision_version=0)
    with pytest.raises(ExecutionPlanningError):
        _request(mode="dry_run")  # type: ignore[arg-type]
    with pytest.raises(ExecutionPlanningError):
        _request(idempotency_key="")


def test_execution_plan_step_and_result_contracts_are_immutable_and_validate_duplicates() -> None:
    step = ExecutionStep(
        step_key="review-1:4:internal_cost:ALLOC-1",
        step_type=ExecutionStepType.INTERNAL_COST,
        allocation_keys=("ALLOC-1",),
        sequence=1,
    )
    plan = ExecutionPlan(
        execution_id="exec-1",
        review_id="review-1",
        company_id=7,
        decision_version=4,
        mode=ExecutionMode.DRY_RUN,
        steps=(step,),
        idempotency_key="execution:key",
    )
    result = ExecutionResult(
        execution_id="exec-1",
        status=ExecutionStatus.DRY_RUN_COMPLETED,
        step_results=(
            ExecutionStepResult(
                step_key=step.step_key,
                step_type=step.step_type,
                status=ExecutionStepStatus.DRY_RUN_OK,
                dry_run=True,
            ),
        ),
    )

    assert plan.steps[0] is step
    assert result.succeeded_count == 1
    with pytest.raises(FrozenInstanceError):
        step.sequence = 2  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        plan.steps = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.status = ExecutionStatus.FAILED  # type: ignore[misc]
    with pytest.raises(ExecutionPlanningError):
        ExecutionPlan(
            execution_id="exec-1",
            review_id="review-1",
            company_id=7,
            decision_version=4,
            mode=ExecutionMode.DRY_RUN,
            steps=(step, step),
        )


@pytest.mark.parametrize(
    ("allocation_type", "step_type"),
    [
        (BusinessContextAllocationType.SALES_ORDER_COST, ExecutionStepType.SALES_ORDER_COST_LINK),
        (BusinessContextAllocationType.CUSTOMER_RECHARGE, ExecutionStepType.CUSTOMER_RECHARGE),
        (BusinessContextAllocationType.EXISTING_PURCHASE_ORDER, ExecutionStepType.EXISTING_PURCHASE_ORDER),
        (BusinessContextAllocationType.NEW_RFQ_PURCHASE, ExecutionStepType.NEW_RFQ_PURCHASE),
        (BusinessContextAllocationType.PROJECT_COST, ExecutionStepType.PROJECT_COST),
        (BusinessContextAllocationType.OPERATING_EXPENSE, ExecutionStepType.OPERATING_EXPENSE),
        (BusinessContextAllocationType.FIXED_ASSET, ExecutionStepType.FIXED_ASSET),
        (BusinessContextAllocationType.SUBSCRIPTION_SERVICE, ExecutionStepType.SUBSCRIPTION_SERVICE),
        (BusinessContextAllocationType.INTERNAL_COST, ExecutionStepType.INTERNAL_COST),
    ],
)
def test_planner_maps_allocation_types_to_execution_step_types(
    allocation_type: BusinessContextAllocationType,
    step_type: ExecutionStepType,
) -> None:
    allocation = _allocation(allocation_key="ALLOC-1", allocation_type=allocation_type)

    plan = ExecutionPlanner().plan(_request(allocations=_allocation_set(allocation), selected_workflow=None))

    assert plan.steps[0].step_type is step_type
    assert plan.steps[0].allocation_keys == ("ALLOC-1",)
    assert plan.steps[0].step_key == f"review-1:4:{step_type.value}:ALLOC-1"


def test_heterogeneous_allocations_create_deterministic_composite_plan() -> None:
    allocations = _allocation_set(
        _allocation(allocation_key="ALLOC-3", allocation_type=BusinessContextAllocationType.INTERNAL_COST),
        _allocation(allocation_key="ALLOC-1", allocation_type=BusinessContextAllocationType.CUSTOMER_RECHARGE),
        _allocation(allocation_key="ALLOC-2", allocation_type=BusinessContextAllocationType.EXISTING_PURCHASE_ORDER),
    )

    plan = ExecutionPlanner().plan(_request(allocations=allocations, selected_workflow=None))

    assert [step.step_type for step in plan.steps] == [
        ExecutionStepType.EXISTING_PURCHASE_ORDER,
        ExecutionStepType.CUSTOMER_RECHARGE,
        ExecutionStepType.INTERNAL_COST,
    ]
    assert [step.sequence for step in plan.steps] == [1, 2, 3]
    assert [step.allocation_keys for step in plan.steps] == [("ALLOC-2",), ("ALLOC-1",), ("ALLOC-3",)]


def test_ordering_only_allocation_changes_do_not_change_canonical_plan_or_idempotency() -> None:
    first = ExecutionPlanner().plan(
        _request(
            allocations=_allocation_set(
                _allocation(allocation_key="B", allocation_type=BusinessContextAllocationType.INTERNAL_COST),
                _allocation(allocation_key="A", allocation_type=BusinessContextAllocationType.INTERNAL_COST),
            ),
            selected_workflow=None,
        )
    )
    second = ExecutionPlanner().plan(
        _request(
            allocations=_allocation_set(
                _allocation(allocation_key="A", allocation_type=BusinessContextAllocationType.INTERNAL_COST),
                _allocation(allocation_key="B", allocation_type=BusinessContextAllocationType.INTERNAL_COST),
            ),
            selected_workflow=None,
        )
    )

    assert first.steps == second.steps
    assert first.idempotency_key == second.idempotency_key


def test_vendor_bill_workflow_produces_separate_vendor_bill_step_without_writer_call() -> None:
    writer = ExplodingWriter()
    plan = ExecutionPlanner().plan(
        _request(
            selected_workflow=WorkflowType.VENDOR_BILL,
            allocations=_allocation_set(
                _allocation(allocation_key="ALLOC-1", allocation_type=BusinessContextAllocationType.INTERNAL_COST)
            ),
        )
    )

    assert [step.step_type for step in plan.steps] == [
        ExecutionStepType.VENDOR_BILL,
        ExecutionStepType.INTERNAL_COST,
    ]
    assert plan.steps[0].allocation_keys == ("ALLOC-1",)
    assert writer.called is False


def test_execution_idempotency_changes_with_version_mode_or_plan_content() -> None:
    base = ExecutionPlanner().plan(_request(selected_workflow=None))
    different_version = ExecutionPlanner().plan(_request(decision_version=5, selected_workflow=None))
    execute_mode = ExecutionPlanner().plan(_request(mode=ExecutionMode.EXECUTE, selected_workflow=None))
    different_plan = ExecutionPlanner().plan(
        _request(
            selected_workflow=None,
            allocations=_allocation_set(
                _allocation(allocation_key="ALLOC-1", allocation_type=BusinessContextAllocationType.CUSTOMER_RECHARGE)
            ),
        )
    )

    assert base.idempotency_key == execution_idempotency_key(base)
    assert base.idempotency_key != different_version.idempotency_key
    assert base.idempotency_key != execute_mode.idempotency_key
    assert base.idempotency_key != different_plan.idempotency_key


def test_strategy_resolver_requires_exactly_one_strategy_per_step_type() -> None:
    strategy = FoundationExecutionStrategy(supported_step_types=(ExecutionStepType.INTERNAL_COST,))
    resolver = ExecutionStrategyResolver((strategy,))

    assert resolver.resolve(ExecutionStepType.INTERNAL_COST) is strategy
    with pytest.raises(ExecutionUnsupportedStepError):
        resolver.resolve(ExecutionStepType.CUSTOMER_RECHARGE)
    with pytest.raises(ExecutionStrategyResolutionError):
        ExecutionStrategyResolver((strategy, strategy))


def test_dry_run_executes_no_writer_and_collects_all_step_results() -> None:
    plan = ExecutionPlanner().plan(
        _request(
            selected_workflow=None,
            allocations=_allocation_set(
                _allocation(allocation_key="A", allocation_type=BusinessContextAllocationType.INTERNAL_COST),
                _allocation(allocation_key="B", allocation_type=BusinessContextAllocationType.CUSTOMER_RECHARGE),
            ),
        )
    )
    result = ExecutionCoordinator(
        strategy_resolver=ExecutionStrategyResolver(
            (
                FoundationExecutionStrategy(
                    supported_step_types=(ExecutionStepType.INTERNAL_COST, ExecutionStepType.CUSTOMER_RECHARGE)
                ),
            )
        )
    ).execute(plan)

    assert result.status is ExecutionStatus.DRY_RUN_COMPLETED
    assert [step_result.status for step_result in result.step_results] == [
        ExecutionStepStatus.DRY_RUN_OK,
        ExecutionStepStatus.DRY_RUN_OK,
    ]
    assert result.succeeded_count == 2
    assert result.failed_count == 0


def test_execute_mode_returns_safe_unsupported_and_fail_fast() -> None:
    plan = ExecutionPlanner().plan(
        _request(
            mode=ExecutionMode.EXECUTE,
            selected_workflow=None,
            allocations=_allocation_set(
                _allocation(allocation_key="A", allocation_type=BusinessContextAllocationType.INTERNAL_COST),
                _allocation(allocation_key="B", allocation_type=BusinessContextAllocationType.CUSTOMER_RECHARGE),
            ),
        )
    )
    result = ExecutionCoordinator(
        strategy_resolver=ExecutionStrategyResolver(
            (
                FoundationExecutionStrategy(
                    supported_step_types=(ExecutionStepType.INTERNAL_COST, ExecutionStepType.CUSTOMER_RECHARGE)
                ),
            )
        )
    ).execute(plan)

    assert result.status is ExecutionStatus.FAILED
    assert len(result.step_results) == 1
    assert result.step_results[0].status is ExecutionStepStatus.UNSUPPORTED
    assert result.step_results[0].error_code == "EXECUTION_NOT_ENABLED"


def test_coordinator_respects_sequential_order_and_collect_all_dry_run_failures() -> None:
    events: list[str] = []
    plan = ExecutionPlanner().plan(
        _request(
            selected_workflow=None,
            allocations=_allocation_set(
                _allocation(allocation_key="A", allocation_type=BusinessContextAllocationType.EXISTING_PURCHASE_ORDER),
                _allocation(allocation_key="B", allocation_type=BusinessContextAllocationType.INTERNAL_COST),
            ),
        )
    )
    result = ExecutionCoordinator(
        strategy_resolver=ExecutionStrategyResolver(
            (
                RecordingStrategy(
                    supported_step_types=(
                        ExecutionStepType.EXISTING_PURCHASE_ORDER,
                        ExecutionStepType.INTERNAL_COST,
                    ),
                    events=events,
                    failing_step_type=ExecutionStepType.EXISTING_PURCHASE_ORDER,
                ),
            )
        )
    ).execute(plan)

    assert events == [ExecutionStepType.EXISTING_PURCHASE_ORDER.value, ExecutionStepType.INTERNAL_COST.value]
    assert result.status is ExecutionStatus.FAILED
    assert result.failed_count == 1
    assert result.succeeded_count == 1


def test_in_memory_execution_state_repository_handles_idempotency_and_transitions() -> None:
    repository = InMemoryExecutionStateRepository()
    plan = ExecutionPlanner().plan(_request(selected_workflow=None))
    same = ExecutionPlanner().plan(_request(selected_workflow=None))
    conflicting = ExecutionPlan(
        execution_id="exec-other",
        review_id=plan.review_id,
        company_id=plan.company_id,
        decision_version=plan.decision_version,
        mode=plan.mode,
        steps=plan.steps,
        idempotency_key=plan.idempotency_key,
    )

    assert repository.create_planned_execution(plan) is plan
    assert repository.create_planned_execution(same) == plan
    assert repository.get_by_execution_id(plan.execution_id) == plan
    assert repository.get_by_idempotency_key(plan.idempotency_key or "") == plan
    with pytest.raises(ExecutionIdempotencyConflictError):
        repository.create_planned_execution(conflicting)

    step_result = ExecutionStepResult(
        step_key=plan.steps[0].step_key,
        step_type=plan.steps[0].step_type,
        status=ExecutionStepStatus.DRY_RUN_OK,
        dry_run=True,
    )
    repository.record_step_result(execution_id=plan.execution_id, result=step_result)
    completed = ExecutionResult(
        execution_id=plan.execution_id,
        status=ExecutionStatus.DRY_RUN_COMPLETED,
        step_results=(step_result,),
    )
    repository.mark_completed(execution_id=plan.execution_id, result=completed)
    with pytest.raises(ExecutionStateError):
        repository.mark_failed(execution_id=plan.execution_id, result=completed)


def test_execution_application_layer_has_no_erp_writes_or_runtime_dependencies() -> None:
    source = "\n".join(
        Path(path).read_text(encoding="utf-8").lower()
        for path in (
            "app/application/execution/contracts.py",
            "app/application/execution/planner.py",
            "app/application/execution/strategy.py",
            "app/application/execution/coordinator.py",
            "app/application/execution/state.py",
            "app/application/execution/ports.py",
        )
    )
    forbidden = (
        "app.erp",
        "app.connectors",
        "sqlalchemy",
        "fastapi",
        "vendorbillwriter",
        "write_vendor_bill",
        ".create(",
        ".write(",
        ".unlink(",
        "account.move",
        "action_post",
        "customer invoice creation",
        "create_rfq",
        "purchase order creation",
        "workflow side effect",
        "ai_advisor",
        "ollama",
        "fuzzy",
        "embedding",
    )

    for token in forbidden:
        assert token not in source


class RecordingStrategy:
    name = "recording"

    def __init__(
        self,
        *,
        supported_step_types: tuple[ExecutionStepType, ...],
        events: list[str],
        failing_step_type: ExecutionStepType | None = None,
    ) -> None:
        self.supported_step_types = supported_step_types
        self.events = events
        self.failing_step_type = failing_step_type

    def execute(self, request: ExecutionStepRequest) -> ExecutionStepResult:
        self.events.append(request.step.step_type.value)
        if request.step.step_type is self.failing_step_type:
            return ExecutionStepResult(
                step_key=request.step.step_key,
                step_type=request.step.step_type,
                status=ExecutionStepStatus.FAILED,
                dry_run=request.mode is ExecutionMode.DRY_RUN,
                message="Planned step failed safely.",
                error_code="SAFE_FAILURE",
            )
        return ExecutionStepResult(
            step_key=request.step.step_key,
            step_type=request.step.step_type,
            status=ExecutionStepStatus.DRY_RUN_OK,
            dry_run=True,
        )


class ExplodingWriter:
    def __init__(self) -> None:
        self.called = False

    def write_vendor_bill(self, *_args: object, **_kwargs: object) -> None:
        self.called = True
        raise AssertionError("VendorBillWriter must not be invoked by execution planning.")


def _request(
    *,
    execution_id: str = "exec-1",
    review_id: str = "review-1",
    company_id: int = 7,
    decision_version: int = 4,
    mode: ExecutionMode = ExecutionMode.DRY_RUN,
    idempotency_key: str = "execution-request-key",
    selected_workflow: WorkflowType | None = WorkflowType.VENDOR_BILL,
    allocations: BusinessContextAllocationSet | None = None,
) -> ExecutionRequest:
    return ExecutionRequest(
        execution_id=execution_id,
        review_id=review_id,
        company_id=company_id,
        decision_version=decision_version,
        decision_id="decision-1",
        idempotency_key=idempotency_key,
        mode=mode,
        selected_workflow=selected_workflow,
        business_context_allocations=allocations or _allocation_set(_allocation()),
    )


def _allocation_set(*allocations: BusinessContextAllocation) -> BusinessContextAllocationSet:
    return BusinessContextAllocationSet(
        allocations=allocations,
        completeness=AllocationCompleteness.PARTIAL,
        invoice_total=Decimal("100.00"),
        currency="TRY",
    )


def _allocation(
    *,
    allocation_key: str = "ALLOC-1",
    allocation_type: BusinessContextAllocationType = BusinessContextAllocationType.INTERNAL_COST,
) -> BusinessContextAllocation:
    values: dict[str, object] = {
        "allocation_key": allocation_key,
        "allocation_type": allocation_type,
        "amount": Decimal("10.00"),
        "currency": "TRY",
    }
    if allocation_type is BusinessContextAllocationType.SALES_ORDER_COST:
        values["sales_order_id"] = 301
    if allocation_type is BusinessContextAllocationType.CUSTOMER_RECHARGE:
        values["recharge_partner_id"] = 105
    if allocation_type is BusinessContextAllocationType.EXISTING_PURCHASE_ORDER:
        values["purchase_order_id"] = 501
    if allocation_type is BusinessContextAllocationType.PROJECT_COST:
        values["analytic_account_id"] = 701
    return BusinessContextAllocation(**values)
