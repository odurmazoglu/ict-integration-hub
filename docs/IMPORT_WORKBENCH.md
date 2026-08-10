# Import Workbench

Import Workbench is the accepted Odoo-side user interface for reviewing import sessions. It lives inside Odoo as UI only and does not contain business logic.

Odoo 19 Online cannot install custom Python modules. The accepted Odoo Online architecture is an Odoo Studio projection model, synchronized by the Hub through JSON-2. This repository includes a production-safe, read-only Odoo candidate reader for configured Studio projection models. It does not create the Studio models, views, ACLs, scheduler, acknowledgement projection, or Odoo UI implementation.

The repository now provides application-layer contracts, durable review item persistence, queue/detail query use cases, explicit review decision submission persistence, authenticated REST API adapters for direct Hub clients, ERP-neutral projection contracts, and a read-only Odoo JSON-2 adapter that can read ready decision candidates plus allocation child rows from configured Odoo Studio models.

## Purpose

The Import Workbench should give users a practical review surface for:

- imported invoices and documents
- deterministic rule outcomes
- missing or ambiguous matches
- procurement traceability links
- AI Advisor recommendations
- approved user actions
- ERP execution results

## Boundary

Import Workbench lives inside Odoo, but business logic does not.

Odoo may display:

- Import Session status
- source invoice metadata
- matching results
- structured Manual Review reasons, rule failures, and warnings
- advisory AI explanations
- links to draft vendor bills
- required user-review actions
- Hub acknowledgement status projected after future decision processing

Odoo must not own:

- workflow selection
- strategy selection
- rule execution
- Manual Review reason creation
- deterministic matching logic
- AI decision making
- procurement traceability policy
- idempotency decisions
- accepted decision ledger

## Expected Interaction

```mermaid
sequenceDiagram
    participant User
    participant Projection as Odoo Studio Projection
    participant Hub as ICT IPP

    Hub->>Projection: Publish WorkbenchProjection through future JSON-2 adapter
    User->>Projection: Review Hub-owned fields
    User->>Projection: Enter explicit decision and set decision_ready
    Hub->>Projection: Read ready OdooWorkbenchDecisionCandidate
    Hub->>Hub: Validate version, idempotency, company, and workflow contract
    Hub->>Projection: Project safe acknowledgement result
```

## Current Application Contracts

The current implementation defines contracts under `app/application/workbench` and a SQLAlchemy persistence adapter outside the Application layer.

Future API or Odoo adapter routes must resolve `RequestContext` before constructing these contracts. `company_id` must come from `RequestContext.company_id`, and future decision submission routes must derive `decided_by` from `RequestContext.user_id`.

Implemented contract types:

- `ReviewItem`: safe invoice summary for a review-required item
- `ReviewStatus`: canonical states for pending, submitted, resolved, and dismissed review records
- `ReviewQueueQuery` and `ReviewQueueResult`: bounded queue listing contract
- `ReviewDetailQuery`: one-item lookup contract
- `ReviewDecisionCommand`: explicit user decision command for `SELECT_WORKFLOW` or `DISMISS`
- `ReviewDecisionAcknowledgement`: safe acknowledgement contract
- `LineResolution` and `TaxResolution`: explicit selected ERP IDs for invoice lines and taxes
- `BusinessContextDecision`: legacy single-context procurement traceability evidence retained for historical rows
- `BusinessContextAllocationType`, `AllocationCompleteness`, `BusinessContextAllocation`, and `BusinessContextAllocationSet`: canonical multi-allocation business context contracts
- `ReviewQueueReader`: read-only application port for future queue/detail adapters
- `ReviewItemWriter`: create-only application port for idempotent pending review item creation
- `ReviewDecisionWriter`: decision submission application port for explicit user decisions
- `ReviewItemCreationService`: small application service that delegates review item creation through the writer port
- `ListReviewQueueUseCase`: application query boundary for listing review items through `ReviewQueueReader`
- `GetReviewItemUseCase`: application query boundary for retrieving one company-scoped review item through `ReviewQueueReader`
- `SubmitReviewDecisionUseCase`: application command boundary for submitting `ReviewDecisionCommand` through `ReviewDecisionWriter`
- `WorkbenchProjection`: ERP-neutral projection of one Hub-owned review item for a future UI projection store
- `OdooWorkbenchDecisionCandidate`: candidate decision read from the future Odoo Studio projection before Hub acceptance
- `ProjectionPublishResult`: safe result for future projection publish or acknowledgement writes
- `WorkbenchProjectionPublisher`: application port for future projection publishing and acknowledgement
- `WorkbenchDecisionCandidateReader`: application port for future candidate decision reads

