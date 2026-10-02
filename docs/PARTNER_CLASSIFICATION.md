# ICT partner classification and the ONE_OFF_VENDOR redesign

## Policy

`res.partner.x_studio_musteri_tipi` ("Müşteri Tipi", a manual Studio selection) is
ICT's explicit **business relationship classification** of a partner. It is
master-data only. It never determines purchase purpose, expense account,
accounting/tax/product treatment or the Vendor Bill workflow -- those stay
review/invoice-level Hub decisions (purchase purpose, accounting resolution,
decision). It is independent of Odoo's `customer_rank` / `supplier_rank`.

| Key (technical) | Label | Meaning |
|---|---|---|
| `customer` | Müşteri | customer |
| `prospect` | Aday | prospect |
| `vendor` | Tedarikçi | normal operational/commercial supplier |
| `expense_vendor` | Gider Tedarikçisi | **new** -- supplier that exists because ICT incurred an incidental/operating expense with that legal entity (restaurants, hotels, customer gifts, occasional office purchases, travel, vehicle charging/fuel, ...) |
| `partner` | İş Otrağı → **İş Ortağı** (label typo fix only) | business partner |
| `Karma` | Karma | mixed; key intentionally left as-is |

There is **no shared/generic one-off supplier**. Every legal supplier keeps its own
`res.partner` and VAT identity, which preserves payable reconciliation, supplier
and VAT identity, invoice history and accounting traceability. The classification
only separates operational suppliers from expense vendors in the UI.

## Hub behaviour

Configuration: `ODOO_PARTNER_CLASSIFICATION_FIELD` (production value
`x_studio_musteri_tipi`). Hub classification keys are fixed in
`app/application/partner_classification.py` (`vendor`, `expense_vendor`) and equal
the Odoo selection keys.

`OdooSupplierPartnerWriter` (the only `res.partner` create path in the Hub) fails
closed **before any partner read or write** when:

1. the field is not configured, or is not a custom `x_` field name;
2. Odoo metadata does not show exactly one `res.partner` field of that name with
   `ttype = selection`;
3. the selection does not offer the required key (e.g. `expense_vendor` not added yet).

Error: `supplier_partner_classification_unavailable` (HTTP 503). Nothing is created.

| Mode | New partner payload | Existing exact-VAT partner |
|---|---|---|
| `CREATE_PERMANENT_SUPPLIER` | `{name, vat, <field>: "vendor"}` | reused; classification **never changed**, outcome reported |
| `ONE_OFF_VENDOR` | `{name, vat, <field>: "expense_vendor"}` | reused only if Hub-owned via a prior ONE_OFF_VENDOR effect (else `SupplierResolutionOneOffVendorNotHubOwnedError`, unchanged); classification **never changed** |
| `MATCH_EXISTING` | n/a | no partner write at all |

The classification is always sent explicitly, so the production `ir.default`
(`customer`) can never apply to a Hub-created supplier. After create, the read-back
must show exactly the requested key; anything else (an `ir.default`, an automation)
fails closed with `supplier_partner_data_integrity_error` and is never reported as
success.

### Existing partners: deterministic, write-free

The Hub has **no capability to update** a partner's classification (no
`res.partner/write` exists anywhere in the Hub after this change). For an existing
partner the writer only evaluates the current value
(`evaluate_existing_classification`):

| Current value | Outcome | Effect |
|---|---|---|
| equals target | `already_classified` | none |
| empty | `unclassified_preserved` | preserved; API warning |
| anything else (`customer`, `vendor`, `partner`, `Karma`, `prospect`, unknown) | `different_classification_preserved` | preserved; API warning |

`POST /api/workbench/reviews/{id}/supplier-resolution` returns
`partner_classification_outcome` and `partner_classification`, plus a warning in
`warnings[]` for the two "preserved" outcomes. Operators correct the value in Odoo.

## ONE_OFF_VENDOR lifecycle: before vs after

| | Before (P0-PROD-08H..09F) | After |
|---|---|---|
| Partner | supplier-specific `{name, vat}` partner (got `customer` via `ir.default`) | supplier-specific partner, explicitly `expense_vendor` |
| After resolution | `workbench_review_one_off_vendor_retirements` row `pending_vendor_bill` | **no retirement row** |
| After successful Vendor Bill | post-execution trigger archived the partner (`active=False`) | **nothing**; partner stays active permanently |
| Later invoice, same VAT | partner archived → `SUPPLIER_NOT_FOUND` → ONE_OFF_VENDOR reused the archived partner (09C) and re-archived it | active partner → normal deterministic matcher `MATCHED`; no remediation needed |
| Archived exact-VAT match | reused if Hub-owned (09C predicate) | **fails closed** (`supplier_partner_inactive`): operator reactivates explicitly |
| Recovery endpoint | `POST .../one-off-vendor-retirement/recover` archived | **410 Gone**, reads/writes nothing |
| `ONE_OFF_VENDOR_ARCHIVE` authorization | issuable/consumable | refused at issuance and consumption |

