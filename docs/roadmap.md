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
- Odoo Online Workbench projection architecture, Studio model contract, immutable projection DTOs, and projection ports
- Business Context Allocation architecture, immutable application contracts, Workbench API submission, append-only decision persistence, and idempotency canonicalization
- Production-safe read-only Odoo Workbench decision candidate reader for configured Studio parent projection and allocation child models
- Odoo Workbench Allocation Candidate Reader
- Odoo Workbench Decision Submission Orchestrator
- Odoo Workbench read-to-Hub-submit flow without Odoo acknowledgement, ERP reference validation, or workflow execution

## Next Milestones

### 1. ERP Reference Validation

- validate selected Customer
- validate Recharge Recipient
- validate Customer Invoice
- validate Target Company
- validate Sales Order
- validate Purchase Order
- validate Opportunity
- validate Project
- validate Analytic Account
- validate Subscription reference
- enforce company-scoped ERP repository checks before workflow execution

### 2. Odoo Online Workbench Projection Synchronization

- controlled Odoo Studio setup for `x_ipp_import_review`
- Hub-to-Odoo JSON-2 projection publishing
- Hub acknowledgement projection
- no custom Odoo Python addon
- no workflow execution

### 3. Odoo Import Workbench UI

- Odoo Studio list, form, and search views backed by the projection model
- queue and detail screens for pending review items
- explicit `SELECT_WORKFLOW` and `DISMISS` decision submission
- no Odoo-owned business logic

### 4. Odoo Vendor Bill Write Service

- dry-run by default
- explicit production approval
- idempotent create
- draft Vendor Bill only
- no automatic posting

### 5. Import Session and Pipeline

- download
- parse
- match
- decide
- build
- write
- per-item and session summaries

### 6. Rule Engine and Decision Engine

- company-scoped deterministic rules
- workflow recommendation
- priority and conflict handling
- full audit trail

### 7. Odoo Import Workbench

- incoming invoice queue
- matching and warning display
- persisted pending review queue and company-scoped review detail reads
- persisted explicit user decision submission with optimistic concurrency and idempotency
- workflow recommendation
- user override and approval
- existing PO selection
- RFQ/PO creation option
- direct Vendor Bill, expense, asset, manual review, and ignore actions

### 8. Procurement Traceability

- link invoice to existing PO where possible
- support reconstructing RFQ/PO for out-of-system purchases
- connect procurement to opportunity, quotation, sales order, project, proposal scenario, customer recharge recipient, target company, and analytical context
- execute accepted Business Context Allocation sets after repository validation
- support future Composite Workflow Strategy for mixed allocation purposes
- expose actual profitability

### 8. Scheduler, Retry, and Recovery

- scheduled collection
- retry policies
- recoverable import states
- idempotent replay

### 9. Monitoring and Operations

- metrics
- structured logs
- import dashboards
- alerts
- operational reconciliation

### 10. AI Advisor and Company Memory

- pgvector retrieval of similar historical decisions
- local Ollama-compatible model
- advisory recommendations only
- user feedback converted into deterministic rules where approved
