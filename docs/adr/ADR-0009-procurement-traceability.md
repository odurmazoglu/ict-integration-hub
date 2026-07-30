# ADR-0009: Procurement Traceability

- Status: Accepted
- Date: 2026-07-30

## Context

Procurement automation must preserve business context from the originating sales need through actual cost and profitability analysis.

## Decision

ICT IPP preserves procurement traceability whenever possible:

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

If a link cannot be preserved, the gap should be explicit and reviewable.

## Consequences

Workflow, matching, import-session, and ERP adapter designs must carry traceability identifiers when available. Future reporting and profitability work should consume these links instead of reconstructing them heuristically.

## Related Documentation

- [Architecture](../ARCHITECTURE.md)
- [Import Session](../IMPORT_SESSION.md)
