# ADR-0003: Provider-Independent Normalized Models

- Status: Accepted
- Date: 2026-07-20

## Context

The document layer stores UBL XML downloaded from Uyumsoft. The parser reads stored `UBL_XML` documents and produces normalized invoice structures. Later layers generate Odoo mapping previews, resolve existing Odoo IDs, and create draft vendor bills when explicitly requested.

The parser must not know about Uyumsoft SOAP transport details or Odoo database identifiers. The normalized invoice model is the contract between ingestion and ERP-specific mapping.

## Decision

UBL parsing produces provider-independent normalized invoice models.

Parser output must not contain:

- Odoo IDs
- Uyumsoft SOAP objects
- Zeep model instances
- transport-layer response structures
- storage implementation details

Mapping, resolution, and persistence consume normalized data through typed internal models. Parsing remains deterministic and side-effect free: it parses local bytes, validates fields, returns normalized models, and raises safe parser errors when parsing fails.

## Consequences

### Positive

- Parser tests can use synthetic UBL fixtures without provider credentials.
- Odoo mapping and resolution can evolve without changing XML parsing.
- Future providers can feed equivalent UBL or normalized invoice data into the same downstream layers.
- Provider-specific transport failures stay outside parser behavior.

### Negative / Trade-offs

- Some provider-specific fields may need explicit mapping into normalized `extra` or related structures before downstream layers can use them.
- Parser output cannot directly optimize for Odoo IDs or provider SOAP conveniences.
- Future non-UBL providers may require an adapter into the normalized model contract.

## Alternatives Considered

- Parse Uyumsoft SOAP response objects directly into Odoo payloads.
- Include Odoo IDs in normalized parser output.
- Let the parser perform Odoo matching or provider calls.

These alternatives were rejected because they couple independent layers and make testing, future provider support, and safe failure handling weaker.

## Operational Notes

- XML parsing must remain local-only and must not resolve external entities.
- Parser errors must not include full XML content.
- Normalized monetary values use `Decimal`.
- Normalized date/time values should be timezone-aware where possible.

## Related Components

- `app/services/document_parser.py`
- `app/schemas/normalized_invoice.py`
- `app/services/odoo_mapping_preview.py`
- `tests/fixtures/ubl`

## Related Documentation

- [Integration Flow](../INTEGRATION_FLOW.md)
- [Architecture](../ARCHITECTURE.md)
