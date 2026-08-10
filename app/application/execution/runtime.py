from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.application.dto import ApplicationDTO
from app.application.execution.contracts import ExecutionMode, ExecutionPlan, ExecutionStepResult, ExecutionStepType
from app.application.execution.exceptions import ExecutionPlanningError, ExecutionStateError


class ExecutionState(StrEnum):
    NEW = "new"
    PLANNED = "planned"
    RUNNING = "running"
    WAITING_RETRY = "waiting_retry"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionRuntimeStepState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_RETRY = "waiting_retry"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionEventType(StrEnum):
    EXECUTION_CREATED = "execution_created"
    PLANNING_COMPLETED = "planning_completed"
    EXECUTION_STARTED = "execution_started"
    STEP_STARTED = "step_started"
    STEP_COMPLETED = "step_completed"
    STEP_FAILED = "step_failed"
    RETRY_SCHEDULED = "retry_scheduled"
    EXECUTION_COMPLETED = "execution_completed"
    EXECUTION_FAILED = "execution_failed"
    EXECUTION_CANCELLED = "execution_cancelled"


class ExecutionRetryPolicyType(StrEnum):
    NEVER = "never"
    IMMEDIATE = "immediate"
    LATER = "later"
    EXPONENTIAL_BACKOFF = "exponential_backoff"


TERMINAL_EXECUTION_STATES = frozenset({ExecutionState.COMPLETED, ExecutionState.FAILED, ExecutionState.CANCELLED})

LEGAL_EXECUTION_TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.NEW: frozenset({ExecutionState.PLANNED, ExecutionState.CANCELLED}),
    ExecutionState.PLANNED: frozenset({ExecutionState.RUNNING, ExecutionState.CANCELLED}),
    ExecutionState.RUNNING: frozenset(
        {
            ExecutionState.WAITING_RETRY,
            ExecutionState.COMPLETED,
            ExecutionState.FAILED,
            ExecutionState.CANCELLED,
        }
    ),
    ExecutionState.WAITING_RETRY: frozenset({ExecutionState.RUNNING, ExecutionState.FAILED, ExecutionState.CANCELLED}),
    ExecutionState.COMPLETED: frozenset(),
    ExecutionState.FAILED: frozenset(),
    ExecutionState.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class ExecutionRetryPolicy(ApplicationDTO):
    policy_type: ExecutionRetryPolicyType
    max_attempts: int = 1
    delay_seconds: int = 0
    backoff_multiplier: int = 2

    def __post_init__(self) -> None:
        _require_enum(self.policy_type, ExecutionRetryPolicyType, "policy_type must be canonical.")
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ExecutionPlanningError("max_attempts must be positive.")
        if type(self.delay_seconds) is not int or self.delay_seconds < 0:
            raise ExecutionPlanningError("delay_seconds must be zero or positive.")
        if type(self.backoff_multiplier) is not int or self.backoff_multiplier < 1:
            raise ExecutionPlanningError("backoff_multiplier must be positive.")

    @classmethod
    def never(cls) -> ExecutionRetryPolicy:
        return cls(policy_type=ExecutionRetryPolicyType.NEVER, max_attempts=1)

    @classmethod
    def immediate(cls, *, max_attempts: int) -> ExecutionRetryPolicy:
        return cls(policy_type=ExecutionRetryPolicyType.IMMEDIATE, max_attempts=max_attempts)

    @classmethod
    def later(cls, *, max_attempts: int, delay_seconds: int) -> ExecutionRetryPolicy:
        return cls(
            policy_type=ExecutionRetryPolicyType.LATER,
            max_attempts=max_attempts,
            delay_seconds=delay_seconds,
        )

    @classmethod
    def exponential_backoff(
        cls,
        *,
        max_attempts: int,
        delay_seconds: int,
        backoff_multiplier: int = 2,
    ) -> ExecutionRetryPolicy:
        return cls(
            policy_type=ExecutionRetryPolicyType.EXPONENTIAL_BACKOFF,
            max_attempts=max_attempts,
            delay_seconds=delay_seconds,
            backoff_multiplier=backoff_multiplier,
        )


