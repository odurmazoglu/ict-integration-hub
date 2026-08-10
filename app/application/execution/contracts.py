from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.application.commands import Command
from app.application.dto import ApplicationDTO
from app.application.execution.exceptions import ExecutionPlanningError
from app.application.workbench.allocations import BusinessContextAllocationSet
from app.application.workflow import WorkflowType


class ExecutionStatus(StrEnum):
    PENDING = "pending"
    PLANNED = "planned"
    DRY_RUN_COMPLETED = "dry_run_completed"
    READY_TO_EXECUTE = "ready_to_execute"
    EXECUTED = "executed"
    PARTIALLY_EXECUTED = "partially_executed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionMode(StrEnum):
    DRY_RUN = "dry_run"
    EXECUTE = "execute"


class ExecutionStepType(StrEnum):
    VENDOR_BILL = WorkflowType.VENDOR_BILL.value
    SALES_ORDER_COST_LINK = "sales_order_cost_link"
    CUSTOMER_RECHARGE = "customer_recharge"
    EXISTING_PURCHASE_ORDER = "existing_purchase_order"
    NEW_RFQ_PURCHASE = "new_rfq_purchase"
    PROJECT_COST = "project_cost"
    OPERATING_EXPENSE = "operating_expense"
    FIXED_ASSET = "fixed_asset"
    SUBSCRIPTION_SERVICE = "subscription_service"
    INTERNAL_COST = "internal_cost"


class ExecutionStepStatus(StrEnum):
    DRY_RUN_OK = "dry_run_ok"
    READY = "ready"
    EXECUTED = "executed"
    SKIPPED = "skipped"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class ExecutionFailurePolicy(StrEnum):
    FAIL_FAST = "fail_fast"
    COLLECT_ALL = "collect_all"


@dataclass(frozen=True, slots=True)
class ExecutionRequest(Command):
    execution_id: str
    review_id: str
    company_id: int
    decision_version: int
    decision_id: str | None
    idempotency_key: str
    mode: ExecutionMode
    selected_workflow: WorkflowType | None
    business_context_allocations: BusinessContextAllocationSet | None = None

    def __post_init__(self) -> None:
        _require_text(self.execution_id, "execution_id is required.")
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.decision_version, "decision_version must be positive.")
        if self.decision_id is not None:
            _require_text(self.decision_id, "decision_id must be non-empty when supplied.")
        _require_text(self.idempotency_key, "idempotency_key is required.")
        _require_enum(self.mode, ExecutionMode, "mode must be a canonical ExecutionMode.")
        if self.selected_workflow is not None:
            _require_enum(
                self.selected_workflow,
                WorkflowType,
                "selected_workflow must be a canonical WorkflowType.",
            )


@dataclass(frozen=True, slots=True)
class ExecutionStep(ApplicationDTO):
    step_key: str
    step_type: ExecutionStepType
    allocation_keys: tuple[str, ...]
    sequence: int
    dry_run_supported: bool = True
    execute_supported: bool = False

    def __post_init__(self) -> None:
        _require_text(self.step_key, "step_key is required.")
        _require_enum(self.step_type, ExecutionStepType, "step_type must be a canonical ExecutionStepType.")
        if type(self.sequence) is not int or self.sequence < 1:
            raise ExecutionPlanningError("step sequence must be positive.")
        allocation_keys = tuple(self.allocation_keys)
        if len(set(allocation_keys)) != len(allocation_keys):
            raise ExecutionPlanningError("allocation_keys must be unique per execution step.")
        for allocation_key in allocation_keys:
            _require_text(allocation_key, "allocation_key is required.")
        object.__setattr__(self, "allocation_keys", allocation_keys)
        _require_bool(self.dry_run_supported, "dry_run_supported must be boolean.")
        _require_bool(self.execute_supported, "execute_supported must be boolean.")


@dataclass(frozen=True, slots=True)
class ExecutionPlan(ApplicationDTO):
    execution_id: str
    review_id: str
    company_id: int
    decision_version: int
    mode: ExecutionMode
    steps: tuple[ExecutionStep, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.execution_id, "execution_id is required.")
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.decision_version, "decision_version must be positive.")
        _require_enum(self.mode, ExecutionMode, "mode must be a canonical ExecutionMode.")
        steps = tuple(self.steps)
        if not steps:
            raise ExecutionPlanningError("execution plan requires at least one step.")
        _reject_duplicate_step_keys(steps)
        object.__setattr__(self, "steps", steps)
        object.__setattr__(self, "warnings", tuple(str(warning) for warning in self.warnings))
        if self.idempotency_key is not None:
            _require_text(self.idempotency_key, "idempotency_key must be non-empty when supplied.")


