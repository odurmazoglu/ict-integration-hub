# Odoo Workbench Product Lines (PR B)

Read-only, per-invoice-line Workbench child projection for PRODUCT_NOT_FOUND handling.

* Odoo model: `x_ipp_wb_product_line` ("IPP Ürün Satırı"), a child of `x_ipp_import_workbench`.
* One child row = one immutable source invoice line of a review.
* Rows are a **projection of Hub truth**, written only by the canonical projection sync
  (`WorkbenchProjectionSynchronizer`): runtime transitions and
  `python -m app.cli.reconcile_workbench_projection`. The Hub never reads them back as input.
* There are no operator request fields on this model yet: no `req_action`, `req_product`,
  `ready`, `requested_by`, `requested_at`, `result`, new-product fields or fuzzy candidates.
  Line-level requests are a later PR. The parent-level "Ürün Eşleştir" path
  (`x_studio_ipp_req_line` / `x_studio_ipp_req_product`) is unchanged and stays operational.

Code: `app/application/workbench/product_line_projection.py` (pure derivation),
`app/erp/odoo/workbench_product_line_publisher.py` (Odoo upsert),
`app/composition/imports.py::build_workbench_projection_synchronizer` (wiring, gate).

## 1. Identity

```
ipp-pl:v1:<company_id>:<review_id>:<source line number>
e.g. ipp-pl:v1:1:review:dfccd66e-43e7-546b-b559-49323e79ab9b:2
```

* `review_id` is derived from the immutable source invoice identity and does not change
  across review versions.
* The UBL line number (`cbc:ID`) is the only line identity the source model has. It is
  only guaranteed unique **within one invoice**, so the key always binds it to company
  and review.
* Review version, description, seller code and Odoo record id are deliberately **not**
  part of the key. A reclassification (v2 → v3) updates the same row.
* A review whose source lines have a blank or repeated line number gets **no** product
  line rows. The line projection fails closed with an explicit error, and the parent
  row is unaffected.

## 2. Line inclusion rule

A review is a *product-resolution review* when any of these holds:

1. Its reasons contain a product-matching reason: `PRODUCT_NOT_FOUND`,
   `PRODUCT_AMBIGUOUS`, `PRODUCT_IDENTIFIER_MISSING` or `PRODUCT_MAPPING_INCOMPLETE`.
   For a decided review these are the decision-basis reasons.
2. Pending: its current-version Stage-1 execution evidence is product mode (no
   whole-invoice operating-expense match, no fixed asset) with at least one MATCHED line.
3. Decided: its accepted decision resolved at least one line to a product.

For a product-resolution review, **every** source line is projected, matched lines
included, so completed lines never disappear. Whole-invoice operating-expense,
accounting-resolution and fixed-asset reviews without any product reason get no rows.

## 3. Evidence → state mapping

The selection keys are the Hub's `ProductLineMatchState` values.

| Key | Label (TR) | Produced when |
| --- | --- | --- |
| `matched` | Eşleşti | Pending: the current Stage-1 evidence line is `MATCHED` with a product, and the line has no current product reason. Decided: the accepted effective resolution is `product`. |
| `product_not_found` | Ürün Bulunamadı | Evidence `NOT_FOUND`, or reason `PRODUCT_NOT_FOUND` when a supplier is known or no supplier blocker exists. |
| `product_ambiguous` | Birden Fazla Aday | Evidence `MULTIPLE_MATCHES`, or reason `PRODUCT_AMBIGUOUS`. |
| `identifier_missing` | Ürün Tanımlayıcısı Yok | Evidence `INVALID_INPUT`, or reason `PRODUCT_IDENTIFIER_MISSING`. The line has no seller code, barcode or buyer code. |
| `supplier_unresolved` | Tedarikçi Kesin Değil | Reason `PRODUCT_NOT_FOUND` with no effective supplier and a supplier blocker (`SUPPLIER_NOT_FOUND` / `SUPPLIER_AMBIGUOUS` / `SUPPLIER_TAX_NUMBER_MISSING`). |
| `resolved_without_product` | Ürünsüz Çözüldü | Decided: the effective resolution is `account_only`, `accounting_resolution`, `operating_expense_mapping` or `fixed_asset`. |
| `no_evidence` | Kanıt Yok | No Stage-1 evidence for the line and no product reason, or decided with an `unresolved` or missing resolution. It is never shown as "matched", because there is no product id to show. |

