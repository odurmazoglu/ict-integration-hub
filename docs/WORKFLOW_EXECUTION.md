# Workflow Execution Foundation

Workflow execution starts only from an accepted, authoritative Hub Workbench decision. It does not start from raw Odoo candidates, Odoo Studio rows, recommendations, or Rule Engine output.

Current flow:

```text
Accepted Workbench Decision
  -> RunAcceptedDecisionExecutionUseCase
  -> AcceptedReviewDecisionReader
  -> ExecutionRequest
  -> ExecutionPlanner
  -> ExecutionPlan
  -> ExecutionRuntimeService.create_or_load
  -> WorkflowExecution / WorkflowExecutionStep / WorkflowExecutionEvent
  -> ExecutionCheckpoint
  -> ExecutionStrategyResolver
  -> ExecutionRuntimeCoordinator
  -> dry-run or supported execution step result values
  -> ExecutionResult
```

This foundation is separate from decision selection. `WorkflowType` remains the high-level review decision vocabulary, while `BusinessContextAllocationType` describes each allocation row's execution purpose. A single accepted decision may therefore become a composite plan with multiple execution steps.

## Identity And Idempotency

`RunAcceptedDecisionExecutionCommand` accepts only `review_id`, `company_id`, accepted decision version, execution mode, and optional explicit execution approval. It does not accept an allocation payload, workflow, execution id, or idempotency key from the caller. The use case reads the canonical persisted Hub decision evidence, then derives the internal `ExecutionRequest`.

`ExecutionRequest` carries `execution_id`, `review_id`, `company_id`, accepted decision version, optional accepted `decision_id`, execution mode, selected workflow, and immutable allocation evidence. In the accepted-decision integration path, `execution_id` is a deterministic runtime identifier derived from `company_id`, `review_id`, accepted decision version, accepted `decision_id` when present, and execution mode. For legacy rows without a `decision_id`, the deterministic identifier uses an explicit `decision-id-absent` fallback. This identifier is not the canonical execution idempotency key.

Execution idempotency is distinct from Workbench decision idempotency and from `execution_id`. The canonical execution idempotency key is owned by `ExecutionPlanner.execution_idempotency_key(...)` and is derived from:

- `company_id`
- `review_id`
- accepted decision version
- execution mode
- canonical execution plan steps
- allocation keys

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

The `FoundationExecutionStrategy` remains the no-write strategy for dry-run planning:

- `DRY_RUN` returns `DRY_RUN_OK`
- `EXECUTE` is unsupported by this strategy
- no writer ports are called
- `VendorBillWriter` is not invoked

`VendorBillExecutionStrategy` is the first production-capable strategy:

- supports only `ExecutionStepType.VENDOR_BILL`
- supports `DRY_RUN` and `EXECUTE`
- requires `ExecutionApproval.approved_by` for `EXECUTE`
- reads authoritative source invoice and deterministic match evidence through `ExecutionSourceInvoiceReader`
- builds only through `VendorBillBuilder`
- writes only through `VendorBillWriter`
- returns a typed `ExecutionArtifact` when a draft Vendor Bill is created or recovered

Workbench `decided_by`, Odoo user identity, and Odoo projection audit fields are not execution approval. The approval acknowledgement needed by the concrete writer remains at the write-policy boundary and is not persisted in runtime events.

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

The event stream records immutable evidence such as `ExecutionCreated`, `PlanningCompleted`, `ExecutionStarted`, `StepStarted`, `StepCompleted`, `StepFailed`, `RetryScheduled`, `ExecutionCompleted`, `ExecutionFailed`, and `ExecutionCancelled`. Transition events are created only inside atomic execution creation or `persist_transition`. Repository APIs expose event history reads only; independent append, event update, and event delete operations are intentionally absent.

One logical runtime transition is persisted atomically by the repository adapter. The application layer prepares a new immutable `ExecutionSnapshot` and one or more `ExecutionEventDraft` values; it does not manage SQLAlchemy sessions, transactions, or event sequence numbers.

The application layer has no independent snapshot, checkpoint, or event mutation API. Runtime mutations are only legal through atomic execution creation or `persist_transition`.

`ExecutionArtifact` is the canonical runtime representation of ERP objects produced by execution. It is immutable, deterministic, and contains only safe artifact identity: artifact type, artifact id, external identity, and whether the current execution created the artifact. It must not contain raw provider payloads, URLs, authentication data, or secrets. Future strategies such as RFQ, Purchase Order, Expense, Fixed Asset, Subscription, and Customer Invoice execution should reuse this same artifact model instead of adding step-specific reference fields.

For each transition, the SQLAlchemy repository persists in one database transaction:

- execution snapshot state
- affected step state and safe result summary
- checkpoint/current step
- retry count and failure summary when applicable
- one or more append-only events
- `checkpoint.last_event_id` pointing at the final committed event in that transition

Event sequence allocation is repository-owned. The `workflow_executions.next_event_sequence` counter is incremented inside the same transaction, and multiple events in a transition receive consecutive sequence numbers. The coordinator never derives event ordering from history length.

Optimistic runtime concurrency is enforced with `workflow_executions.runtime_version`. `persist_transition` receives the expected version from the snapshot and updates only when the persisted runtime version still matches. Stale concurrent attempts fail safely with a runtime concurrency conflict and do not append events.

## Accepted Decision Integration

