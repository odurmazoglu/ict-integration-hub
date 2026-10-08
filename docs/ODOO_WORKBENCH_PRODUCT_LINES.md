# Odoo Workbench Product Lines (PR B)

Read-only, per-invoice-line Workbench child projection for PRODUCT_NOT_FOUND handling.

* Odoo model: `x_ipp_wb_product_line` ("IPP Ürün Satırı"), a child of `x_ipp_import_workbench`.
* One child row = one immutable source invoice line of a review.
* Rows are a **projection of committed Hub truth**. Only the canonical projection sync
  (`WorkbenchProjectionSynchronizer`) writes them: runtime transitions and
  `python -m app.cli.reconcile_workbench_projection`. The Hub never reads them back as input.
* There are no operator request fields on this model: no `req_action`, `req_product`,
  `ready`, `requested_by`, `requested_at`, `result`, new-product fields or fuzzy
  candidates. Line-level requests are a later PR.
* The parent-level "Ürün Eşleştir" path (`x_studio_ipp_req_line`,
  `x_studio_ipp_req_product`, server action 1026, the #208 handler) is unchanged and stays
  operational.

Code:
* `app/application/workbench/product_line_projection.py`: pure derivation.
* `app/erp/odoo/workbench_product_line_publisher.py`: Odoo upsert.
* `app/composition/imports.py::build_workbench_projection_synchronizer`: wiring and gate.

## 1. Identity and ordering

**Identity** (Hub-owned, `x_studio_ipp_line_key`):

```text
ipp-pl:v1:<company_id>:<review_id>:<source line id>
e.g. ipp-pl:v1:1:review:dfccd66e-43e7-546b-b559-49323e79ab9b:2
```

* `review_id` is derived from the immutable source invoice identity and does not change
  across review versions.
* The source line id is the UBL `cbc:ID`, whitespace-stripped. It is the only line identity
  the source model has, and it is only guaranteed unique **within one invoice**, so the key
  always binds it to company and review.
* Review version, description, seller code and Odoo record id are **not** part of the key.
  A reclassification (v2 → v3) updates the same row.
* A review whose source lines have a blank or repeated id gets **no** product line rows. It
  fails closed with an explicit error, and the parent row is unaffected.

**Ordering** (`x_studio_ipp_line_sequence`, Integer, UI order only, never identity):

* If every source line id of the invoice is a plain decimal integer and their integer
  values are distinct, the sequence is that integer: `1, 2, 10` → `1, 2, 10`, and
  `000001` → `1`.
* Otherwise (any non-numeric id, or ids such as `1`/`01` that collide as integers), the
  sequence is the 1-based ordinal position of **every** line in the immutable source
  invoice. A mix of numeric and non-numeric ids therefore never yields equal sequences.
* Only the immutable source ids and the source order are used. Description, seller code
  and any mutable field never affect the order.
* The visible **Satır** column shows the original source id (`x_studio_ipp_line_number`).

## 2. Line inclusion rule

A review is a *product-resolution review* when any of these committed facts holds:

1. Its reasons, whether current blockers or decision basis, contain a product reason:
   `PRODUCT_NOT_FOUND`, `PRODUCT_AMBIGUOUS`, `PRODUCT_IDENTIFIER_MISSING` or
   `PRODUCT_MAPPING_INCOMPLETE`.
2. Its committed reclassification history (`workbench_review_reclassifications`,
   previous or new reasons) contains a product reason. This makes membership **sticky**: when
   the last `PRODUCT_NOT_FOUND` line is mapped and reclassified to MATCHED, the review keeps its
   rows, and the same row turns `matched`.
3. Pending: its current-version Stage-1 execution evidence has at least one `MATCHED`
   product line.
4. Decided: its accepted decision resolved at least one line to a product.

For a product-resolution review **every** source line is projected, matched lines
included. Whole-invoice operating-expense, accounting-resolution and fixed-asset reviews,
whose lines are `INVALID_INPUT` or account-resolved and which never had a product reason,
get no rows. Not every invoice line of every review is projected.

## 3. Evidence → state mapping

The selection keys are the Hub's `ProductLineMatchState` values.

| Key | Label (TR) | Produced when |
| --- | --- | --- |
| `matched` | Eşleşti | Pending: the current Stage-1 evidence line is `MATCHED` with a product, and the line has no current product reason. Decided: the accepted effective resolution is `product`. |
| `product_not_found` | Ürün Bulunamadı | Evidence `NOT_FOUND`, or reason `PRODUCT_NOT_FOUND`, unless the conditions for `supplier_unresolved` hold. |
| `product_ambiguous` | Birden Fazla Aday | Evidence `MULTIPLE_MATCHES`, or reason `PRODUCT_AMBIGUOUS`. |
| `identifier_missing` | Ürün Tanımlayıcısı Yok | Evidence `INVALID_INPUT`, or reason `PRODUCT_IDENTIFIER_MISSING`. The line has no seller code, barcode or buyer code. |
| `supplier_unresolved` | Tedarikçi Kesin Değil | Reason `PRODUCT_NOT_FOUND`, no committed supplier, and a supplier blocker (`SUPPLIER_NOT_FOUND`, `SUPPLIER_AMBIGUOUS` or `SUPPLIER_TAX_NUMBER_MISSING`). |
| `resolved_without_product` | Ürünsüz Çözüldü | Decided: the effective resolution is `account_only`, `accounting_resolution`, `operating_expense_mapping` or `fixed_asset`. |
| `no_evidence` | Kanıt Yok | No Stage-1 evidence for the line and no product reason, or decided with an `unresolved` or missing resolution. It is never shown as "matched", because there is no committed product id to show. |

Precedence for a pending review:

1. A current line-scoped product reason always wins over a `MATCHED` evidence result
   (fail closed).
2. On a repeated reason the most blocking one wins: identifier, then ambiguous, then not found.
3. `identifier_missing` is a property of the line and is never converted to
   `supplier_unresolved`.

"Missing seller code" is not a separate state. The matcher treats a line as
identifier-free only when it has *no* identifier at all. A line with a barcode or buyer
code but no seller code shows an empty **Satıcı Ürün Kodu**. When such a line is not found,
its message is "Ürün bulunamadı; satırda satıcı ürün kodu yok."

## 4. Committed-truth boundary (supplier and product)

Projection **never performs matching**. It makes no Odoo call to re-resolve a supplier,
runs no fuzzy matching, infers nothing from names, and makes no product decision from
current Odoo master data. During a sync the Hub talks to Odoo only for the projection
models themselves (`x_ipp_import_workbench`, `x_ipp_wb_product_line`, their selection
metadata) and the existing currency lookup of the parent. A test asserts this, along with
the absence of any `res.partner`, `product.*` or supplierinfo access.

* **Supplier, pending review:** the `partner_match` of the current version's Stage-1
  execution evidence, only when it is `MATCHED`. Since PR #211 that is exactly the
  supplier the persisted product matching ran under: either the raw deterministic match
  or a proven accepted MATCH_EXISTING / CREATE_PERMANENT_SUPPLIER / ONE_OFF_VENDOR
  supplier. Stage-1 evidence is written only when the supplier matched. Without it the
  supplier is **empty by design**.
* **Supplier, decided review:** the accepted decision's pinned `partner_match`.
* **Matched product:** comes only from the current Stage-1 evidence (pending) or the
  accepted effective resolution (decided). It is never taken from a name, a seller-code
  similarity, a fuzzy match, `default_code`, or a product that happens to exist in Odoo
  today. An unresolved line always has an empty product, and the DTO enforces this.

## 5. Final Studio contract (exact)

### 5.1 Model

| Property | Value |
| --- | --- |
| `model` | `x_ipp_wb_product_line` |
| `name` | `IPP Ürün Satırı` |
| `state` | `manual` |

The model gets **no** `x_active` field. Currency is the explicit Hub-owned boolean
`x_studio_ipp_is_current`; no native Odoo archive behavior is used or assumed. `x_name`
only gives rows a readable display name. Nothing in the Hub depends on how Odoo renders it.

### 5.2 Fields on `x_ipp_wb_product_line` (18)

All fields: `store=True`, `copied=False`, no tracking, no compute, no default. The Hub
writes every value on create, including `x_studio_ipp_is_current = True`.

| # | Technical name | Label | `ttype` | Relation / extra | `required` | `index` |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `x_name` | Ad | char | | no | no |
| 2 | `x_studio_ipp_workbench_id` | IPP Workbench | many2one | `x_ipp_import_workbench`, `on_delete=cascade` | **yes** | yes |
| 3 | `x_studio_ipp_line_key` | Hub Satır Anahtarı | char | | **yes** | yes |
| 4 | `x_studio_ipp_is_current` | Güncel | boolean | Hub-owned: True = currently projected, False = stale/historical | no | yes |
| 5 | `x_studio_ipp_review_id` | Review ID | char | | no | yes |
| 6 | `x_studio_ipp_company_id` | Şirket ID | integer | | no | no |
| 7 | `x_studio_ipp_review_version` | İnceleme Versiyonu | integer | | no | no |
| 8 | `x_studio_ipp_line_number` | Satır | char | original source line id | no | no |
| 9 | `x_studio_ipp_line_sequence` | Satır Sırası | integer | UI ordering only (section 1) | no | no |
| 10 | `x_studio_ipp_supplier_id` | Tedarikçi | many2one | `res.partner`, `on_delete=set null` | no | no |
| 11 | `x_studio_ipp_seller_code` | Satıcı Ürün Kodu | char | | no | no |
| 12 | `x_studio_ipp_description` | Açıklama | char | | no | no |
| 13 | `x_studio_ipp_quantity` | Miktar | float | | no | no |
| 14 | `x_studio_ipp_uom_code` | Birim | char | UBL unit code (e.g. `C62`) | no | no |
| 15 | `x_studio_ipp_match_state` | Durum | selection | keys below | no | no |
| 16 | `x_studio_ipp_product_id` | Eşleşen Ürün | many2one | `product.product`, `on_delete=set null` | no | no |
| 17 | `x_studio_ipp_matched_by` | Eşleşme Yöntemi | char | | no | no |
| 18 | `x_studio_ipp_line_message` | Durum Açıklaması | char | | no | no |

`x_studio_ipp_match_state` selection, created as `ir.model.fields.selection` rows with
`value` = key and `name` = label:

| sequence | value | name |
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
| `x_studio_ipp_product_line_ids` | Ürün Satırları | one2many | `x_ipp_wb_product_line` | `x_studio_ipp_workbench_id` | `[('x_studio_ipp_is_current', '=', True)]` |

The domain is the **only** visibility mechanism. Non-current rows stay in the table and
are reachable by the Hub, but they are never shown in the parent tab.

### 5.4 Views

**Parent form extension.** New `ir.ui.view`, `type=form`, `model=x_ipp_import_workbench`,
`inherit_id` = parent form 3984, `priority` 222 (after 4206 and 4208), `mode=extension`,
`active=True`:

```xml
<data>
  <xpath expr="//notebook" position="inside">
    <page string="Ürün Satırları" name="ipp_product_lines">
      <field name="x_studio_ipp_product_line_ids" readonly="1" nolabel="1">
        <list create="0" delete="0" edit="0" default_order="x_studio_ipp_line_sequence asc">
          <field name="x_studio_ipp_line_sequence" column_invisible="1"/>
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

**Child form.** New standalone `ir.ui.view`, `type=form`, `model=x_ipp_wb_product_line`,
`priority` 16. Every field is read-only, and there is no create, edit or delete:

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
        <field name="x_studio_ipp_line_sequence" readonly="1"/>
        <field name="x_studio_ipp_review_id" readonly="1"/>
        <field name="x_studio_ipp_review_version" readonly="1"/>
        <field name="x_studio_ipp_company_id" readonly="1"/>
        <field name="x_studio_ipp_is_current" readonly="1"/>
      </group>
    </group>
  </sheet>
</form>
```

No menu and no window action are added. Rows are reached only through the parent form.

### 5.5 ACLs (`ir.model.access` on `x_ipp_wb_product_line`)

| Name | Group | read | write | create | unlink |
| --- | --- | --- | --- | --- | --- |
| `ipp_wb_product_line operator read` | IPP Workbench Operator (`studio_customization.ipp_workbench_operator_group`, id 88) | 1 | 0 | 0 | 0 |
| `ipp_wb_product_line reader read` | one row per group found in 6.2 step 3 (groups with read on `x_ipp_import_workbench`), except group 88 and `base.group_system` | 1 | 0 | 0 | 0 |
| `ipp_wb_product_line hub writer` | `base.group_system`, the group of the Hub integration user (uid 2, whose API key the Hub uses) | 1 | 1 | 1 | 0 |

* Normal operators can only **read**. Nobody gets `unlink`: stale rows become non-current
  and are never deleted.
* No write access is granted ahead of time for future request fields.
* No record rules. The company is a plain projected integer, not `company_id`.
* No Odoo → Hub calls, no Hub credentials in Odoo, no server actions, no automations and
  no computed fields. Studio holds no business logic.

### 5.6 Hub configuration

| Env | Value | Meaning |
| --- | --- | --- |
| `ODOO_WORKBENCH_PRODUCT_LINE_PROJECTION_ENABLED` | absent / `false` (default) | No child read or write, and the parent projection is unchanged. |
| `ODOO_WORKBENCH_PRODUCT_LINE_PROJECTION_ENABLED` | `true` | Child rows are projected by runtime syncs and reconcile. |
| `ODOO_WORKBENCH_PRODUCT_LINE_*` field overrides | **not set** | The defaults are exactly the names in 5.2. Overrides exist only for non-production setups, and a set-but-blank override is a contract error. |

## 6. Production provisioning runbook (NOT executed in PR B)

Each step needs its own explicit approval. The Hub code can be deployed before any of
it: with the gate off, the Hub never reads or writes the child model.

### 6.1 Preconditions

* PR B is deployed with `ODOO_WORKBENCH_PRODUCT_LINE_PROJECTION_ENABLED` absent.
* A fresh Hub DB backup and a `.env.production` backup exist (`pg_restore -l` verified).
* The evidence directory `/var/tmp/pr_b_provision_<ts>/` exists.

### 6.2 Read-only discovery (user-2 JSON-2 key, `search_read` only)

| # | Model | Domain | Expect |
| --- | --- | --- | --- |
| 1 | `ir.model` | `[('model', '=', 'x_ipp_wb_product_line')]` | 0 rows |
| 2 | `ir.model.fields` | `[('model', '=', 'x_ipp_import_workbench'), ('name', '=', 'x_studio_ipp_product_line_ids')]` | 0 rows |
| 3 | `ir.model.access` | `[('model_id.model', '=', 'x_ipp_import_workbench'), ('perm_read', '=', True)]` | Record `group_id` values for 5.5 |
| 4 | `ir.ui.view` | `[('id', '=', 3984)]` | active; `arch_db` contains `<notebook` |
| 5 | `ir.ui.view` | `[('inherit_id', '=', 3984), ('priority', '=', 222)]` | 0 rows |
| 6 | `ir.model` | `[('model', '=', 'x_ipp_import_workbench')]` | 1 row; record its id as `P` |

Save the results to `pre_snapshot.json`. Stop if any expectation fails.

### 6.3 Create (JSON-2, user-2 key, exactly this order)

1. `ir.model/create` with
   `{"model": "x_ipp_wb_product_line", "name": "IPP Ürün Satırı", "state": "manual"}`.
   Record the new id as `M`.
2. `ir.model.fields/create`, once per row of 5.2, with
   `{"model_id": M, "name": …, "field_description": …, "ttype": …, "required": …, "index": …, "store": true, "copied": false}`.
   * For many2one fields, also pass `relation` and `on_delete`.
   * For `x_studio_ipp_match_state`, also pass
     `selection_ids: [[0, 0, {"value": <key>, "name": <label>, "sequence": <seq>}], …]`
     with the 7 rows of 5.2.
3. `ir.model.fields/create` for 5.3:
   `{"model_id": P, "name": "x_studio_ipp_product_line_ids", "field_description": "Ürün Satırları", "ttype": "one2many", "relation": "x_ipp_wb_product_line", "relation_field": "x_studio_ipp_workbench_id", "domain": "[('x_studio_ipp_is_current', '=', True)]", "store": true, "copied": false}`.
4. `ir.ui.view/create` for the two views of 5.4.
5. `ir.model.access/create` for the rows of 5.5, with `model_id: M`.

Record every created id in `provision.json`.

### 6.4 Contract verification (read-only)

1. Run `ir.model.fields` `search_read` with domain `[('model_id', '=', M)]` and fields
   `name, ttype, relation, required, index`.
   * Exactly the 18 names of 5.2 must be present, plus Odoo's own magic fields.
   * Each must have the exact `ttype`.
   * Each relation must be one of `x_ipp_import_workbench`, `res.partner` or `product.product`.
   * `x_studio_ipp_workbench_id` and `x_studio_ipp_line_key` must be `required`.
2. `ir.model.fields.selection` `search_read` with domain
   `[('field_id.model', '=', 'x_ipp_wb_product_line'), ('field_id.name', '=', 'x_studio_ipp_match_state')]`
   must return exactly the 7 keys.
3. Read the parent field `x_studio_ipp_product_line_ids` back. It must be `one2many`
   with the exact `relation`, `relation_field` and `domain` of 5.3.
4. As a group-88 user, open any parent row. The "Ürün Satırları" tab must show an
   empty, non-editable list.
5. Run the Hub dry-run with the gate enabled for **this one command only**. `.env.production`
   is not changed.

   ```sh
   docker exec -w /app config-uyumsoft-inbound-poller-1 sh -c '
     export ODOO_API_KEY="$(cat /run/secrets/odoo_api_key)";
     export DATABASE_URL="postgresql+psycopg://ict_hub_prod:$(cat /run/secrets/hub_db_password)@db:5432/ict_integration_hub_prod";
     export ODOO_WORKBENCH_PRODUCT_LINE_PROJECTION_ENABLED=true;
     exec python -m app.cli.reconcile_workbench_projection --company 1'
   ```

   The Hub verifies the contract itself before any child write:
   * the selection must offer all 7 keys;
   * every mapped field is read on each lookup, so a missing field fails as `CHILD ERROR`.

   Expected result:
   * `Children: CREATE=51 UPDATE=0 NO_CHANGE=0 DEACTIVATE=0 ERROR=0 | applied=False` (section 7);
   * parent `Totals` unchanged by PR B: 4 `UPDATE` (the pending 7B "Tamamlananlar"
     refresh of rows 17, 24, 44 and 45) and 42 `NO_CHANGE`.

### 6.5 Apply and enable (separate approvals)

1. **Backfill.** Run the same command with `--apply`. To keep the 4 stale parent rows
   out of this run, use one `--review-id` per review instead.
   * Follow-up dry-run: `Children: CREATE=0 UPDATE=0 NO_CHANGE=51 DEACTIVATE=0 ERROR=0`
     and no `CHILD` lines.
   * The Odoo count of `x_ipp_wb_product_line` must be exactly 51, with
     `x_studio_ipp_is_current = True` on all of them.
2. **Runtime.** Back up `.env.production`. Add
   `ODOO_WORKBENCH_PRODUCT_LINE_PROJECTION_ENABLED=true`. Then recreate both containers:
   `up -d --no-deps api`, then `up -d --no-deps uyumsoft-inbound-poller`. From then on,
   every projection-relevant transition also syncs that review's lines.

### 6.6 Rollback / hide

* **Stop Hub writes.** Remove the env key and recreate api and poller. The parent
  projection continues exactly as before PR B.
* **Hide from operators.** Set `active=False` on the parent form extension view from 6.3
  step 4. The tab disappears; no data changes.
* **Hide specific rows.** Rows disappear from the tab when the Hub sets
  `x_studio_ipp_is_current = False`. Rows are never deleted as part of a rollback.
* **Delete the model.** `ir.model` unlink drops the table. This, and deleting fields,
  needs a separate explicit approval and is never part of a routine rollback.
* No Hub schema exists for this feature, so there is nothing to downgrade.

## 7. Reconcile output

```text
UPDATE        review:dfccd66e-… v3 odoo_id=24
    x_studio_ipp_completed: … -> …
    CHILD CREATE     1 ipp-pl:v1:1:review:dfccd66e-…:1
        x_studio_ipp_match_state: None -> 'matched'
    CHILD CREATE     2 ipp-pl:v1:1:review:dfccd66e-…:2
...
Totals: CREATE=0 UPDATE=4 NO_CHANGE=42 SKIPPED_STALE=0 ERROR=0 | reviews=46 | applied=False
Children: CREATE=51 UPDATE=0 NO_CHANGE=0 DEACTIVATE=0 ERROR=0 | applied=False
```

* `CHILD DEACTIVATE` means `x_studio_ipp_is_current` True → False. Nothing is archived
  or deleted.
* A row that becomes projected again is the **same** record set back to True, reported as
  `CHILD UPDATE`.
* `NO_CHANGE` children are counted, not listed.
* `CHILD ERROR: …` reports a child failure for one review. The parent outcome is
  unaffected, and the exit code becomes 1.

## 8. Expected production projection (read-only survey, 2026-10-08, prod 8a021c9)

Derived with the rules above from read-only status/id aggregates of current Hub
evidence and reclassification history. Nothing was written.

**ICT Bulut: `review:dfccd66e…` v3, supplier partner 24 (Stage-1 evidence, tax-number match):**

| Satır (sequence) | Satıcı Ürün Kodu | Açıklama | Durum | Eşleşen Ürün | matched_by |
| --- | --- | --- | --- | --- | --- |
| 1 (1) | 100020 | Microsoft 365 Business Basic | `matched` | product.product 393 | `supplier_product_code` |
| 2 (2) | 100021 | Microsoft 365 Business Standard | `product_not_found` | — | — |

**All 46 reviews:** 34 qualify, giving 51 lines. 12 reviews are excluded: 10 pending
whole-invoice operating-expense reviews, CloudSpark (`b9aacadc`, accounting resolution)
and Apple (`8f24539e`, fixed asset). None of the excluded reviews has a product reason in
its reclassification history.

| Group | Reviews | Lines | States |
| --- | --- | --- | --- |
| Pending, supplier matched (Stage-1 evidence) | 12 | 22 | 7 `matched` (VİTEL 1c825f3b L1/L5 → 187/215; dfccd66e L1 → 393; LogoSoft 267f23a2 and 9afe7695 L1/L2 → 393/394), 15 `product_not_found` (suppliers 24, 75, 434) |
| Pending, no Stage-1 evidence (supplier not found/ambiguous) | 15 | 22 | 12 `supplier_unresolved`, 10 `identifier_missing` (6d071152 L1, d99bcefd L1, 8a867709 L1–L8) |
| Decided (accepted decision evidence) | 7 | 7 | 4 `matched` (1316ab15 → 389 and 2fd26e5e → 392 human-selected; cee1f8d5 → 393 and 2fb46a57 → 394 automatic), 3 `resolved_without_product` (9b13ad00, 1cbf1ff0, 6074973d: account-only) |
| **Total** | **34** | **51** | `matched` 11, `product_not_found` 15, `supplier_unresolved` 12, `identifier_missing` 10, `resolved_without_product` 3 |

ICT Bulut's other PRODUCT_NOT_FOUND reviews project as `product_not_found` under partner
24: b7c4b622 L1, eeec6686 L1, 3943f7f9 L1–L2 and 07d81248 L1.

Line ids "10" (1cbf1ff0 and 6074973d, single-line invoices) get sequence 10. The Apple ids
`000001`/`000002` would get 1 and 2, but Apple is excluded.
