# ADR-0008: Local Persistence After External Write

- Status: Accepted
- Date: 2026-07-20

## Context

Odoo draft creation occurs in an external system through the Odoo JSON-2 API. Integration Hub stores a local `odoo_draft_invoices` record to preserve ETTN idempotency, operation status, Odoo move reference, attempt count, and safe error information.

A distributed transaction across Odoo and PostgreSQL is not available. Once Odoo creates a draft, Integration Hub cannot atomically guarantee that local persistence succeeds in the same transaction.

## Decision

Integration Hub records the Odoo draft creation result locally after the external Odoo write.

If Odoo draft creation succeeds and local persistence fails afterward, the system may require manual reconciliation. ETTN, source UUID, invoice number, and Odoo move references must support that reconciliation.

Destructive automatic cleanup is not allowed. Blind draft recreation is not allowed. Future improvements may include reconciliation jobs or stronger outbox-style workflows, but they are not part of the current version.

## Consequences

### Positive

- The current workflow remains simple and explicit.
- ETTN-based local records reduce duplicate draft risk for normal retries.
- Manual reconciliation can identify source invoice and Odoo draft using durable references.
- The integration avoids destructive Odoo cleanup behavior.

### Negative / Trade-offs

- Rare external-write/local-persistence split-brain cases remain possible.
- Operational runbooks are required for reconciliation.
- Full exactly-once behavior across Odoo and PostgreSQL is not guaranteed.
- Future automation may be needed if volume or incident rate increases.

## Alternatives Considered

- Attempt a distributed transaction across Odoo and PostgreSQL.
- Automatically unlink Odoo drafts when local persistence fails.
- Always retry draft creation without checking local or Odoo state.
- Delay Odoo creation until a separate outbox worker is implemented.

These alternatives were rejected because they are unavailable, destructive, duplicate-prone, or beyond the current implementation scope.

## Operational Notes

- Manual reconciliation must verify Odoo draft state before any retry.
- Reconciliation must not post, unlink, cancel, or mutate provider state automatically.
- Incident notes should record ETTN/UUID and Odoo move id.
- Future reconciliation automation should get its own ADR if it changes this consistency model.

## Related Components

- `app/services/odoo_draft_invoice.py`
- `app/models/odoo_draft_invoice.py`
- `app/connectors/odoo/client.py`

## Related Documentation

- [Production Readiness](../PRODUCTION_READINESS.md)
- [Integration Flow](../INTEGRATION_FLOW.md)
- [Architecture](../ARCHITECTURE.md)