`RunAcceptedDecisionExecutionUseCase` is the current end-to-end runtime integration. It reads the accepted decision through `AcceptedReviewDecisionReader` by exact `review_id`, `company_id`, and decision version. It never reads raw Odoo projection rows, never infers the latest decision, and never accepts caller-supplied workflow or allocation data.

Execution evidence is intentionally two-stage. Stage 1 pre-decision evidence is persisted in `workbench_review_execution_evidence` before the Workbench review becomes available for human decision. It is pinned by exact `company_id`, `review_id`, and `review_version`, where `review_version` equals the decision command's later `expected_version`. Stage 2 accepted execution evidence is a future decision-time copy pinned to the accepted `decision_id`; that accepted decision version is `expected_version + 1`.

Neither stage may reconstruct evidence from Odoo, Uyumsoft, current ERP master data, Workbench display fields, rematching, fuzzy matching, or AI. Stage 1 is the authoritative source for the later PR #83 `ReviewExecutionEvidenceReader`; Stage 2 is the historical execution snapshot consumed by `ExecutionSourceInvoiceReader`.

For `SELECT_WORKFLOW` decisions, the use case plans the canonical decision evidence, calls `ExecutionRuntimeService.create_or_load`, and delegates runtime mutation to `ExecutionRuntimeCoordinator`, which uses repository-owned atomic `persist_transition` calls. Repeating the same command loads the same runtime and returns the completed result without replaying completed steps.

`DISMISS` decisions return `NOT_EXECUTABLE` and create no runtime rows. `EXECUTE` mode is allowed only after the plan is built, explicit approval is present, and every planned step supports `EXECUTE`. In this slice only a pure `VENDOR_BILL` plan can pass that preflight. Heterogeneous plans, customer recharge, purchase, project cost, expense, asset, subscription, and internal-cost execution are rejected before runtime creation and before any writer call.

## Vendor Bill Execution

Vendor Bill execution bridges the durable runtime to the existing Draft Vendor Bill writer. The strategy constructs a deterministic writer idempotency key from execution identity and the `VENDOR_BILL` step key, then delegates duplicate detection and production gates to `VendorBillWriter` and its concrete Odoo implementation.

On `created` or `existing` writer results, the step result contains exactly one `ExecutionArtifact`:

- `artifact_type`: `VENDOR_BILL`
- `artifact_id`: canonical Odoo `account.move` identifier
- `external_identity`: deterministic writer idempotency identity used for duplicate detection
- `created`: `true` when the writer created the draft, `false` when duplicate lookup returned an existing draft

The strategy itself does not call Odoo, SQLAlchemy, Uyumsoft, AI, fuzzy matching, provider clients, posting, payment, reconciliation, unlink, customer invoice creation, RFQ, Purchase Order creation, workers, or schedulers.

Hub runtime persistence and the Odoo draft write are a distributed write boundary. They cannot be committed atomically in one database transaction. Recovery relies on runtime state, deterministic writer idempotency, and Odoo-side duplicate lookup before draft creation. `ExecutionArtifact` is retained for audit, operator visibility, and replay diagnostics; it is not the recovery mechanism itself. If an Odoo draft is created and the response is lost before Hub records the step result, retrying the same step must use the same writer idempotency key so the writer can return the existing draft instead of creating a duplicate.

Checkpoint consistency remains inside the Hub runtime boundary. Checkpoints are not independently writable; all checkpoint changes occur through `persist_transition`.

`SqlAlchemyExecutionSourceInvoiceReader` is the production persistence adapter for the `ExecutionSourceInvoiceReader` port. It reconstructs execution input only from persisted Hub evidence tied to the accepted decision:

- exact `review_id`
- exact `company_id`
- exact accepted `decision_version`
- exact accepted `decision_id`
- persisted structured `InternalInvoice`
- persisted supplier partner match evidence
- persisted product match evidence
- persisted tax mapping evidence

The reader does not call Odoo, call Uyumsoft, re-download raw documents, rerun matching, rerun tax mapping, use current product names, use current supplier resolution, parse Workbench comments, or infer missing evidence. If evidence is absent, malformed, cross-company, or ambiguous, it fails closed with safe execution-source errors. Production Vendor Bill execute wiring remains disabled unless this full evidence snapshot is available.

For accepted executable Vendor Bill decisions, decision persistence and execution source evidence capture are one Hub database transaction. The SQLAlchemy review repository updates the pending review, inserts the accepted decision, and inserts the `execution_source_invoice_evidence` row linked to the generated decision id in the same transaction. If source evidence is missing, malformed, from another company, linked to another invoice, or conflicts with existing idempotent evidence, the decision is not committed.

Execution source evidence is immutable historical data. It uses schema version `1`, stores Decimal values as strings, stores enum values as stable strings, and enforces one evidence snapshot per accepted decision id. A later review version creates a separate evidence row and never updates the older one. Idempotent replay verifies semantic equality and does not duplicate evidence.

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

- write to Odoo outside the `VendorBillWriter` port
- create non-draft Vendor Bills
- create Customer Invoices
- create RFQs or Purchase Orders
- create Expenses, Assets, or Subscriptions
- write analytic distribution or profitability data
- acknowledge Odoo Workbench projections
- call live Odoo or Uyumsoft providers
- use AI or fuzzy matching
- run a scheduler or background worker
