# ADR-0013: Odoo Online Workbench Operator Requests (extends ADR-0011)

- Status: Accepted
- Date: 2026-10-06
- Amends: [ADR-0011](ADR-0011-odoo-online-import-workbench-projection.md) (extends, does not supersede)

## Context

ADR-0011 made the Odoo Studio model `IPP Import Workbench` a *projection* of Hub-owned
review state and allowed exactly one inbound path: the Hub reads explicit decision
candidates from the projection, validates them, persists them, and acknowledges Odoo.

Operating a review end to end needs more than a decision. Before a review can be decided
an operator must, depending on the review, resolve the supplier, state the purchase
purpose, and choose an accounting treatment (expense account or fixed-asset
capitalization). After the decision a direct Vendor Bill must be executed. Today all of
those steps exist only as authenticated Hub REST endpoints, so operators leave Odoo and
use an API client.

Constraints confirmed for production (Odoo Online / saas~19.3):

- No custom Python addon, no `TransientModel`, no server-side wizard code can be deployed.
- Odoo must not hold Hub credentials. Browser JavaScript must not call the Hub.
- Studio automated actions must not implement business rules.
- ADR-0011's rejected alternatives remain rejected (direct Odoo -> Hub calls, Keycloak
  secrets in Studio fields, Odoo as decision ledger).

## Decision

Extend the existing Hub-pull pattern from "decision candidates" to **typed operator
requests**.

```text
Odoo Studio operator request (typed fields on the Workbench row)
        -> request flag set by a Studio button (data capture only)
        -> Hub scheduled ingestion (poller process tick)
        -> existing Hub use case (unchanged)
        -> authoritative Hub state (Hub PostgreSQL)
        -> Workbench projection refresh (OPS-UI-01A synchronizer)
        -> Hub request result written back, flag cleared
        -> Odoo shows the result and the next required action
```

### Source-of-truth boundary

| Data | Owner |
| --- | --- |
| Review lifecycle, version, reasons, resolutions, decisions, authorizations, executions | Hub PostgreSQL |
| Operator guidance (next action, to-do text, completed summary, eligible asset accounts) | Hub, projected to Odoo |
| Pending operator request (action, snapshotted version, action values, requester) | Odoo row, untrusted input until the Hub validates it |
| Request result (outcome, message, processed at) | Hub, projected to Odoo |
| Request ledger (identity, outcome, issued authorization) | Hub PostgreSQL (`workbench_operator_requests`) |

Projected authoritative fields are never made writable. Request fields are a separate,
explicitly Odoo-owned group. The projection publisher never writes request fields, and
the request acknowledger never writes authoritative projection fields.

### Request schema (typed, smallest practical)

One request at a time per review row. Typed fields instead of one JSON command:

- action type (selection): supplier resolution, purchase purpose, accounting resolution,
  decision, Vendor Bill execution
- expected review version (integer, snapshotted from the projected version when the
  operator presses **İşleme Gönder**)
- requested by (`res.users`) and requested at (datetime), set by the same button
- request ready flag
- action values: supplier mode, partner, purchase purpose, treatment type, expense
  account, expense category, asset account, depreciation model, note
- decision values reuse the existing decision fields and allocation child rows
  (ADR-0011 / ADR-0012). No duplicate decision schema is introduced.

The button is a Studio server action that only copies values (`expected version :=
projected version`, `requested by := current user`, `requested at := now`, `ready :=
true`, previous result cleared). It contains no business rule.

### Hub ingestion

`OperatorRequestIngestionWorkflow` is an adapter. For each ready row it:

1. parses the typed request (malformed input is rejected, never guessed);
2. derives a deterministic request key (hash of company, review, Odoo row, action,
   expected version, values, requester, requested at);
3. consults the Hub request ledger (terminal ledger rows are re-acknowledged, never
   re-executed);
4. authorizes the requester through a Hub-side actor directory (Odoo user id -> Hub actor
   name and existing `Permission` values). Odoo identity alone is never sufficient;
5. maps the request to exactly one existing use case and invokes it unchanged:
   `ResolveWorkbenchSupplierUseCase`, `SubmitPurchasePurposeUseCase`,
   `SubmitReviewAccountingResolutionUseCase`, `SubmitReviewDecisionUseCase` (through the
   existing decision candidate reader), `CreateWriteAuthorizationUseCase` +
   `WorkbenchAcceptedDecisionExecutionDispatcher`;
6. refreshes the projection through the canonical synchronizer;
7. writes the request result and clears the ready flag only if the row still carries the
   same request (a newer submission is never cleared).

No business rule is reimplemented in the adapter. Eligibility, write gates, the
production kill switch, named-approver checks, frozen evidence and idempotency stay in
the use cases.

### Optimistic concurrency

The request carries the version the operator saw. The adapter passes it unchanged as
`expected_version` / `decision_version`. A request is never reinterpreted against a newer
version. When the Hub has moved on, the existing use cases raise a version conflict and
the adapter reports:

> Bu inceleme siz işlem yaparken değişti. Güncel bilgiler yüklendi; lütfen işlemi tekrar
> kontrol edin.