`ReviewDecisionCommand`, the authenticated Workbench decision API, decision persistence, idempotency comparison, and `OdooWorkbenchDecisionCandidate` use `business_context_allocations: BusinessContextAllocationSet | None`. The active write path no longer accepts legacy `business_context`, and it does not accept both fields. Existing historical decision rows with legacy `business_context` are not rewritten; the legacy object remains legacy evidence because lossless allocation conversion would require amounts that were never captured.

## Current Persistence Foundation

The persistence foundation stores Workbench review items in `workbench_review_items` and append-only user decisions in `workbench_review_decisions`.

Implemented behavior:

- create one `PENDING_REVIEW` item idempotently through `ReviewItemWriter`
- list the review queue through `ListReviewQueueUseCase` and `ReviewQueueReader`
- retrieve one review item through `GetReviewItemUseCase` and `ReviewQueueReader`
- submit an explicit `SELECT_WORKFLOW` or `DISMISS` decision through `SubmitReviewDecisionUseCase` and `ReviewDecisionWriter`
- transition `PENDING_REVIEW` to `DECISION_SUBMITTED` or `DISMISSED`
- increment `version` exactly once on first accepted decision submission
- enforce optimistic concurrency with `expected_version`
- replay identical decision commands idempotently by company-scoped idempotency key
- reject conflicting decision idempotency-key reuse without mutating the review item
- return an existing item when the same company-scoped idempotency key is reused with identical immutable business content
- raise a safe idempotency conflict when the same key is reused for different content
- read one item by `review_id` and `company_id`
- list a company-scoped queue with exact status, workflow, supplier tax number, created-from, and created-to filters
- return paginated results with `total_count`
- persist structured `ManualReviewReason` values as controlled JSON
- persist `total_amount` as `Numeric(24, 6)` to preserve UBL monetary values up to six fractional digits
- compare persisted monetary values through canonical `Decimal` values for idempotency, not display formatting
- store `version` starting at `1` for future optimistic concurrency updates
- preserve original review reasons when a decision is submitted
- persist selected workflow, partner, line resolutions, tax resolutions, business context allocation evidence, comment, user identity, idempotency key, and version-before/version-after as controlled audit data
- serialize allocation evidence as deterministic JSON with enum values as strings, Decimal values as canonical strings, and allocation rows sorted by `allocation_key`
- include complete allocation payloads in decision idempotency while treating equivalent Decimal forms and list/tuple hydration differences as identical

The persistence adapter does not store raw XML, provider payloads, credentials, tokens, HTTP responses, stack traces, or unsafe provider exception text.

```mermaid
flowchart TB
    ManualReview[Manual Review Result]
    Service[Review Item Creation Service]
    Writer[ReviewItemWriter Port]
    Repository[SQLAlchemy Review Repository]
    ReviewItems[(PostgreSQL workbench_review_items)]
    Decisions[(PostgreSQL workbench_review_decisions)]
    ProjectionAdapter[Future Projection Adapter]
    Reader[ReviewQueueReader Port]
    Submit[SubmitReviewDecisionUseCase]
    DecisionWriter[ReviewDecisionWriter Port]

    ManualReview --> Service
    Service --> Writer
    Writer --> Repository
    Repository --> ReviewItems
    Repository --> Decisions
    ProjectionAdapter --> Reader
    Reader --> Repository
    ProjectionAdapter --> Submit
    Submit --> DecisionWriter
    DecisionWriter --> Repository
```

