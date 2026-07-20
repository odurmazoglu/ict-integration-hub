# Edge Cases

This document describes uncommon situations and expected behavior. It does not authorize new runtime behavior.

## Duplicate Imports

Expected behavior:

- Invoice metadata uses ETTN/UUID when present and fallback identity when missing.
- Repeated syncs update or skip existing rows; they do not create duplicates.
- Draft vendor bill creation uses local ETTN idempotency and must not create a second Odoo draft for the same ETTN.

## Provider Timeout

Expected behavior:

- Uyumsoft transport timeouts are reported as connector timeout errors.
- Retries are bounded and limited to transient transport failures.
- SOAP faults and authorization failures are not retried as transient transport errors.
- Logs contain safe categories and durations only.

## Odoo Timeout

Expected behavior:

- Odoo timeout is returned as a safe connector error.
- Resolution does not mutate Odoo.
- Draft creation does not retry unsafely without ETTN idempotency protection.
- Full Odoo payloads, credentials, and secrets are not logged.

## Database Unavailable

Expected behavior:

- Readiness fails when database connectivity is unavailable.
- Persistence workflows fail safely.
- Workflows should not continue to external write stages when required local persistence preconditions are unavailable.

## Disk Full

Expected behavior:

- Document download/storage fails safely.
- XML bytes are not stored in PostgreSQL as a fallback.
- Existing metadata is not silently marked successful when the file was not stored.

## Storage Unavailable

Expected behavior:

- Readiness detects non-writable document storage when applicable.
- Parser reports storage read failure safely.
- Document download does not log XML content.

## Invalid Credentials

Expected behavior:

- Uyumsoft or Odoo authentication failures are classified as configuration or authorization failures.
- Credentials are never printed.
- Authentication failures do not trigger state-changing fallback behavior.

## Network Interruption

Expected behavior:

- Bounded retries apply only to transient transport failures.
- Partial sync progress is recorded when a sync run fails after some pages.
- Re-running the same bounded window is safe because metadata persistence is idempotent.

## Partial Persistence

Expected behavior:

- If provider reads succeed but local persistence fails, the operation is reported failed.
- Re-running after local recovery must not create duplicates.
- If Odoo draft creation succeeds but local persistence fails afterward, use the manual reconciliation procedure in [Production Readiness](../PRODUCTION_READINESS.md); do not unlink or recreate blindly.

## Ambiguous Partner

Expected behavior:

- Odoo Resolution Engine reports ambiguity.
- No partner is auto-selected.
- Draft creation is blocked until reviewed Odoo ids are supplied.

## Ambiguous Product

Expected behavior:

- Product ambiguity is reported per line.
- No product is created or selected automatically.
- Draft creation is blocked until reviewed ids are supplied.

## Unexpected XML Namespace

Expected behavior:

- Parser handles supported UBL namespaces used by representative UBL-TR fixtures.
- Unsupported or unexpected structures fail with a safe parser error instead of silently dropping required data.

## Unknown Tax

Expected behavior:

- Parser preserves tax values from XML.
- Mapping preview reports missing or unsupported tax where needed.
- Odoo resolution leaves unknown taxes unresolved; it does not create taxes.

## Unsupported Currency

Expected behavior:

- Parser preserves the document currency code.
- Mapping preview reports missing currency when absent.
- Odoo resolution reports unresolved or inactive currency; it does not create currency records.

## Cancelled Invoice Status

Expected behavior:

- If Uyumsoft returns cancelled-like metadata, the provider status is preserved.
- The system does not acknowledge, accept, reject, cancel, or mutate provider state.
- Finance and technical owners decide whether the invoice should proceed in UAT or production.
