# ADR-0003: ERP Boundary

- Status: Accepted
- Date: 2026-07-30

## Context

Odoo Online is the current ERP. Future ERP adapters may be added. Procurement decisions must remain portable and auditable.

## Decision

ERP systems execute decisions; they do not make decisions.

Odoo is an adapter. Business decisions, deterministic rules, workflow selection, strategy selection, idempotency, matching, and traceability policy live inside ICT IPP.

The Odoo Import Workbench may provide UI, but it must not contain business logic.

## Consequences

Future ERP integrations must use adapter and repository boundaries. Odoo-specific capabilities must not leak into ERP-independent domain logic. Any new ERP write behavior requires explicit approval and documentation.

## Related Documentation

- [Architecture](../ARCHITECTURE.md)
- [Import Workbench](../IMPORT_WORKBENCH.md)