and refreshes the projection.

### Idempotency and crash recovery

- Every target use case is already replay-safe for an identical command:
  supplier/purpose/accounting use cases `_resume_if_already_applied` when the review has
  advanced past `expected_version` with the same persisted resolution, decisions replay
  by idempotency key, and execution replays `ALREADY_EXECUTED` from the stored snapshot.
- The ledger row (`in_progress`) is committed before the use case runs and finished
  (`completed`, `stale`, `rejected`, `failed`) after it returns.
- Crash after the Hub commit but before the ledger finish: the next tick finds
  `in_progress`, re-invokes the identical command, the use case resumes
  (`already_applied`), the ledger is finished, Odoo is acknowledged.
- Crash after the ledger finish but before the Odoo acknowledgement: the next tick finds
  a terminal ledger row and only re-acknowledges.
- Vendor Bill execution checks the runtime's own replay identity first
  (`accepted_decision_execution_id` + stored snapshot): when the exact accepted decision
  already completed, no authorization is issued and the dispatcher's stored
  `ALREADY_EXECUTED` replay is returned. A genuinely new execution still requires the
  narrow authorization.
- Write authorizations are not idempotent by themselves; the adapter stores the issued
  authorization id on the ledger row and reuses it on resume, so a successfully consumed
  request never issues a second authorization. The only residual window (crash between
  the authorization commit and the ledger update) can leave one unused authorization that
  expires after 15 minutes; it can never authorize a second write.

### Scheduler ownership

The existing Hub poller process (`app.workers.uyumsoft_inbound_poller`) owns the tick.
It gains a second, independent periodic task with its own enable flag
(`ODOO_WORKBENCH_OPERATOR_REQUESTS_ENABLED`, default `false`), interval
(`ODOO_WORKBENCH_OPERATOR_REQUESTS_INTERVAL_SECONDS`, default 60) and PostgreSQL advisory
lock. No Odoo cron runs business logic and no second scheduler process is added.

### Security

- The Hub authenticates to Odoo with the existing restricted JSON-2 key.
- No Hub credential, token or endpoint is exposed to Odoo or the browser.
- Requests are accepted only from Odoo users mapped in `ODOO_OPERATOR_REQUEST_ACTORS`;
  each action requires the same `Permission` its REST endpoint requires
  (`workbench_review_decide` for supplier/purpose/accounting/decision,
  `workbench_execute` for execution and for the authorizations a write needs).
- Write gates (`EXECUTION_EXECUTE_ENABLED`, `SUPPLIER_REMEDIATION_WRITE_ENABLED`,
  `PRODUCTION_OPERATIONS_ENABLED`) are unchanged. Where a gate is closed the adapter uses
  the existing narrow single-use write authorization, exactly as an operator would via
  the API.
- Company scope is taken from the Hub tick configuration and checked against the row.

## Consequences

Positive:

- Operators work entirely in Odoo; Hub remains the only workflow authority.
- No new business logic, no change to existing domain contracts or endpoints.
- Concurrency, idempotency and audit are Hub-owned and tested.

Trade-offs:

- Eventual consistency: a request completes on the next tick (default 60 s). The UI shows
  "İşlem gönderildi" while pending and never pretends to be synchronous.
- The completed summary shows account and depreciation-model names read through the
  existing read-only account/model reference port; when Odoo cannot answer it shows a
  neutral "okunamadı" text, never a raw id (ids stay in the technical view).
- Studio fields, views and the submit button need controlled manual setup
  (provisioning plan in `docs/ODOO_WORKBENCH_OPERATOR_UI.md`).
- `requested by` is set by a Studio button; a user with raw JSON-2 write access to the
  row could forge it. The actor directory limits accepted identities, and Odoo ACLs must
  restrict who can write request fields.

## Alternatives considered

- **Custom Odoo addon / wizard (TransientModel).** Impossible on Odoo Online.
- **Studio button calling the Hub REST API.** Needs Hub credentials in Odoo; rejected by
  ADR-0011.
- **Browser JavaScript calling the Hub.** Exposes credentials; rejected by ADR-0011.
- **Making projected fields writable.** Blurs ownership; the Hub could not tell operator
  intent from stale projection data.
- **One generic JSON command field.** Unvalidatable in Studio, error-prone for operators.
- **Odoo cron triggering the Hub.** Needs credentials in Odoo and moves scheduling into the
  ERP.
- **A separate scheduler process.** Unnecessary; the poller already provides a
  single-flight, Hub-owned loop.

## Relationship to ADR-0011

This ADR keeps ADR-0011's core principle: Odoo is the user interface and projection
store; the Hub is the only decision authority and ledger. It widens the set of explicit,
Hub-validated operator inputs read from the projection, from "decision" to the typed
request actions above. Every ADR-0011 rejected alternative stays rejected. The ADR-0011
safety boundary still applies: the only Odoo writes added here target the dedicated
Workbench projection model; ERP writes (supplier partner, Vendor Bill) happen only inside
existing use cases under their existing gates.
