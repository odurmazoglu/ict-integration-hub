# Product Matching Safety — Production Transition

Scope: retiring `seller_item_code -> product.default_code` as an independent product identity
(see `docs/MATCHING.md`, "Product Matching"). This document is the controlled rollout plan.
Nothing in this change writes production data, and no migration seeds mappings.

## What changes

| Identity | Before | After |
| --- | --- | --- |
| `(supplier, seller_item_code) -> product.supplierinfo -> variant` | deterministic, combined | deterministic, **primary** `matched_by` |
| buyer item code -> `default_code` | deterministic | deterministic (unchanged) |
| barcode -> `barcode` | deterministic | deterministic (unchanged) |
| manufacturer / profile SKU -> `default_code` | deterministic | deterministic (unchanged) |
| seller item code -> `default_code` | deterministic (legacy chain, step 3) | **advisory only**: never `MATCHED`, never a conflict, never ambiguity |

A line whose only hit was the seller-code probe becomes `NOT_FOUND` (`PRODUCT_NOT_FOUND`) and needs
an explicit supplier-specific mapping (the #208 "Ürün Eşleştir" action, or an Odoo supplierinfo row).

## Production snapshot (read-only, 2026-10-07, production at `de3e300`)

Supplier: LOGOSOFT BİLİŞİM TEKNOLOJİLERİ SAN.VE TİC.A.Ş. — `res.partner` **450**, VAT `6090213253`,
`is_company=true`, own commercial partner, active.

Products (both active, `company_id=false`, `type=service`, single-variant templates):

| Seller code | `product.product` | template | `default_code` |
| --- | --- | --- | --- |
| `CFQ7TTC0LH18:0001` | 393 Microsoft 365 Business Basic | 162 | `CFQ7TTC0LH18:0001` |
| `CFQ7TTC0LDPB:0001` | 394 Microsoft 365 Business Standard | 163 | `CFQ7TTC0LDPB:0001` |

Existing supplier-specific mappings — **already present**, exactly the required shape:

| `product.supplierinfo` | partner | product_code | template | variant | company |
| --- | --- | --- | --- | --- | --- |
| 6 | 450 | `CFQ7TTC0LH18:0001` | 162 | 393 | 1 |
| 7 | 450 | `CFQ7TTC0LDPB:0001` | 163 | 394 | 1 |

These are the only supplierinfo rows for partner 450 and the only rows carrying either code.

Affected Hub evidence (six lines, all `MATCHED` with `matched_by=seller_item_code`):

| Review | Invoice | Status (version) | Line → product |
| --- | --- | --- | --- |
| `review:2fb46a57-…` | I182026000000037 | decision_submitted (v3, evidence v2) | 1 → 394 |
| `review:cee1f8d5-…` | I182026000000047 | decision_submitted (v3, evidence v2) | 1 → 393 |
| `review:267f23a2-…` | J062026000000005 | pending_review (v1) | 1 → 393, 2 → 394 |
| `review:9afe7695-…` | J062026000000021 | pending_review (v1) | 1 → 393, 2 → 394 |

With supplierinfo 6/7 in place, the new matcher resolves every one of these lines to the same
product, now with `matched_by=supplier_product_code` and the seller-code hit as advisory
corroboration (covered by `test_logosoft_shapes_match_once_explicit_supplier_mappings_exist`).

## Safe production sequence

Every step that touches production needs explicit operator approval at the time it runs.

1. **Mappings (no write expected).** Re-read supplierinfo 6 and 7 read-only and confirm partner 450,
   the two codes, variants 393/394 and company 1 are unchanged. If either row is missing or
   different, stop: create/repair it through an approved, explicit operator action (never a
   migration, never code), then re-read.
2. **Census gate (read-only).** In the Hub DB, list every latest-version product evidence line with
   `status=MATCHED` and `matched_by=seller_item_code`, across all suppliers. Expected: exactly the
   six lines above. For any other line, confirm a supplierinfo row for its `(partner, seller code)`
   resolves to the same product, or create one through step 1 before continuing.
3. **Prove (read-only).** For the four reviews, replay the new matcher against their stored invoice
   evidence and the deterministic partner match with read-only Odoo repositories, persisting
   nothing. Expected: six `MATCHED`, products 393/394, `matched_by=supplier_product_code`.
4. **Deploy** the matcher change through the standard procedure (detached checkout of the merge
   SHA, `docker compose -f docker-compose.prod.yml up -d --build api`, recreate the poller), then
   health check. Note: main already contains #208 (migration 0037), so this deploy also ships #208
   unless #208 is provisioned first; order those two deliberately.
5. **Reclassification: not required.** Persisted evidence is immutable and already names the correct
   products; a future reclassification of these reviews yields the same products via supplierinfo,
   so no status change is expected. Reclassify only if step 3 disagrees.

## Rollback

Code-only change: redeploy the previous SHA. No migration, no data written, no evidence shape change
(`matched_by` values and `ProductMatchStatus` are pre-existing), so evidence produced either way stays
readable by both versions.

## Residual effect after deploy

A new invoice from a supplier *without* a supplierinfo row, whose seller code happens to equal an
ICT Internal Reference, now lands as `PRODUCT_NOT_FOUND` instead of an automatic match. The operator
maps it once per supplier (#208), after which matching is deterministic and supplier-scoped.
