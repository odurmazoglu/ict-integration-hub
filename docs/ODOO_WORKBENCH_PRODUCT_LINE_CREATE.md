# Odoo Workbench New Product Creation (PR D)

An operator creates a **new** Odoo product for one PRODUCT_NOT_FOUND invoice line directly on
its Workbench child row (`x_ipp_wb_product_line`). The request runs through the existing
ADR-0013 Hub-pull pipeline (ledger, actor directory, narrow write authorization, projection
refresh, acknowledgement) as a third request channel next to the PR C mapping channel.

* PR C "Eşleştir" (map to an existing product) is **unchanged**. Use it whenever a suitable
  product already exists; PR D refuses obvious duplicates and points back to it.
* No candidate suggestions (PR F), no parent-input retirement (PR E), no VAT cleanup, no
  Odoo → Hub call, no Hub credentials in Odoo, no custom addon.

Code:

* `app/application/workbench/product_line_creation.py`: intent, command, state, errors, ports.
* `app/application/workbench/product_line_creation_policy.py`: master-data policy (section 3).
* `app/application/workbench/product_line_creation_use_cases.py`: `CreateLineProductUseCase`.
* `app/erp/odoo/workbench_product_line_create_request_reader.py`: reader + acknowledger.
* `app/erp/odoo/product_line_creation_reader.py`: read-only catalog / recovery lookups.
* `app/erp/write/odoo_product_writer.py`: `ValidatedSupplierLink` (inline `seller_ids`).
* `alembic/versions/202607170040_product_line_creation.py`: ledger action
  `product_line_create` + table `workbench_product_line_creations`.

## 1. Flow

```text
Operator (group 88), Workbench form → tab "Ürün Satırları", a product_not_found line
  fills Yeni Ürün Adı, Ürün Türü, Ürün Kategorisi, Birim (Odoo), optional İç Referans
  presses the row's "Yeni Ürün Oluştur"
  └─ Studio server action (copies values only): new_version := ipp_review_version,
     new_requested_by := env.user, new_requested_at := now, result/message := False, new_ready := True

Hub poller, operator request tick (every 60 s, advisory lock), channels in fixed order:
  1. product_line         (PR C mapping, unchanged)
  2. product_line_create  (PR D, only when ODOO_WORKBENCH_PRODUCT_LINE_CREATE_ENABLED=true)
       ready rows (x_studio_ipp_new_ready) → identity verification (PR C rules) → OperatorRequest(PRODUCT_LINE_CREATE)
       → ledger → actor + permission → narrow CREATE_NEW_PRODUCT authorization
       → ProductLineCreateRequestHandler → CreateLineProductUseCase
       → projection refresh → acknowledgement on the child row
  3. parent               (unchanged)
```

The create request has its **own** ready flag, version, requester and timestamp. The PR C
reader filters on `x_studio_ipp_req_ready` and never sees a create request, so with the create
gate off a submitted create request stays completely inert. Only the three Hub-owned result
fields (`x_studio_ipp_req_result`, `_message`, `_processed_at`) are shared, so the row has one
"Sonuç" column. A row with both a pending mapping and a pending create request is refused on
the create side (*Reddedildi*: "Bu satırda bekleyen bir 'Eşleştir' isteği var …").

## 2. What the use case does

Every check runs before any write. The **only** Odoo write is one `product.template` create.

1. If a creation record exists for (review, company, snapshotted version, line): resume it
   (section 4). Never a second create.
2. Review pending at exactly the snapshotted version; the line carries `PRODUCT_NOT_FOUND`.
   Otherwise *Güncel Değil* / *Reddedildi*.
3. Seller product code from the **immutable source invoice line**. Missing → refused.
4. Supplier = the review's **effective supplier** (`EffectiveSupplierResolver`), the partner the
   follow-up reclassification matches products under.
5. Master-data policy (section 3), read-only.
6. No `product.supplierinfo` for (supplier, code) and no identity claim by another line.
   Otherwise *Reddedildi* with the existing record named.