Implemented REST API endpoints:

- `GET /api/workbench/reviews`
- `GET /api/workbench/reviews/{review_id}`
- `POST /api/workbench/reviews/{review_id}/decision`

Permissions:

- queue and detail reads require `workbench_review_read`
- decision submission requires `workbench_review_decide`

Company isolation and user identity:

- `company_id` always comes from `RequestContext.company_id`
- `decided_by` always comes from `RequestContext.user_id`
- neither field is accepted from query parameters, path parameters, request bodies, or custom Workbench headers
- detail reads always use both `review_id` and trusted `company_id`, so reviews from another company are returned as not found

Response shape:

```json
{
  "success": true,
  "data": {},
  "warnings": [],
  "errors": [],
  "trace_id": "trace-123"
}
```

Errors use the same envelope with `success=false`, `data=null`, and safe error codes/messages. `X-Trace-ID` is also returned in the response header. Workbench API responses do not expose ORM objects, tokens, SQL, connection strings, provider responses, or stack traces. Monetary values are serialized as strings.

Decision submission may include `business_context_allocations` for `SELECT_WORKFLOW` decisions:

```json
{
  "decision": "select_workflow",
  "selected_workflow": "vendor_bill",
  "expected_version": 4,
  "idempotency_key": "example-key",
  "comment": "Reviewed",
  "business_context_allocations": {
    "completeness": "complete",
    "invoice_total": "100000.000000",
    "currency": "TRY",
    "allocations": [
      {
        "allocation_key": "ALLOC-001",
        "allocation_type": "sales_order_cost",
        "source_line_number": "1",
        "amount": "40000.000000",
        "percentage": "40",
        "currency": "TRY",
        "customer_id": 101,
        "recharge_partner_id": 105,
        "customer_invoice_id": 9001,
        "sales_order_id": 301
      },
      {
        "allocation_key": "ALLOC-002",
        "allocation_type": "internal_cost",
        "amount": "60000.000000",
        "percentage": "60",
        "currency": "TRY"
      }
    ]
  }
}
```

JSON floating-point values are rejected for allocation Decimal fields. Clients should send monetary and percentage values as strings. `company_id` and `decided_by` are never accepted from the client payload.

Not implemented in this slice:

- Odoo UI
- Odoo Studio model creation
- Odoo Studio views and ACLs
- Odoo JSON-2 projection publishing
- Hub acknowledgement projection to Odoo
- user decision execution
- workflow execution
- ERP writes
- RFQ, Purchase Order, expense, asset, or subscription workflows
- business context allocation execution, Odoo synchronization, or acknowledgement
- AI recommendations
- attachments or raw XML display

The contracts keep supplier name as display data only. Matching remains deterministic and does not use supplier name, fuzzy text search, AI similarity, or name-only selections.

Recommendation acceptance is intentionally not part of the current contract. A future recommendation contract must include recommendation id, recommendation version, source, and rationale to prevent stale acceptance. Until that exists, users can only submit explicit workflow selections or dismissals. `WorkflowType.MANUAL_REVIEW` is the unresolved state and cannot be selected as a resolution.

```mermaid
flowchart TB
    OdooWorkbench[Odoo Import Workbench UI - future]
    Query[ReviewQueueQuery / ReviewDetailQuery]
    ListUseCase[ListReviewQueueUseCase]
    GetUseCase[GetReviewItemUseCase]
    Reader[ReviewQueueReader Port]
    Writer[ReviewItemWriter Port]
    Submit[SubmitReviewDecisionUseCase]
    DecisionWriter[ReviewDecisionWriter Port]
    Repository[SQLAlchemy Review Repository]
    ReviewItems[(PostgreSQL workbench_review_items)]
    Decisions[(PostgreSQL workbench_review_decisions)]
    Item[ReviewItem]
    Command[ReviewDecisionCommand]
    Ack[ReviewDecisionAcknowledgement]

    OdooWorkbench --> Query
    Query --> ListUseCase
    Query --> GetUseCase
    ListUseCase --> Reader
    GetUseCase --> Reader
    Writer --> Repository
    Reader --> Repository
    Submit --> DecisionWriter
    DecisionWriter --> Repository
    Repository --> ReviewItems
    Repository --> Decisions
    Reader --> Item
    OdooWorkbench --> Command
    Command --> Submit
    Submit --> Ack
```

