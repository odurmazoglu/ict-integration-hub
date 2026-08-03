# ADR-0011: Odoo Online Import Workbench Projection

- Status: Accepted
- Date: 2026-08-03

## Context

ICT uses Odoo 19 Online as the current ERP. Odoo Online does not support installing custom Python modules, so the Import Workbench cannot be delivered as an Odoo server addon or as Odoo-side Python code that calls the Hub.

Users still need to review IPP Workbench items inside Odoo. At the same time, ICT IPP must remain the authority for Rule Engine execution, Decision Engine workflow semantics, validation, idempotency, traceability, optimistic concurrency, accepted decision persistence, and future workflow execution.

The authenticated Hub REST API exists for direct clients, but Odoo Online cannot consume it through an installed Python addon. Keycloak protects direct Hub clients; it is not a mechanism for storing Hub bearer tokens, Keycloak client secrets, or Odoo-side service credentials in Odoo Studio fields.

## Decision

Use an Odoo Studio custom model as a projection of Hub-owned Import Workbench review items.

The proposed Studio model technical name is:

```text
x_ipp_import_review
```

Future Hub synchronization will:

- publish pending review projections to Odoo through JSON-2
- update Hub-owned projection fields when Hub review state changes
- read explicit user decision candidates from the Studio projection model
- validate candidate `review_id`, `company_id`, expected version, idempotency key, selected workflow, and resolution details inside the Hub
- persist accepted decisions in Hub PostgreSQL
- project Hub acknowledgement status back to Odoo
- execute workflows only in later focused PRs after separate accepted scope

Odoo must not independently decide workflow semantics. Odoo is the user interface, projection store, and ERP execution adapter. The Hub remains the decision authority and accepted decision ledger.

## Consequences

Positive consequences:

- compatible with Odoo 19 Online
- no custom Odoo module installation
- users remain inside Odoo for review work
- business rules remain in ICT IPP
- Hub remains source of truth for review lifecycle, accepted decisions, idempotency, and traceability
- ERP-independent application design is preserved
- future ERP adapters can implement equivalent projection ports without changing Hub decision logic

Trade-offs:

- Studio model, fields, views, and access rights require controlled manual setup
- Odoo and Hub projections become eventually consistent
- duplicate or stale Odoo projection records require safe reconciliation
- Odoo records cannot be treated as the authoritative decision ledger
- Odoo JSON-2 API key access requires restricted configuration and operational rotation
- direct Keycloak user-token propagation from Odoo Online UI is not part of this design

## Rejected Alternatives

- installing a custom Odoo Python addon on Odoo Online
- placing business rules in Odoo Studio automated actions
- allowing Odoo to call the Hub with untrusted company or user headers
- treating Odoo as the authoritative Rule Engine or Decision Engine
- storing service credentials in browser-side JavaScript
- embedding Keycloak client secrets or Hub bearer tokens in Odoo Studio fields
- using the Odoo projection as the accepted decision ledger

## Safety Boundary

This ADR permits a future, separately implemented Odoo write category only for the dedicated Workbench projection model.

That future write exception does not authorize:

- `account.move` posting
- payment creation
- payment registration
- reconciliation
- master-data mutation
- RFQ or Purchase Order creation
- expense, asset, or subscription execution
- deletion or unlink operations
- workflow execution

Any additional Odoo write category requires its own accepted scope and, when architectural, a new ADR.

## Related Documentation

- [Odoo Workbench Projection](../ODOO_WORKBENCH_PROJECTION.md)
- [Import Workbench](../IMPORT_WORKBENCH.md)
- [Security](../SECURITY.md)
- [Keycloak OIDC Adapter](../KEYCLOAK.md)
- [ERP Boundary ADR](ADR-0003-erp-boundary.md)
