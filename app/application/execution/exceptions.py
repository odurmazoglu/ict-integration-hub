from __future__ import annotations

from app.application.exceptions import ApplicationError


class ExecutionError(ApplicationError):
    """Safe base error for workflow execution foundation failures."""

    error_category = "execution_error"


class ExecutionPlanningError(ExecutionError):
    """Safe error raised when an execution request cannot be planned."""

    error_category = "execution_planning_error"


class ExecutionStrategyResolutionError(ExecutionError):
    """Safe error raised when an execution strategy cannot be resolved exactly."""

    error_category = "execution_strategy_resolution_error"


class ExecutionUnsupportedStepError(ExecutionError):
    """Safe error raised when an execution step type has no supported strategy."""

    error_category = "execution_unsupported_step_error"


class ExecutionIdempotencyConflictError(ExecutionError):
    """Safe error raised when an execution idempotency key conflicts."""

    error_category = "execution_idempotency_conflict"


class ExecutionStateError(ExecutionError):
    """Safe error raised for invalid execution state operations."""

    error_category = "execution_state_error"


class ExecutionRuntimeError(ExecutionError):
    """Safe error raised by the durable execution runtime."""

    error_category = "execution_runtime_error"


class ExecutionPersistenceError(ExecutionRuntimeError):
    """Safe error raised when execution runtime persistence fails."""

    error_category = "execution_persistence_error"


class ExecutionNotFoundError(ExecutionRuntimeError):
    """Safe error raised when an execution runtime cannot be found."""

    error_category = "execution_not_found"
