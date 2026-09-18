# Vendor Bill runtime authorization (P0-PROD-09D1)

The existing Workbench execute API can use an explicit persisted authorization ID
for exactly `(company_id, review_id, EXECUTE_VENDOR_BILL, decision_version)`.
Only one direct Vendor Bill step is eligible. Existing accepted decisions and
immutable Stage-2 evidence are read; they are never recreated. Builder, planner,
strategy, writer, accounting and deterministic execution/writer identities remain
unchanged. Customer invoice/quotation, supplier and product operations receive no
runtime authorization support.

## Gate hierarchy

`PRODUCTION_OPERATIONS_ENABLED=false` always blocks production ERP writes. Runtime
authorization bypasses only `EXECUTION_EXECUTE_ENABLED` for its exact execution.
The existing production approval ACK, named approval, writer policy and source
integrity checks still apply. Other business gates are unaffected. An authorization
must be explicitly supplied; the application never chooses a pending row implicitly
or falls back to another authorization after a denial. Dry-run rejects authorization
IDs. All production defaults remain unchanged.

## API and lifecycle

- `POST /api/workbench/reviews/{review_id}/write-authorizations`: issue a UUID-scoped
  authorization for the current accepted direct Vendor Bill decision and existing
  Stage-2. Body: `decision_version`, optional `operation_type=EXECUTE_VENDOR_BILL`
  and `justification`. TTL is fixed at 15 minutes. Company and issuing actor derive
  from authenticated `RequestContext`; existing `workbench_execute` permission is
  required. Unknown actor/company/ERP payload fields are rejected.
- `GET` on the same path: list records, requiring `workbench_review_read` and a
  company-scoped review. Expiration is visible without a scheduler or DB mutation.
- `POST .../write-authorizations/{authorization_id}/revoke`: requires
  `workbench_execute`; revoke pending or consumed authorization to block further
  recovery. Already-revoked requests preserve the original revocation audit.
- `POST .../execute`: the existing endpoint accepts `authorization_id` alongside
  existing execute-mode/named approval fields. No second execution path is added.

Issuing/revoking use the application UnitOfWork. Claiming locks the exact company-
scoped row with `SELECT FOR UPDATE`, validates scope/current review version/TTL,
and flushes `pending -> consumed` bound to the existing deterministic execution ID.
The row lock remains held until the application commits or rolls back the entire
runtime outcome. Repositories never commit. Audit fields retain issuing actor,
justification, timestamps, first consumption trace/execution, use count, latest
attempt trace/time, and revocation actor/time.

Single-use means **one canonical execution identity**, not an unrecoverable token
that is discarded on an interrupted attempt. A consumed row can only admit an
explicit recovery of that same execution before TTL expiry. Each admission is
counted and audited. TTL is checked on admission and preflight; an already admitted
attempt may finish after expiry. New recovery admissions after expiry fail closed.

## Crash, retry and concurrency

- Crash after consumption but before Odoo write: consumption is uncommitted; the
  dead connection/session rolls it back together with pending runtime state. The
  existing authorization remains pending and can resume within TTL.
- Durable consumed-before-write checkpoint without runtime state: the same bound
  deterministic execution may resume within TTL. A different execution cannot
  take ownership. Recovery uses normal runtime creation/loading and writer lookup.
- Odoo success followed by crash or Hub finalization/commit failure: pending Hub
  consumption/runtime changes roll back, while the remote bill remains possible.
  Reconcile by the unchanged writer identity; explicitly authorized replay uses
  existing duplicate lookup to recover that bill without creating another one.
- Committed `waiting_retry`: consumption and safe diagnostics commit atomically.
  Explicit same-execution recovery may reuse the consumed row within TTL. Existing
  runtime retry limits/terminal states remain unchanged; no automatic retry exists.
- Expiry: require a newly issued authorization for the same existing accepted
  decision/evidence. Its authorization ID does not change execution or writer
  identity, and duplicate recovery still precedes any draft creation.
- PostgreSQL concurrent use of one authorization is serialized by its row lock.
  Separate authorizations for the same execution still use the existing runtime
  unique identity and optimistic transitions. Completed replay returns committed
  artifacts without invoking the writer or consuming authorization again.

Hub/Odoo atomicity is not claimed. Operator reconciliation remains required when
remote outcome is uncertain. No production execution, gate opening, provider write,
posting, scheduler, new supplier/product authorization or compensating cleanup is
part of this change.

## Migration and rollback

Migration `202607170028` adds only `workbench_review_write_authorizations`, with
scope/lifecycle constraints and audit indexes. Downgrade to `202607170027` removes
only that table and its audit history; it does not undo ERP effects. Roll back code
and schema together after separately reviewing any outstanding authorizations.

Tests use mocked ERP and isolated SQLite/PostgreSQL databases. The opt-in
`TEST_EXECUTION_TRANSACTION_DATABASE_URL` is guarded to a local `db`/loopback host
and the exact dedicated database `ict_execution_transaction_test`; its contents
are recreated for each test and must never contain business data.