@dataclass(frozen=True, slots=True)
class ExecutionFailure(ApplicationDTO):
    step_key: str | None
    error_code: str
    safe_message: str

    def __post_init__(self) -> None:
        if self.step_key is not None:
            _require_text(self.step_key, "step_key must be non-empty when supplied.")
        _require_text(self.error_code, "error_code is required.")
        _require_text(self.safe_message, "safe_message is required.")


@dataclass(frozen=True, slots=True)
class ExecutionCheckpoint(ApplicationDTO):
    execution_id: str
    completed_step_keys: tuple[str, ...]
    failed_step_key: str | None
    current_step_key: str | None
    retry_count: int
    last_event_id: str | None

    def __post_init__(self) -> None:
        _require_text(self.execution_id, "execution_id is required.")
        completed = tuple(self.completed_step_keys)
        if len(set(completed)) != len(completed):
            raise ExecutionPlanningError("completed_step_keys must be unique.")
        for step_key in completed:
            _require_text(step_key, "completed step_key is required.")
        object.__setattr__(self, "completed_step_keys", completed)
        if self.failed_step_key is not None:
            _require_text(self.failed_step_key, "failed_step_key must be non-empty when supplied.")
        if self.current_step_key is not None:
            _require_text(self.current_step_key, "current_step_key must be non-empty when supplied.")
        if type(self.retry_count) is not int or self.retry_count < 0:
            raise ExecutionPlanningError("retry_count must be zero or positive.")
        if self.last_event_id is not None:
            _require_text(self.last_event_id, "last_event_id must be non-empty when supplied.")


@dataclass(frozen=True, slots=True)
class ExecutionCursor(ApplicationDTO):
    execution_id: str
    next_step_key: str | None
    last_event_id: str | None

    def __post_init__(self) -> None:
        _require_text(self.execution_id, "execution_id is required.")
        if self.next_step_key is not None:
            _require_text(self.next_step_key, "next_step_key must be non-empty when supplied.")
        if self.last_event_id is not None:
            _require_text(self.last_event_id, "last_event_id must be non-empty when supplied.")


@dataclass(frozen=True, slots=True)
class ExecutionEvent(ApplicationDTO):
    event_id: str
    execution_id: str
    event_type: ExecutionEventType
    sequence: int
    state: ExecutionState
    step_key: str | None = None
    step_type: ExecutionStepType | None = None
    data: dict[str, str | int | bool | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.event_id, "event_id is required.")
        _require_text(self.execution_id, "execution_id is required.")
        _require_enum(self.event_type, ExecutionEventType, "event_type must be canonical.")
        if type(self.sequence) is not int or self.sequence < 1:
            raise ExecutionPlanningError("event sequence must be positive.")
        _require_enum(self.state, ExecutionState, "state must be canonical.")
        if self.step_key is not None:
            _require_text(self.step_key, "step_key must be non-empty when supplied.")
        if self.step_type is not None:
            _require_enum(self.step_type, ExecutionStepType, "step_type must be canonical.")
        object.__setattr__(self, "data", dict(self.data))


@dataclass(frozen=True, slots=True)
class ExecutionRuntimeStep(ApplicationDTO):
    step_key: str
    step_type: ExecutionStepType
    sequence: int
    state: ExecutionRuntimeStepState
    allocation_keys: tuple[str, ...]
    retry_count: int = 0
    last_result: ExecutionStepResult | None = None

    def __post_init__(self) -> None:
        _require_text(self.step_key, "step_key is required.")
        _require_enum(self.step_type, ExecutionStepType, "step_type must be canonical.")
        if type(self.sequence) is not int or self.sequence < 1:
            raise ExecutionPlanningError("step sequence must be positive.")
        _require_enum(self.state, ExecutionRuntimeStepState, "step state must be canonical.")
        allocation_keys = tuple(self.allocation_keys)
        if len(set(allocation_keys)) != len(allocation_keys):
            raise ExecutionPlanningError("allocation_keys must be unique.")
        object.__setattr__(self, "allocation_keys", allocation_keys)
        if type(self.retry_count) is not int or self.retry_count < 0:
            raise ExecutionPlanningError("retry_count must be zero or positive.")
        if self.last_result is not None and not isinstance(self.last_result, ExecutionStepResult):
            raise ExecutionPlanningError("last_result must be an ExecutionStepResult.")