7. **One commit before the Odoo call:** supplier-product identity claim (shared with REST
   CREATE_NEW_PRODUCT, so the two can never race; `owner_kind = product_line_creation`),
   consumed CREATE_NEW_PRODUCT authorization (consumer id bound to the full intent digest),
   creation record `create_attempted`.
8. `product.template` create with `name`, `type`, `uom_id`, `is_storable`, `categ_id`, optional
   `default_code` and exactly one inline `seller_ids` row
   `{partner_id, product_code, company_id, product_name}`. Odoo commits template, variant and
   supplier mapping in **one transaction**. The writer reads back name, Internal Reference,
   category, storability, type and UoM, and resolves the single variant.
9. `product_created` recorded with template + variant ids.
10. Post-create verification (category policy re-read; RESALE eligibility) and supplier-mapping
    read-back, then `completed` committed **together** with the `MASTER_DATA_CHANGED`
    reclassification.

The projection refresh then shows the line `matched` (template-level supplierinfo on a
single-variant template is a deterministic match). The acknowledgement writes *Tamamlandı* and
empties the five create inputs. Future invoices of the same supplier and code match automatically.

## 3. Master-data policy (fail closed)

Grounded in the ICT Product Master Data Policy draft (2026-09-16 read-only production audit).

| Rule | Check | Result |
| --- | --- | --- |
| Supplier code is never Internal Reference | `İç Referans` equal (ignoring case and whitespace) to the line's seller code, to any other seller code on the same invoice, or to any vendor code stored on an Odoo `product.supplierinfo` (any vendor) | *Reddedildi* |
| Variant/license catalogs are manual-only | seller code matches the built-in ManageEngine license SKU shape `^\d{4,6}\.\d[0-9A-Z]{2,8}$` (e.g. `85710.1SS3`, `67005.6MPMFA4`, `702012.0SUP6`) or a configured extra pattern | *Reddedildi* (manual Odoo creation with variants, then *Eşleştir*) |
| | `İç Referans` has that SKU shape | *Reddedildi* |
| | category is, or is under, a `PRODUCT_CREATION_MANUAL_ONLY_CATEGORY_IDS` root (by Odoo `complete_name`; an unprovable hierarchy fails closed) | *Reddedildi* |
| Category is an explicit operator choice | must exist; under a current-version RESALE purpose: exact `RESALE_PRODUCT_CATEGORY_IDS` member, non-storable, valid purchase account (existing 18E-2 policy) | *Reddedildi* |
| Type is an explicit operator choice | one selection: *Hizmet* (service), *Stoksuz Mal* (consu, not storable), *Stoklu Mal* (consu, storable). No default. | — |
| UoM is an explicit operator choice | must be in `PRODUCT_CREATION_UOM_IDS` (no UoM is assumed; see 3.1); if `PRODUCT_CREATION_UOM_UNIT_CODES` lists codes for it, the invoice line's UN/CEFACT unit code must be one of them; Odoo read-back must equal it | *Reddedildi* |
| No duplicates | an Odoo variant (active or archived) whose Internal Reference equals the seller code; an Internal Reference already in use; an active template with the same name (case-insensitive) | *Reddedildi* → map the existing product instead |

### 3.1 Unit of measure policy

Nothing in the code assumes "Units": with an empty `PRODUCT_CREATION_UOM_IDS` every create is
refused and the gate fails startup. Production starts restrictive with only the UoM ICT uses for
100% of its catalog (policy §7: `[1]`). To safely allow another UoM later (separate approval):

1. Read-only: confirm the `uom.uom` id, name, active flag and its relation to Units in Odoo 19.3
   (19 has no UoM categories; conversions follow `relative_uom_id` / factor).
