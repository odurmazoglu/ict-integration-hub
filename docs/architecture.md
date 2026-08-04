# Architecture

## High-Level Flow

```text
Uyumsoft
  → Connector Layer
  → UBL XML
  → InternalInvoice Domain
  → Deterministic Matching
  → Rule Engine
  → Decision Engine
  → Workflow Strategy
  → Odoo Adapter
  → Odoo Import Workbench and ERP Records
```

## Core Layers

### Connector Layer

Owns provider communication, authentication, invoice listing, download, integrity checks, and production safety. It contains no ERP business rules.

### Domain Layer

Owns immutable invoice DTOs, UBL parsing, validation, and domain exceptions. It does not depend on Odoo, SOAP, HTTP, or persistence.

### Matching and Mapping

Performs deterministic matching only:

- partner by VKN/TCKN
- product by exact identifiers
- tax by exact company, canonical type, and Decimal rate

Name similarity, fuzzy matching, and AI matching are excluded from automatic matching.

### Rule and Decision Layer

The Rule Engine evaluates deterministic company-scoped rules. Complete matches select the direct Vendor Bill workflow; safe business mismatches select Manual Review with structured reasons. Repository, provider, authorization, timeout, transport, mapper, and unexpected dependency failures remain safe application exceptions. The Decision Engine converts rule results and context into a workflow recommendation.

### AI Advisor

Runs only when deterministic rules cannot provide a sufficient decision. It uses company memory and a local model to produce an advisory recommendation. It cannot perform writes.

### Workflow Layer

Executes an approved strategy such as direct Vendor Bill, manual review, existing-PO matching, RFQ/PO creation, expense, asset, subscription, or ignore. The current Manual Review strategy is non-writing and returns review-required results only.

### API Security Context

FastAPI adapters resolve a trusted `RequestContext` before future authenticated routes construct application commands or queries. The Application layer remains unaware of HTTP headers, cookies, sessions, JWTs, OAuth providers, or FastAPI request objects.

The current implementation provides two explicit resolver modes:

- `development_headers`: temporary local/test authentication through IPP headers, disabled by default and forbidden in production.
- `oidc_jwt`: production authentication through standard OIDC discovery, JWKS, and signed bearer JWT validation.

`IPP_AUTH_MODE` selects exactly one resolver. Production runtime validation requires `IPP_AUTH_MODE=oidc_jwt`, valid issuer/audience settings, and HTTPS OIDC endpoints. There is no anonymous fallback.

```mermaid
flowchart TB
    Client[API Client]
    Keycloak[OIDC Provider / Keycloak]
    JWKS[JWKS]
    Resolver[RequestContextResolver]
    OIDC[OidcJwtRequestContextResolver]
    Context[RequestContext]
    Dependency[FastAPI Dependency]
    Routes[Future Workbench Routes]
    UseCases[Application Use Cases]

    Client -->|Authorization Bearer JWT| Dependency
    OIDC -->|OIDC discovery| Keycloak
    OIDC -->|cached signing keys| JWKS
    Resolver --> OIDC
    Dependency --> Resolver
    Resolver --> Context
    Context --> Routes
    Routes --> UseCases
```

### Import Workbench Persistence

Manual Review items can be persisted durably for future Odoo Workbench display. The Application layer owns the immutable `ReviewItem`, `ReviewQueueQuery`, `ReviewDetailQuery`, `ReviewDecisionCommand`, `ReviewDecisionAcknowledgement`, `ReviewQueueReader`, `ReviewItemWriter`, and `ReviewDecisionWriter` contracts. The SQLAlchemy repository is an infrastructure adapter that creates pending review records idempotently, serves company-scoped read-only queue/detail queries, and submits explicit user decisions with optimistic concurrency and idempotency.

Decision submission changes only local Workbench review state. It does not execute the selected workflow, write ERP records, create Vendor Bills, create rules, call AI, or contact Odoo/Uyumsoft.