Precedence for a pending review:

1. A current line-scoped product reason always wins over a `MATCHED` evidence result
   (fail closed).
2. On a repeated reason, the most blocking wins: identifier > ambiguous > not found.
3. `identifier_missing` is a property of the line and is not converted to
   `supplier_unresolved`.

"Missing seller code" is not a separate state. The matcher only treats a line as
identifier-free when it has *no* identifier at all. A line with a barcode or buyer code
but no seller code shows an empty **Satıcı Ürün Kodu** and, when not found, the message
"Ürün bulunamadı; satırda satıcı ürün kodu yok."

## 4. Effective supplier and matched product

* **Pending:** the supplier is the `partner_match` of the current version's Stage-1
  execution evidence, but only when it is `MATCHED`. Since PR #211, that is exactly the
  supplier the persisted product matching ran under: a raw deterministic match, or a
  proven accepted MATCH_EXISTING / CREATE_PERMANENT_SUPPLIER / ONE_OFF_VENDOR supplier.
  Stage-1 evidence exists only when the supplier matched. Without it, the supplier is
  **empty**: never guessed, never inferred from the name.
* **Decided:** the supplier is the accepted decision's pinned `partner_match`.
* The matched product comes only from the current evidence (pending) or the accepted
  effective resolution (decided). It is never inferred from a name, a seller-code
  similarity, a fuzzy match or `default_code` alone. An unresolved line always has an
  empty product.
* No live Odoo partner or product resolution happens during projection.

## 5. Studio contract (exact)

### 5.1 Model

| Property | Value |
| --- | --- |
| `model` | `x_ipp_wb_product_line` |
| `name` | `IPP Ürün Satırı` |
| `state` | `manual` |
| `_rec_name` | `x_name` (Odoo picks `x_name` automatically) |
| archive field | `x_active` (Odoo uses `x_active` as the active field of a custom model) |

### 5.2 Fields on `x_ipp_wb_product_line`

All fields have `store=True`, `copied=False`, no tracking, no compute and no default
(the Hub writes every value, including `x_active`).

| Technical name | Label | `ttype` | Relation / extra | `required` | `index` |
| --- | --- | --- | --- | --- | --- |
| `x_name` | Ad | char | | no | no |
| `x_active` | Aktif | boolean | | no | no |
| `x_studio_ipp_workbench_id` | IPP Workbench | many2one | `x_ipp_import_workbench`, `on_delete=cascade` | **yes** | yes |
| `x_studio_ipp_line_key` | Hub Satır Anahtarı | char | | **yes** | yes |
| `x_studio_ipp_review_id` | Review ID | char | | no | yes |
| `x_studio_ipp_company_id` | Şirket ID | integer | | no | no |
| `x_studio_ipp_review_version` | İnceleme Versiyonu | integer | | no | no |
| `x_studio_ipp_line_number` | Satır | char | | no | no |
| `x_studio_ipp_supplier_id` | Tedarikçi | many2one | `res.partner`, `on_delete=set null` | no | no |
| `x_studio_ipp_seller_code` | Satıcı Ürün Kodu | char | | no | no |
| `x_studio_ipp_description` | Açıklama | char | | no | no |
| `x_studio_ipp_quantity` | Miktar | float | | no | no |
| `x_studio_ipp_uom_code` | Birim | char | UBL unit code (e.g. `C62`) | no | no |
| `x_studio_ipp_match_state` | Durum | selection | keys below | no | no |
| `x_studio_ipp_product_id` | Eşleşen Ürün | many2one | `product.product`, `on_delete=set null` | no | no |
| `x_studio_ipp_matched_by` | Eşleşme Yöntemi | char | | no | no |
| `x_studio_ipp_line_message` | Durum Açıklaması | char | | no | no |

`x_studio_ipp_match_state` selection (`ir.model.fields.selection`; `value` = key, `name` = label):

