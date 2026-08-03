# Import Workbench

Import Workbench is the accepted Odoo-side user interface for reviewing import sessions. It lives inside Odoo as UI only and does not contain business logic.

No Odoo Import Workbench UI implementation exists in this repository yet. The repository now provides application-layer contracts and durable review item persistence that a future Odoo Workbench adapter can consume.

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

Odoo must not own:

- workflow selection
- strategy selection
- rule execution
- Manual Review reason creation
- deterministic matching logic
- AI decision making
- procurement traceability policy
- idempotency decisions

## Expected Interaction

```mermaid
sequenceDiagram
    participant User
    participant Workbench as Odoo Import Workbench
    participant Hub as ICT IPP
    participant ERP as Odoo ERP Records

    User->>Workbench: Open import session
    Workbench->>Hub: Fetch session, rule results, recommendations
    Hub-->>Workbench: Review state and allowed actions
    User->>Workbench: Approve reviewed draft action
    Workbench->>Hub: Request approved execution
    Hub->>ERP: Execute through adapter
    Hub-->>Workbench: Execution result
```

## Current Application Contracts

The current implementation defines contracts under `app/application/workbench` and a SQLAlchemy persistence adapter outside the Application layer.

Implemented contract types:

- `ReviewItem`: safe invoice summary for a review-required item
- `ReviewStatus`: canonical states for pending, submitted, resolved, and dismissed review records
- `ReviewQueueQuery` and `ReviewQueueResult`: bounded queue listing contract
- `ReviewDetailQuery`: one-item lookup contract
- `ReviewDecisionCommand`: explicit user decision command for `SELECT_WORKFLOW` or `DISMISS`
- `ReviewDecisionAcknowledgement`: safe acknowledgement contract
- `LineResolution` and `TaxResolution`: explicit selected ERP IDs for invoice lines and taxes
- `BusinessContextDecision`: explicit procurement traceability identifiers selected by the user
- `ReviewQueueReader`: read-only application port for future queue/detail adapters
- `ReviewItemWriter`: create-only application port for idempotent pending review item creation
- `ReviewItemCreationService`: small application service that delegates review item creation through the writer port

## Current Persistence Foundation

The first persistence slice stores Workbench review items in `workbench_review_items`.

Implemented behavior:

- create one `PENDING_REVIEW` item idempotently through `ReviewItemWriter`
- return an existing item when the same company-scoped idempotency key is reused with identical immutable business content
- raise a safe idempotency conflict when the same key is reused for different content
- read one item by `review_id` and `company_id`
- list a company-scoped queue with exact status, workflow, supplier tax number, created-from, and created-to filters
- return paginated results with `total_count`
- persist structured `ManualReviewReason` values as controlled JSON
- persist `total_amount` as `Numeric(24, 6)` to preserve UBL monetary values up to six fractional digits
- compare persisted monetary values through canonical `Decimal` values for idempotency, not display formatting
- store `version` starting at `1` for future optimistic concurrency updates

The persistence adapter does not store raw XML, provider payloads, credentials, tokens, HTTP responses, stack traces, or unsafe provider exception text.

```mermaid
flowchart TB
    ManualReview[Manual Review Result]
    Service[Review Item Creation Service]
    Writer[ReviewItemWriter Port]
    Repository[SQLAlchemy Review Repository]
    Database[(PostgreSQL workbench_review_items)]
    Workbench[Odoo Workbench Adapter - future]
    Reader[ReviewQueueReader Port]

    ManualReview --> Service
    Service --> Writer
    Writer --> Repository
    Repository --> Database
    Workbench --> Reader
    Reader --> Repository
```

Not implemented in this slice:

- Odoo UI
- FastAPI routes
- user decision execution
- ERP writes
- RFQ, Purchase Order, expense, asset, or subscription workflows
- AI recommendations
- attachments or raw XML display

The contracts keep supplier name as display data only. Matching remains deterministic and does not use supplier name, fuzzy text search, AI similarity, or name-only selections.

Recommendation acceptance is intentionally not part of the current contract. A future recommendation contract must include recommendation id, recommendation version, source, and rationale to prevent stale acceptance. Until that exists, users can only submit explicit workflow selections or dismissals. `WorkflowType.MANUAL_REVIEW` is the unresolved state and cannot be selected as a resolution.

```mermaid
flowchart TB
    OdooWorkbench[Odoo Import Workbench UI - future]
    Query[ReviewQueueQuery / ReviewDetailQuery]
    Reader[ReviewQueueReader Port]
    Writer[ReviewItemWriter Port]
    Repository[SQLAlchemy Review Repository]
    Store[(PostgreSQL review item table)]
    Item[ReviewItem]
    Command[ReviewDecisionCommand]
    Ack[ReviewDecisionAcknowledgement]

    OdooWorkbench --> Query
    Query --> Reader
    Writer --> Repository
    Reader --> Repository
    Repository --> Store
    Reader --> Item
    OdooWorkbench --> Command
    Command --> Ack
```

## UI Responsibilities

Future Workbench screens should support:

- session list and detail
- status filters
- rule result display
- missing master-data indicators
- ambiguous match review
- AI recommendation display marked as advisory
- traceability chain display
- action confirmation
- links to created draft ERP documents

## Implementation Requirements

- Business decisions must remain API calls into ICT IPP.
- UI actions must send explicit user intent and confirmation.
- User decisions must include explicit user identity, idempotency key, and expected version.
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
- [ERP Boundary ADR](adr/ADR-0003-erp-boundary.md)
