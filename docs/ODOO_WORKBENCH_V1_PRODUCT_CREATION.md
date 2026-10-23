# V1 Workbench — product line mapping and creation completion

Status: Hub change in this PR. Studio objects and the production gate are **prepared, not applied**.
Base contract: `docs/ODOO_WORKBENCH_PRODUCT_LINE_REQUESTS.md` (PR C, "Eşleştir") and
`docs/ODOO_WORKBENCH_PRODUCT_LINE_CREATE.md` (PR D, "Yeni Ürün Oluştur").

## Why

Read-only investigation, 2026-10-23, production `ada9681`:

* **The product lines list edits rows inline.** View 4211 renders "Ürün Satırları" as
  `<list editable="bottom">`, so clicking a line edits that row in place and never opens
  form 4209. This is by design. Mapping already works inline: choose "Eşleştirilecek
  Ürün", and the row button (action 1027) appears. The button was only labelled "Eşleştir".
* **"Yeni Ürün Oluştur" is hidden and inert.** View 4217 (go-live, 2026-10-20) sets action
  1028's button `invisible="1"`, and `ODOO_WORKBENCH_PRODUCT_LINE_CREATE_ENABLED=false`.
  Showing the button alone would leave inert pending rows behind.
* **Created products were always saleable.** The Hub's create payload had no
  `sale_ok`/`purchase_ok`, so Odoo defaulted both to `True`. Template 164, created
  2026-10-13, is `sale_ok=True`. An internal-use service must be purchasable but not
  saleable.

## Hub change (live after deploy; behaviour unchanged while the create gate is off)

* **`NewProductIntent.sale_ok`.** The operator's explicit "Satılabilir" choice, read from the
  new child field `x_studio_ipp_new_sale_ok`. An unchecked box means `False`; any value
  other than an Odoo boolean refuses the request. It is never inferred from the purchase
  purpose, the supplier or the category.
* **Every new creation requires a boolean.** A new creation is refused before any write
  without an explicit boolean. Products created through this workflow are always
  `purchase_ok=True`.
* **Odoo write and read-back.** The supplier-linked create payload adds exactly `sale_ok`
  and `purchase_ok`; the exact key allowlist is extended by those two keys only. The
  writer reads both back, and any mismatch is a data-integrity error, never a silent
  success.
* **Lost-response recovery.** A product is adopted only if its flags match too; otherwise
  the line goes to `NEEDS_RECONCILIATION`.
* **Idempotency.** `sale_ok` is part of the intent digest, so the consumed write
  authorization and the operator-request key bind to it. An exact retry is
  `already_applied`; the same line with a different flag is a duplicate and is refused.
* **Legacy records.** A creation recorded before this change has `sale_ok = NULL`. Its
  digest is computed exactly as before and still verifies, and a V1 retry of that line is
  treated as the same request.
* **Unchanged.**
  * The duplicate guards: internal reference, product name, supplier product code,
    identity claim.
  * ManageEngine category [5] and VİTEL supplier [434] stay manual-only.
  * The UoM allowlist, the RESALE category allowlist and the PRD-SKU automations.
  * Group 88 permissions and actions 1027/1028.
  * Creating a product never sets a purchase purpose, assigns an expense account, creates
    a Vendor Bill or posts anything. Product creation is master data only, followed by a
    `MASTER_DATA_CHANGED` reclassification.

### Database

Migration **`202607170044`** adds one nullable boolean `sale_ok` column to
`workbench_product_line_creations`. It is required: crash recovery rebuilds the intent
from this row and verifies it against its digest. Existing rows keep `NULL`. The downgrade
refuses while any row records a value.

## Studio (prepared — separately approved steps)

