from __future__ import annotations

from uuid import uuid4

from app.application.execution.contracts import (
    ExecutionApproval,
    ExecutionMode,
    ExecutionPlan,
    ExecutionResult,
    ExecutionStatus,
    ExecutionStepRequest,
    ExecutionStepResult,
    ExecutionStepStatus,
)
from app.application.execution.exceptions import ExecutionNotFoundError
from app.application.execution.ports import (
    ExecutionEventRepository,
    ExecutionRuntimeRepository,
)
from app.application.execution.runtime import (
    ExecutionCheckpoint,
    ExecutionEventDraft,
    ExecutionEventType,
    ExecutionFailure,
    ExecutionRetryPolicy,
    ExecutionRetryPolicyType,
    ExecutionRuntime,
    ExecutionRuntimeStep,
    ExecutionRuntimeStepState,
    ExecutionSnapshot,
    ExecutionState,
    assert_legal_transition,
)
from app.application.execution.strategy import ExecutionStrategyResolver


class _Missing:
    pass


_MISSING = _Missing()


class StaticRetryPolicyResolver:
    """Deterministic retry policy resolver for the runtime foundation."""

    def __init__(self, policy: ExecutionRetryPolicy | None = None) -> None:
        self._policy = policy or ExecutionRetryPolicy.never()

    def resolve(self, _plan: ExecutionPlan) -> ExecutionRetryPolicy:
        return self._policy


class ExecutionRuntimeService:
    """Create or load durable runtime snapshots from planned executions."""

    def __init__(
        self,
        *,
        runtime_repository: ExecutionRuntimeRepository,
        event_repository: ExecutionEventRepository,
    ) -> None:
        self._runtime_repository = runtime_repository
        self._event_repository = event_repository

    def create_or_load(self, *, plan: ExecutionPlan, retry_policy: ExecutionRetryPolicy) -> ExecutionRuntime:
        snapshot = self._runtime_repository.create_from_plan(plan=plan, retry_policy=retry_policy)
        history = self._event_repository.history(execution_id=snapshot.execution_id)
        return ExecutionRuntime(snapshot=snapshot, history=history)


