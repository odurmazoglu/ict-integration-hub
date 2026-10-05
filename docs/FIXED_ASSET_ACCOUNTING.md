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
* **Asset posting accounts (saas~19.2+).** Where Odoo exposes them, the selected
  account's `asset_depreciation_account_id` (accumulated depreciation) and
  `asset_expense_account_id` (depreciation expense) must both be set, must differ
  from the asset account, and must be active and company-compatible. Otherwise the
  posted bill would produce an asset Odoo cannot depreciate. The Turkish chart
  template ships 253/255 without either, and 796000 inactive, so this check fails
  closed until the accountant configures the account.
* **Line label.** Every fixed-asset line needs a non-empty description: Odoo cannot
  create an asset from a product-less journal item that has no label.
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
* **Quantity > 1 on one line.** The Hub keeps source lines one-to-one and never
  splits a line. Whether Odoo then creates one asset or one asset per unit is
  unverified for saas~19.3. Separately identifiable items should arrive as separate
  quantity=1 lines (the Apple invoice does).

## Native Odoo asset behavior (research for PR #204)

Odoo Enterprise `account_asset` source is not public, and no test Odoo is available,
so the conclusions below are graded:

- **PROVEN:** directly stated in official Odoo docs, release notes or public source.
- **STRONGLY SUPPORTED:** implied by official sources.
- **UNVERIFIED:** cannot be established safely.

| Topic | Conclusion | Evidence |
|---|---|---|
| Configuration location (saas~19.2+) | Asset models were replaced by `account.depreciation.model`, which holds only the calculation. The accumulated depreciation and depreciation expense accounts, plus the default model, now live on the fixed-asset account (`asset_depreciation_account_id`, `asset_expense_account_id`, `depreciation_model_id`). | **PROVEN.** Odoo 19.2 release notes. odoo/odoo commit `81a32e482b` "vendor bill assets improvements" (enterprise#102950): it deletes the old `account.asset` model CSVs, which carried `account_depreciation_id` / `account_depreciation_expense_id`, and sets the three fields on fixed-asset accounts, e.g. `addons/l10n_uk/models/template_uk.py` (saas-19.3). |
| `account.account.depreciation_model_id` | Only the default model proposed on bill lines. | **PROVEN.** Production field help: "default depreciation model to use on a vendor bill or a refund". |
| `account.move.line.depreciation_model_id` | A per-line choice shown under the account on bills; the created asset uses it. | **STRONGLY SUPPORTED.** Odoo 19.3 release notes ("options added on a bill such as depreciation models … stacked vertically under the account"). Commit `81a32e482b` adds the `m2o_cell_with_extra_m2o_fields` widget to the bill line `account_id`. The field is stored and writable in production. |
| When assets are created | When a human posts the bill, not while it is draft. The Hub never needs to write `account.asset`. | **PROVEN for 19.0:** `vendor_bills/assets.rst` "Automate the Assets": "Whenever a transaction is posted on the account…". **STRONGLY SUPPORTED for saas~19.3:** 19.2 release notes say the "automated behaviors are located on asset accounts". |
| Initial asset state | Draft or running, depending on the account's automation setting. | **PROVEN for 19.0:** "Create in draft" / "Create and validate". **UNVERIFIED** for the saas~19.3 field name and default. |
| Two quantity=1 lines | Two separately identifiable assets, one per line, each linked via `original_move_line_ids` / `asset_ids`. | **STRONGLY SUPPORTED.** |
| One quantity=2 line | One asset or two. | **UNVERIFIED.** |
| `product_id` | Not required. An account-only line with a label suffices. | **STRONGLY SUPPORTED.** The 19.0 docs select the asset account directly on the draft bill line. |
| `deductible_percentage` | Community field, default 1.0 (fully deductible), allowed only on purchase documents. The Hub omits it and the existing tax mapping is unchanged. | **PROVEN.** odoo saas-19.3 `addons/account/models/account_move_line.py` (`deductible_percentage`, `_constrains_deductible_percentage`). |

Payload sufficiency: the draft line `name`, `quantity`, `price_unit`, `account_id`,
`tax_ids` and `depreciation_model_id` is sufficient (**STRONGLY SUPPORTED**), provided
the selected account is configured as below. The explicit model is not a duplicated
Odoo default: the Turkish accounts have no default model, and an explicit model also
freezes the operator's choice.

Bill posting itself (asset account / VAT / payable) is ordinary Odoo accounting and
is correct regardless of the asset machinery. The remaining uncertainties affect only
the asset record created after posting, which the accountant can still fix in Odoo.

## Configuration after merge (production; separate approvals)

1. The accountant decides the fixed-asset account(s). Set
   `ODOO_FIXED_ASSET_ACCOUNT_IDS` to exactly those ids, for example `[74]` for 255000
   or `[72,74]`. Never include accumulated-depreciation accounts.
2. **Technically required** on each approved account in Odoo (the Hub fails closed
   without the first two):
   - the accumulated depreciation account (`asset_depreciation_account_id`, e.g. 257000);
   - the depreciation expense account (`asset_expense_account_id`). It must be active;
     796000 is inactive today;
   - the account's asset automation setting, if the form shows one, must create assets
     ("create in draft" lets the accountant review before depreciation starts).

   Optional: a default `depreciation_model_id`. The Hub always sends the model
   explicitly.
3. The accountant chooses which depreciation model to use (existing: 3/5/10/20 Year
   Linear, No depreciation) or creates one.
4. VAT deductibility is an accountant decision; it is outside this feature.