| seq | value | name |
| --- | --- | --- |
| 1 | `matched` | Eşleşti |
| 2 | `product_not_found` | Ürün Bulunamadı |
| 3 | `product_ambiguous` | Birden Fazla Aday |
| 4 | `identifier_missing` | Ürün Tanımlayıcısı Yok |
| 5 | `supplier_unresolved` | Tedarikçi Kesin Değil |
| 6 | `resolved_without_product` | Ürünsüz Çözüldü |
| 7 | `no_evidence` | Kanıt Yok |

### 5.3 Parent one2many on `x_ipp_import_workbench`

| Technical name | Label | `ttype` | `relation` | `relation_field` | `domain` |
| --- | --- | --- | --- | --- | --- |
| `x_studio_ipp_product_line_ids` | Ürün Satırları | one2many | `x_ipp_wb_product_line` | `x_studio_ipp_workbench_id` | `[('x_active', '=', True)]` |

The explicit domain hides archived rows even if Odoo does not treat `x_active` as the
active field.

### 5.4 Views

**Parent form extension.** New `ir.ui.view`: `inherit_id` = parent form 3984, `priority`
222 (after 4206 / 4208), mode `extension`.

```xml
<data>
  <xpath expr="//notebook" position="inside">
    <page string="Ürün Satırları" name="ipp_product_lines">
      <field name="x_studio_ipp_product_line_ids" readonly="1" nolabel="1">
        <list create="0" delete="0" edit="0" default_order="x_studio_ipp_line_number">
          <field name="x_studio_ipp_line_number" string="Satır"/>
          <field name="x_studio_ipp_seller_code" string="Satıcı Ürün Kodu"/>
          <field name="x_studio_ipp_description" string="Açıklama"/>
          <field name="x_studio_ipp_quantity" string="Miktar"/>
          <field name="x_studio_ipp_uom_code" string="Birim"/>
          <field name="x_studio_ipp_match_state" string="Durum"
                 decoration-success="x_studio_ipp_match_state == 'matched'"
                 decoration-warning="x_studio_ipp_match_state in ('product_not_found', 'product_ambiguous')"
                 decoration-danger="x_studio_ipp_match_state in ('identifier_missing', 'supplier_unresolved')"
                 decoration-muted="x_studio_ipp_match_state in ('resolved_without_product', 'no_evidence')"/>
          <field name="x_studio_ipp_product_id" string="Eşleşen Ürün"/>
        </list>
      </field>
    </page>
  </xpath>
</data>
```

Note: `default_order` on an inline list orders by text; line numbers `10`/`2` sort as
text. This is acceptable for the compact list, because the Hub line key is the identity,
not the order.

**Child form.** New standalone `ir.ui.view`, `type=form`, model
`x_ipp_wb_product_line`, `priority` 16. Every field is read-only, and there is no
create, edit or delete:

```xml
<form create="0" edit="0" delete="0">
  <sheet>
    <group>
      <group string="Satır">
        <field name="x_studio_ipp_workbench_id" readonly="1"/>
        <field name="x_studio_ipp_line_number" readonly="1"/>
        <field name="x_studio_ipp_seller_code" readonly="1"/>
        <field name="x_studio_ipp_description" readonly="1"/>
        <field name="x_studio_ipp_quantity" readonly="1"/>
        <field name="x_studio_ipp_uom_code" readonly="1"/>
        <field name="x_studio_ipp_supplier_id" readonly="1"/>
      </group>
      <group string="Ürün Eşleşmesi">
        <field name="x_studio_ipp_match_state" readonly="1"/>
        <field name="x_studio_ipp_product_id" readonly="1"/>
        <field name="x_studio_ipp_matched_by" readonly="1"/>
        <field name="x_studio_ipp_line_message" readonly="1"/>
      </group>
      <group string="Teknik" groups="base.group_system">
        <field name="x_studio_ipp_line_key" readonly="1"/>
        <field name="x_studio_ipp_review_id" readonly="1"/>
        <field name="x_studio_ipp_review_version" readonly="1"/>
        <field name="x_studio_ipp_company_id" readonly="1"/>
        <field name="x_active" readonly="1"/>
      </group>
    </group>
  </sheet>
</form>
```

No menu and no window action are added: the rows are reached only through the parent form.

### 5.5 ACLs (`ir.model.access` on `x_ipp_wb_product_line`)

