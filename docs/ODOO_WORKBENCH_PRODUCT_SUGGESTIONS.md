# Odoo Workbench Product Suggestions (PR F / F3)

Advisory product suggestions on `x_ipp_wb_product_line` rows ([ADR-0014](adr/ADR-0014-advisory-product-suggestions.md)).
F3.1 is the Hub code. **The Studio work in this document is a plan: it is not provisioned by F3.1,**
and every step in section 4 needs its own explicit approval.

Suggestions are display-only. They never select, map or create a product, never write supplierinfo
or an Internal Reference, and never change the line's match state. The operator still picks the
product in the existing request fields and presses *Eşleştir*; the Hub validates that request
exactly as before.

## 1. Hub contract (F3.1)

| What | Value |
| --- | --- |
| Display flag | `ODOO_WORKBENCH_PRODUCT_LINE_SUGGESTIONS_DISPLAY_ENABLED` (default `false`) |
| Requires | `ODOO_WORKBENCH_PRODUCT_LINE_SUGGESTIONS_ENABLED=true` (F2 worker), `ODOO_WORKBENCH_PRODUCT_LINE_PROJECTION_ENABLED=true`, both field envs below |
| Many2many field env | `ODOO_WORKBENCH_PRODUCT_LINE_SUGGESTED_PRODUCTS_FIELD` |
| Summary field env | `ODOO_WORKBENCH_PRODUCT_LINE_SUGGESTION_SUMMARY_FIELD` |
| Partial mapping | rejected (`WorkbenchContractError`); both or neither |
| Unmapped | child publisher reads/writes exactly the PR B fields (byte-identical) |
| Mapped, display off | both fields are cleared on every synced row |
| Validity | 20 minutes from the last successful catalog validation (unchanged) |
| Projection cutoff | 19 minutes (validity − one 60 s sweep interval) |
| Sweep | worker task `advisory_suggestion_display_sweep`, every 60 s, database-only due check |

A suggestion is shown only when **all** hold: review pending; set `current`; same review version;
line state `product_not_found`; same line key and line number; same effective supplier; same seller
code; at least one candidate **or** advisory note; validated within the 19-minute cutoff. Anything
else clears both fields. Deactivated (non-current) rows are always cleared.

The summary is plain text. Per candidate: rank, product name (variant included), tier and score,
then the engine's persisted reasons (`Gerekçe:`) and the candidate's warnings (`Uyarı:`), verbatim.
After the candidates, the set's advisory notes (`Uyarı:`). For example:

```text
1. Microsoft Windows Server Standard (per VM) — ürün adı benzerliği (skor 0.90)
   Gerekçe: Ad benzerliği 0.90
   Gerekçe: Ortak anlamlı sözcükler: windows
```

A T0 result (always returned alone):

```text
1. Microsoft Windows Server Standard (per VM) — tedarikçi ürün kaydı artık mevcut; kontrollü yeniden sınıflandırma gerekir (skor 1.00)
   Gerekçe: Tedarikçi ürün kaydı #10: tedarikçi #24, kod TFZP
   Uyarı: Kayıtlı inceleme kanıtı eski: bu tedarikçi kodu artık bu ürüne bağlı. …
```

**Notes-only.** When no candidate passed the engine's guards but the set has an advisory note, the
row gets an **empty** Many2many and a warning-only summary (no candidate is invented), e.g.:

```text
Güvenli ürün önerisi yok.
Uyarı: Bu tedarikçinin ürün kaydındaki kod yalnız büyük/küçük harf veya boşlukla farklı; kesin kod eşleşmesi değil, yeniden sınıflandırma bu satırı eşleştirmez.
```

Notes-only covers the near-miss supplier code, ambiguous supplierinfo, CSP ProductId-only (other
SKU) and multi-variant (ManageEngine) catalog cases, and passes exactly the same gate. A set with
neither a candidate nor a note shows nothing.

Names come from the candidate's `product_name` snapshot (the `display_name` read by the catalog
snapshot that generated the set); a pre-0042 row without one shows `Ürün #<id>`.

## 2. Staleness contract (must be stated in the UI)

* The Hub **never publishes** an expired suggestion: every projection, from every flow, applies the
  gate with the 19-minute cutoff.
* While the worker and Odoo are operational, the 60 s sweep (and the refresh after every advisory
  tick) clears expired, superseded and invalidated suggestions from Odoo, normally before the
  20-minute validity ends.
* A failed clearing is retried (60 s backoff doubling to 5 min) and never extends validity.
* **Odoo UI expiry is not guaranteed during an outage.** If the worker is down or saturated, or
  Odoo writes fail, the Odoo UI may keep the last published suggestions until the next successful
  refresh.

