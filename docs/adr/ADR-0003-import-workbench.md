# ADR-0003: Odoo Import Workbench

- Status: Accepted
- Date: 2026-07-21

## Context

Incoming invoices can represent purchases, expenses, assets, subscriptions, services, or documents requiring review. Users need one controlled operational surface without leaving Odoo.

## Decision

The primary user interface is an **Import Workbench inside Odoo**.

The workbench displays invoice details, deterministic matching results, related procurement and sales context, workflow recommendations, warnings, and approval actions.

Supported decisions include:

- match an existing Purchase Order
- create a new RFQ and Purchase Order flow
- create a direct Vendor Bill
- process as expense
- process as fixed asset
- process as subscription or service
- manual review
- ignore or hide with a recorded reason

The workbench does not own the Rule Engine or Decision Engine.

## Consequences

- Users remain in Odoo for daily operations.
- Recommendation logic remains in ICT IPP.
- User overrides and approvals must be auditable.
- The same decision APIs may support another UI later.