| Name | Group | read | write | create | unlink |
| --- | --- | --- | --- | --- | --- |
| `ipp_wb_product_line operator read` | IPP Workbench Operator (`studio_customization.ipp_workbench_operator_group`, id 88) | 1 | 0 | 0 | 0 |
| `ipp_wb_product_line reader read` | each group that has **read** on `x_ipp_import_workbench` (list them read-only at provisioning time; see 6.2) | 1 | 0 | 0 | 0 |
| `ipp_wb_product_line hub writer` | Settings / Administrator (`base.group_system`), the group of the Hub integration user (uid 2, whose API key the Hub uses) | 1 | 1 | 1 | 0 |

* Normal operators can only **read**. Nobody gets `unlink`: stale rows are archived, never deleted.
* No write access is granted ahead of time for future request fields.
* No record rules. The company is a plain projected integer, not `company_id`.
* No Odoo → Hub calls, no Hub credentials in Odoo, no server actions, no automations,
  no computed fields: Studio holds no business logic.

## 6. Production provisioning runbook (NOT executed in PR B)

Each step needs its own explicit approval. The Hub code can be deployed before any of
it: with the gate off, the Hub never reads or writes the child model.

### 6.1 Preconditions

* PR B is deployed with `ODOO_WORKBENCH_PRODUCT_LINE_PROJECTION_ENABLED` absent (= false).
* Fresh Hub DB backup and `.env.production` backup, as in every phase.
* A read-only pre-snapshot of the parent form view 3984 combined arch, and of
  `ir.model` / `ir.model.fields` for `x_ipp_wb_product_line`, which must not exist yet.

### 6.2 Read-only discovery

With the user-2 JSON-2 key, read the following. Record each result and write nothing:

1. `ir.model` where `model = 'x_ipp_wb_product_line'`. Expect **0** rows.
2. `ir.model.fields` where `model = 'x_ipp_import_workbench'` and
   `name = 'x_studio_ipp_product_line_ids'`. Expect **0** rows.
3. `ir.model.access` where `model_id.model = 'x_ipp_import_workbench'` and
   `perm_read = True`. The resulting groups become the "reader read" ACL rows.
4. `ir.ui.view` 3984. It must be active and have a `<notebook>`. Also check that no
   active view with priority 222 inherits it.

### 6.3 Create (JSON-2, user-2 key, in this order)

1. `ir.model/create` with `{"model": "x_ipp_wb_product_line", "name": "IPP Ürün Satırı", "state": "manual"}`. Record the id as `M`.
2. `ir.model.fields/create`, one call per row of 5.2, with `model_id: M`, `name`,
   `field_description`, `ttype`, `required`, `index`, plus `relation` and `on_delete`
   for many2one fields.
   * For `x_studio_ipp_match_state`, add
     `selection_ids: [[0, 0, {"value": <key>, "name": <label>, "sequence": <seq>}], ...]`
     with the 7 rows of 5.2.
3. `ir.model.fields/create` for the parent one2many of 5.3 (`model_id` = the
   `x_ipp_import_workbench` model id).
4. `ir.ui.view/create` for the parent form extension and the child form of 5.4.
5. `ir.model.access/create` for the rows of 5.5.

Record every created id in the evidence directory (`/var/tmp/pr_b_provision_<ts>/provision.json`).

### 6.4 Contract verification (read-only)

1. `ir.model.fields` for `x_ipp_wb_product_line`: all 17 names of 5.2 exist, with the
   exact `ttype` and `relation` (`x_ipp_import_workbench`, `res.partner`,
   `product.product`), and `x_studio_ipp_workbench_id` is required.
2. `ir.model.fields.selection` of `x_studio_ipp_match_state`: exactly the 7 keys.
3. The parent form, opened as a group-88 user, shows the "Ürün Satırları" tab with an
   empty, non-editable list.
