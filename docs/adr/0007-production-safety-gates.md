# ADR-0007: Production Safety Gates

- Status: Accepted
- Date: 2026-07-20

## Context

ICT Integration Hub can communicate with external provider and ERP systems. Production access carries operational, financial, and security risk. A single accidental environment-variable change must not be enough to enable production operation.

The current production-readiness implementation validates runtime configuration at startup and through readiness checks. It also documents manual go-live approval requirements.

## Decision

Production requires multiple explicit conditions:

- `APP_ENV=production`
- `PRODUCTION_OPERATIONS_ENABLED=true`
- `PRODUCTION_APPROVAL_ACK=APPROVED_FOR_PRODUCTION`
- approved production Uyumsoft environment
- approved production WSDL host
- non-example Odoo host
- non-local database URL
- non-placeholder credentials

Contradictory configuration must fail at startup. Test and production endpoint separation is mandatory. Secrets must never be logged. Runtime gates complement operational governance but do not replace manual go-live approval.

## Consequences

### Positive

- Production is protected against a single accidental setting change.
- Unsafe endpoint and placeholder credential combinations are rejected before serving.
- Readiness can report safe configuration status without provider mutation.
- Operations teams have explicit manual gates to review before production use.

### Negative / Trade-offs

- Production rollout requires more configuration work.
- Some valid-but-unusual deployment topologies may need explicit policy updates.
- Runtime gates cannot prove business approvals occurred beyond the configured acknowledgement value.

## Alternatives Considered

- Use `APP_ENV=production` alone.
- Rely only on documentation and manual checklists.
- Let production and test endpoints be selected independently without contradiction checks.
- Run provider checks automatically during readiness.

These alternatives were rejected because they are either too easy to misconfigure or risk unsafe external calls.

## Operational Notes

- Provider readiness checks must remain explicit and read-only.
- Production credentials and endpoints must be stored outside the repository.
- Production go-live remains subject to finance/business, technical, rollback, and monitoring owner approval.
- Runtime validation messages must avoid passwords, API keys, and full connection strings.

## Related Components

- `app/core/config.py`
- `app/core/runtime_checks.py`
- `app/api/routers/health.py`
- `app/core/logging.py`

## Related Documentation

- [Production Readiness](../PRODUCTION_READINESS.md)
- [Integration Flow](../INTEGRATION_FLOW.md)
- [Architecture](../ARCHITECTURE.md)