class ExecutionRuntimeCoordinator:
    """Runtime-aware sequential coordinator that persists state and events."""

    def __init__(
        self,
        *,
        runtime_repository: ExecutionRuntimeRepository,
        event_repository: ExecutionEventRepository,
        strategy_resolver: ExecutionStrategyResolver,
    ) -> None:
        self._runtime_repository = runtime_repository
        self._event_repository = event_repository
        self._strategy_resolver = strategy_resolver

    def resume(
        self,
        *,
        execution_id: str,
        approval: ExecutionApproval | None = None,
    ) -> ExecutionResult:
        snapshot = self._runtime_repository.get_snapshot(execution_id=execution_id)
        if snapshot is None:
            raise ExecutionNotFoundError("Execution runtime was not found.")
        return self.execute(snapshot, approval=approval)

    def ensure_plan_supports_mode(self, *, plan: ExecutionPlan, mode: ExecutionMode) -> None:
        self._strategy_resolver.ensure_plan_supports_mode(plan=plan, mode=mode)

    def execute(
        self,
        snapshot: ExecutionSnapshot,
        *,
        approval: ExecutionApproval | None = None,
    ) -> ExecutionResult:
        if snapshot.state in {ExecutionState.COMPLETED, ExecutionState.FAILED, ExecutionState.CANCELLED}:
            return _result_from_snapshot(snapshot)

        snapshot = self._start(snapshot)
        results: list[ExecutionStepResult] = [
            step.last_result for step in snapshot.steps if step.last_result is not None
        ]

        for step in _pending_steps(snapshot):
            snapshot = self._mark_step_running(snapshot, step)
            result = self._execute_step(snapshot, step, approval=approval)
            results.append(result)
            if _is_failure(result):
                snapshot = self._handle_failure(snapshot, step, result)
                if snapshot.state in {ExecutionState.WAITING_RETRY, ExecutionState.FAILED}:
                    break
                continue
            snapshot = self._mark_step_completed(snapshot, step, result)

        if snapshot.state is ExecutionState.RUNNING and _all_steps_completed(snapshot):
            snapshot = self._complete(snapshot)

        return _result_from_snapshot(snapshot, step_results=tuple(results))

    def _start(self, snapshot: ExecutionSnapshot) -> ExecutionSnapshot:
        if snapshot.state is ExecutionState.RUNNING:
            return snapshot
        if snapshot.state is ExecutionState.WAITING_RETRY:
            assert_legal_transition(snapshot.state, ExecutionState.RUNNING)
        else:
            assert_legal_transition(snapshot.state, ExecutionState.RUNNING)
        next_snapshot = _replace_snapshot(snapshot, state=ExecutionState.RUNNING)
        return self._runtime_repository.persist_transition(
            snapshot=next_snapshot,
            events=(
                _event(
                    execution_id=next_snapshot.execution_id,
                    event_type=ExecutionEventType.EXECUTION_STARTED,
                    state=ExecutionState.RUNNING,
                ),
            ),
            expected_runtime_version=snapshot.runtime_version,
        )

    def _mark_step_running(self, snapshot: ExecutionSnapshot, step: ExecutionRuntimeStep) -> ExecutionSnapshot:
        next_snapshot = _replace_step(
            snapshot,
            step_key=step.step_key,
            state=ExecutionRuntimeStepState.RUNNING,
            current_step_key=step.step_key,
        )
        return self._runtime_repository.persist_transition(
            snapshot=next_snapshot,
            events=(
                _event(
                    execution_id=next_snapshot.execution_id,
                    event_type=ExecutionEventType.STEP_STARTED,
                    state=ExecutionState.RUNNING,
                    step=step,
                ),
            ),
            expected_runtime_version=snapshot.runtime_version,
        )

    def _execute_step(
        self,
        snapshot: ExecutionSnapshot,
        step: ExecutionRuntimeStep,
        *,
        approval: ExecutionApproval | None,
    ) -> ExecutionStepResult:
        plan_step = next(plan_step for plan_step in snapshot.plan.steps if plan_step.step_key == step.step_key)
        strategy = self._strategy_resolver.resolve(plan_step.step_type)
        return strategy.execute(
            ExecutionStepRequest(
                execution_id=snapshot.execution_id,
                review_id=snapshot.review_id,
                company_id=snapshot.company_id,
                decision_version=snapshot.decision_version,
                mode=snapshot.mode,
                step=plan_step,
                approval=approval,
            )
        )

    def _mark_step_completed(
        self,
        snapshot: ExecutionSnapshot,
        step: ExecutionRuntimeStep,
        result: ExecutionStepResult,
    ) -> ExecutionSnapshot:
        completed = tuple([*snapshot.checkpoint.completed_step_keys, step.step_key])
        next_step = _next_step_key(snapshot, completed_step_keys=frozenset(completed))
        next_snapshot = _replace_step(
            snapshot,
            step_key=step.step_key,
            state=ExecutionRuntimeStepState.COMPLETED,
            result=result,
            completed_step_keys=completed,
            current_step_key=next_step,
        )
        return self._runtime_repository.persist_transition(
            snapshot=next_snapshot,
            events=(
                _event(
                    execution_id=next_snapshot.execution_id,
                    event_type=ExecutionEventType.STEP_COMPLETED,
                    state=ExecutionState.RUNNING,
                    step=step,
                    result=result,
                ),
            ),
            expected_runtime_version=snapshot.runtime_version,
        )

    def _handle_failure(
        self,
        snapshot: ExecutionSnapshot,
        step: ExecutionRuntimeStep,
        result: ExecutionStepResult,
    ) -> ExecutionSnapshot:
        retry_count = step.retry_count + 1
        if _should_retry(snapshot.retry_policy, retry_count=retry_count):
            next_snapshot = _replace_step(
                snapshot,
                step_key=step.step_key,
                state=ExecutionRuntimeStepState.WAITING_RETRY,
                result=result,
                retry_count=retry_count,
                current_step_key=step.step_key,
                state_override=ExecutionState.WAITING_RETRY,
                failed_step_key=step.step_key,
                failure=_failure_from_result(result),
            )
            return self._runtime_repository.persist_transition(
                snapshot=next_snapshot,
                events=(
                    _event(
                        execution_id=next_snapshot.execution_id,
                        event_type=ExecutionEventType.STEP_FAILED,
                        state=ExecutionState.RUNNING,
                        step=step,
                        result=result,
                    ),
                    _event(
                        execution_id=next_snapshot.execution_id,
                        event_type=ExecutionEventType.RETRY_SCHEDULED,
                        state=ExecutionState.WAITING_RETRY,
                        step=step,
                        result=result,
                    ),
                ),
                expected_runtime_version=snapshot.runtime_version,
            )

        next_snapshot = _replace_step(
            snapshot,
            step_key=step.step_key,
            state=ExecutionRuntimeStepState.FAILED,
            result=result,
            retry_count=retry_count,
            current_step_key=step.step_key,
            state_override=ExecutionState.FAILED,
            failed_step_key=step.step_key,
            failure=_failure_from_result(result),
        )
        return self._runtime_repository.persist_transition(
            snapshot=next_snapshot,
            events=(
                _event(
                    execution_id=next_snapshot.execution_id,
                    event_type=ExecutionEventType.STEP_FAILED,
                    state=ExecutionState.RUNNING,
                    step=step,
                    result=result,
                ),
                _event(
                    execution_id=next_snapshot.execution_id,
                    event_type=ExecutionEventType.EXECUTION_FAILED,
                    state=ExecutionState.FAILED,
                    step=step,
                    result=result,
                ),
            ),
            expected_runtime_version=snapshot.runtime_version,
        )

    def _complete(self, snapshot: ExecutionSnapshot) -> ExecutionSnapshot:
        assert_legal_transition(snapshot.state, ExecutionState.COMPLETED)
        next_snapshot = _replace_snapshot(snapshot, state=ExecutionState.COMPLETED, current_step_key=None)
        return self._runtime_repository.persist_transition(
            snapshot=next_snapshot,
            events=(
                _event(
                    execution_id=next_snapshot.execution_id,
                    event_type=ExecutionEventType.EXECUTION_COMPLETED,
                    state=ExecutionState.COMPLETED,
                ),
            ),
            expected_runtime_version=snapshot.runtime_version,
        )


