# ADR-0005: ETTN Idempotency

- Status: Accepted
- Date: 2026-07-20

## Context

E-Fatura documents include an ETTN/UUID that identifies the source invoice. Integration Hub persists Uyumsoft invoice metadata, downloaded document metadata, sync run state, and Odoo draft creation references.

Reprocessing the same source invoice can happen through repeated sync windows, retries, partial failures, and manual reruns. Duplicate Odoo draft vendor bills would create finance risk.

## Decision

ETTN/UUID is the primary idempotency key for invoice processing and Odoo draft creation.

Repeated processing of the same source invoice must not create duplicate invoice metadata records or duplicate Odoo draft vendor bills. Local persistence records external creation results and retries must respect existing ETTN records.

When ETTN is missing in provider metadata persistence, a documented deterministic fallback identity strategy is used. For Odoo draft creation, ETTN remains required.

Idempotency does not mean every operation can be retried blindly. External writes and local persistence have separate failure modes.

## Consequences

### Positive

- Repeated sync runs can safely refresh metadata.
- Odoo draft creation can detect existing successful creation by ETTN.
- Partial failures can be retried with lower duplicate risk.
- ETTN supports manual reconciliation across source, Integration Hub, and Odoo.

### Negative / Trade-offs

- Draft creation cannot proceed without ETTN.
- Idempotency depends on correct source UUID handling and local persistence availability.
- Distributed systems can still produce reconciliation cases when an external write succeeds but local persistence fails afterward.

## Alternatives Considered

- Use provider invoice ID as the only idempotency key.
- Use invoice number and date only.
- Let Odoo duplicate detection handle idempotency.
- Retry Odoo creation blindly after connector or persistence errors.

These alternatives were rejected because they provide weaker uniqueness guarantees or shift duplicate risk into Odoo operations.

## Operational Notes

- Manual reconciliation should use ETTN/UUID and Odoo move references.
- Retry workflows must check local idempotency state before external creation.
- Duplicate draft concerns require manual review, not automatic unlink or destructive cleanup.

## Related Components

- `app/models/uyumsoft_invoice.py`
- `app/services/invoice_persistence.py`
- `app/models/odoo_draft_invoice.py`
- `app/services/odoo_draft_invoice.py`

## Related Documentation

- [Production Readiness](../PRODUCTION_READINESS.md)
- [Integration Flow](../INTEGRATION_FLOW.md)
- [Architecture](../ARCHITECTURE.md)