Therefore the Studio UI shows this static warning wherever suggestions appear:

> **Öneriler bilgilendirme amaçlıdır. Güncellikleri değişebilir; eşleştirme öncesinde ürünü doğrulayın.**

## 3. Studio plan (F3.2 — not provisioned)

### 3.1 Fields on `x_ipp_wb_product_line` (2)

Both `store=True`, `copied=False`, `readonly=True` in every view, no tracking, compute or default.

| Technical name | Label | `ttype` | Relation / extra |
| --- | --- | --- | --- |
| `x_studio_ipp_suggested_product_ids` | Önerilen Ürünler | many2many | `product.product`; `relation_table` `x_ipp_wb_pl_suggested_product_rel`, `column1` `x_ipp_wb_product_line_id`, `column2` `product_product_id` |
| `x_studio_ipp_suggestion_summary` | Öneri Özeti | text | plain text, no HTML |

Both names deliberately avoid the operator input prefixes `x_studio_ipp_req_` / `x_studio_ipp_new_`
(the Hub rejects a mapping that uses them). The existing ACLs already cover the new fields: operators
read, the Hub integration user writes. No new ACL, record rule, automation or server action.

### 3.2 Views — preferred: row-detail dialog

Extend the PR B product line tab list (on the parent form) with `open_form_view="1"` so a row opens its
read-only child form in a dialog, and extend the child form with a suggestions group:

```xml
<data>
  <!-- Visibility follows the SUMMARY only, never the Many2many: a notes-only row has no products. -->
  <xpath expr="//sheet" position="inside">
    <group string="Ürün Önerileri"
           invisible="x_studio_ipp_match_state != 'product_not_found' or not x_studio_ipp_suggestion_summary">
      <div class="alert alert-warning" role="alert" colspan="2">
        Öneriler bilgilendirme amaçlıdır. Güncellikleri değişebilir; eşleştirme öncesinde ürünü doğrulayın.
      </div>
      <field name="x_studio_ipp_suggestion_summary" readonly="1" nolabel="1" colspan="2"/>
      <field name="x_studio_ipp_suggested_product_ids" readonly="1" widget="many2many_tags"
             invisible="not x_studio_ipp_suggested_product_ids"/>
    </group>
  </xpath>
</data>
```

`open_form_view` on an inherited one2many list is unproven on this Odoo 19.3 database. Probe it
first with an extension view restricted to `base.group_system` (`group_ids`), confirm the dialog opens
read-only and the request fields are untouched, then widen it to the operator group.

### 3.3 Views — fallback: separate tab

If the dialog does not work, add a second parent tab *Ürün Önerileri* with a second one2many field on
`x_ipp_import_workbench` (`relation_field` `x_studio_ipp_workbench_id`, domain
`[('x_studio_ipp_is_current', '=', True), ('x_studio_ipp_match_state', '=', 'product_not_found')]`),
the same warning as an `alert-warning` div above the list, and the columns line, seller code,
description, summary and suggested products — all read-only, `create="0" edit="0" delete="0"`. Do not
filter that list on the Many2many: notes-only rows have no products but must still show their summary.

## 4. Enablement order (each step separately approved)

1. Deploy the Hub code (F1–F3.1) with every advisory flag off; `alembic upgrade head` (0041, 0042).
2. Provision the two fields (3.1) and verify them read-only (`fields_get` / `search_read`).
3. Set both field envs with `ODOO_WORKBENCH_PRODUCT_LINE_SUGGESTIONS_DISPLAY_ENABLED=false`; reconcile
   dry-run must show at most `x_studio_ipp_suggestion_summary` / `..._suggested_product_ids` clears.
4. Enable `ODOO_WORKBENCH_PRODUCT_LINE_SUGGESTIONS_ENABLED` (F2 worker); review generated sets.
5. Provision the views (3.2, or 3.3), admin-only first.
6. Enable `ODOO_WORKBENCH_PRODUCT_LINE_SUGGESTIONS_DISPLAY_ENABLED`; recreate the worker and API.

## 5. Rollback

* Hub: `ODOO_WORKBENCH_PRODUCT_LINE_SUGGESTIONS_DISPLAY_ENABLED=false` and recreate the API and worker.
  With the field envs still set, the next sync of each review clears both fields; reconcile
  `--apply` clears them everywhere at once. Unset the field envs only after that.
* Studio: deactivate the views; the fields can stay (unmapped fields are never read or written).
* Database: `alembic downgrade 202607170041` drops only the two display columns.