def _event(
    *,
    execution_id: str,
    event_type: ExecutionEventType,
    state: ExecutionState,
    step: ExecutionRuntimeStep | None = None,
    result: ExecutionStepResult | None = None,
    data: dict[str, str | int | bool | None] | None = None,
) -> ExecutionEventDraft:
    event_data = data or {}
    if result is not None:
        event_data = {
            "status": result.status.value,
            "dry_run": result.dry_run,
            "error_code": result.error_code,
            "message": result.message,
        }
    return ExecutionEventDraft(
        event_id=f"execution-event:{uuid4()}",
        execution_id=execution_id,
        event_type=event_type,
        state=state,
        step_key=step.step_key if step else None,
        step_type=step.step_type if step else None,
        data=event_data,
    )


def _pending_steps(snapshot: ExecutionSnapshot) -> tuple[ExecutionRuntimeStep, ...]:
    completed = set(snapshot.checkpoint.completed_step_keys)
    return tuple(
        step
        for step in sorted(snapshot.steps, key=lambda item: item.sequence)
        if step.step_key not in completed and step.state is not ExecutionRuntimeStepState.COMPLETED
    )


def _all_steps_completed(snapshot: ExecutionSnapshot) -> bool:
    return all(step.state is ExecutionRuntimeStepState.COMPLETED for step in snapshot.steps)


def _is_failure(result: ExecutionStepResult) -> bool:
    return result.status in {ExecutionStepStatus.FAILED, ExecutionStepStatus.UNSUPPORTED}


def _should_retry(policy: ExecutionRetryPolicy, *, retry_count: int) -> bool:
    return policy.policy_type is not ExecutionRetryPolicyType.NEVER and retry_count < policy.max_attempts


def _failure_from_result(result: ExecutionStepResult) -> ExecutionFailure:
    return ExecutionFailure(
        step_key=result.step_key,
        error_code=result.error_code or result.status.value,
        safe_message=result.message or "Execution step failed safely.",
    )


