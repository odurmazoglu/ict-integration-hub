# ADR-0005: Preserve Procurement and Sales Profitability Traceability

- Status: Accepted
- Date: 2026-07-21

## Context

Supplier invoices are needed not only for accounting but also to measure the real cost and profitability of customer sales. Some purchases already have an Odoo purchasing chain; others were performed outside Odoo and may require controlled reconstruction.

## Decision

ICT IPP must preserve or establish traceability whenever applicable:

`Opportunity → Sales Quotation → Sales Order → RFQ → Purchase Order → Vendor Invoice → Vendor Bill → Actual Cost → Sales Profitability`

The Import Workbench must support:

- matching an invoice to an existing Purchase Order
- creating a controlled RFQ and Purchase Order flow when the purchase occurred outside Odoo
- linking procurement to the relevant opportunity, proposal scenario, sale, customer, project, and analytical context
- direct Vendor Bill, expense, or asset processing when no sales-related procurement context exists

## Consequences

- Direct Vendor Bill creation is not the universal default.
- Workflow decisions must consider business context, not only supplier identity.
- Profitability reporting should use actual procurement and Vendor Bill cost where available.
- Reconstructed purchasing history must be clearly marked and auditable.
