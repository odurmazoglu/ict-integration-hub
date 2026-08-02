# ICT Intelligent Procurement Platform (IPP) — Project Constitution

The Project Constitution is the binding architectural guide for **ICT Integration Hub** and its internal **ICT Intelligent Procurement Platform (IPP)** architecture.

If any future issue, pull request, code generation task, AI assistant instruction, or implementation conflicts with this document, **this Constitution takes precedence unless superseded by an accepted ADR.**

---

# Product Identity

- **External Product Name:** ICT Integration Hub
- **Internal Architecture Name:** ICT Intelligent Procurement Platform (IPP)
- **Vision:** AI-assisted Procurement Automation built on deterministic business rules.

The public product is referred to as **ICT Integration Hub**.

Architecture discussions may use **ICT IPP** when referring to the internal Decision Engine, Rule Engine, Workflow Engine, Matching Engine, Company Memory, and ERP Adapter architecture.

---

# Source of Truth

Architecture decisions shall be interpreted in the following order:

1. Main branch implementation
2. This Project Constitution
3. Accepted Architecture Decision Records (ADRs)
4. Architecture documentation
5. Current GitHub Issue / Pull Request context

Any architectural change that conflicts with an accepted ADR requires a new superseding ADR.

---

# Non-Negotiable Principles

The following principles are mandatory:

1. Clean Architecture
2. Lightweight Domain Driven Design
3. Repository Pattern
4. Immutable DTOs
5. ERP-independent business logic
6. Deterministic matching
7. Deterministic workflow selection
8. Small production-ready pull requests
9. Odoo is an Adapter
10. Hub owns business decisions
11. AI is advisory only
12. Rule Engine executes before AI Advisor
13. Production writes require explicit safety gates
14. Procurement traceability should be preserved whenever possible

---

# Decision Authority

ICT IPP owns all business decisions.

This includes:

- Uyumsoft integration
- UBL parsing
- Internal Invoice Domain
- Partner matching
- Product matching
- Tax mapping
- Rule Engine
- Decision Engine
- Workflow selection
- Strategy selection
- Company Memory
- AI Advisor
- Import Session orchestration
- Procurement traceability

ERP systems execute decisions.

ERP systems do **not** make procurement decisions.

**Hub decides. Odoo executes.**

---

# ERP Boundary

Odoo is an execution platform.

Odoo owns:

- Import Workbench user interface
- ERP master data
- Purchase Orders
- RFQs
- Vendor Bills
- Expenses
- Assets
- Subscriptions
- Accounting records
- Sales documents
- Profitability reporting

Business logic never belongs inside Odoo.

---

# Safety Boundaries

## Uyumsoft

Uyumsoft is **read-only by default**.

Allowed operations include:

- Authentication
- Invoice listing
- Invoice download
- XML / UBL retrieval
- Connectivity validation

Forbidden operations include (unless a future ADR explicitly changes this):

- SetInvoicesTaken
- SendInvoice
- Cancel*
- RetrySendInvoices
- MoveToDraftStatus
- Any provider-side state mutation

---

## Odoo

Current write scope is intentionally limited.

The Hub may create **Draft Vendor Bills** only after successful validation and explicit approval.

The Hub must **not**:

- Post accounting entries
- Register payments
- Reconcile
- Delete accounting documents
- Modify ERP master data automatically

Production deployments require explicit operational gates, validation, backups, and rollback procedures.

---

# Workflow Decisions

Every imported invoice must result in exactly one workflow.

Supported workflows:

- Match existing Purchase Order
- Create RFQ + Purchase Order
- Direct Vendor Bill
- Expense
- Fixed Asset
- Subscription / Service
- Manual Review
- Ignore

Workflow selection belongs exclusively to the Decision Engine.

---

# AI Boundary

AI Advisor executes **only after deterministic Rule Engine evaluation**.

AI may:

- Recommend workflows
- Explain rule failures
- Summarize invoices
- Suggest missing ERP master data
- Retrieve historical Company Memory
- Explain previous decisions

AI must **never**:

- Choose workflow
- Choose strategy
- Select ambiguous ERP records
- Override deterministic rules
- Approve invoices
- Reject invoices
- Create ERP records
- Update ERP records
- Delete ERP records
- Post accounting entries

AI recommendations always require deterministic policy or user approval.

Default AI deployment is local through Ollama-compatible models.

Company data should remain inside the controlled environment by default.

---

# Procurement Traceability

Whenever possible the platform preserves the complete procurement chain.

```
Opportunity
        ↓
Sales Quotation
        ↓
Sales Order
        ↓
RFQ
        ↓
Purchase Order
        ↓
Vendor Invoice
        ↓
Vendor Bill
        ↓
Actual Cost
        ↓
Sales Profitability
```

If one relationship cannot be reconstructed, the platform should explicitly record the missing link instead of silently breaking traceability.

---

# Current Baseline

The current implementation includes:

- Uyumsoft Authentication
- Invoice Listing
- Invoice Download
- Production Read-only Validation
- Immutable InternalInvoice Domain
- UBL Parser
- Deterministic Tax Mapping
- Read-only ERP Repository Layer
- Odoo JSON-2 Adapter
- Deterministic Partner Matching
- Deterministic Product Matching
- Vendor Bill Builder
- Deterministic account.move payload generation
- Import Workbench application contracts for future review queue and explicit user decision adapters

The next implementation milestone is:

- Durable Import Session and Import Workbench persistence when explicitly scoped

Future milestones include:

- Import Workbench UI
- AI Advisor
- Company Memory

---

# Development Workflow

Every architectural change follows this lifecycle:

1. Architecture Discussion
2. Architecture Decision Record (ADR)
3. GitHub Issue
4. Codex Implementation
5. Draft Pull Request
6. Architecture Review
7. Merge
8. Production Validation

Each Pull Request should have a single responsibility, include appropriate documentation updates, and remain production-safe.

---

# Documentation Policy

Documentation must describe:

- Current implementation
- Accepted architecture
- Operational boundaries
- Safety constraints
- Developer expectations

Documentation must **never** describe future features as already implemented.

Whenever architecture changes, the corresponding documentation and ADRs must be updated within the same Pull Request.

---

# Related Documentation

- Vision
- Architecture
- Rule Engine
- AI Advisor
- Workflows
- Company Memory
- Import Session
- Import Workbench
- Matching
- Roadmap
- Architecture Decision Records