2. Set `PRODUCT_CREATION_UOM_UNIT_CODES` for **every** allowed UoM, e.g.
   `{"1": ["C62", "NIU", "EA"], "5": ["DAY"]}` (the startup check refuses entries for UoMs not in
   the allowlist and empty code lists). A product invoiced per piece can then never be created
   with "Days", which would silently change vendor bill quantities.
3. Add the id to `PRODUCT_CREATION_UOM_IDS`.

`uom.uom` intentionally stays outside the Hub's Odoo read allowlist (P0-PROD-12C): existence is
enforced by the curated allowlist plus Odoo's own FK validation at create (a missing id is a
refusal, i.e. no write) and the strict read-back.

### 3.2 ManageEngine / vendor SKUs

* Existing rules are preserved: #209 keeps `seller_item_code → default_code` advisory only;
  ManageEngine license SKUs live on product **variants** (policy §14) and are never created here.
* Seller codes cannot become Internal References: refused against this line, the whole invoice
  and every stored Odoo vendor code; additionally `CreateProductCommand` refuses a `default_code`
  equal to the supplier link's code (defense in depth). The Hub never writes the input fields
  except to clear them after completion.
* Manual-only categories fail closed: unknown target category, a configured root missing from
  Odoo, or an unreadable `complete_name` refuse the request. Subtree membership uses Odoo's
  `complete_name` ("A / B / C"); a category *name* containing " / " can only cause a false
  refusal, never a false pass. ManageEngine *services* placed under a manual-only root are
  refused too (create them manually).
* No SKU generator: Internal Reference is an optional operator text, never generated.

The Hub never prefills, derives or copies any of the inputs. The supplier code is written only to
`product.supplierinfo.product_code`. Taxes, barcode, company, `x_studio_ana_tedarikci` and other
Studio fields are never written (exact key allowlist in the writer).

## 4. Crash safety, idempotency and conflicts

The inline supplier mapping is the creation key: it exists iff the template create committed.

| Situation | Outcome |
| --- | --- |
| Odoo refused before or at the create (gate, validation, auth) | No product can exist: record and claim are deleted; *Reddedildi*; a corrected resubmission starts fresh. |
| Create sent, response lost (timeout etc.) | Record stays `create_attempted`; *Reddedildi* "sonucu doğrulanamadı … 30 dakika sonra tekrar gönderin". |
| Resubmission, mapping found and template matches the recorded intent exactly | Product adopted, line completed. No second create. |
| Resubmission, mapping found but template differs, or several mappings | `needs_reconciliation`; every later submission refused until a human reconciles. Never guessed. |
| Resubmission, no mapping, within 30 min of the attempt | *Reddedildi* "… HH:MM UTC sonrasında tekrar gönderin" (the create could still commit server-side). |
| Resubmission, no mapping, after 30 min | The create provably never committed: record and claim deleted, fresh attempt. The duplicate-name check is a second guard. |
| Review moved after a lost response (older-version attempt of the same line) | Same rules: inside the grace → wait; after it, no mapping → the old attempt and its claim are discarded and the new version proceeds; a completed/created older attempt → *Reddedildi* (never a second product). |
| Read-back refused after the template was created (4xx/401/403 on the read) | Classified "outcome unknown", never "no write" (the claim is kept); the next submission adopts. |
| Reclassification refused after the create (review moved) | Product and mapping stay (`product_created`); *Güncel Değil*. A resubmission at the same version completes; at a newer version the line is already matched. |
| Odoo acknowledgement fails after the Hub commit | Ledger terminal → the next tick only re-acknowledges. |
| Same request again after completion | *Tamamlandı (zaten)*, nothing written. A different intent for the same line → *Reddedildi*. |
| Supplier code already mapped / claimed by another line | *Reddedildi*, no write. |
| Actor without `workbench_execute` | *Reddedildi* before any authorization or write. |

Ledger: `action = 'product_line_create'`, `odoo_record_id` = child row id; the request key includes
the intent digest. State: `workbench_product_line_creations` (one row per line/version).

**Locks** (proven on PostgreSQL in `tests/integration/test_pr_d_creation_locks_postgres.py`):