## Odoo Online Projection

The future Odoo Workbench UI uses a configured Studio model as a projection store. ADR-0011 proposed `x_ipp_import_review`; current deployments may use Studio-generated technical names such as `x_ipp_import_workbench` for the parent projection and `x_ipp_review_allocatio` for child allocation rows. Hub PostgreSQL remains authoritative for review lifecycle, review version, accepted decisions, decision idempotency, acknowledgement, and execution state. The Odoo projection is authoritative only for the user-entered candidate decision before Hub acceptance and for the Odoo user identity captured as audit evidence.

Field ownership is explicit:

- Hub-owned fields: review identity, company identity, invoice display data, review reasons, warnings, Hub status/version, synchronization metadata, and acknowledgement result.
- Odoo-user-owned fields: explicit decision, selected workflow, selected partner, line resolutions, tax resolutions, business context allocation candidates, comment, and decision-ready flag.
- System-derived Odoo fields: Odoo user id and decision timestamp when safely available.

Odoo users must not edit Hub-owned identity or version fields. The Hub must not silently overwrite submitted user-decision fields. A decision candidate is read only when the configured decision-ready field is explicitly `true`; `false` is treated as no ready candidate, and a missing or malformed readiness value is a safe data error.

Business Context Allocation child lines allow one supplier invoice to be split across multiple Sales Orders, commercial customers, recharge recipients, target companies, projects, and internal costs. Candidate allocation lines from Odoo are untrusted until Hub validation. The read-only Odoo candidate reader maps child rows into `business_context_allocations` but does not persist the decision, execute the selected workflow, or write acknowledgement fields. A Studio-only Department field remains ignored until explicitly accepted by a future ADR or PR.

## Odoo Candidate Reader

`OdooWorkbenchDecisionCandidateReader` implements the `WorkbenchDecisionCandidateReader` port through read-only Odoo JSON-2 `search_read` calls. It reads one decision-ready parent projection record and its child allocation records into immutable application DTOs.

Read operations:

- parent lookup: `search_read` on the configured parent Studio model
- child lookup: `search_read_all` on the configured allocation child Studio model
- no Odoo `create`, `write`, `unlink`, `action_post`, payment, reconciliation, customer invoice creation, RFQ, Purchase Order, or workflow execution calls

Lookup domains:

```text
parent: [[review_id_field, "=", review_id], [company_id_field, "=", company_id]]
child:  [[parent_many2one_field, "=", parent_odoo_record_id]]
```

The parent lookup uses `limit=2` so duplicate Odoo projection rows for the same company-scoped review id are detected as ambiguity instead of silently selecting a record.

Mapping is deployment configuration, not application contract. `OdooWorkbenchFieldMapping` groups parent and allocation field mappings so Studio-generated technical names remain outside the Application layer.