@dataclass(frozen=True, slots=True)
class ExecutionStepRequest(ApplicationDTO):
    execution_id: str
    review_id: str
    company_id: int
    decision_version: int
    mode: ExecutionMode
    step: ExecutionStep

    def __post_init__(self) -> None:
        _require_text(self.execution_id, "execution_id is required.")
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.decision_version, "decision_version must be positive.")
        _require_enum(self.mode, ExecutionMode, "mode must be a canonical ExecutionMode.")
        if not isinstance(self.step, ExecutionStep):
            raise ExecutionPlanningError("ExecutionStep is required.")


@dataclass(frozen=True, slots=True)
class ExecutionStepResult(ApplicationDTO):
    step_key: str
    step_type: ExecutionStepType
    status: ExecutionStepStatus
    dry_run: bool
    message: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)
    produced_reference_ids: tuple[str, ...] = field(default_factory=tuple)
    error_code: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.step_key, "step_key is required.")
        _require_enum(self.step_type, ExecutionStepType, "step_type must be a canonical ExecutionStepType.")
        _require_enum(self.status, ExecutionStepStatus, "status must be a canonical ExecutionStepStatus.")
        _require_bool(self.dry_run, "dry_run must be boolean.")
        if self.message is not None:
            _require_text(self.message, "message must be non-empty when supplied.")
        if self.error_code is not None:
            _require_text(self.error_code, "error_code must be non-empty when supplied.")
        object.__setattr__(self, "warnings", tuple(str(warning) for warning in self.warnings))
        object.__setattr__(self, "produced_reference_ids", tuple(str(ref) for ref in self.produced_reference_ids))


@dataclass(frozen=True, slots=True)
class ExecutionResult(ApplicationDTO):
    execution_id: str
    status: ExecutionStatus
    step_results: tuple[ExecutionStepResult, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_text(self.execution_id, "execution_id is required.")
        _require_enum(self.status, ExecutionStatus, "status must be a canonical ExecutionStatus.")
        step_results = tuple(self.step_results)
        object.__setattr__(self, "step_results", step_results)
        object.__setattr__(self, "warnings", tuple(str(warning) for warning in self.warnings))

    @property
    def succeeded_count(self) -> int:
        return sum(
            1
            for result in self.step_results
            if result.status
            in {ExecutionStepStatus.DRY_RUN_OK, ExecutionStepStatus.READY, ExecutionStepStatus.EXECUTED}
        )

    @property
    def failed_count(self) -> int:
        return sum(1 for result in self.step_results if result.status is ExecutionStepStatus.FAILED)

    @property
    def skipped_count(self) -> int:
        return sum(1 for result in self.step_results if result.status is ExecutionStepStatus.SKIPPED)


@dataclass(frozen=True, slots=True)
class AcceptedReviewDecision(ApplicationDTO):
    review_id: str
    company_id: int
    decision_version: int
    decision_id: str | None
    selected_workflow: WorkflowType | None
    business_context_allocations: BusinessContextAllocationSet | None = None

    def __post_init__(self) -> None:
        _require_text(self.review_id, "review_id is required.")
        _require_positive_int(self.company_id, "company_id must be positive.")
        _require_positive_int(self.decision_version, "decision_version must be positive.")
        if self.decision_id is not None:
            _require_text(self.decision_id, "decision_id must be non-empty when supplied.")
        if self.selected_workflow is not None:
            _require_enum(
                self.selected_workflow,
                WorkflowType,
                "selected_workflow must be a canonical WorkflowType.",
            )


def _reject_duplicate_step_keys(steps: tuple[ExecutionStep, ...]) -> None:
    seen: set[str] = set()
    for step in steps:
        if step.step_key in seen:
            raise ExecutionPlanningError("execution plan step_key values must be unique.")
        seen.add(step.step_key)


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise ExecutionPlanningError(message)


def _require_positive_int(value: int, message: str) -> None:
    if type(value) is not int or value <= 0:
        raise ExecutionPlanningError(message)


def _require_bool(value: bool, message: str) -> None:
    if type(value) is not bool:
        raise ExecutionPlanningError(message)


def _require_enum(value: StrEnum, expected_type: type[StrEnum], message: str) -> None:
    if not isinstance(value, expected_type):
        raise ExecutionPlanningError(message)