* line lock: unique `(review, company, version, line)` on the creation table;
* identity lock: unique `(company, supplier, seller code)` on the shared claim table, for both
  REST CREATE_NEW_PRODUCT and PR D. `owner_kind` says which table owns a claim (migration 0040
  replaced the former reservation-only FK, which would have rejected every PR D claim);
* claim + authorization + record commit together; a race loser rolls all three back and gets
  *Reddedildi* ("… şu anda işleniyor"); the operator request tick is single-flight anyway
  (advisory lock), so races only come from the REST path or a second Hub process.

## 5. Identity and trust boundary

Line identity: identical to PR C ([ODOO_WORKBENCH_PRODUCT_LINE_REQUESTS.md](ODOO_WORKBENCH_PRODUCT_LINE_REQUESTS.md)
section 3): identity fields must agree with the deterministic line key and the parent row; the row
must be current; duplicate ready keys are refused; seller code, description, supplier, match state
and projected product are never read.

### 5.1 How `requested_by` can be forged (exactly)

The Hub authorizes whoever `x_studio_ipp_new_requested_by` **names**. That field is an ordinary
stored many2one; Odoo only checks model ACLs (no field `groups`, no record rules). Any internal
user with **write** on `x_ipp_wb_product_line` can therefore, without the button:

1. JSON-2 / XML-RPC: `x_ipp_wb_product_line.write([row], {"x_studio_ipp_new_requested_by": <uid of
   an allowlisted actor>, "x_studio_ipp_new_requested_at": <now>, "x_studio_ipp_new_version": <the
   row's x_studio_ipp_review_version>, "x_studio_ipp_new_ready": true, …inputs})`;
2. or the standard UI **Import** on the list (update existing rows by `id`), which writes any
   writable field the same way;
3. a user with **create** could also fabricate a child row with a valid line key and parent
   (identity checks pass because they only prove internal consistency).

The next tick then issues a `CREATE_NEW_PRODUCT` authorization *as the named actor* and creates
the product; the ledger attributes it to that actor. The actor allowlist limits **whom** one can
impersonate, not **whether**.

### 5.2 Is production contained for the initial rollout?

Read-only check 2026-10-09 (PR C §3.1): writers on the child model are ACL 1371 (group 4) and
ACL 1372 (group 88, provisioned 10-09); both resolve to **uid 2 only**, which is also the only
allowlisted actor **and the Hub's own API user**. Forging therefore grants nothing uid 2 does not
already have: **contained for initial rollout, on the condition** that the 7.0 #9 check is re-run
(read-only) immediately before enabling and that group 88 / group 4 membership and the actor
allowlist stay unchanged while the gate is on.

### 5.3 Smallest safe mitigation (no new event model)

As long as the operator and the Hub share uid 2, no Odoo-side control can tell them apart, so the
root fix is to separate them. Before **any** second Odoo user gets write access (separate design
and approval; not part of PR D):

1. a dedicated Odoo integration user for the Hub API key (no human login);
2. a Studio group "IPP Request Stamp" containing **only** that user, set as field `groups` on the
   four stamp fields (`…_new_ready`, `…_new_version`, `…_new_requested_by`, `…_new_requested_at`,
   and the PR C equivalents): operators can then neither read nor write them via JSON-2 or Import;
3. the two server actions write the stamp with `record.sudo().write(...)` (field groups do not
   apply in sudo), taking `env.user` *before* `sudo()`;
4. the list views stop referencing the stamp fields in `invisible`/`readonly` expressions (Odoo
   strips fields the user cannot access from the arch), e.g. by showing the result field instead.

This keeps the current request models, needs no Hub code change (the Hub reads the fields as the
integration user) and closes paths 1–2; path 3 is closed by keeping create on the child model
for the integration user / administrators only. Until then the operating rule stands: group 88
= uid 2 only, and any membership or ACL change is a security decision needing its own approval.