| Logical value | Configured mapping |
| --- | --- |
| Parent model | `ODOO_WORKBENCH_PARENT_MODEL` |
| Child allocation model | `ODOO_WORKBENCH_ALLOCATION_MODEL` |
| Review id | `ODOO_WORKBENCH_PARENT_REVIEW_ID_FIELD` |
| Company id | `ODOO_WORKBENCH_PARENT_COMPANY_ID_FIELD` |
| Version | `ODOO_WORKBENCH_PARENT_EXPECTED_VERSION_FIELD` |
| Decision ready | `ODOO_WORKBENCH_PARENT_DECISION_READY_FIELD` |
| Decision | `ODOO_WORKBENCH_PARENT_DECISION_FIELD` |
| Selected workflow | `ODOO_WORKBENCH_PARENT_SELECTED_WORKFLOW_FIELD` |
| Selected partner | `ODOO_WORKBENCH_PARENT_SELECTED_PARTNER_FIELD` |
| Decision idempotency key | `ODOO_WORKBENCH_PARENT_IDEMPOTENCY_KEY_FIELD` |
| Decided by Odoo user | `ODOO_WORKBENCH_PARENT_DECIDED_BY_FIELD` |
| Decided at | `ODOO_WORKBENCH_PARENT_DECIDED_AT_FIELD` |
| Invoice total | `ODOO_WORKBENCH_PARENT_INVOICE_TOTAL_FIELD` |
| Currency | `ODOO_WORKBENCH_PARENT_CURRENCY_FIELD` |
| Allocation completeness | `ODOO_WORKBENCH_PARENT_ALLOCATION_COMPLETENESS_FIELD` or configured fixed completeness |
| Allocation parent link | `ODOO_WORKBENCH_ALLOCATION_PARENT_MANY2ONE_FIELD` |
| Allocation key/type/amount/percentage | `ODOO_WORKBENCH_ALLOCATION_*_FIELD` |
| Customer invoice | optional `ODOO_WORKBENCH_ALLOCATION_CUSTOMER_INVOICE_FIELD` |

Monetary and percentage values are parsed into `Decimal` without float arithmetic. Equivalent textual forms such as `259.2000` and `259.20` remain the same business value inside the immutable allocation contract; invalid, infinite, or boolean numeric values are rejected with safe errors. The reader does not infer allocation completeness from totals: completeness must come from an explicit mapped parent field or a configured fixed completeness value.

`customer_invoice_id` is optional evidence for an existing outgoing customer invoice or refund. If its field mapping is absent, the reader sets it to `None`. If it is present, the value is parsed from a Many2one or integer identifier only; the reader does not validate move type and does not create customer invoices.

See [Odoo Workbench Projection](ODOO_WORKBENCH_PROJECTION.md) and [ADR-0011](adr/ADR-0011-odoo-online-import-workbench-projection.md).

## Decision Submission

Decision submission persists explicit user intent only. It does not execute the selected workflow.

```mermaid
sequenceDiagram
    participant Adapter as Decision Adapter
    participant UseCase as SubmitReviewDecisionUseCase
    participant Port as ReviewDecisionWriter
    participant Repo as SQLAlchemy Review Repository
    participant Items as workbench_review_items
    participant Decisions as workbench_review_decisions

    Adapter->>UseCase: ReviewDecisionCommand(expected_version, idempotency_key)
    UseCase->>Port: submit_review_decision(command)
    Port->>Repo: submit_review_decision(command)
    Repo->>Decisions: find by company_id + idempotency_key
    alt identical replay
        Repo-->>UseCase: original ReviewDecisionAcknowledgement
    else first submission
        Repo->>Items: atomic status/version update for pending expected_version
        Repo->>Decisions: append decision audit row
        Repo-->>UseCase: ReviewDecisionAcknowledgement
    else stale or invalid state
        Repo-->>UseCase: safe conflict exception
    end
```

`SELECT_WORKFLOW` stores the selected canonical workflow and any explicit partner, line, tax, or traceability choices. It moves the review item to `DECISION_SUBMITTED` and leaves execution to a future approved workflow slice.

Future `SELECT_WORKFLOW` decisions may carry `BusinessContextAllocationSet` evidence. Future `DISMISS` decisions must reject allocations. Allocation requirements will depend on selected workflow and future execution strategy.

## Odoo Candidate Submission Orchestration

The Odoo Workbench decision submission orchestrator is an application-layer bridge from the read-only Odoo candidate reader to `SubmitReviewDecisionUseCase`:

```text
Odoo Studio
  -> OdooWorkbenchDecisionCandidateReader
  -> OdooWorkbenchDecisionCandidate
  -> WorkbenchErpReferenceValidator
  -> SubmitOdooWorkbenchCandidateUseCase
  -> ReviewDecisionCommand
  -> SubmitReviewDecisionUseCase
  -> append-only Hub decision evidence
```

It reads one decision-ready candidate for an exact `review_id` and requested `company_id`. If the candidate is not ready or cannot be found, the result is `NOT_READY_OR_NOT_FOUND` and no Hub decision is submitted. If a candidate is returned for a different company, orchestration fails safely and the company scope is not rewritten.

