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

The existing `BusinessContextDecision` remains the legacy single-context runtime contract until a later focused implementation PR replaces it with:

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

## Future Command Shape

The intended future `ReviewDecisionCommand` shape is:

```text
business_context_allocations: BusinessContextAllocationSet | None
```

Future rules:

- `SELECT_WORKFLOW` may carry allocations
- `DISMISS` must reject allocations
- allocation requirements depend on selected workflow and future strategy
- Hub validates allocation totals before accepting a decision
- accepted allocations become immutable decision evidence

This ADR does not implement that command change, API schema change, persistence change, Odoo mapping, or workflow execution.

## Related Documentation

- [Application Layer](../APPLICATION_LAYER.md)
- [Import Workbench](../IMPORT_WORKBENCH.md)
- [Odoo Workbench Projection](../ODOO_WORKBENCH_PROJECTION.md)
- [Workflows](../WORKFLOWS.md)
- [Procurement Traceability ADR](ADR-0009-procurement-traceability.md)