## 6. Studio contract (exact; NOT provisioned by PR D)

### 6.1 New fields on `x_ipp_wb_product_line` (9)

All: `store=True`, `copied=False`, no compute, **no default**, not required.

| # | Technical name | Label | `ttype` | Relation / extra | Owner |
| --- | --- | --- | --- | --- | --- |
| 1 | `x_studio_ipp_new_name` | Yeni Ürün Adı | char | | operator |
| 2 | `x_studio_ipp_new_kind` | Ürün Türü | selection | `Hizmet`, `Stoksuz Mal`, `Stoklu Mal` (value = label) | operator |
| 3 | `x_studio_ipp_new_categ` | Ürün Kategorisi | many2one | `product.category`, `on_delete=set null` | operator |
| 4 | `x_studio_ipp_new_uom` | Birim (Odoo) | many2one | `uom.uom`, `on_delete=set null` | operator |
| 5 | `x_studio_ipp_new_ref` | İç Referans | char | | operator |
| 6 | `x_studio_ipp_new_ready` | Yeni Ürün İsteği Hazır | boolean | | button / Hub |
| 7 | `x_studio_ipp_new_version` | İnceleme Sürümü (yeni ürün) | integer | | button |
| 8 | `x_studio_ipp_new_requested_by` | Yeni Ürünü İsteyen | many2one | `res.users`, `on_delete=set null` | button |
| 9 | `x_studio_ipp_new_requested_at` | Yeni Ürün İstek Zamanı | datetime | | button |

Result fields are the existing PR C `x_studio_ipp_req_result` / `_message` / `_processed_at`.
Each name can be overridden by `ODOO_WORKBENCH_PRODUCT_LINE_NEW_<NAME>_FIELD` (non-production only).

### 6.2 Server action "Yeni Ürün Oluştur"

`ir.actions.server`: `model_id` = `x_ipp_wb_product_line`, `state=code`, `group_ids` = [88], no
binding. Copies values only:

```python
for record in records:
    record.write(
        {
            "x_studio_ipp_new_version": record.x_studio_ipp_review_version,
            "x_studio_ipp_new_requested_by": env.user.id,
            "x_studio_ipp_new_requested_at": datetime.datetime.now(),
            "x_studio_ipp_req_result": False,
            "x_studio_ipp_req_message": False,
            "x_studio_ipp_new_ready": True,
        }
    )
```

Action 1027 ("Eşleştir") is not changed.

### 6.3 View