Retired from runtime (code removed): `ArchiveOneOffVendorUseCase`,
`OneOffVendorRetirementTrigger` and its execution wiring,
`RecoverOneOffVendorRetirementWorkflow`, `OdooOneOffVendorRetirementWriter`,
`OdooJson2Client.archive_res_partner`, the archive port/command/DTOs and the 09C
`authorize_inactive_reuse` predicate.

Kept, deliberately:

- `workbench_review_one_off_vendor_retirements` table, its repository, the
  `OneOffVendorRetirement` DTO and status enum -- historical rows are audit evidence;
- `GET /api/workbench/reviews/{id}/one-off-vendor-retirement` (read-only);
- the `ONE_OFF_VENDOR_ARCHIVE` enum value (historical authorization rows + DB CHECK
  constraint unchanged);
- the P0-PROD-10D effective-decision substitution (a review's own accepted
  remediation-effect partner is used for execution evidence when the raw match is
  not `MATCHED`). It is generic (also serves MATCH_EXISTING ambiguity, 15N) and
  removing it would change the effective decision of historical reviews. New
  ONE_OFF_VENDOR partners are active and match on their own, so it no longer
  exists to serve archived reuse.

The Vendor Bill `partner_id` is always the partner from the deterministic match (or
the review's own remediation effect): the actual supplier-specific `res.partner`.

**No migration.** No schema or data changes; no review identity changes; no re-import.

## Historical runtime state

### Pelit (Workbench row 18, review `81616376…`)

Production holds, from 2026-10-02 08:52Z: supplier resolution 10 / remediation
effect 10 (`one_off_vendor`, partner 452), retirement 3 `pending_vendor_bill`,
reclassification 14 (v2→v3).

Plan (do **not** execute as part of the PR):

1. **Deploy this PR before any Vendor Bill execution for row 18.** On the old code
   a successful Vendor Bill fires the archive trigger. (With
   `SUPPLIER_REMEDIATION_WRITE_ENABLED=false` it fails the gate and reverts to
   `pending_vendor_bill`, but it must not be relied on.)
2. After deploy, retirement 3 stays `pending_vendor_bill` permanently as audit
   evidence. Nothing reads it for behaviour; nothing can advance it (no trigger,
   recovery is 410, archive authorizations are refused). No row is edited or deleted.
   API flags `one_off_vendor_awaiting_vendor_bill` on an already-applied replay
   reflect that historical row only.
3. Correct partner 452 to `expense_vendor` (runbook below). Partner 167 is unrelated
   and stays untouched.

### Archived Hub-owned partners from the old lifecycle (e.g. D-Market 448)

A new invoice from such a VAT now arrives `SUPPLIER_NOT_FOUND`, and ONE_OFF_VENDOR
fails closed with `supplier_partner_inactive`. The operator path is:

1. Read-only preflight: confirm the partner id from the Hub effect
   (`workbench_review_supplier_remediation_effects`, `mode='one_off_vendor'`) and in
   Odoo (`active=false`, exact VAT, no duplicate active VAT partner).
2. In Odoo, **unarchive** the partner and set Müşteri Tipi = `Gider Tedarikçisi`.
3. Resolve the review with `MATCH_EXISTING` (partner id from step 1), or reclassify.
   Later invoices then match deterministically.

Find candidates read-only:

```sql
SELECT e.resolved_partner_id, e.source_supplier_tax_number, r.status AS retirement_status
FROM workbench_review_supplier_remediation_effects e
LEFT JOIN workbench_review_one_off_vendor_retirements r
  ON r.review_id = e.review_id AND r.company_id = e.company_id AND r.review_version = e.review_version
WHERE e.mode = 'one_off_vendor';
```

## Operator runbook (after merge; each step needs its own approval)

Order matters: until the field is configured, every Hub supplier-partner create
fails closed (safe). Until `expense_vendor` exists in Odoo, ONE_OFF_VENDOR fails
closed while CREATE_PERMANENT_SUPPLIER already works (`vendor` exists).

### 1. Odoo Studio -- selection value and label (production)

Contacts → any company → Studio → field **Müşteri Tipi** → selection values:

- add value: technical key **`expense_vendor`**, label **`Gider Tedarikçisi`**
  (type the key explicitly; Studio otherwise derives it from the label, which is how
  `Karma` got a capitalised key);
- rename label `İş Otrağı` → `İş Ortağı`; **do not** change the key `partner`;
- do not touch `Karma`.

Read-only verification:

```python
# ir.model.fields.selection search_read, domain:
[["field_id.model", "=", "res.partner"], ["field_id.name", "=", "x_studio_musteri_tipi"]]
# expect values: customer, prospect, vendor, partner, Karma, expense_vendor
```

### 2. Odoo -- remove the `customer` default (separate decision)

Production has `ir.default` id 21: `x_studio_musteri_tipi = "customer"` for company 1.
The Hub does not depend on it (explicit value + read-back check), and the Hub never
modifies `ir.default`. Removing it stops manual Contacts creates from silently
becoming Müşteri.

- Preflight (read-only): `ir.default` search_read
  `[["field_id.model","=","res.partner"],["field_id.name","=","x_studio_musteri_tipi"]]`
  → expect exactly id 21, `json_value "\"customer\""`, company 1, user false.
- Action: Settings → Technical → User-defined Defaults → delete the record (or Studio
  field → default value → empty).
- Rollback: recreate the default (field Müşteri Tipi, value `customer`, company ICT).

### 3. Hub configuration and deploy

1. Add `ODOO_PARTNER_CLASSIFICATION_FIELD=x_studio_musteri_tipi` to the production env
   file (backup the env file first).
2. Standard deploy of the merge SHA (`up -d --build api`), then recreate the poller
   (`up -d --no-deps uyumsoft-inbound-poller`) so both run the same image.
3. Health check; no migration to apply (`alembic heads` unchanged).

### 4. Correct partners 451 and 452 (operator, in Odoo -- never via the Hub)

Target: **451 → `vendor`**, **452 → `expense_vendor`**, **167 → no change**.

Read-only preflight (must all hold, else stop):

| Check | Expected |
|---|---|
| `res.partner` 451 | name `APPLE Teknoloji ve Satış Limited Şirketi`, vat `0710414224`, active, `x_studio_musteri_tipi = customer` |
| `res.partner` 452 | name `PELİT PASTACILIK VE GIDA SANAYİ ANONİM  ŞİRKETİ`, vat `7280014037`, active, `x_studio_musteri_tipi = customer` |
| `res.partner` 167 | unchanged (vat empty, `x_studio_musteri_tipi` empty) -- not touched |
| Hub `workbench_review_supplier_remediation_effects` | id 9 `create_permanent_supplier` → `resolved_partner_id` 451; id 10 `one_off_vendor` → 452 |
| Selection | contains `vendor` and `expense_vendor` |
| No other partner shares either VAT | exactly one record per VAT (active in [true,false]) |

Change (Contacts UI, one field per partner, nothing else):

- 451 → Müşteri Tipi = **Tedarikçi** (`vendor`)
- 452 → Müşteri Tipi = **Gider Tedarikçisi** (`expense_vendor`)

Post-check (read-only): both values read back exactly; `active`, `name`, `vat`,
`supplier_rank`, `customer_rank` unchanged; partner 167 `write_date` unchanged; Hub
reconcile `NO_CHANGE` for all Workbench rows (the Hub stores no classification).

Rollback: set the field back to `customer` on the affected partner.

### 5. Contacts UI (Studio / Settings → Technical; no Hub involvement)

Prefer `x_studio_musteri_tipi` for ICT business classification. The existing
rank-based Customers (action 430) / Vendors (431) stay as the accounting views and
are not modified.

Classification is maintained on **top-level** partners (companies and individual
top-level persons); child contacts (`parent_id` set, 82 today) normally stay empty,
so every list filters `parent_id = False`.

Window actions (`ir.actions.act_window`, `res_model = res.partner`,
`view_mode = list,kanban,form`), each with a menu under the Contacts app root:

| Menu | domain | context |
|---|---|---|
| Müşteriler | `[('x_studio_musteri_tipi','in',['customer','Karma']),('parent_id','=',False)]` | `{'default_x_studio_musteri_tipi':'customer','default_is_company':True}` |
| Tedarikçiler | `[('x_studio_musteri_tipi','in',['vendor','Karma']),('parent_id','=',False)]` | `{'default_x_studio_musteri_tipi':'vendor','default_is_company':True}` |
| Gider Tedarikçileri | `[('x_studio_musteri_tipi','=','expense_vendor'),('parent_id','=',False)]` | `{'default_x_studio_musteri_tipi':'expense_vendor','default_is_company':True}` |
| İş Ortakları | `[('x_studio_musteri_tipi','=','partner'),('parent_id','=',False)]` | `{'default_x_studio_musteri_tipi':'partner','default_is_company':True}` |
| Adaylar | `[('x_studio_musteri_tipi','=','prospect'),('parent_id','=',False)]` | `{'default_x_studio_musteri_tipi':'prospect','default_is_company':True}` |

Business decision noted in the table: `Karma` (mixed) appears in both Müşteriler and
Tedarikçiler. Drop it from either domain if that is not wanted.

Search view (Studio on `res.partner` search): add filters Müşteri / Tedarikçi / Gider
Tedarikçisi / İş Ortağı / Aday / Sınıflandırılmamış
(`[('x_studio_musteri_tipi','=',False),('parent_id','=',False)]`) and a group-by
"Müşteri Tipi". Today 265 of 286 partners are unclassified; a backfill is a separate
data-quality task.

## Rollback of this change

Revert the merge commit and redeploy (`api` + poller). No migration to reverse; no Hub
data was changed. Partners created while the change was live keep their explicit
classification (harmless). After rollback the Hub again creates `{name, vat}` only
(Odoo's `ir.default`, if still present, applies) and the archive lifecycle code
returns -- deploy order for any pending one-off Vendor Bill must then be re-checked.
