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
from app.application.workbench.allocations import BusinessContextAllocation, BusinessContextAllocationType
from app.application.workflow import WorkflowType
from app.billing.dto import CustomerInvoiceBillingInstruction

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
            decision_id=request.decision_id,
            warnings=_warnings(request),
        )
        return ExecutionPlan(
            execution_id=plan_without_key.execution_id,
            review_id=plan_without_key.review_id,
            company_id=plan_without_key.company_id,
            decision_version=plan_without_key.decision_version,
            mode=plan_without_key.mode,
            steps=plan_without_key.steps,
            decision_id=plan_without_key.decision_id,
            warnings=plan_without_key.warnings,
            idempotency_key=execution_idempotency_key(plan_without_key),
        )


def execution_idempotency_key(plan: ExecutionPlan) -> str:
    identity = {
        "company_id": plan.company_id,
        "review_id": plan.review_id,
        "decision_version": plan.decision_version,
        "decision_id": plan.decision_id,
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
    allocations_by_step: dict[ExecutionStepType, list[BusinessContextAllocation]] = defaultdict(list)
    if request.business_context_allocations is not None:
        for allocation in request.business_context_allocations.allocations:
            try:
                step_type = ALLOCATION_STEP_TYPES[allocation.allocation_type]
            except KeyError as exc:
                raise ExecutionPlanningError("Unsupported allocation execution type.") from exc
            if step_type is ExecutionStepType.CUSTOMER_RECHARGE:
                continue
            grouped[step_type].add(allocation.allocation_key)
            allocations_by_step[step_type].append(allocation)

    if request.selected_workflow is WorkflowType.VENDOR_BILL:
        grouped.setdefault(ExecutionStepType.VENDOR_BILL, set())
        if request.business_context_allocations is not None:
            grouped[ExecutionStepType.VENDOR_BILL].update(
                allocation.allocation_key for allocation in request.business_context_allocations.allocations
            )
            allocations_by_step[ExecutionStepType.VENDOR_BILL].extend(request.business_context_allocations.allocations)

    steps: list[ExecutionStep] = []
    for step_type in STEP_TYPE_ORDER:
        if step_type is ExecutionStepType.CUSTOMER_RECHARGE:
            steps.extend(_customer_recharge_steps(request, first_sequence=len(steps) + 1))
            continue
        allocation_keys = tuple(sorted(grouped.get(step_type, ())))
        if step_type in grouped:
            allocations = tuple(
                sorted(allocations_by_step.get(step_type, ()), key=lambda allocation: allocation.allocation_key)
            )
            steps.append(
                ExecutionStep(
                    step_key=_step_key(request, step_type=step_type, allocation_keys=allocation_keys),
                    step_type=step_type,
                    allocation_keys=allocation_keys,
                    sequence=len(steps) + 1,
                    dry_run_supported=True,
                    execute_supported=_execute_supported(step_type=step_type, allocations=allocations),
                    writer_required=_writer_required(step_type=step_type, allocations=allocations),
                    allocations=allocations,
                )
            )
    if not steps:
        raise ExecutionPlanningError("Accepted decision has no executable workflow or allocation steps.")
    return tuple(steps)


def _step_key(request: ExecutionRequest, *, step_type: ExecutionStepType, allocation_keys: tuple[str, ...]) -> str:
    allocation_part = "+".join(allocation_keys) if allocation_keys else "workflow"
    return f"{request.review_id}:{request.decision_version}:{step_type.value}:{allocation_part}"


def _customer_recharge_steps(request: ExecutionRequest, *, first_sequence: int) -> tuple[ExecutionStep, ...]:
    if request.business_context_allocations is None:
        return ()
    recharge_allocations = tuple(
        sorted(
            (
                allocation
                for allocation in request.business_context_allocations.allocations
                if allocation.allocation_type is BusinessContextAllocationType.CUSTOMER_RECHARGE
            ),
            key=lambda allocation: allocation.allocation_key,
        )
    )
    if not recharge_allocations:
        return ()

    steps: list[ExecutionStep] = []
    existing_allocations = tuple(
        allocation for allocation in recharge_allocations if allocation.customer_invoice_id is not None
    )
    if existing_allocations:
        allocation_keys = tuple(allocation.allocation_key for allocation in existing_allocations)
        steps.append(
            ExecutionStep(
                step_key=_step_key(
                    request,
                    step_type=ExecutionStepType.CUSTOMER_RECHARGE,
                    allocation_keys=allocation_keys,
                ),
                step_type=ExecutionStepType.CUSTOMER_RECHARGE,
                allocation_keys=allocation_keys,
                sequence=first_sequence,
                dry_run_supported=True,
                execute_supported=True,
                writer_required=False,
                allocations=existing_allocations,
            )
        )

    creation_allocations = tuple(
        allocation for allocation in recharge_allocations if allocation.customer_invoice_id is None
    )
    if not creation_allocations:
        return tuple(steps)

    billing_instructions = request.accepted_billing_instructions
    if billing_instructions:
        steps.extend(
            _customer_invoice_creation_steps_from_billing(
                request,
                creation_allocations=creation_allocations,
                first_sequence=first_sequence + len(steps),
                billing_instructions=billing_instructions,
            )
        )
        return tuple(steps)

    for allocation in creation_allocations:
        allocation_keys = (allocation.allocation_key,)
        steps.append(
            ExecutionStep(
                step_key=_step_key(
                    request,
                    step_type=ExecutionStepType.CUSTOMER_RECHARGE,
                    allocation_keys=allocation_keys,
                ),
                step_type=ExecutionStepType.CUSTOMER_RECHARGE,
                allocation_keys=allocation_keys,
                sequence=first_sequence + len(steps),
                dry_run_supported=True,
                execute_supported=False,
                writer_required=True,
                allocations=(allocation,),
            )
        )
    return tuple(steps)


def _customer_invoice_creation_steps_from_billing(
    request: ExecutionRequest,
    *,
    creation_allocations: tuple[BusinessContextAllocation, ...],
    first_sequence: int,
    billing_instructions: tuple[CustomerInvoiceBillingInstruction, ...],
) -> tuple[ExecutionStep, ...]:
    allocation_by_key = {allocation.allocation_key: allocation for allocation in creation_allocations}
    covered_keys: list[str] = []
    steps: list[ExecutionStep] = []
    for instruction in sorted(billing_instructions, key=lambda item: item.billing_key):
        line_keys = tuple(line.allocation_key for line in instruction.lines)
        for allocation_key in line_keys:
            if allocation_key not in allocation_by_key:
                raise ExecutionPlanningError("Billing instruction references an unknown creation allocation.")
        allocations = tuple(allocation_by_key[allocation_key] for allocation_key in line_keys)
        for allocation in allocations:
            if allocation.recharge_partner_id != instruction.customer_id:
                raise ExecutionPlanningError("Billing instruction customer must match recharge_partner_id.")
        covered_keys.extend(line_keys)
        allocation_keys = tuple(sorted(line_keys))
        steps.append(
            ExecutionStep(
                step_key=_customer_invoice_creation_step_key(request, billing_key=instruction.billing_key),
                step_type=ExecutionStepType.CUSTOMER_RECHARGE,
                allocation_keys=allocation_keys,
                sequence=first_sequence + len(steps),
                dry_run_supported=True,
                execute_supported=True,
                writer_required=True,
                allocations=tuple(sorted(allocations, key=lambda allocation: allocation.allocation_key)),
                customer_invoice_billing_instruction=instruction,
            )
        )
    if len(set(covered_keys)) != len(covered_keys):
        raise ExecutionPlanningError("Billing instructions must not duplicate allocation coverage.")
    if set(covered_keys) != set(allocation_by_key):
        raise ExecutionPlanningError("Billing instructions must cover every creation allocation exactly.")
    return tuple(steps)


def _customer_invoice_creation_step_key(request: ExecutionRequest, *, billing_key: str) -> str:
    return f"{request.review_id}:{request.decision_version}:customer_invoice_create:{billing_key}"


def _execute_supported(*, step_type: ExecutionStepType, allocations: tuple[BusinessContextAllocation, ...]) -> bool:
    if step_type is ExecutionStepType.VENDOR_BILL:
        return True
    if step_type is ExecutionStepType.CUSTOMER_RECHARGE:
        return bool(allocations)
    return False


def _writer_required(*, step_type: ExecutionStepType, allocations: tuple[BusinessContextAllocation, ...]) -> bool:
    if step_type is ExecutionStepType.VENDOR_BILL:
        return True
    if step_type is ExecutionStepType.CUSTOMER_RECHARGE:
        return bool(allocations) and all(allocation.customer_invoice_id is None for allocation in allocations)
    return False


def _warnings(request: ExecutionRequest) -> tuple[str, ...]:
    if request.mode is ExecutionMode.EXECUTE:
        return ("Execution mode requested. Only execute-capable plan steps may run.",)
    return ()
