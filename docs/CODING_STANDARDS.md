# Coding Standards

These standards apply to implementation work in ICT Integration Hub. Documentation-only PRs should not modify source code, tests, dependencies, migrations, or runtime behavior.

## Language And Tooling

- Use Python 3.12 type hints.
- Keep `ruff check .`, `ruff format --check .`, and `pytest` green.
- Add tests for every implementation change.
- Add Alembic migrations for schema changes.
- Do not add dependencies without a clear PR justification.

## Architecture Boundaries

- Keep provider-specific SOAP/WSDL code under `app/connectors/uyumsoft`.
- Keep Odoo JSON-2 transport code under Odoo connector/adapter modules.
- Keep business workflows under `app/services`.
- Keep immutable ERP-independent domain concepts under `app/domain`, `app/matching`, `app/tax_mapping`, and `app/billing` where applicable.
- Keep SQLAlchemy persistence under `app/models` and `app/db`.
- Keep HTTP behavior under `app/api`.

Connector layers must not raise FastAPI `HTTPException`. They should raise provider/domain exceptions that API routers can map safely.

## DTOs

- Prefer frozen dataclasses for domain DTOs and matching results.
- Prefer typed Pydantic models for API schemas.
- Do not pass Zeep objects, raw SOAP responses, or Odoo transport payloads across application boundaries.
- Use `Decimal` for monetary values and rates.
- Use timezone-aware datetimes where runtime timestamps are stored.

## Matching And Rules

- Use deterministic exact matching.
- Make priority order explicit.
- Return ambiguous and missing states instead of selecting silently.
- Do not introduce fuzzy matching or AI matching into business decisions without a new accepted ADR.

## External Systems

Uyumsoft:

- Use the test environment by default.
- Allowed operations remain read-only unless a future accepted ADR changes the boundary.
- Do not call `SetInvoicesTaken`, `SendInvoice`, `Cancel*`, `RetrySendInvoices`, `MoveToDraftStatus`, or similar mutations.

Odoo:

- Treat Odoo as an adapter.
- Use read-only `search_read` for resolution.
- Create only draft vendor bills when explicitly confirmed and allowed.
- Do not call `action_post`, unlink, payment registration, reconciliation, or automatic master-data creation.

## Logging And Errors

Structured logs may include safe identifiers and aggregate counts. They must not include:

- passwords
- API keys
- tokens
- full database URLs
- SOAP envelopes
- raw XML
- full invoice payloads
- full Odoo payloads

Errors should be clear, safe, and actionable. Do not swallow SOAP/API failures.

## Documentation

Update documentation and ADRs when implementation changes:

- architecture boundaries
- workflow behavior
- safety gates
- provider or ERP capabilities
- persistence or idempotency behavior
- AI or rule authority

## Related Documents

- [Project Constitution](PROJECT_CONSTITUTION.md)
- [Development Workflow](DEVELOPMENT_WORKFLOW.md)
- [Contributing](CONTRIBUTING.md)