@dataclass(frozen=True, slots=True)
class ExecutionSnapshot(ApplicationDTO):
    execution_id: str
    review_id: str
    company_id: int
    decision_version: int
    mode: ExecutionMode
    state: ExecutionState
    idempotency_key: str
    plan: ExecutionPlan
    steps: tuple[ExecutionRuntimeStep, ...]
    checkpoint: ExecutionCheckpoint
    retry_policy: ExecutionRetryPolicy
    failure: ExecutionFailure | None = None

    def __post_init__(self) -> None:
        _require_text(self.execution_id, "execution_id is required.")
        _require_text(self.review_id, "review_id is required.")
        if type(self.company_id) is not int or self.company_id <= 0:
            raise ExecutionPlanningError("company_id must be positive.")
        if type(self.decision_version) is not int or self.decision_version <= 0:
            raise ExecutionPlanningError("decision_version must be positive.")
        _require_enum(self.mode, ExecutionMode, "mode must be canonical.")
        _require_enum(self.state, ExecutionState, "state must be canonical.")
        _require_text(self.idempotency_key, "idempotency_key is required.")
        if not isinstance(self.plan, ExecutionPlan):
            raise ExecutionPlanningError("ExecutionPlan is required.")
        steps = tuple(self.steps)
        if not steps:
            raise ExecutionPlanningError("runtime snapshot requires steps.")
        object.__setattr__(self, "steps", steps)
        if not isinstance(self.checkpoint, ExecutionCheckpoint):
            raise ExecutionPlanningError("ExecutionCheckpoint is required.")
        if not isinstance(self.retry_policy, ExecutionRetryPolicy):
            raise ExecutionPlanningError("ExecutionRetryPolicy is required.")
        if self.failure is not None and not isinstance(self.failure, ExecutionFailure):
            raise ExecutionPlanningError("failure must be an ExecutionFailure.")

    @property
    def cursor(self) -> ExecutionCursor:
        return ExecutionCursor(
            execution_id=self.execution_id,
            next_step_key=self.checkpoint.current_step_key,
            last_event_id=self.checkpoint.last_event_id,
        )


@dataclass(frozen=True, slots=True)
class ExecutionHistory(ApplicationDTO):
    execution_id: str
    events: tuple[ExecutionEvent, ...]

    def __post_init__(self) -> None:
        _require_text(self.execution_id, "execution_id is required.")
        events = tuple(self.events)
        for event in events:
            if not isinstance(event, ExecutionEvent):
                raise ExecutionPlanningError("history requires ExecutionEvent values.")
        object.__setattr__(self, "events", events)


@dataclass(frozen=True, slots=True)
class ExecutionRuntime(ApplicationDTO):
    snapshot: ExecutionSnapshot
    history: ExecutionHistory

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, ExecutionSnapshot):
            raise ExecutionPlanningError("ExecutionSnapshot is required.")
        if not isinstance(self.history, ExecutionHistory):
            raise ExecutionPlanningError("ExecutionHistory is required.")
        if self.snapshot.execution_id != self.history.execution_id:
            raise ExecutionPlanningError("runtime snapshot and history execution_id must match.")


def assert_legal_transition(current: ExecutionState, target: ExecutionState) -> None:
    _require_enum(current, ExecutionState, "current state must be canonical.")
    _require_enum(target, ExecutionState, "target state must be canonical.")
    if target not in LEGAL_EXECUTION_TRANSITIONS[current]:
        raise ExecutionStateError(f"Illegal execution state transition: {current.value} -> {target.value}.")


def _require_text(value: str | None, message: str) -> None:
    if value is None or not isinstance(value, str) or not value.strip():
        raise ExecutionPlanningError(message)


def _require_enum(value: StrEnum, expected_type: type[StrEnum], message: str) -> None:
    if not isinstance(value, expected_type):
        raise ExecutionPlanningError(message)
