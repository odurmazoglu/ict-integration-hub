# Roadmap

## Completed Foundation

- Uyumsoft SOAP connector
- production-safe read-only smoke validation
- invoice listing and download
- document storage and SHA256 idempotency
- UBL parser and immutable InternalInvoice domain
- deterministic tax mapping
- read-only ERP repository layer with Odoo adapter
- deterministic partner and product matching
- pure Vendor Bill builder and account.move payload generation
- Import Workbench review item persistence for idempotent pending review creation, company-scoped queue/detail reads, and explicit review decision submission
- API RequestContext foundation with gated development-header authentication and production OIDC/JWT validation for future authenticated routes
- Authenticated Import Workbench REST API exposing queue, detail, and decision submission adapters

## Next Milestones

### 1. Odoo Import Workbench UI

- Odoo-side UI that consumes the authenticated Workbench REST API
- queue and detail screens for pending review items
- explicit `SELECT_WORKFLOW` and `DISMISS` decision submission
- no Odoo-owned business logic

### 2. Odoo Vendor Bill Write Service

- dry-run by default
- explicit production approval
- idempotent create
- draft Vendor Bill only
- no automatic posting

### 3. Import Session and Pipeline

- download
- parse
- match
- decide
- build
- write
- per-item and session summaries

### 4. Rule Engine and Decision Engine

- company-scoped deterministic rules
- workflow recommendation
- priority and conflict handling
- full audit trail

### 5. Odoo Import Workbench

- incoming invoice queue
- matching and warning display
- persisted pending review queue and company-scoped review detail reads
- persisted explicit user decision submission with optimistic concurrency and idempotency
- workflow recommendation
- user override and approval
- existing PO selection
- RFQ/PO creation option
- direct Vendor Bill, expense, asset, manual review, and ignore actions

### 6. Procurement Traceability

- link invoice to existing PO where possible
- support reconstructing RFQ/PO for out-of-system purchases
- connect procurement to opportunity, quotation, sales order, project, proposal scenario, and analytical context
- expose actual profitability

### 7. Scheduler, Retry, and Recovery

- scheduled collection
- retry policies
- recoverable import states
- idempotent replay

### 8. Monitoring and Operations

- metrics
- structured logs
- import dashboards
- alerts
- operational reconciliation

### 9. AI Advisor and Company Memory

- pgvector retrieval of similar historical decisions
- local Ollama-compatible model
- advisory recommendations only
- user feedback converted into deterministic rules where approved