def _replace_snapshot(
    snapshot: ExecutionSnapshot,
    *,
    state: ExecutionState,
    current_step_key: str | None | object = _MISSING,
) -> ExecutionSnapshot:
    checkpoint = snapshot.checkpoint
    if current_step_key is not _MISSING:
        checkpoint = ExecutionCheckpoint(
            execution_id=checkpoint.execution_id,
            completed_step_keys=checkpoint.completed_step_keys,
            failed_step_key=checkpoint.failed_step_key,
            current_step_key=current_step_key if isinstance(current_step_key, str) else None,
            retry_count=checkpoint.retry_count,
            last_event_id=checkpoint.last_event_id,
        )
    return ExecutionSnapshot(
        execution_id=snapshot.execution_id,
        review_id=snapshot.review_id,
        company_id=snapshot.company_id,
        decision_version=snapshot.decision_version,
        mode=snapshot.mode,
        state=state,
        idempotency_key=snapshot.idempotency_key,
        plan=snapshot.plan,
        steps=snapshot.steps,
        checkpoint=checkpoint,
        retry_policy=snapshot.retry_policy,
        runtime_version=snapshot.runtime_version,
        failure=snapshot.failure,
    )


def _replace_step(
    snapshot: ExecutionSnapshot,
    *,
    step_key: str,
    state: ExecutionRuntimeStepState,
    result: ExecutionStepResult | None = None,
    retry_count: int | None = None,
    completed_step_keys: tuple[str, ...] | None = None,
    current_step_key: str | None = None,
    state_override: ExecutionState | None = None,
    failed_step_key: str | None = None,
    failure: ExecutionFailure | None = None,
) -> ExecutionSnapshot:
    steps: list[ExecutionRuntimeStep] = []
    for step in snapshot.steps:
        if step.step_key == step_key:
            steps.append(
                ExecutionRuntimeStep(
                    step_key=step.step_key,
                    step_type=step.step_type,
                    sequence=step.sequence,
                    state=state,
                    allocation_keys=step.allocation_keys,
                    retry_count=retry_count if retry_count is not None else step.retry_count,
                    last_result=result if result is not None else step.last_result,
                )
            )
        else:
            steps.append(step)
    checkpoint = ExecutionCheckpoint(
        execution_id=snapshot.execution_id,
        completed_step_keys=completed_step_keys or snapshot.checkpoint.completed_step_keys,
        failed_step_key=failed_step_key,
        current_step_key=current_step_key,
        retry_count=retry_count if retry_count is not None else snapshot.checkpoint.retry_count,
        last_event_id=snapshot.checkpoint.last_event_id,
    )
    target_state = state_override or snapshot.state
    if target_state is not snapshot.state:
        assert_legal_transition(snapshot.state, target_state)
    return ExecutionSnapshot(
        execution_id=snapshot.execution_id,
        review_id=snapshot.review_id,
        company_id=snapshot.company_id,
        decision_version=snapshot.decision_version,
        mode=snapshot.mode,
        state=target_state,
        idempotency_key=snapshot.idempotency_key,
        plan=snapshot.plan,
        steps=tuple(steps),
        checkpoint=checkpoint,
        retry_policy=snapshot.retry_policy,
        runtime_version=snapshot.runtime_version,
        failure=failure or snapshot.failure,
    )


def _next_step_key(snapshot: ExecutionSnapshot, *, completed_step_keys: frozenset[str]) -> str | None:
    for step in sorted(snapshot.steps, key=lambda item: item.sequence):
        if step.step_key not in completed_step_keys:
            return step.step_key
    return None


def _result_from_snapshot(
    snapshot: ExecutionSnapshot,
    *,
    step_results: tuple[ExecutionStepResult, ...] | None = None,
) -> ExecutionResult:
    results = step_results or tuple(step.last_result for step in snapshot.steps if step.last_result is not None)
    return ExecutionResult(
        execution_id=snapshot.execution_id,
        status=_execution_status(snapshot),
        step_results=results,
        warnings=snapshot.plan.warnings,
    )


def _execution_status(snapshot: ExecutionSnapshot) -> ExecutionStatus:
    if snapshot.state is ExecutionState.COMPLETED and snapshot.mode is ExecutionMode.DRY_RUN:
        return ExecutionStatus.DRY_RUN_COMPLETED
    if snapshot.state is ExecutionState.COMPLETED:
        return ExecutionStatus.EXECUTED
    if snapshot.state is ExecutionState.FAILED:
        return ExecutionStatus.FAILED
    if snapshot.state is ExecutionState.CANCELLED:
        return ExecutionStatus.CANCELLED
    return ExecutionStatus.PLANNED