4. Hub dry-run with the gate enabled **for this one command only**. The `.env` is not changed:

   ```sh
   docker exec -w /app config-uyumsoft-inbound-poller-1 sh -c '
     export ODOO_API_KEY="$(cat /run/secrets/odoo_api_key)";
     export DATABASE_URL="postgresql+psycopg://ict_hub_prod:$(cat /run/secrets/hub_db_password)@db:5432/ict_integration_hub_prod";
     export ODOO_WORKBENCH_PRODUCT_LINE_PROJECTION_ENABLED=true;
     exec python -m app.cli.reconcile_workbench_projection --company 1'
   ```

   The Hub verifies the contract itself on the first review:
   * the selection must offer all 7 keys;
   * every mapped field is read, so an unknown field fails with an Odoo error that is
     reported as `line ERROR`.

   Expected: `Lines: CREATE=51 UPDATE=0 NO_CHANGE=0 DEACTIVATE=0 ERROR=0`. See section 7.
   The parent `Totals` stay what they are without PR B: 4 `UPDATE` (the pending 7B
   "Tamamlananlar" refresh of rows 17, 24, 44 and 45) and 42 `NO_CHANGE`.

### 6.5 Enable and backfill (separate approvals)

1. **Backfill.** Run the same command with `--apply`, or `--review-id …` per review to
   avoid touching the 4 stale parent rows in the same run. The follow-up dry-run must
   show `Lines: … NO_CHANGE=51 …` and no `line CREATE/UPDATE`.
2. **Runtime.** Set `ODOO_WORKBENCH_PRODUCT_LINE_PROJECTION_ENABLED=true` in
   `.env.production` (back it up first). Then recreate both containers:
   `up -d --no-deps api` and `up -d --no-deps uyumsoft-inbound-poller`. From then on,
   every projection-relevant transition also syncs that review's lines.

### 6.6 Rollback / hide

* **Stop Hub writes.** Remove the env key (or set it to false) and recreate api and
  poller. The parent projection continues exactly as before PR B.
* **Hide from operators.** Deactivate the parent form extension view (the tab disappears).
* **Hide the rows but keep them.** Archive them: `x_active = False`.
* Deleting the model (`ir.model` unlink drops the table) or its fields needs a separate
  explicit approval. It is never part of a routine rollback.
* No Hub schema exists for this feature, so there is nothing to downgrade.

## 7. Expected production projection (read-only survey, 2026-10-08, prod 8a021c9)

Derived with the rules above from read-only status/id aggregates of current Hub
evidence. Nothing was written.

**ICT Bulut — `review:dfccd66e…` v3, supplier partner 24 (Stage-1 evidence, tax-number match):**

| Satır | Satıcı Ürün Kodu | Açıklama | Durum | Eşleşen Ürün | matched_by |
| --- | --- | --- | --- | --- | --- |
| 1 | 100020 | Microsoft 365 Business Basic | `matched` | product.product 393 | `supplier_product_code` |
| 2 | 100021 | Microsoft 365 Business Standard | `product_not_found` | — | — |

**All 46 reviews:** 34 qualify, giving 51 lines. 12 reviews are excluded: 10 pending
whole-invoice operating-expense reviews, CloudSpark (`b9aacadc`, accounting resolution)
and Apple (`8f24539e`, fixed asset).

| Group | Reviews | Lines | States |
| --- | --- | --- | --- |
| Pending, supplier matched (Stage-1 evidence) | 12 | 22 | 7 `matched` (VİTEL 1c825f3b L1/L5 → 187/215; dfccd66e L1 → 393; LogoSoft 267f23a2 and 9afe7695 L1/L2 → 393/394), 15 `product_not_found` (suppliers 24, 75, 434) |
| Pending, no Stage-1 evidence (supplier not found/ambiguous) | 15 | 22 | 12 `supplier_unresolved`, 10 `identifier_missing` (6d071152 L1, d99bcefd L1, 8a867709 L1–L8) |
| Decided (accepted decision evidence) | 7 | 7 | 4 `matched` (1316ab15 → 389 and 2fd26e5e → 392 human-selected; cee1f8d5 → 393 and 2fb46a57 → 394 automatic), 3 `resolved_without_product` (9b13ad00, 1cbf1ff0, 6074973d: account-only) |
| **Total** | **34** | **51** | `matched` 11, `product_not_found` 15, `supplier_unresolved` 12, `identifier_missing` 10, `resolved_without_product` 3 |

ICT Bulut's other PRODUCT_NOT_FOUND reviews project as `product_not_found` under partner
24: b7c4b622 L1, eeec6686 L1, 3943f7f9 L1–L2 and 07d81248 L1.
