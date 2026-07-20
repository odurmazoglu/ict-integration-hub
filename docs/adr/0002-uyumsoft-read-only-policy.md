# ADR-0002: Uyumsoft Read-Only Policy

- Status: Accepted
- Date: 2026-07-20

## Context

The current priority is to pull incoming and outgoing e-Fatura data from the Uyumsoft test environment and store it safely in Integration Hub. Uyumsoft exposes operations that can list, retrieve, download, acknowledge, send, cancel, retry, or otherwise change provider-side document state.

The project has deliberately implemented read-only listing, identity/system probes, WSDL discovery, and UBL XML document retrieval. It has not implemented status-changing provider operations.

## Decision

Uyumsoft access is read-only by default.

Allowed operations are limited to:

- `TestConnection`
- `WhoAmI`
- `GetSystemDate`
- `GetInboxInvoiceList`
- `GetOutboxInvoiceList`
- `GetInboxInvoiceData`
- `GetOutboxInvoiceData`

Status-changing, acceptance, rejection, acknowledgment, cancellation, receipt, send, retry-send, move-to-draft, or similar operations are forbidden unless separately approved in a dedicated task and ADR.

The integration must never silently change provider-side document status. Production permissions must follow least privilege and must not include provider-side mutation permissions by default.

## Consequences

### Positive

- Uyumsoft test and production access can be granted with lower operational risk.
- Invoice metadata and UBL documents can be collected without changing provider-side workflow state.
- Provider-side accounting or legal document status remains under explicit human or provider-system control.
- Tests can enforce that forbidden operation names are not used by workflows.

### Negative / Trade-offs

- Any future acknowledgement or status workflow requires a separate design and approval.
- Operations teams may need manual provider-side handling until write workflows are explicitly added.
- Read-only permissions may need careful provider account configuration.

## Alternatives Considered

- Automatically mark invoices as taken after download.
- Implement Uyumsoft send/cancel/retry operations with feature flags.
- Allow connector code to call any WSDL operation by name.

These alternatives were rejected because they risk silent provider-side state changes and violate the current safety boundary.

## Operational Notes

- Provider smoke tests must remain explicit, opt-in, and read-only.
- Production Uyumsoft permissions should include invoice listing, invoice detail retrieval, and UBL download only.
- SOAP faults and provider errors must be surfaced as safe connector errors, not swallowed.

## Related Components

- `app/connectors/uyumsoft/client.py`
- `app/services/uyumsoft_invoice_sync.py`
- `app/services/document_service.py`
- `scripts/uyumsoft_readonly_smoke.py`

## Related Documentation

- [Integration Flow](../INTEGRATION_FLOW.md)
- [Production Readiness](../PRODUCTION_READINESS.md)
- [Architecture](../ARCHITECTURE.md)