```mermaid
flowchart TB
    ManualReview[Manual Review Result]
    Creation[Review Item Creation Service]
    Writer[ReviewItemWriter Port]
    Submit[SubmitReviewDecisionUseCase]
    DecisionWriter[ReviewDecisionWriter Port]
    Repository[SQLAlchemy Review Repository]
    ReviewItems[(PostgreSQL workbench_review_items)]
    Decisions[(PostgreSQL workbench_review_decisions)]
    API[Authenticated Workbench REST API]
    Workbench[Odoo Workbench UI - future]
    Reader[ReviewQueueReader Port]

    ManualReview --> Creation
    Creation --> Writer
    Writer --> Repository
    Repository --> ReviewItems
    Repository --> Decisions
    Workbench --> API
    API --> Reader
    API --> Submit
    Reader --> Repository
    Submit --> DecisionWriter
    DecisionWriter --> Repository
```

### Odoo Online Workbench Projection

Odoo 19 Online cannot install custom Python modules. The accepted Workbench UI architecture therefore uses configured Odoo Studio custom models as projections of Hub-owned review items and allocation child rows. ADR-0011 proposed `x_ipp_import_review`; actual Odoo Online deployments may use configured Studio-generated model names. The Application layer defines ERP-neutral `WorkbenchProjection`, `OdooWorkbenchDecisionCandidate`, `ProjectionPublishResult`, `WorkbenchProjectionPublisher`, and `WorkbenchDecisionCandidateReader` contracts. The current Odoo adapter implements read-only candidate ingestion behind `WorkbenchDecisionCandidateReader`; projection publishing and acknowledgement writes remain future work.

The projection does not move decision authority to Odoo. Odoo displays Hub-owned review data and captures explicit candidate decisions. The Hub later reads candidates, validates existing version and idempotency rules, persists accepted decisions in PostgreSQL, and projects acknowledgement status back to Odoo.

```mermaid
flowchart TB
    Store[(Hub PostgreSQL Workbench Tables)]
    Projection[WorkbenchProjection]
    Publisher[WorkbenchProjectionPublisher Port]
    OdooAdapter[Future Odoo Projection Adapter]
    Studio[(Odoo Studio x_ipp_import_review)]
    User[Odoo User]
    Candidate[OdooWorkbenchDecisionCandidate]
    Reader[WorkbenchDecisionCandidateReader Port]
    AllocationRows[Business Context Allocation Child Rows]
    Submit[SubmitReviewDecisionUseCase]
    Ack[ReviewDecisionAcknowledgement]

    Store --> Projection
    Projection --> Publisher
    Publisher --> OdooAdapter
    OdooAdapter --> Studio
    User --> Studio
    Studio --> Candidate
    Studio --> AllocationRows
    Candidate --> Reader
    AllocationRows --> Reader
    Reader --> Submit
    Submit --> Store
    Submit --> Ack
    Ack --> Publisher
```

### ERP Adapter

Odoo implementations translate approved workflow commands into Odoo records. Odoo is not allowed to own cross-ERP business rules.

## User Interaction

The user works in an Odoo Import Workbench. The workbench displays source invoice data, matching status, workflow recommendation, related sales/procurement context, warnings, and available actions.

## Traceability

When relevant, all generated or matched records must retain links sufficient to traverse from sale to procurement and actual cost.

Business Context Allocation contracts extend this traceability from one invoice-level context to multiple allocation lines. They are application contracts only in the current codebase; accepted allocation persistence, Odoo synchronization, customer recharge execution, and profitability posting are future work.

```mermaid
flowchart TB
    VendorInvoice[Incoming Vendor Invoice]
    VendorBill[Vendor Bill Actual Cost]
    Allocations[Business Context Allocation Lines]
    SalesA[Sales Order A]
    SalesB[Sales Order B]
    Project[Project]
    Recharge[Customer Recharge Recipient]
    Internal[Internal Cost]
    Profitability[Actual Profitability Reporting]

    VendorInvoice --> VendorBill
    VendorBill --> Allocations
    Allocations --> SalesA
    Allocations --> SalesB
    Allocations --> Project
    Allocations --> Recharge
    Allocations --> Internal
    SalesA --> Profitability
    SalesB --> Profitability
    Project --> Profitability
    Recharge --> Profitability
    Internal --> Profitability
```

## Safety

- read-only by default where possible
- dry-run before production write
- explicit production approval gates
- idempotent creates
- draft records only unless a separate approved workflow posts them
- sanitized logs
- immutable result DTOs