| Artifact | Object | sha256 |
|---|---|---|
| `docs/odoo_studio/v1_product_lines/field_new_sale_ok.json` | `ir.model.fields` on `x_ipp_wb_product_line` (6943): `x_studio_ipp_new_sale_ok`, boolean, "Satılabilir", not required, no default (= False) | `fabd8bf2abacf63d6f9c85ddb4d2a6b0f374f73a42592eead3d9e78e7b23e2b2` |
| `docs/odoo_studio/v1_product_lines/view_ux.xml` | `ir.ui.view` "ICT V1 Workbench product line actions": form extension, inherit 3984, priority **275** | `2de6ee1f394bbf4b77a9c594f02a18b047801224e033d823401b54c2f0f31d1d` |
| `docs/odoo_studio/v1_product_lines/view_create_enable.xml` | `ir.ui.view` "ICT V1 Workbench product creation enabled": form extension, inherit 3984, priority **280** | `52b56aa202a13af3c68ff881b7b460b59821a5e2e2e4fc9073524ac62f812b2c` |

**`view_ux.xml`** is safe at any time:

* it renames the row button 1027 to **"Mevcut Ürünle Eşleştir"**;
* it adds a one-line instruction above the product lines;
* creation stays hidden.

**`view_create_enable.xml`** is the creation switch on the Odoo side, and depends on the UX
view's hint anchor:

* it adds the **"Satılabilir"** column after "Birim (Odoo)", editable only on a
  `product_not_found` row without a pending create;
* it restores the PR D guard on button 1028. The button is visible only on a
  `product_not_found` line with no pending create or mapping request and no selected
  existing product, once name, kind, category and Odoo unit are filled;
* it adds a confirmation dialog: purchasable always, saleable only if ticked, and no
  purpose, account or bill;
* it adds the creation hint.

Create this view **only together with** the Hub gate, and deactivate it whenever the gate
is turned off.

No existing view, field or server action is modified. Views 4211, 4212 and 4217 stay as
they are and are overridden by priority only.

## Deployment order

Each step needs separate approval. Stop on any failure.

1. **Merge and deploy the Hub.**
   * Take a backup and verify it with an isolated restore.
   * Run `alembic upgrade head` (0043 → 0044).
   * Recreate the api and the poller.
   * The create gate stays `false`. With the gate off, `x_studio_ipp_new_sale_ok` is never
     read.
2. **Create the Studio field** from `field_new_sale_ok.json`.
   * Read it back: boolean, not required, no default.
3. **Create `view_ux.xml`** (priority 275).
   * Read back `arch_db`, which must be canonically equal to the artifact.
   * `get_views` must compile, 1027 must read "Mevcut Ürünle Eşleştir", and 1028 must
     still be hidden.
4. **Activation, done together:**
   * Set `ODOO_WORKBENCH_PRODUCT_LINE_CREATE_ENABLED=true` in `.env.production`. Keep the
     existing `PRODUCT_CREATION_MANUAL_ONLY_CATEGORY_IDS=[5]`,
     `PRODUCT_CREATION_MANUAL_ONLY_SUPPLIER_IDS=[434]`, `PRODUCT_CREATION_UOM_IDS=[1]` and
     `PRODUCT_CREATION_UOM_UNIT_CODES`, then recreate the api and the poller.
   * Then create `view_create_enable.xml` (priority 280) and verify that `get_views` shows
     the 1028 guard and the "Satılabilir" column.
5. **One supervised end-to-end creation.** For example, UYUMSOFT `UBF2026000000649` line 1
   `H02 013.01`: kind Hizmet, an accountant-approved services category, unit Units,
   Satılabilir **unchecked**. Read back:
   * `sale_ok=False` and `purchase_ok=True`;
   * a supplierinfo for partner 453 with code `H02 013.01`;
   * the line matched, no Vendor Bill, no purpose and no account.

## Rollback

* **Odoo side of creation:** set the `view_create_enable` view `active=False` and set the
  gate back to `false`, then recreate the api and the poller.
* **UX:** set the `view_ux` view `active=False`. The field may stay; it is unused when no
  view shows it.
* **Hub:** redeploy the previous image.
  * Downgrade 0044 only if no creation row records `sale_ok`; the downgrade refuses
    otherwise.
  * Products already created stay as master data. Change them in Odoo only with
    accounting approval.
