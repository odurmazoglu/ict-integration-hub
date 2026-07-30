# Project Constitution

The Project Constitution is the binding architecture guide for ICT Integration Hub and its internal ICT Intelligent Procurement Platform (IPP) architecture. If a future issue, PR, code generation task, or AI assistant instruction conflicts with this document, the constitution wins unless a new accepted ADR explicitly changes it.

## Product And Architecture Names

- External product name: **ICT Integration Hub**
- Internal architecture name: **ICT Intelligent Procurement Platform (IPP)**
- Vision: **AI-assisted Procurement Automation built on deterministic business rules**

The public product may be described as Integration Hub. Architecture discussions may use ICT IPP when referring to internal decision, rule, workflow, matching, memory, and ERP adapter boundaries.

## Non-Negotiable Principles

1. Clean Architecture: domain and application rules must not depend on HTTP, SOAP, JSON-2, SQLAlchemy, Odoo, or provider SDK details.
2. Domain Driven Design: procurement, invoice, matching, import, and traceability concepts are modeled in the Hub, not hidden inside adapters.
3. Repository Pattern: ERP master-data access is expressed through repository protocols and provider implementations.
4. Immutable DTOs: cross-layer domain and matching data should be immutable whenever possible.
5. ERP-independent business logic: business rules must run without Odoo-specific assumptions.
6. Deterministic matching: exact rules, priority order, and ambiguity handling are preferred over fuzzy or silent fallback behavior.
7. Small production-ready PRs: every implementation slice must be narrow, tested, documented, and rollback-aware.
8. Odoo is an Adapter: Odoo executes reviewed decisions; it does not own IPP decisions.
9. Hub owns business decisions: workflow, strategy, matching, validation, idempotency, and traceability decisions live in ICT IPP.
10. AI is advisory only: AI never makes or executes business decisions.

## Safety Boundaries

Uyumsoft is read-only by default. Allowed operations are limited to connectivity probes, invoice listing, and UBL XML document retrieval as documented in [Integration Flow](INTEGRATION_FLOW.md). State-changing provider operations such as `SetInvoicesTaken`, `SendInvoice`, `Cancel*`, `RetrySendInvoices`, and `MoveToDraftStatus` are forbidden unless a future accepted ADR changes this boundary.

Odoo is draft-only for the current write scope. The Hub may create draft vendor bills only after explicit confirmation and reviewed identifiers. It must not call `action_post`, unlink records, register payments, reconcile, or mutate master data by default.

Production requires explicit runtime gates, operational approvals, safe configuration, backups, and validation evidence. Production gates never override the read-only Uyumsoft policy, the Odoo draft-only policy, or AI advisory-only policy.

## Decision Authority

The Decision Engine, Rule Engine, workflow selection, strategy selection, matching, and procurement traceability policies live inside ICT IPP. ERP systems execute decisions and store operational accounting records; they do not choose procurement workflows.

Odoo import screens, including the future Import Workbench, are user interfaces only. They may present decisions, warnings, review states, and recommendations, but business logic remains in the Hub.

## AI Boundary

AI Advisor runs after deterministic rules. It may:

- recommend next actions
- summarize imported data
- explain rule failures
- identify likely missing master data
- surface historical company context from Company Memory

AI Advisor must not:

- choose the workflow
- choose the strategy
- approve or reject an invoice
- select one ambiguous ERP record
- create, update, post, cancel, or delete records
- override deterministic rules

## Traceability Boundary

Procurement traceability should be preserved whenever possible:

```text
Sales
  -> Quotation
  -> RFQ
  -> Purchase Order
  -> Vendor Invoice
  -> Vendor Bill
  -> Actual Cost
  -> Sales Profitability
```

If a workflow cannot preserve one link, the Hub should record the gap explicitly and keep the rest of the chain intact.

## Documentation Requirements

Documentation must describe the current implementation and accepted decisions. It must not pretend future features are already implemented.

Implementation PRs must update relevant documentation when they change architecture, workflows, operational behavior, safety gates, or developer expectations.

## Related Documents

- [Vision](VISION.md)
- [Architecture](ARCHITECTURE.md)
- [Rule Engine](RULE_ENGINE.md)
- [AI Advisor](AI_ADVISOR.md)
- [Workflows](WORKFLOWS.md)
- [Architecture Decisions](adr/README.md)