A new `ir.ui.view`: `type=form`, `model=x_ipp_import_workbench`, `inherit_id` = 3984, `priority`
224 (after PR C's 4211 at 223), `mode=extension`. It extends 4211's editable list only;
deactivating it restores the PR C UI exactly.

```xml
<data>
  <xpath expr="//field[@name='x_studio_ipp_product_line_ids']/list/field[@name='x_studio_ipp_req_message']"
         position="after">
    <field name="x_studio_ipp_new_ready" column_invisible="1"/>
    <field name="x_studio_ipp_new_name" string="Yeni Ürün Adı" optional="show"
           readonly="x_studio_ipp_match_state != 'product_not_found' or x_studio_ipp_new_ready"/>
    <field name="x_studio_ipp_new_kind" string="Ürün Türü" optional="show"
           readonly="x_studio_ipp_match_state != 'product_not_found' or x_studio_ipp_new_ready"/>
    <field name="x_studio_ipp_new_categ" string="Kategori" optional="show"
           readonly="x_studio_ipp_match_state != 'product_not_found' or x_studio_ipp_new_ready"/>
    <field name="x_studio_ipp_new_uom" string="Birim (Odoo)" optional="show"
           readonly="x_studio_ipp_match_state != 'product_not_found' or x_studio_ipp_new_ready"/>
    <field name="x_studio_ipp_new_ref" string="İç Referans" optional="hide"
           readonly="x_studio_ipp_match_state != 'product_not_found' or x_studio_ipp_new_ready"/>
    <button name="ACTION_ID_YENI_URUN" type="action" string="Yeni Ürün Oluştur" icon="fa-plus"
            invisible="x_studio_ipp_match_state != 'product_not_found' or x_studio_ipp_new_ready
                       or x_studio_ipp_req_ready or x_studio_ipp_req_product
                       or not x_studio_ipp_new_name or not x_studio_ipp_new_kind
                       or not x_studio_ipp_new_categ or not x_studio_ipp_new_uom"/>
  </xpath>
</data>
```

View attributes are convenience only; the Hub re-validates everything.

### 6.4 ACL

**No new ACL.** PR C's ACL 1372 (group 88, read + write, no create/unlink) already covers the new
fields. Group 88 needs read on `product.category` and `uom.uom` (standard for internal users;
verify in 7.0).

### 6.5 Hub configuration

| Env | Value |
| --- | --- |
| `ODOO_WORKBENCH_PRODUCT_LINE_CREATE_ENABLED` | absent / `false` (default): create fields never read. `true`: the channel runs. |
| prerequisites (startup check) | `ODOO_WORKBENCH_PRODUCT_LINE_REQUESTS_ENABLED=true` (and its own prerequisites) |
| `PRODUCT_CREATION_UOM_IDS` | required when on; production starts with `[1]` (Units) only — do not expand without 3.1 |
| `PRODUCT_CREATION_UOM_UNIT_CODES` | optional; required by 3.1 before a second UoM is allowed |
| `PRODUCT_CREATION_MANUAL_ONLY_CATEGORY_IDS` | required when on; the id(s) of "Software Licenses / ManageEngine" (and any other variant-catalog root) from the 7.0 read-only check |
| `PRODUCT_CREATION_MANUAL_ONLY_SKU_PATTERNS` | optional extra regexes; the ManageEngine shape always applies |
| `RESALE_PRODUCT_CATEGORY_IDS` | unchanged (existing RESALE rules) |
| `ODOO_WORKBENCH_PRODUCT_LINE_NEW_*_FIELD` | **not set** in production |

The creating actor needs `workbench_review_decide` + `workbench_execute` (uid 2 has both).
`PRODUCT_REMEDIATION_WRITE_ENABLED` stays **unset**: the narrow authorization bypasses only that
flag, never the production kill switch, approval ack or named approver.

## 7. Rollout (NOT executed in PR D; each step needs its own approval)

1. **Merge.**
2. **Deploy with the create gate absent.** Back up DB and config; build; `alembic upgrade head`
   (0039 → 0040) through the established one-off container; recreate api and poller.
   Expect no behaviour change: PR C channel reads byte-identical fields; reconcile dry-run
   NO_CHANGE; 0 `channel_failed`.
3. **Studio provisioning** (user-2 JSON-2 key), in order: 9 fields (6.1), server action (6.2),
   view (6.3, action id substituted). Record every id in `provision.json`. Read-only verify:
   names/types/relations, the 3 selection labels, action `group_ids`, combined arch contains the
   new button exactly once and 4211's *Eşleştir* still once, ACL 1372 unchanged.
4. **Button acceptance with the create gate OFF** (7.A).
5. **Configure + enable the gate** (7.B), then **negative acceptance** (7.C) and
   **positive acceptance** (7.D), each separately approved.

### 7.0 Pre-checks (read-only; STOP if any fails)

| # | Check | Expect |
| --- | --- | --- |
| 1 | Acceptance line (candidate: ICT Bulut `TFZP`, review `3943f7f9…` line 1 — confirm it is still the right business choice) | review `pending_review` at version `V`; line carries `PRODUCT_NOT_FOUND`; child row current, `product_not_found`; all `x_studio_ipp_new_*` empty; PR C request fields empty |
| 2 | `product.supplierinfo` (partner 24, code `TFZP`) | 0 rows |
| 3 | Identity claims for (1, 24, `TFZP`); `workbench_product_line_creations` | none |
| 4 | Odoo products with name = intended name, or Internal Reference = `TFZP` / intended reference | none |
| 5 | `product.category` ids and `complete_name`: target category; "Software Licenses / ManageEngine" root(s) | record ids for 6.5 |
| 6 | `uom.uom` 1 | "Units", active |
| 6b | Field metadata (`ir.model.fields`, read-only): `product.template` `type` (selection incl. `consu`, `service`), `is_storable` (boolean; needs the stock module), `uom_id`, `categ_id`, `default_code`, `seller_ids` (one2many → `product.supplierinfo`); `product.supplierinfo` `partner_id`, `product_code`, `product_name`, `company_id`, `product_tmpl_id`, `product_id` | all present with these types; otherwise STOP |
| 7 | Purchase purpose of the review at `V` | record it; RESALE requires an allowlisted category |
| 8 | Parent row 24-style request inputs on the acceptance review's parent row | empty (PR C 7.A lesson: the row button saves dirty parent inputs) |
| 9 | Group 88 members / writers on both Workbench models | uid 2 only |
| 10 | `ODOO_WORKBENCH_PRODUCT_LINE_CREATE_ENABLED` in api and poller | absent |

### 7.A Button acceptance (create gate OFF)

1. Back up the DB; snapshot the child row, its siblings, the parent row, counts/max ids of
   `product.template`, `product.product`, `product.supplierinfo`, ledger count.
2. uid 2 fills the 4–5 inputs on the acceptance line and presses **Yeni Ürün Oluştur**. Note any
   dialog verbatim.
3. Verify (read-only): `new_ready=True`, `new_version=V`, `new_requested_by=2`, `new_requested_at`
   set (record `T`), inputs exactly as typed, result/message empty, projection fields unchanged,
   PR C request fields unchanged, parent row unchanged except `write_date`, no product /
   supplierinfo / ledger change.
4. PASS only if all hold and no dialog appeared. The request is inert (nothing reads it).

STOP conditions: button not visible with all inputs filled; any dialog; a "discard changes" prompt
or lost input; ready/requested_at not set; wrong version/requester; any unexpected write. On STOP:
change nothing, keep the gate OFF, report (recovery 7.E).

### 7.B Configure and enable

Only after 7.A PASS and a new approval. Pre-enable check: the only ready create row is the 7.A row
with the same `T`; no ready PR C row on it; review still at `V`. Back up `.env.production`; add
`ODOO_WORKBENCH_PRODUCT_LINE_CREATE_ENABLED=true`, `PRODUCT_CREATION_UOM_IDS=[1]`,
`PRODUCT_CREATION_MANUAL_ONLY_CATEGORY_IDS=[<7.0 #5>]`; recreate api, then poller (same image).
The first tick processes the 7.A request → continue with 7.D expectations. (If the negative test
7.C is wanted first, run 7.A on the ManageEngine line instead and the positive line afterwards.)

### 7.C Negative acceptance (no write expected)

On a VİTEL ManageEngine license line (seller code e.g. `85710.1SS3`): submit a create request.
Expect within one tick: *Reddedildi* "… varyantlı bir lisans kataloğu SKU'su …", inputs kept,
ledger row `rejected`, **no** `product.template` / supplierinfo / creation row / claim.

### 7.D Positive acceptance — expected end state

* Ledger: one `product_line_create` row, `completed`, with an authorization id;
  authorization `CREATE_NEW_PRODUCT` at `V`, consumed once (consumer `product-line-create:…`).
* Exactly one new `product.template` + one `product.product`: name / type / UoM / category /
  Internal Reference exactly as requested; `default_code` ≠ `TFZP`.
* Exactly one new `product.supplierinfo`: partner 24, `product_code TFZP`, that template,
  company 1, `product_name` = invoice description.
* `workbench_product_line_creations` row `completed` with the three ids; identity claim owned by
  this line.
* Reclassification `MASTER_DATA_CHANGED`, `V → V+1`; the line no longer `PRODUCT_NOT_FOUND`.
* Child row: `matched`, product = new variant; result *Tamamlandı*; create inputs empty.
* Nothing else changed (fingerprints). Next 2 ticks idempotent; reconcile dry-run NO_CHANGE.

### 7.E STOP and recovery

* **Gate OFF:** leaving a pending create request untouched is always safe. Approved
  neutralization (explicit approval naming row id and `T`): one guarded write
  `{"x_studio_ipp_new_ready": false, "x_studio_ipp_req_result": "Reddedildi",
  "x_studio_ipp_req_message": "Kabul testi durduruldu; istek işlenmedi."}` after re-reading that
  `new_requested_at == T` and the gate is off in both containers.
* **Gate ON:** no manual Odoo cleanup. The Hub owns the outcome. On anything unexpected: STOP,
  remove the gate key, recreate api and poller, and resolve from ledger + creation record through
  a separately approved plan. Never clear `new_ready` on a row whose ledger entry is `in_progress`.
* **`needs_reconciliation`:** compare the recorded intent with the Odoo template behind the
  supplier mapping; any fix (archive, correct, re-link) is a separately approved master-data change.

## 8. Rollback / disable

* Stop consuming: remove `ODOO_WORKBENCH_PRODUCT_LINE_CREATE_ENABLED`, recreate api + poller.
  Ready create rows stay inert.
* Hide the UI: `active=False` on the 6.3 view (PR C list returns unchanged); delete the 6.2 action.
* Schema: `alembic downgrade 202607170039` refuses while any `product_line_create` ledger row,
  creation row or `product_line_creation` identity claim exists (audit history of master-data
  writes); otherwise it restores the claim owner FK exactly. A code rollback needs no downgrade.
* Created products are approved master data and are never deleted automatically; archiving one
  needs a separate approval.

## 9. Known limits

* Simple single-variant products only. Variant/license catalogs (ManageEngine) stay manual.
* One line per request; two lines of one review in one tick → the second is *Güncel Değil*.
* Lines without a seller code cannot create a product (no supplier-specific key).
* Name-duplicate detection is exact (case-insensitive), not fuzzy (PR F).
* `x_studio_ana_tedarikci` (Main Supplier) is not set (policy §11 open question).
* Requester impersonation exposure as in PR C §3.1.
* The 30-minute in-flight grace is a fixed constant (Odoo Online's server-side request limit is
  not documented for this instance; it is assumed to be far below 30 minutes).

## 10. Odoo 19.3 assumptions (verified by code review; proven only by the 7.0 #6b check and 7.D)

| Assumption | Basis | Consequence if wrong |
| --- | --- | --- |
| A template without attributes gets exactly one `product.product` in the same transaction | standard `product.template.create` | writer read-back fails → outcome unknown → adopt/reconcile; never a second create |
| `seller_ids` `(0, 0, vals)` creates the `product.supplierinfo` in the same transaction, with `product_tmpl_id` set and `product_id` empty (template-level) | standard one2many create; `product.supplierinfo` belongs to the `product` module | matcher needs a single active variant for a template-level row (true for a new simple product) |
| `type ∈ {consu, service}`, `is_storable` boolean (stock module), `uom_id` required, no `uom_po_id` in 19 | Odoo 18/19 product model; production audit (65/65 templates use these) | payload refused by Odoo → certain no-write, nothing created |
| A JSON-2 call is one transaction; a 4xx/401/403 means nothing was committed | JSON-2 controller commits on success only | the writer still treats any failure *after* the create call as unknown |
| The client never retries a create | `OdooJson2Client._create_one` has no retry | — |
| Product `company_id` stays empty (shared); supplierinfo `company_id` = tick company | payload never sends template `company_id` | post-create verification accepts empty or the tick company only |
| Category is any existing `product.category` (no company); RESALE rules from 18E-2 | category lister + verification re-read | refused before any write |
