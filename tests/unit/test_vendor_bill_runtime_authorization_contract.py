from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.application.execution.contracts import (
    ExecutionApproval,
    ExecutionMode,
    ExecutionPlan,
    ExecutionStep,
    ExecutionStepType,
)
from app.application.execution.exceptions import ExecutionModeNotEnabledError, ExecutionPlanningError
from app.application.execution.preflight import ExecutionPreflightPolicy
from app.application.workbench.write_authorization import (
    WriteAuthorizationError,
    WriteAuthorizationOperationType,
    WriteAuthorizationRecord,
    WriteAuthorizationStatus,
)


def _authorization() -> WriteAuthorizationRecord:
    now = datetime.now(UTC)
    return WriteAuthorizationRecord(
        authorization_id="00000000-0000-0000-0000-000000000001",
        company_id=7,
        review_id="review-1",
        operation_type=WriteAuthorizationOperationType.EXECUTE_VENDOR_BILL,
        target_version=2,
        status=WriteAuthorizationStatus.CONSUMED,
        authorized_by="finance",
        created_at=now,
        expires_at=now + timedelta(minutes=15),
        consumed_at=now,
        consumed_by_execution_id="bound-execution",
        use_count=1,
    )


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        execution_id="bound-execution",
        company_id=7,
        review_id="review-1",
        decision_version=2,
        mode=ExecutionMode.EXECUTE,
        steps=(
            ExecutionStep(
                step_key="vendor-bill",
                step_type=ExecutionStepType.VENDOR_BILL,
                allocation_keys=(),
                sequence=1,
                execute_supported=True,
                writer_required=True,
            ),
        ),
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"company_id": 8},
        {"review_id": "another-review"},
        {"decision_version": 3},
        {"execution_id": "another-execution"},
    ],
)
def test_preflight_authorization_is_exactly_scoped(changes) -> None:
    with pytest.raises(WriteAuthorizationError):
        ExecutionPreflightPolicy(production_operations_enabled=True).ensure_execute_allowed(
            plan=replace(_plan(), **changes),
            approval=ExecutionApproval(approved_by="operator", authorization=_authorization()),
        )


@pytest.mark.parametrize(
    "step_type",
    [
        ExecutionStepType.CREATE_CUSTOMER_QUOTATION,
        ExecutionStepType.EXISTING_PURCHASE_ORDER,
        ExecutionStepType.CUSTOMER_RECHARGE,
    ],
)
def test_preflight_authorization_never_enables_non_vendor_bill_steps(step_type) -> None:
    step = replace(
        _plan().steps[0],
        step_type=step_type,
        customer_quotation_scenario_id="scenario" if step_type is ExecutionStepType.CREATE_CUSTOMER_QUOTATION else None,
    )
    with pytest.raises(ExecutionModeNotEnabledError):
        ExecutionPreflightPolicy(production_operations_enabled=True).ensure_execute_allowed(
            plan=replace(_plan(), steps=(step,)),
            approval=ExecutionApproval(approved_by="operator", authorization=_authorization()),
        )


def test_preflight_authorization_rejects_multi_step_plan() -> None:
    second = replace(_plan().steps[0], step_key="second-vendor-bill", sequence=2)
    with pytest.raises(ExecutionModeNotEnabledError):
        ExecutionPreflightPolicy(production_operations_enabled=True).ensure_execute_allowed(
            plan=replace(_plan(), steps=(_plan().steps[0], second)),
            approval=ExecutionApproval(approved_by="operator", authorization=_authorization()),
        )


def test_untyped_authorization_is_rejected() -> None:
    with pytest.raises(ExecutionPlanningError):
        ExecutionApproval(approved_by="operator", authorization=object())
