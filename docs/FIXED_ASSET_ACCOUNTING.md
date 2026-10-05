# Fixed-asset accounting (CAPITALIZE_FIXED_ASSET)

Company-owned equipment (phones, computers, laptops, office equipment, other
depreciable equipment) can be capitalized instead of being forced through an
operating-expense account. The capability is generic; nothing is supplier- or
invoice-specific.

## Who is responsible for what

| Party | Responsibility |
|---|---|
| **Hub** | Records the business classification (purchase purpose `internal_use`) and the explicit accounting decision `capitalize_fixed_asset` with the operator-selected `asset_account_id` + `depreciation_model_id`; validates both read-only against Odoo; freezes them into execution evidence; writes a deterministic **draft** Vendor Bill whose lines carry `account_id` + `depreciation_model_id`. Never computes depreciation, never creates `account.asset`, never posts (`action_post`). |
| **Odoo** | Native asset creation and depreciation when a human posts the Vendor Bill (Assets module, `account.depreciation.model`). |
| **Accountant** | Which accounts are fixed-asset accounts (e.g. demirbaş), the accumulated depreciation account, the depreciation expense account, depreciation models / useful life, and VAT policy. |
| **Operator** | Selects an approved asset account and depreciation model per review, and explicitly approves decision and execution. |

## Flow

1. `POST /api/workbench/reviews/{id}/purchase-purpose` with `purchase_purpose=internal_use`.
2. `POST /api/workbench/reviews/{id}/accounting-resolution`:

   ```json
   {
     "expected_version": 3,
     "treatment_type": "capitalize_fixed_asset",
     "asset_account_id": 74,
     "depreciation_model_id": 3,
     "note": "Company-owned equipment"
   }
   ```

   The ids are examples. The review is reclassified; `OPERATING_EXPENSE_MAPPING_REQUIRED`
   is cleared and the workflow becomes `vendor_bill`.
3. The normal Vendor Bill decision and the explicitly authorized execution follow,
   unchanged. The draft bill has **one line per invoice line** (never merged):

   ```json
   {"name": "...", "quantity": "1", "price_unit": "114999.17",
    "account_id": 74, "tax_ids": [[6, 0, [34]]], "depreciation_model_id": 3}
   ```

4. A human reviews and posts the bill in Odoo. Odoo then applies its native asset
   behavior.

## Validation (all read-only, before anything is persisted)

* **Fields per treatment.** `expense_account` needs `expense_account_id` and
  `expense_category`. `capitalize_fixed_asset` needs `asset_account_id` and
  `depreciation_model_id`. A payload carrying the other treatment's fields is
  rejected (HTTP 400), and so is a database row (CHECK constraint).
* **Purpose.** `capitalize_fixed_asset` is allowed only for `internal_use`. `resale`,
  `customer_project` and `other_operating_expense` are rejected.
* **Asset account.** The account must be in `ODOO_FIXED_ASSET_ACCOUNT_IDS` (an empty
  list fails closed, HTTP 503). It must also exist, be active, belong to the review's
  company, have `account_type = asset_fixed`, and have `can_create_asset = true` where
  this Odoo version has that field. The allowlist is what keeps accounts such as
  *accumulated depreciation* (also `asset_fixed` in the Turkish chart) or unrelated
  `asset_fixed` accounts from ever being selected.
* **Depreciation model.** The `account.depreciation.model` must exist, be active, and
  be global or belong to the review's company. No useful life or method is hard-coded.

## Freezing and idempotency

The accepted selection is copied into Stage-1 (review) and Stage-2 (decision)
execution evidence as `fixed_asset_accounting`:

```json
{"schema_version": 1, "source": "review_accounting_resolution",
 "accounting_resolution_id": 12, "asset_account_id": 74, "depreciation_model_id": 3}
```

Execution, preview and every retry/resume build the bill from that frozen evidence
together with the frozen tax mapping. They never read Odoo account defaults
(`account.account.depreciation_model_id`), so a later Odoo configuration change
cannot alter an already-decided review.

Evidence without fixed-asset accounting serializes exactly as before (the key is
absent). Historical evidence, fingerprints and payloads are byte-identical.

Existing `expense_account` and RESALE payloads are unchanged. `depreciation_model_id`
is the only new `account.move.line` key, and it appears only on fixed-asset lines.

## Known limits

* **Serial numbers and IMEI are lost.** The UBL may carry them in `cbc:Note`, but
  normalized invoice evidence does not keep line notes. The Vendor Bill line name is
  the line description only. Preserving serial/IMEI is a separate follow-up.
* **VAT is unchanged.** The existing tax mapping stays authoritative; no
  deductible/non-deductible VAT policy is introduced.
* **Reason naming debt.** The cleared reason is still called
  `OPERATING_EXPENSE_MAPPING_REQUIRED`. It really means "this account-mode invoice
  needs an accounting decision". It is kept for historical and API compatibility.
* **Odoo posting behavior is not verified here.** Production Odoo 19.3 metadata
  (read-only) shows:
  - `account.move.line.depreciation_model_id` is a stored, writable many2one to
    `account.depreciation.model`;
  - `account.account.depreciation_model_id` is documented as the "default depreciation
    model to use on a vendor bill or a refund";
  - `can_create_asset` is a computed account flag;
  - `account.asset.original_move_line_ids` links assets to bill lines.

  Not proven without posting (deliberately not done in production): exactly when the
  asset is created, whether it is draft or running, and whether a quantity=1 line
  yields one asset and two lines yield two assets. Verify in a non-production Odoo, or
  on the first real bill, before relying on it.

## Configuration after merge (production; separate approvals)

1. The accountant decides the fixed-asset account(s). Set
   `ODOO_FIXED_ASSET_ACCOUNT_IDS` to exactly those ids, for example `[74]` for 255000
   or `[72,74]`. Never include accumulated-depreciation accounts.
2. The accountant configures, on each approved account in Odoo:
   - the accumulated depreciation account (`asset_depreciation_account_id`, e.g. 257000);
   - the depreciation expense account (`asset_expense_account_id`; 796000 is inactive
     today);
   - optionally a default depreciation model.

   The Hub always sends the model explicitly.
3. The accountant chooses which depreciation model to use (existing: 3/5/10/20 Year
   Linear, No depreciation) or creates one.
4. VAT deductibility is an accountant decision; it is outside this feature.
