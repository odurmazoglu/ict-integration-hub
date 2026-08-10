from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from app.application.execution.contracts import (
    ExecutionMode,
    ExecutionPlan,
    ExecutionRequest,
    ExecutionStep,
    ExecutionStepType,
)
from app.application.execution.exceptions import ExecutionPlanningError
from app.application.workbench.allocations import BusinessContextAllocationType
from app.application.workflow import WorkflowType

STEP_TYPE_ORDER: tuple[ExecutionStepType, ...] = (
    ExecutionStepType.EXISTING_PURCHASE_ORDER,
    ExecutionStepType.NEW_RFQ_PURCHASE,
    ExecutionStepType.VENDOR_BILL,
    ExecutionStepType.SALES_ORDER_COST_LINK,
    ExecutionStepType.CUSTOMER_RECHARGE,
    ExecutionStepType.PROJECT_COST,
    ExecutionStepType.OPERATING_EXPENSE,
    ExecutionStepType.FIXED_ASSET,
    ExecutionStepType.SUBSCRIPTION_SERVICE,
    ExecutionStepType.INTERNAL_COST,
)

ALLOCATION_STEP_TYPES: dict[BusinessContextAllocationType, ExecutionStepType] = {
    BusinessContextAllocationType.SALES_ORDER_COST: ExecutionStepType.SALES_ORDER_COST_LINK,
    BusinessContextAllocationType.CUSTOMER_RECHARGE: ExecutionStepType.CUSTOMER_RECHARGE,
    BusinessContextAllocationType.EXISTING_PURCHASE_ORDER: ExecutionStepType.EXISTING_PURCHASE_ORDER,
    BusinessContextAllocationType.NEW_RFQ_PURCHASE: ExecutionStepType.NEW_RFQ_PURCHASE,
    BusinessContextAllocationType.PROJECT_COST: ExecutionStepType.PROJECT_COST,
    BusinessContextAllocationType.OPERATING_EXPENSE: ExecutionStepType.OPERATING_EXPENSE,
    BusinessContextAllocationType.FIXED_ASSET: ExecutionStepType.FIXED_ASSET,
    BusinessContextAllocationType.SUBSCRIPTION_SERVICE: ExecutionStepType.SUBSCRIPTION_SERVICE,
    BusinessContextAllocationType.INTERNAL_COST: ExecutionStepType.INTERNAL_COST,
}


class ExecutionPlanner:
    """Build immutable no-write execution plans from accepted Workbench decisions."""

    def plan(self, request: ExecutionRequest) -> ExecutionPlan:
        if not isinstance(request, ExecutionRequest):
            raise ExecutionPlanningError("ExecutionRequest is required.")
        steps = _planned_steps(request)
        plan_without_key = ExecutionPlan(
            execution_id=request.execution_id,
            review_id=request.review_id,
            company_id=request.company_id,
            decision_version=request.decision_version,
            mode=request.mode,
            steps=steps,
            warnings=_warnings(request),
        )
        return ExecutionPlan(
            execution_id=plan_without_key.execution_id,
            review_id=plan_without_key.review_id,
            company_id=plan_without_key.company_id,
            decision_version=plan_without_key.decision_version,
            mode=plan_without_key.mode,
            steps=plan_without_key.steps,
            warnings=plan_without_key.warnings,
            idempotency_key=execution_idempotency_key(plan_without_key),
        )


def execution_idempotency_key(plan: ExecutionPlan) -> str:
    identity = {
        "company_id": plan.company_id,
        "review_id": plan.review_id,
        "decision_version": plan.decision_version,
        "mode": plan.mode.value,
        "steps": [
            {
                "step_type": step.step_type.value,
                "allocation_keys": list(step.allocation_keys),
            }
            for step in plan.steps
        ],
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"execution:{digest}"


def _planned_steps(request: ExecutionRequest) -> tuple[ExecutionStep, ...]:
    grouped: dict[ExecutionStepType, set[str]] = defaultdict(set)
    if request.business_context_allocations is not None:
        for allocation in request.business_context_allocations.allocations:
            try:
                step_type = ALLOCATION_STEP_TYPES[allocation.allocation_type]
            except KeyError as exc:
                raise ExecutionPlanningError("Unsupported allocation execution type.") from exc
            grouped[step_type].add(allocation.allocation_key)

    if request.selected_workflow is WorkflowType.VENDOR_BILL:
        grouped.setdefault(ExecutionStepType.VENDOR_BILL, set())
        if request.business_context_allocations is not None:
            grouped[ExecutionStepType.VENDOR_BILL].update(
                allocation.allocation_key for allocation in request.business_context_allocations.allocations
            )

    steps: list[ExecutionStep] = []
    for step_type in STEP_TYPE_ORDER:
        allocation_keys = tuple(sorted(grouped.get(step_type, ())))
        if step_type in grouped:
            steps.append(
                ExecutionStep(
                    step_key=_step_key(request, step_type=step_type, allocation_keys=allocation_keys),
                    step_type=step_type,
                    allocation_keys=allocation_keys,
                    sequence=len(steps) + 1,
                    dry_run_supported=True,
                    execute_supported=step_type is ExecutionStepType.VENDOR_BILL,
                )
            )
    if not steps:
        raise ExecutionPlanningError("Accepted decision has no executable workflow or allocation steps.")
    return tuple(steps)


def _step_key(request: ExecutionRequest, *, step_type: ExecutionStepType, allocation_keys: tuple[str, ...]) -> str:
    allocation_part = "+".join(allocation_keys) if allocation_keys else "workflow"
    return f"{request.review_id}:{request.decision_version}:{step_type.value}:{allocation_part}"


def _warnings(request: ExecutionRequest) -> tuple[str, ...]:
    if request.mode is ExecutionMode.EXECUTE:
        return ("Execution mode requested. Only execute-capable plan steps may run.",)
    return ()
