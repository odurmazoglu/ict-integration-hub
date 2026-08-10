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
- Vendor Bill Execution Strategy for approved pure `VENDOR_BILL` plans, Draft Vendor Bill writer delegation, deterministic write idempotency, and distributed-write recovery documentation
- Execution Source Invoice Persistence Adapter for version-pinned persisted Hub invoice, supplier match, product match, and tax mapping evidence
- Pre-Decision Execution Evidence Persistence for immutable review-version source snapshots before human decision

## Next Milestones

### 1. Rebase Atomic Decision Evidence Capture

- rebase open PR #83 onto main after pre-decision evidence persistence merges
- add the concrete `ReviewExecutionEvidenceReader` backed by `workbench_review_execution_evidence`
- validate atomic accepted-decision snapshot capture from Stage 1 evidence into Stage 2 accepted execution evidence
- do not proceed to live Vendor Bill production wiring before PR #83 is completed

### 2. Vendor Bill Production Wiring and Recovery Verification

- wire production execution only when full source evidence is available
- reconcile Hub runtime status with Odoo duplicate lookup after response loss
- operator-facing safe recovery diagnostics
- no posting, payment, or reconciliation

### 3. Purchase Workflow Strategies

- existing Purchase Order matching execution
- RFQ/Purchase Order creation strategy contracts
- no posting or payment side effects

### 4. Customer Recharge Strategy

- customer invoice/recharge execution boundaries
- intercompany safety model
- profitability traceability without automatic posting

### 5. Expense / Asset / Subscription Strategies

- workflow-specific strategy separation
- dry-run support for each strategy
- ERP write safety gates

### 6. Odoo Projection Acknowledgement

- Hub-to-Odoo acknowledgement projection
- decision-ready clearing only after Hub acceptance
- safe projection status update boundaries

### 7. Odoo Online Workbench Projection Synchronization

- controlled Odoo Studio setup for `x_ipp_import_review`
- Hub-to-Odoo JSON-2 projection publishing
- Hub acknowledgement projection
- no custom Odoo Python addon
- no workflow execution

### 8. Odoo Import Workbench UI

- Odoo Studio list, form, and search views backed by the projection model
- queue and detail screens for pending review items
- explicit `SELECT_WORKFLOW` and `DISMISS` decision submission
- no Odoo-owned business logic

### 9. Odoo Vendor Bill Write Service

- dry-run by default
- explicit production approval
- idempotent create
- draft Vendor Bill only
- no automatic posting

### 10. Import Session and Pipeline

- download
- parse
- match
- decide
- build
- write
- per-item and session summaries

### 11. Rule Engine and Decision Engine

- company-scoped deterministic rules
- workflow recommendation
- priority and conflict handling
- full audit trail

### 12. Odoo Import Workbench

- incoming invoice queue
- matching and warning display
- persisted pending review queue and company-scoped review detail reads
- persisted explicit user decision submission with optimistic concurrency and idempotency
- workflow recommendation
- user override and approval
- existing PO selection
- RFQ/PO creation option
- direct Vendor Bill, expense, asset, manual review, and ignore actions

### 13. Procurement Traceability

- link invoice to existing PO where possible
- support reconstructing RFQ/PO for out-of-system purchases
- connect procurement to opportunity, quotation, sales order, project, proposal scenario, customer recharge recipient, target company, and analytical context
- execute accepted Business Context Allocation sets after repository validation
- support future Composite Workflow Strategy for mixed allocation purposes
- expose actual profitability

### 14. Scheduler And Runtime Recovery

- scheduled collection
- retry execution workers
- recoverable import states
- idempotent replay

### 15. Monitoring and Operations

- metrics
- structured logs
- import dashboards
- alerts
- operational reconciliation

### 16. AI Advisor and Company Memory

- pgvector retrieval of similar historical decisions
- local Ollama-compatible model
- advisory recommendations only
- user feedback converted into deterministic rules where approved
