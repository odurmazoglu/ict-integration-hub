# ADR-0004: Odoo Resolution Engine

- Status: Accepted
- Date: 2026-07-20

## Context

Mapping Preview produces business-level Odoo-compatible intent from normalized invoice data. It prepares draft invoice structure, line intent, partner candidate data, product candidates, tax candidates, currency, and journal intent, but it does not resolve Odoo database IDs.

Existing Odoo records must be resolved before draft creation can safely run. The project needs deterministic matching and explicit ambiguity handling without mutating Odoo master data.

## Decision

Odoo Resolution is a dedicated read-only layer that converts business identifiers into existing Odoo IDs.

Resolution rules:

- Partner matching uses VAT/VKN first, then normalized exact name fallback.
- Product matching uses `default_code` first, then normalized exact name fallback.
- Currency resolution uses exact ISO code.
- Journal resolution uses explicit configuration by purchase journal id or code.
- Tax resolution is deterministic and currently limited to supported purchase-percentage taxes.

Resolution never creates or updates partner, product, tax, currency, or journal master data. Fuzzy matching, contains matching, heuristic matching, and AI matching are intentionally excluded.

Ambiguous and missing matches are returned as structured review states. The system must not silently pick one candidate when multiple exact candidates exist.

## Consequences

### Positive

- Finance review remains possible before draft creation.
- Master-data mutation is not hidden inside invoice processing.
- Matching behavior is deterministic, testable, and auditable.
- The draft-creation service can require already reviewed/resolved Odoo IDs.

### Negative / Trade-offs

- More invoices may require manual review when Odoo master data is incomplete or ambiguous.
- Exact matching may reject data that a human could reasonably recognize.
- Tax matching currently supports only the implemented purchase-percentage cases.

## Alternatives Considered

- Resolve IDs inside Mapping Preview.
- Create missing Odoo master-data records automatically.
- Use fuzzy name matching or AI-assisted matching.
- Fall back to the first candidate when multiple matches exist.

These alternatives were rejected because they reduce auditability and can create accounting or master-data errors.

## Operational Notes

- Odoo resolution uses read-only JSON-2 `search_read` on allowlisted models.
- Missing or ambiguous records should be reviewed by finance or system owners.
- Configured purchase journal values are operational configuration, not parser output.
- Resolution must not call draft creation.

## Related Components

- `app/services/odoo_resolution.py`
- `app/schemas/odoo_resolution.py`
- `app/connectors/odoo/client.py`
- `app/services/odoo_mapping_preview.py`

## Related Documentation

- [Integration Flow](../INTEGRATION_FLOW.md)
- [Architecture](../ARCHITECTURE.md)
- [Production Readiness](../PRODUCTION_READINESS.md)
