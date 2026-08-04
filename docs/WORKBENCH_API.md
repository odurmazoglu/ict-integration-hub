# Import Workbench API

The Import Workbench API exposes authenticated Hub endpoints for review queue reads, review detail reads, and explicit review decision submission.

Company identity and user identity always come from `RequestContext`. Clients must not supply `company_id`, `decided_by`, or body-level `review_id`.

## Decision Submission

Endpoint:

```text
POST /api/workbench/reviews/{review_id}/decision
```

Supported decisions:

- `select_workflow`
- `dismiss`

`select_workflow` requires `selected_workflow` and may include `business_context_allocations`. `dismiss` rejects workflow-specific selections, including allocation evidence.

Example:

```json
{
  "decision": "select_workflow",
  "selected_workflow": "vendor_bill",
  "expected_version": 4,
  "idempotency_key": "example-key",
  "comment": "Reviewed",
  "business_context_allocations": {
    "completeness": "complete",
    "invoice_total": "100000.000000",
    "currency": "TRY",
    "allocations": [
      {
        "allocation_key": "ALLOC-001",
        "allocation_type": "sales_order_cost",
        "source_line_number": "1",
        "amount": "40000.000000",
        "percentage": "40",
        "currency": "TRY",
        "customer_id": 101,
        "recharge_partner_id": 105,
        "customer_invoice_id": 9001,
        "sales_order_id": 301
      },
      {
        "allocation_key": "ALLOC-002",
        "allocation_type": "internal_cost",
        "amount": "60000.000000",
        "percentage": "60",
        "currency": "TRY"
      }
    ]
  }
}
```

Decimal fields should be sent as strings. JSON floating-point values are rejected before they enter the immutable allocation contracts.

## Allocation Semantics

The source vendor invoice is already identified by the Workbench review item and is not repeated on allocation rows.

- `customer_id`: commercial customer context
- `recharge_partner_id`: actual party expected to be invoiced or recharged
- `customer_invoice_id`: optional existing outgoing customer invoice or refund evidence link

`customer_invoice_id` does not create an invoice, prove recharge completion, grant authorization, or execute profitability posting. Future repository validators must verify record existence, company access, move type, and partner/company relationships before execution.

## Persistence And Idempotency

Accepted decisions are append-only. New decisions write allocation evidence to `business_context_allocations` JSON. Legacy `business_context` is no longer accepted by the active API.

Idempotency is scoped by `(company_id, idempotency_key)`. Allocation comparison uses canonical Decimal strings, canonical currency case, enum values as strings, and allocation rows sorted by `allocation_key`. List reordering alone is idempotent; changed amounts, percentages, allocation types, target ERP identifiers, customer invoice links, completeness, totals, currency, or allocation keys conflict.

## Non-Goals

This API does not execute workflows, create Vendor Bills, create customer invoices, perform recharge, validate ERP record existence, synchronize Odoo Studio child lines, call AI, or post profitability.
