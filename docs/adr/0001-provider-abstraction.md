# ADR-0001: Provider Abstraction

- Status: Accepted
- Date: 2026-07-20

## Context

ICT Integration Hub currently integrates with Uyumsoft e-Fatura, but Uyumsoft is an external provider adapter rather than the business domain itself. The system already separates provider communication under `app/connectors/uyumsoft`, business workflows under `app/services`, persistent records under `app/models`, and HTTP concerns under `app/api`.

Future providers such as Logo, SAP, Netsis, or Mikro may be added, but multi-provider support is not implemented yet. The current architecture must avoid leaking Uyumsoft SOAP contracts into the rest of the application while also avoiding premature generic frameworks that do not reflect proven shared behavior.

## Decision

Provider-specific communication and DTO handling remain inside provider adapters. Core invoice processing must use provider-independent models and service contracts.

Uyumsoft SOAP contracts, Zeep objects, WSDL query models, and provider-specific response shapes must not become the application-wide business model. Any future provider will be added through a separate adapter that normalizes into the same internal boundaries where practical.

Shared abstractions should emerge from repeated provider behavior. The project will not introduce a broad multi-provider framework before a second provider proves the common surface.

## Consequences

### Positive

- Core services can be tested without SOAP provider objects.
- Future providers can be introduced without rewriting downstream parsing, mapping, resolution, and persistence layers.
- Provider security and authentication concerns stay isolated.
- Provider-specific quirks can be documented and contained at the connector boundary.

### Negative / Trade-offs

- Some duplicated connector code may exist until common behavior is proven.
- Provider adapters need explicit mapping code.
- Multi-provider runtime selection is not available in the current version.

## Alternatives Considered

- Hard-code the entire ingestion and sync flow around Uyumsoft SOAP types.
- Create a broad generic provider framework before another provider exists.
- Expose provider DTOs directly to API, service, or persistence layers.

These alternatives were rejected because they would make future provider support harder or create abstractions before the system has enough evidence.

## Operational Notes

- Provider connector classes must remain mockable.
- Provider adapters must not raise FastAPI `HTTPException`.
- Provider credentials and raw provider payloads must not be logged.
- New provider work should add an ADR if it changes these boundaries.

## Related Components

- `app/connectors/uyumsoft`
- `app/services`
- `app/schemas/uyumsoft_invoices.py`
- `app/schemas/normalized_invoice.py`

## Related Documentation

- [Architecture](../ARCHITECTURE.md)
- [Integration Flow](../INTEGRATION_FLOW.md)
- [Production Readiness](../PRODUCTION_READINESS.md)