The orchestrator preserves `expected_version` and `idempotency_key` from the Odoo candidate. The Odoo user id is retained only as audit evidence in `decided_by`; it is not authorization. Stale-version and idempotency conflicts from Hub decision submission are not reread or retried.

ERP reference validation runs before Hub decision evidence is accepted. It proves supported references exist, match requested company scope where applicable, have expected types, and satisfy focused exact-ID relationships. It does not persist validation results, cache decisions, infer missing references, or auto-correct invalid choices.

This slice does not write acknowledgement fields to Odoo, clear readiness, update projection version, execute workflows, create Vendor Bills, create customer invoices, create RFQs or Purchase Orders, perform profitability posting, infer allocations, or use AI/fuzzy matching. Hub-to-Odoo acknowledgement projection remains a future focused slice.

Accepted decisions may now be planned for workflow execution through the no-write execution foundation. Planning starts only after Hub decision acceptance and ERP reference validation. It produces deterministic dry-run execution plans and does not invoke writer ports or acknowledge Odoo projections.

## ERP Reference Validation Policy

Validation is read-only and deterministic:

- Partners: `customer_id` and `recharge_partner_id` must exist. Shared partners with no `company_id` are accepted; explicit wrong-company partners are rejected. Display names are never identity.
- Target Company: `target_company_id` must exist when supplied, but it is business context only and does not authorize intercompany execution or override requested `company_id`.
- Sales Order: `sales_order_id` must exist in requested company. If `customer_id` is supplied, the Sales Order partner must match exactly. Commercial-partner normalization is deferred until a deterministic resolver exists.
- Sales Order Line: supported `sale.order.line` references must exist. If `sales_order_id` is also supplied, the line must belong to that order.
- Purchase Order: `purchase_order_id` must exist in requested company.
- Customer Invoice: `customer_invoice_id` must be an `account.move` with `move_type` `out_invoice` or `out_refund`, in requested company. Its partner must match `recharge_partner_id` when supplied, otherwise `customer_id` when supplied.
- Opportunity: `opportunity_id` must exist, match requested company when its company field is set, and match `customer_id` when both have partner IDs.
- Analytic Account: `analytic_account_id` must exist. Shared accounts with empty company are accepted; wrong-company accounts are rejected.
- Project and Subscription: non-null references fail safely as unsupported until exact model/repository support is introduced.

`DISMISS` stores the dismissal decision, moves the review item to `DISMISSED`, and stores no workflow-specific selections.

## UI Responsibilities

Future Workbench screens should support:

- session list and detail
- status filters
- rule result display
- missing master-data indicators
- ambiguous match review
- AI recommendation display marked as advisory
- traceability chain display
- business context allocation line review
- action confirmation
- links to created draft ERP documents

## Implementation Requirements

- Business decisions must remain API calls into ICT IPP.
- UI actions must send explicit user intent and confirmation.
- User decisions must include explicit user identity, idempotency key, and expected version.
- API adapters must derive trusted user identity and company identity from `RequestContext`.
- `company_id` must not be trusted from request bodies, query strings, or path parameters.
- Future decision adapters must derive `decided_by` from `RequestContext.user_id`.
- Pending review item creation must be idempotent by company-scoped key.
- Detail reads must always include both `review_id` and `company_id`.
- Current explicit decisions are `SELECT_WORKFLOW` and `DISMISS`.
- `MANUAL_REVIEW` must not be submitted as a selected resolution workflow.
- Procurement traceability fields must be explicit user choices, not inferred by Odoo UI logic.
- The Hub must revalidate rules before execution.
- Workbench must display AI recommendations as advisory.
- Workbench must not call Odoo posting, unlink, payment, or reconciliation actions as part of import automation.

## Related Documents

- [Import Session](IMPORT_SESSION.md)
- [Workflows](WORKFLOWS.md)
- [Security](SECURITY.md)
- [ERP Boundary ADR](adr/ADR-0003-erp-boundary.md)
