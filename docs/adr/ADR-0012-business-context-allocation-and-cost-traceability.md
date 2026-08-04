# ADR-0012: Business Context Allocation and Cost Traceability

- Status: Accepted
- Date: 2026-08-03

## Context

One incoming supplier invoice can serve multiple commercial transactions, customers, affiliates, projects, internal departments, fixed assets, subscriptions, or mixed cost purposes.

A single invoice-level Sales Order, Purchase Order, project, or customer field is not sufficient for procurement traceability or actual-cost profitability. Some portions of one supplier invoice may be recharged through different customer invoices, while other portions may remain internal cost. The commercial customer and the actual recharge or invoice recipient may also differ.

Odoo is the user interface, projection store, and ERP adapter. The Hub owns allocation semantics, validation, idempotency, traceability, and future workflow execution decisions.

## Decision

Model business context as one or more immutable allocation lines.

Each allocation line means:

> this amount or source portion of the incoming invoice belongs to this business and accounting context.

Allocation lines may refer to source invoice lines, commercial customers, recharge recipients, target companies or affiliates, Sales Orders, future Sales Order lines, opportunities, proposal scenarios, existing Purchase Orders, projects, analytic accounts, subscriptions, and cost purpose.

The canonical allocation purpose vocabulary is `BusinessContextAllocationType`. The aggregate allocation contract is `BusinessContextAllocationSet`.

The Hub validates the allocation set. Odoo only captures candidate allocation lines for future Hub validation.

The existing `BusinessContextDecision` was the legacy single-context runtime contract until the Workbench decision-submission path replaced it with:

```text
business_context_allocations: BusinessContextAllocationSet | None
```

Initial allocation lines require positive amount and/or percentage values. Negative, zero, and credit-note allocation semantics are future work.

## Consequences

Positive consequences:

- supports multiple Sales Orders for one supplier invoice
- supports split customer recharge and affiliate billing
- supports mixed customer-related and internal costs
- preserves actual-cost profitability traceability
- avoids false one-invoice/one-sale assumptions
- keeps allocation semantics ERP-neutral and Hub-owned

Trade-offs:

- UI requires child allocation lines
- amount and percentage reconciliation are required
- persistence, API, and Odoo mappings become more complex in future PRs
- future workflow execution may create multiple downstream records
- allocation changes require version and idempotency protection
- mixed allocations may require a future Composite Workflow Strategy

## Rejected Alternatives

- comma-separated Sales Order IDs
- Many2many links without amounts
- one invoice-level `BusinessContextDecision`
- free-form JSON with no canonical contract
- allocation logic inside Odoo Studio automation
- silently assigning the full invoice to one Sales Order
- fuzzy inference of target customer, recharge recipient, or affiliate
- adding `WorkflowType.MIXED` without a separate workflow decision

## Source Of Truth

Hub PostgreSQL is authoritative for accepted allocations.

Odoo Studio is authoritative only for user-entered candidate allocation lines before Hub acceptance.

Future ingestion must validate allocation company isolation, selected ERP IDs, allocation totals, idempotency, and expected review version before accepting allocation evidence.

## Command Shape

The current `ReviewDecisionCommand` shape is:

```text
business_context_allocations: BusinessContextAllocationSet | None
```

Current rules:

- `SELECT_WORKFLOW` may carry allocations
- `DISMISS` must reject allocations
- allocation requirements are structural in this slice; workflow-specific allocation requirements belong to future strategy work
- Hub validates allocation totals before accepting a decision
- accepted allocations become immutable decision evidence

This ADR amendment implements the command, API schema, persistence, and idempotency contract change. It does not implement Odoo JSON-2 mapping or workflow execution.

## Accepted Amendment: Workbench Decision Submission

The Workbench decision submission path now uses:

```text
business_context_allocations: BusinessContextAllocationSet | None
```

This replaces the active writable `business_context` command/API field. `BusinessContextDecision` remains only as legacy historical evidence for old persisted rows. New decision submissions write allocation evidence only to `business_context_allocations`.

`BusinessContextAllocation` includes optional `customer_invoice_id`, the ERP identifier of an existing outgoing customer invoice or refund when that invoice already exists. The source supplier invoice is the Workbench review item and is not duplicated on allocation rows. `customer_invoice_id` is optional because customer invoicing or recharge may occur later. It is evidence only: it does not create a customer invoice, prove recharge completion, authorize access, or execute profitability posting.

The relationship fields have distinct meanings:

- `customer_id`: commercial customer context
- `recharge_partner_id`: actual party expected to be invoiced or recharged
- `customer_invoice_id`: existing outgoing customer invoice or refund evidence link

These values may differ. Multiple allocations under one vendor review may reference different `customer_invoice_id` values, and multiple vendor reviews may allocate cost to the same customer invoice. There is no uniqueness constraint on `customer_invoice_id`.

Decision evidence is persisted append-only as deterministic JSON. Enum values are serialized as strings, Decimal values as canonical strings, and allocation rows are sorted by `allocation_key` for idempotency. Equivalent Decimal forms such as `259.2000` and `259.20` compare as identical business content. Changed amounts, percentages, allocation types, target ERP identifiers, customer invoice links, completeness, invoice totals, currency, allocation keys, added rows, or removed rows conflict under the same company-scoped idempotency key.

Legacy persisted rows are not rewritten. Rows with no allocation data remain readable. Rows with legacy `business_context` preserve that object as legacy evidence; the Hub does not fabricate allocation amounts or silently convert it into allocation lines.

This amendment does not implement Odoo JSON-2 allocation readers or writers, allocation execution, customer invoice creation, recharge execution, ERP record existence validation, company-scope repository validation, outgoing-invoice move type validation, Sales Order/customer consistency validation, Composite Workflow Strategy, profitability posting, or analytic distribution posting.

## Related Documentation

- [Application Layer](../APPLICATION_LAYER.md)
- [Import Workbench](../IMPORT_WORKBENCH.md)
- [Odoo Workbench Projection](../ODOO_WORKBENCH_PROJECTION.md)
- [Workflows](../WORKFLOWS.md)
- [Procurement Traceability ADR](ADR-0009-procurement-traceability.md)
