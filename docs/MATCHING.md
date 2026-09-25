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

Legacy priority chain (unchanged; the first identifier with any active candidate stops it):

1. buyer item code -> ERP `default_code`
2. barcode (UBL `StandardItemIdentification` only) -> ERP barcode
3. seller item code -> ERP `default_code`

Authoritative manufacturer SKUs (P0-PROD-19A-2), each always looked up when present:

- `manufacturer_item_code` (UBL `ManufacturersItemIdentification`) -> ERP `default_code`, for every supplier (`matched_by="manufacturer_item_code"`)
- a supplier source-profile SKU -> ERP `default_code` (`matched_by="supplier_profile_sku"`); see below

Behavior:

- the legacy chain outcome and every SKU outcome are combined; no identifier silently wins
- all resolving identities agree on one active product: `MATCHED` (`matched_by` is the first agreeing identity, the others are named in `reason` as corroborating)
- any identity with more than one active candidate: `MULTIPLE_MATCHES`
- identities resolving to different products: `MULTIPLE_MATCHES` with reason `Conflicting product identities resolve to different products: ...` (fail closed; surfaces as `PRODUCT_AMBIGUOUS`)
- zero active candidates for every identity: `NOT_FOUND`
- missing line identifier or missing deterministic identifiers: `INVALID_INPUT`; repository failure raises `ProductMatchingError`
- a line without an authoritative SKU performs exactly the legacy lookups and returns exactly the legacy result

Conflicts reuse the existing persisted `MULTIPLE_MATCHES` status on purpose: a new status value would make persisted evidence unreadable after a rollback.

### Supplier Source Profiles

`app/domain/invoice/source_profiles.py`. A source profile says where one known supplier places an authoritative manufacturer SKU when `ManufacturersItemIdentification` is absent. Profiles are keyed on the invoice supplier tax number, never on names, and live in the domain layer, not in the ERP adapter.

VİTEL (VKN `9250020961`) puts the ManageEngine SKU in `cac:Item/cbc:Description`. That value is an authoritative SKU only when:

- the supplier VKN (trimmed) is exactly `9250020961`;
- `InvoiceLine.description_source == "Description"`, i.e. the description provably came from `cbc:Description`, not from the `cbc:Name` fallback and not from evidence of unknown provenance;
- the whole trimmed value fully matches `[0-9]{4,6}\.[0-9][0-9A-Z]{1,9}` (e.g. `85710.1S1`).

Prose, substrings, lowercase variants, `cbc:Name` and every other supplier's Description never participate. The product matching package itself never reads `description`.

The matcher does not use product name, description, fuzzy scoring, keyword search, or AI similarity.

### UBL Item Identity Namespaces (P0-PROD-19A-1)

The parser keeps each UBL item identifier in its own `InvoiceLine` field and never folds one namespace into another:

| UBL element | `InvoiceLine` field | Meaning |
| --- | --- | --- |
| `BuyersItemIdentification/ID` | `buyer_item_code` | buyer's own code |
| `SellersItemIdentification/ID` | `seller_item_code` | supplier/distributor code (e.g. VİTEL `1531012114`), not a manufacturer SKU |
| `ManufacturersItemIdentification/ID` | `manufacturer_item_code` | manufacturer/vendor SKU; exact `default_code` lookup since 19A-2 |
| `StandardItemIdentification/ID` | `barcode` | true barcode only |
| `CommodityClassification/ItemClassificationCode` | `commodity_classification` | classification (e.g. `Subscription`); never a barcode, never looked up |

Before 19A-1, `CommodityClassification` was a fallback for `barcode`, so a classification such as `Subscription` could reach an Odoo barcode lookup. That fallback is removed.

The operating-expense / RESALE routing predicates (`invoice_is_product_identifier_free`, `invoice_has_product_identifier`) count every identity the matcher looks up (since 19A-2 including `manufacturer_item_code` and a source-profile SKU). They still count `commodity_classification`, which keeps the routing boundary that existed before the fallback was removed. Changing that boundary is a separate business decision.

Source evidence writes `description_source`, `manufacturer_item_code` and `commodity_classification` only when present (schema_version stays 1, no migration). Evidence persisted earlier round-trips byte-identically and keeps any stored `barcode` value verbatim.

## Supplier Partner Matching

Current implementation: `app/matching/partner.py`.

Supplier matching consumes `InternalInvoice` and a `RepositoryProvider`. It matches only by supplier tax number through the partner repository.

Behavior:

- exactly one active candidate: `MATCHED`
- zero active candidates: `NOT_FOUND`
- multiple active candidates: `MULTIPLE_MATCHES`
- missing supplier tax number or invalid invoice DTO: `INVALID_INPUT`
- repository/provider failure: raise `PartnerMatchingError` with safe diagnostic text

The matcher does not use supplier name fallback, fuzzy scoring, keyword search, or AI similarity.

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
