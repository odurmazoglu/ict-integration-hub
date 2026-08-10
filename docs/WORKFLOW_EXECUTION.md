# Workflow Execution Foundation

Workflow execution starts only from an accepted, authoritative Hub Workbench decision. It does not start from raw Odoo candidates, Odoo Studio rows, recommendations, or Rule Engine output.

Current flow:

```text
Accepted Workbench Decision
  -> ExecutionRequest
  -> ExecutionRuntimeService
  -> ExecutionPlanner
  -> ExecutionPlan
  -> WorkflowExecution / WorkflowExecutionStep / WorkflowExecutionEvent
  -> ExecutionCheckpoint
  -> ExecutionStrategyResolver
  -> ExecutionRuntimeCoordinator
  -> dry-run ExecutionStepResult values
  -> ExecutionResult
```

This foundation is separate from decision selection. `WorkflowType` remains the high-level review decision vocabulary, while `BusinessContextAllocationType` describes each allocation row's execution purpose. A single accepted decision may therefore become a composite plan with multiple execution steps.

## Identity And Idempotency

`ExecutionRequest` carries `execution_id`, `review_id`, `company_id`, accepted decision version, optional accepted `decision_id`, execution mode, selected workflow, and immutable allocation evidence.

Execution idempotency is distinct from Workbench decision idempotency. The deterministic execution key is derived from:

- `company_id`
- `review_id`
- accepted decision version
- execution mode
- canonical execution plan steps

The key does not use timestamps or random UUIDs. The same accepted decision in `DRY_RUN` and `EXECUTE` mode has different execution idempotency.

## Planning

`ExecutionPlanner` performs no ERP calls, no persistence, no provider calls, and no writes. It maps allocation purposes to stable step types:

- `SALES_ORDER_COST` -> `SALES_ORDER_COST_LINK`
- `CUSTOMER_RECHARGE` -> `CUSTOMER_RECHARGE`
- `EXISTING_PURCHASE_ORDER` -> `EXISTING_PURCHASE_ORDER`
- `NEW_RFQ_PURCHASE` -> `NEW_RFQ_PURCHASE`
- `PROJECT_COST` -> `PROJECT_COST`
- `OPERATING_EXPENSE` -> `OPERATING_EXPENSE`
- `FIXED_ASSET` -> `FIXED_ASSET`
- `SUBSCRIPTION_SERVICE` -> `SUBSCRIPTION_SERVICE`
- `INTERNAL_COST` -> `INTERNAL_COST`

When `selected_workflow == WorkflowType.VENDOR_BILL`, the planner adds a separate `VENDOR_BILL` step. This step is selected-workflow behavior, not inferred from every allocation row.

Step order is deterministic:

1. `EXISTING_PURCHASE_ORDER`
2. `NEW_RFQ_PURCHASE`
3. `VENDOR_BILL`
4. `SALES_ORDER_COST_LINK`
5. `CUSTOMER_RECHARGE`
6. `PROJECT_COST`
7. `OPERATING_EXPENSE`
8. `FIXED_ASSET`
9. `SUBSCRIPTION_SERVICE`
10. `INTERNAL_COST`

Allocation keys are preserved in each step. Allocation ordering alone does not change the canonical plan identity.

## Strategy Separation

Execution strategies use the execution-specific `ExecutionStrategy` contract. They do not overload the import-time `WorkflowStrategy` used by `DecisionEngine`.

`ExecutionStrategyResolver` requires exactly one strategy per `ExecutionStepType`. Missing or duplicate strategies fail safely; the resolver never silently chooses the first match.

The current foundation includes a no-write `FoundationExecutionStrategy` only:

- `DRY_RUN` returns `DRY_RUN_OK`
- `EXECUTE` returns `UNSUPPORTED`
- no writer ports are called
- `VendorBillWriter` is not invoked

## Composite Coordination

`ExecutionCoordinator` executes plan steps sequentially. It does not parallelize.

Failure policy:

- `DRY_RUN`: `COLLECT_ALL`, so all steps are evaluated and results are aggregated.
- `EXECUTE`: `FAIL_FAST`, so execution stops at the first failed or unsupported step.

## Durable Runtime

`ExecutionRuntimeService` and `ExecutionRuntimeCoordinator` are the durable runtime entry points for future ERP execution. They persist execution snapshots, steps, checkpoints, and append-only events through application ports implemented by SQLAlchemy adapters.

Canonical execution states:

```text
NEW -> PLANNED -> RUNNING -> WAITING_RETRY -> COMPLETED
                                    |              ^
                                    v              |
                                  FAILED           |
```

`CANCELLED` is a terminal state reachable only through explicit runtime state transition rules. The runtime rejects illegal transitions, does not skip states implicitly, and does not treat retries as background work.

Durable tables:

- `workflow_executions`: execution identity, mode, current state, deterministic idempotency key, safe plan metadata, retry policy, failure summary, and checkpoint
- `workflow_execution_steps`: one row per planned step, with sequence, step type, allocation keys, step state, retry count, and safe result summary
- `workflow_execution_events`: append-only event stream with sequence and safe event data

The event stream records immutable evidence such as `ExecutionCreated`, `PlanningCompleted`, `ExecutionStarted`, `StepStarted`, `StepCompleted`, `StepFailed`, `RetryScheduled`, `ExecutionCompleted`, `ExecutionFailed`, and `ExecutionCancelled`. Repository APIs expose append and history operations only; event update and delete operations are intentionally absent.

One logical runtime transition is persisted atomically by the repository adapter. The application layer prepares a new immutable `ExecutionSnapshot` and one or more `ExecutionEventDraft` values; it does not manage SQLAlchemy sessions, transactions, or event sequence numbers.

For each transition, the SQLAlchemy repository persists in one database transaction:

- execution snapshot state
- affected step state and safe result summary
- checkpoint/current step
- retry count and failure summary when applicable
- one or more append-only events
- `checkpoint.last_event_id` pointing at the final committed event in that transition

Event sequence allocation is repository-owned. The `workflow_executions.next_event_sequence` counter is incremented inside the same transaction, and multiple events in a transition receive consecutive sequence numbers. The coordinator never derives event ordering from history length.

Optimistic runtime concurrency is enforced with `workflow_executions.runtime_version`. `persist_transition` receives the expected version from the snapshot and updates only when the persisted runtime version still matches. Stale concurrent attempts fail safely with a runtime concurrency conflict and do not append events.

## Recovery And Retry

`ExecutionCheckpoint` stores completed step keys, failed step key, current cursor, retry count, and last event id. On restart, the runtime loads the snapshot and checkpoint, then resumes from the current incomplete step instead of replaying completed steps from the beginning.

Retry policy is persisted as policy only:

- `NeverRetry`
- `RetryImmediately`
- `RetryLater`
- `ExponentialBackoff`

The current runtime can mark an execution `WAITING_RETRY` and append `RetryScheduled`. It does not schedule jobs, start workers, run timers, or perform automatic retry execution.

## Safety Boundaries

This foundation does not:

- write to Odoo
- invoke `VendorBillWriter`
- create Vendor Bills
- create Customer Invoices
- create RFQs or Purchase Orders
- create Expenses, Assets, or Subscriptions
- write analytic distribution or profitability data
- acknowledge Odoo Workbench projections
- call live Odoo or Uyumsoft providers
- use AI or fuzzy matching
- run a scheduler or background worker
