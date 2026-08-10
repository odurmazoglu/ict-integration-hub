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
- Odoo Workbench read-to-validation-to-Hub-submit flow without Odoo acknowledgement or workflow execution
- ERP Reference Validation for Odoo Workbench allocation decisions
- Workflow Execution Foundation for accepted decisions, deterministic plans, dry-run strategy coordination, and execution state ports
- Execution Runtime Foundation with SQLAlchemy persistence, state machine, append-only events, checkpoints, recovery contracts, and retry policy vocabulary
- No-write Runtime Integration from accepted Hub decisions to durable dry-run completed runtimes without ERP/provider mutation

## Next Milestones

### 1. Vendor Bill Execution Strategy

- bridge accepted execution plans to `VendorBillWriter`
- dry-run-first strategy behavior
- production gates before real ERP writes
- draft-only Vendor Bill execution

### 2. Purchase Workflow Strategies

- existing Purchase Order matching execution
- RFQ/Purchase Order creation strategy contracts
- no posting or payment side effects

### 3. Customer Recharge Strategy

- customer invoice/recharge execution boundaries
- intercompany safety model
- profitability traceability without automatic posting

### 4. Expense / Asset / Subscription Strategies

- workflow-specific strategy separation
- dry-run support for each strategy
- ERP write safety gates

### 5. Odoo Projection Acknowledgement

- Hub-to-Odoo acknowledgement projection
- decision-ready clearing only after Hub acceptance
- safe projection status update boundaries

### 6. Odoo Online Workbench Projection Synchronization

- controlled Odoo Studio setup for `x_ipp_import_review`
- Hub-to-Odoo JSON-2 projection publishing
- Hub acknowledgement projection
- no custom Odoo Python addon
- no workflow execution

### 7. Odoo Import Workbench UI

- Odoo Studio list, form, and search views backed by the projection model
- queue and detail screens for pending review items
- explicit `SELECT_WORKFLOW` and `DISMISS` decision submission
- no Odoo-owned business logic

### 8. Odoo Vendor Bill Write Service

- dry-run by default
- explicit production approval
- idempotent create
- draft Vendor Bill only
- no automatic posting

### 9. Import Session and Pipeline

- download
- parse
- match
- decide
- build
- write
- per-item and session summaries

### 10. Rule Engine and Decision Engine

- company-scoped deterministic rules
- workflow recommendation
- priority and conflict handling
- full audit trail

### 11. Odoo Import Workbench

- incoming invoice queue
- matching and warning display
- persisted pending review queue and company-scoped review detail reads
- persisted explicit user decision submission with optimistic concurrency and idempotency
- workflow recommendation
- user override and approval
- existing PO selection
- RFQ/PO creation option
- direct Vendor Bill, expense, asset, manual review, and ignore actions

### 12. Procurement Traceability

- link invoice to existing PO where possible
- support reconstructing RFQ/PO for out-of-system purchases
- connect procurement to opportunity, quotation, sales order, project, proposal scenario, customer recharge recipient, target company, and analytical context
- execute accepted Business Context Allocation sets after repository validation
- support future Composite Workflow Strategy for mixed allocation purposes
- expose actual profitability

### 13. Scheduler And Runtime Recovery

- scheduled collection
- retry execution workers
- recoverable import states
- idempotent replay

### 14. Monitoring and Operations

- metrics
- structured logs
- import dashboards
- alerts
- operational reconciliation

### 15. AI Advisor and Company Memory

- pgvector retrieval of similar historical decisions
- local Ollama-compatible model
- advisory recommendations only
- user feedback converted into deterministic rules where approved
