# Vision

## ICT Intelligent Procurement Platform (IPP)

ICT IPP is an AI-assisted procurement automation platform built on deterministic business rules.

It transforms incoming supplier invoices into controlled business decisions rather than blindly importing every document as a Vendor Bill.

## Target Outcome

For each incoming invoice, the platform should:

1. retrieve and validate the source document
2. parse it into an ERP-neutral invoice model
3. resolve supplier, product, and tax context deterministically
4. identify related purchasing and sales context
5. recommend the safest workflow
6. present exceptions in the Odoo Import Workbench
7. apply an approved workflow in Odoo
8. preserve traceability for actual profitability analysis

## Automation Philosophy

The target is controlled automation, not unreviewed automation.

- Known, deterministic cases may be automated.
- Ambiguous cases require user review.
- Local AI assists when deterministic rules are insufficient.
- AI never creates or posts ERP records on its own.
- User decisions can become reusable rules.

## Business Value

The platform must support both ordinary operating invoices and sale-related procurement, including:

- vehicle charging
- meals and accommodation
- utilities and telecom
- software subscriptions
- professional services
- inventory purchases
- customer-specific hardware or software procurement
- fixed assets

For sale-related procurement, the platform must connect actual supplier cost to the relevant sales opportunity, quotation, order, project, or proposal scenario.
