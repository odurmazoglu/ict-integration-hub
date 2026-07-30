# Matching

Matching in ICT IPP is deterministic, explainable, and ERP-independent. The Hub must never silently choose between ambiguous candidates.

## Principles

- Prefer exact identifiers over names.
- Use stable priority order.
- Stop when a higher-priority rule returns ambiguity.
- Return reviewable states instead of guessing.
- Keep matching logic outside Odoo.
- Keep AI out of automatic matching decisions.

## Product Matching

Current implementation: `app/matching/product.py`.

Product matching consumes `InternalInvoice` and a `RepositoryProvider`. It does not call Odoo directly and does not import SOAP, FastAPI, SQLAlchemy, or persistence layers.

Priority order:

1. buyer item code -> ERP `default_code`
2. barcode -> ERP barcode
3. seller item code -> ERP `default_code`

Behavior:

- exactly one active candidate: `MATCHED`
- zero active candidates after all identifiers: `NOT_FOUND`
- multiple active candidates at the current priority: `MULTIPLE_MATCHES`
- missing line identifier, missing deterministic identifiers, or repository failure: `INVALID_INPUT`

The matcher does not use product name, description, fuzzy scoring, keyword search, or AI similarity.

## Tax Mapping

Current implementation: `app/tax_mapping/engine.py`.

Tax mapping consumes `InternalInvoice` line-level taxes and a `TaxRepository`. It matches active candidates by:

- company id, when provided
- canonical tax type
- normalized `Decimal` rate

Supported canonical tax types:

- `VAT`
- `WITHHOLDING`
- `EXEMPTION`
- `UNKNOWN`

`UNKNOWN`, unsupported types, malformed rates, negative rates, missing line identifiers, and repository lookup failures become safe invalid results. Zero-rate VAT remains distinct from exemption.

## Odoo Resolution Matching

Current implementation: `app/services/odoo_resolution.py`.

Odoo resolution is read-only and uses existing Odoo records:

- partner: exact VAT/VKN, then exact normalized name
- product: exact `default_code`, then exact normalized name in the mapping-preview flow
- tax: purchase usage, percent amount, company, `price_include`, active
- currency: exact active ISO code
- journal: explicit configured purchase journal id or code

Resolution statuses include `resolved`, `unresolved`, `ambiguous`, `invalid`, and `not_required`.

## Vendor Bill Build Preconditions

Current implementation: `app/billing/builder.py`.

The Vendor Bill builder requires:

- matched supplier partner
- matched product for every invoice line
- matched tax for every line tax
- invoice number
- invoice date
- currency
- positive quantities
- non-negative prices

It produces immutable ERP-neutral `VendorBill` DTOs and a deterministic Odoo account move payload dictionary. It does not send the payload.

## Matching And AI

AI Advisor may explain missing or ambiguous matches. It may not select candidates or override deterministic results.

## Related Documents

- [Rule Engine](RULE_ENGINE.md)
- [Architecture](ARCHITECTURE.md)
- [Strategy Pattern ADR](adr/ADR-0010-strategy-pattern.md)
