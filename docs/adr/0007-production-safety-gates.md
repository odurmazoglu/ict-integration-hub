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

### Sanctioned staging Vendor Bill execute exception

A single narrow exception allows a draft `account.move` Vendor Bill write outside `APP_ENV=production` so the incoming-invoice pipeline can be proven end to end against an approved staging Odoo tenant. It never reuses the production flags.

The staging path is allowed only when all of the following hold:

- `APP_ENV != production`
- `STAGING_VENDOR_BILL_EXECUTE_ENABLED=true` (default `false`)
- `ODOO_BASE_URL` hostname is an exact match in the code-owned `APPROVED_STAGING_ODOO_HOSTS` allowlist (no suffix or wildcard matching, never sourced from env)
- a named `approved_by` is supplied at execution time

`EXECUTION_EXECUTE_ENABLED=true` is accepted outside production only when the staging path above is sanctioned. `PRODUCTION_OPERATIONS_ENABLED` and `PRODUCTION_APPROVAL_ACK` must still be false/empty outside production, and `STAGING_VENDOR_BILL_EXECUTE_ENABLED` must be false in production. The exception is scoped to `ExecutionStepType.VENDOR_BILL` only; customer invoice, customer quotation, purchase order, subscription, recharge, and every other executable step type stay blocked. Draft-only guarantees are unchanged: still one `account.move/create`, still no `action_post`, `unlink`, payment, or reconciliation, and still an idempotency check before create.

### Uyumsoft inbound sync execute gate

`UYUMSOFT_ENVIRONMENT` selects which Uyumsoft tenant/WSDL a request targets (`test` or `production`); it is not an execution authorization. `UYUMSOFT_SYNC_EXECUTE_ENABLED` (default `false`) is the separate, explicit opt-in that authorizes `POST /api/v1/sync/uyumsoft/invoices` to run the existing `UyumsoftInvoiceSyncWorkflow -> UyumsoftCanonicalInvoiceImporter -> ImportInvoiceUseCase` pipeline at all. When the gate is `false`, the endpoint fails closed with `404` before any Uyumsoft connector call, Hub persistence, or Workbench projection, for every value of `UYUMSOFT_ENVIRONMENT`. When the gate is `true`, the identical existing workflow becomes reachable against whichever Uyumsoft environment is configured -- no separate production-only code path exists. The gate does not itself authorize any Odoo write: Workbench projection publish, Vendor Bill execution, supplier creation, customer invoice execution, and customer quotation execution each remain independently controlled by their own existing gate (`ODOO_WORKBENCH_PROJECTION_PUBLISH_ENABLED`, `EXECUTION_EXECUTE_ENABLED`, `SUPPLIER_REMEDIATION_WRITE_ENABLED`, `CUSTOMER_INVOICE_EXECUTE_ENABLED`, `CUSTOMER_QUOTATION_EXECUTE_ENABLED`). Production is allowed to boot with this gate at either value; it governs reachability of one endpoint, not runtime validity.

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
- `app/erp/write/odoo_vendor_bill_writer.py`
- `app/application/execution/preflight.py`
- `app/composition/execution.py`
- `app/api/routers/uyumsoft_sync.py`

## Related Documentation

- [Production Readiness](../PRODUCTION_READINESS.md)
- [Integration Flow](../INTEGRATION_FLOW.md)
- [Architecture](../ARCHITECTURE.md)
