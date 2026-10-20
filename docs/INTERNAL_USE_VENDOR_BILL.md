# INTERNAL_USE Vendor Bills for product invoices

## Problem

Some invoices from product suppliers are bought for ICT's own use. One example is
J062026000000005 (LOGOSOFT): Microsoft 365 Business Basic/Standard for ICT staff.

These lines match real Odoo products (393/394). Those products sit in category 41,
whose purchase account (294 / 153200) is configured for RESALE. A Vendor Bill line sent
with only `product_id` would therefore book an internal subscription to the RESALE
account. The category must not change globally, because RESALE depends on it.

## Behaviour

| Step | Endpoint / Workbench action | Rule |
|---|---|---|
| 1. Purpose | `POST /reviews/{id}/purchase-purpose` `internal_use` | Also allowed on a *technically unblocked* review (no reasons, workflow `vendor_bill`) when at least one line carries a product identifier. |
| 2. Account | `POST /reviews/{id}/accounting-resolution` `expense_account` + `expense_category` | On an unblocked INTERNAL_USE review this only records the selection. Nothing is reclassified and the review version does not change. The account must be an eligible, company-scoped `expense` account (the existing `find_eligible_by_id` rule). |
| 3. Decision | `POST /reviews/{id}/decision` (or Workbench "Karar") | `InternalUseDecisionGate` requires the step-2 resolution for the *same* review version. Every line must be matched to exactly one product, and no line may be account-only. The account is re-validated and pinned with the decision (`execution_source_invoice_evidence.internal_use_accounting_pin`). |
| 4. Preview | `GET /reviews/{id}/vendor-bill-preview` | Shows `internal_use_accounting` and the pinned `account_id` on every line. |
| 5. Execute | write authorization + execute (unchanged) | `InternalUseExecutionAccountingValidator` re-reads the pinned account before any Odoo call. It fails closed (`internal_use_execution_accounting_error`) if the pin is missing or invalid, or if the account is no longer eligible or its code/type drifted. Each line is sent with `product_id` **and** the pinned `account_id`. |
| 6. Readback | `GET /reviews/{id}/vendor-bill-readback` | `internal_use_verification` checks that every Odoo line is on the pinned account and that the products match the pin. |

### What does not change

- **RESALE:** pin, drift and fiscal-position checks work as before; the RESALE and INTERNAL_USE pins are mutually exclusive.
- **Identifier-free INTERNAL_USE invoices** (operating-expense / fixed-asset paths) behave exactly as before. The new gate and validator do nothing for them.
- **FX:** the UBL `PricingExchangeRate` remains authoritative (`invoice_currency_rate`).
- **Taxes, draft-only:** tax mapping is unchanged and bills are never posted (`action_post` is never called).
- **Duplicate safety:** the decision idempotency key is unchanged and a replayed decision is never re-pinned. Retries reuse the same `invoice_origin` lookup.

### Audit evidence

Each step leaves a record:
- **Purpose:** `workbench_review_purchase_purpose_resolutions` holds the purpose, actor and version.
- **Account selection:** `workbench_review_accounting_resolutions` holds the account, category and actor.
- **Accepted pin:** `execution_source_invoice_evidence.internal_use_accounting_pin` holds the account id/code/name/type, the category, the resolution id and the per-line products.
- **Odoo result:** readback `internal_use_verification` confirms what Odoo actually stored.

## Operator workflow (Odoo Workbench)

`next_action` becomes **Muhasebe İşlemi Seçilmeli** once INTERNAL_USE is recorded on an
unblocked review, and stays there until an expense account is recorded for that version.
It then returns to **Karar Verilmeli**. The Hub side is complete.

Before an operator can do this from the UI, the Studio views must show the existing
accounting-resolution fields at those stages. That is an Odoo configuration change and
needs separate approval. Until then, use the API.

## Rollout / rollback

- **Migration `202607170043`:** additive, one nullable JSON column. Downgrade drops it; run the downgrade only while no row holds a pin.
- **Feature flags:** none. Behaviour activates only when an operator records INTERNAL_USE on an unblocked product review.
- **Code rollback:** redeploy the previous image. Decisions pinned in the meantime cannot execute on the old code: the old code ignores the pin, which is exactly the unsafe behaviour this change removes. Do not execute them until the new code is back.
