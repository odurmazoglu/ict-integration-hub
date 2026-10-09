# Odoo Workbench Product Line Requests (PR C)

An operator maps one PRODUCT_NOT_FOUND invoice line to an **existing** Odoo product
directly on its Workbench child row (`x_ipp_wb_product_line`, PR B). The request runs
through the existing ADR-0013 Hub-pull pipeline and the existing #208
`MapExistingProductUseCase`. Nothing new decides business state.

* Parent "Ürün Eşleştir" (`x_studio_ipp_req_line` / `x_studio_ipp_req_product`) is
  **unchanged** and stays operational until PR E retires it.
* No new product creation, no candidate suggestions, no VAT cleanup, no Odoo → Hub call,
  no Hub credentials in Odoo, no custom addon.

Code:

* `app/erp/odoo/workbench_product_line_request_reader.py`: child request reader,
  identity verification and acknowledger.
* `app/application/workbench/operator_request_ingestion.py`: action
  `PRODUCT_LINE_MAPPING` (`product_line_mapping`).
* `app/composition/operator_requests.py`: `build_product_line_request_workflow`,
  `SequentialOperatorRequestWorkflows`, tick wiring.
* `alembic/versions/202607170039_operator_request_ledger_product_line_mapping.py`:
  widens the ledger action CHECK constraint.

## 1. Flow

```text
Operator (group 88), Workbench form → tab "Ürün Satırları"
  selects "Eşleştirilecek Ürün" on a product_not_found line → presses the row's "Eşleştir"
  └─ Studio server action (copies values only): req_version := ipp_review_version,
     req_requested_by := env.user, req_requested_at := now, result/message := False, ready := True

Hub poller, operator request tick (every 60 s, advisory lock)
  1. child channel: OdooProductLineRequestReader
       ready child rows of the company → identity verification (section 3) → OperatorRequest(PRODUCT_LINE_MAPPING)
  2. OperatorRequestIngestionWorkflow (unchanged)
       ledger key → actor directory + permission → narrow MAP_EXISTING_PRODUCT authorization
       → ProductMappingRequestHandler → MapExistingProductUseCase (the #208 use case, unchanged)
       → projection refresh (parent + child rows) → acknowledgement on the child row
  3. parent channel: the existing parent request workflow (unchanged)
```

The two channels run **sequentially** in one tick, child rows first. The order is fixed
only for deterministic results. Two requests for the same review are never concurrent, and
the optimistic review-version check decides between them, whichever channel runs first.

**Channel isolation.** An unexpected exception that escapes one channel never blocks the
other channel. This includes a ledger failure such as the #210 failure mode (gate on
without migration 0039), an unexpected acknowledgement error, and a reader bug.
`SequentialOperatorRequestWorkflows` handles it as follows:

* logs `workbench.operator_request.channel_failed` with the traceback and channel name;
* rolls back the shared business and ledger sessions;
* runs the remaining channel;
* then raises `OperatorRequestChannelError`, chained to the first failure, so the
  scheduler still logs `periodic_task_crashed`. Nothing is swallowed.

If the rollback itself fails, no further channel runs, because its session state cannot be
proven clean. Per-request outcomes are unchanged: a handler exception is still that
request's *Hata*, and does not count as a channel crash. A request whose channel crashed
was neither finished nor acknowledged, so the next tick resumes it from its ledger state.

## 2. What the use case does (unchanged #208 semantics)

1. The review must be pending at exactly the snapshotted version, and the line must carry
   `PRODUCT_NOT_FOUND`. Otherwise the result is *Güncel Değil* or *Reddedildi*.
2. The seller product code comes from the **immutable source invoice line**. A missing
   code is refused.
3. The supplier is the review's **effective supplier** (`EffectiveSupplierResolver`).
4. The product must exist, be active, be company-compatible and have a template
   (read-only check).
5. It fails closed on: an existing mapping of this supplier and code to a different
   product, several such mappings, or a CREATE_NEW_PRODUCT claim. An identical existing
   mapping is reused (no write).
6. It writes one `product.supplierinfo` (supplier, seller code → product) through the
   guarded writer and consumes the narrow single-use `MAP_EXISTING_PRODUCT` authorization.
7. It reclassifies with `MASTER_DATA_CHANGED`.

After that, the projection refresh shows the child row as `matched` with the
authoritative product. The acknowledgement writes *Tamamlandı* and empties the row's
*Eşleştirilecek Ürün*.

## 3. Identity and trust boundary

Odoo ACLs are per model. To edit the request fields, the operator group needs **write**
access to `x_ipp_wb_product_line`, so a raw JSON-2 client in that group could also write
the Hub-owned projection fields. The Hub therefore treats every child field as untrusted
and accepts a row only when all of the following hold. Otherwise the row is closed as
*Reddedildi* with an explicit message, and no use case runs.

| Check | Fails as |
| --- | --- |
| `x_studio_ipp_req_requested_at` set (the button was used) | "İstek zamanı eksik …" |
| `x_studio_ipp_is_current` is `True` | "Bu ürün satırı artık güncel değil …" |
| No second ready row in the batch with the same line key | "Aynı fatura satırı için birden fazla bekleyen istek …" |
| `product_line_key(company, review_id, line_number)` of the row's own Hub fields equals `x_studio_ipp_line_key`, and the row company is the tick company | "Ürün satırının kimliği Hub kaydıyla tutarsız …" |
| The parent row exists, and its `x_studio_review_id` and company equal the child's | "Ürün satırı bağlı olduğu inceleme kaydıyla tutarsız …" |
| Positive snapshotted version, requester and product | the existing "… geçersiz / eksik / seçilmelidir" texts |

What the Hub never reads from the child row: seller code, description, supplier,
match state, projected product and review version. The use case re-derives each of these
from committed Hub evidence. A tampered identity can at most point the request at another
line of a review the same authorized operator could already address through the parent
request. That line must still independently pass every use-case check.

**Requester.** See section 3.1. The Hub authorizes whoever `requested_by` *names*. It
cannot prove that this user actually pressed the button.

### 3.1 `requested_by` impersonation exposure

**The exposure.** The Studio button writes `x_studio_ipp_req_requested_by = env.user`. It
is an ordinary field, though: any Odoo user with **write** access to the row can set it
through raw JSON-2 or RPC to any user id, together with `req_ready = True`. The Hub then:

* looks up the *named* user in `ODOO_OPERATOR_REQUEST_ACTORS`;
* checks that user's permissions;
* issues the narrow write authorization **as that named actor**.

The allowlist therefore limits **whom** an attacker can impersonate (only allowlisted
actors are worth naming). It does **not** prevent impersonation. Any Odoo user who can
write the row can act with the full Hub permissions of any allowlisted actor, and the
ledger and audit note will attribute the action to that actor.

**Who can write today** (read-only production check, 2026-10-09):

| Model | Write ACL | Group | Effective internal users |
| --- | --- | --- | --- |
| `x_ipp_import_workbench` (parent) | 1368 | 88 IPP Workbench Operator | uid 2 |
| `x_ipp_import_workbench` (parent) | 1294 | 4 Role / Administrator | uid 2 |
| `x_ipp_wb_product_line` (child) | 1371 | 4 Role / Administrator | uid 2 |

Group 88 has exactly one member, uid 2, directly and effectively, and no group implies it.
uid 2 is also the only allowlisted actor and the user whose API key the Hub uses. **Today
nobody but the allowlisted actor can forge a request.** The exposure becomes real as soon
as any other user gains write access to either Workbench model, through group 88, an
administrator role, or a new ACL. The section 5.4 ACL adds group 88 on the child model and
so inherits this rule.

**Same exposure on both channels.** The parent row (ADR-0013) has the identical weakness,
and offers the same product mapping operation. PR C neither introduces nor removes it.

**Operating rules until identity binding exists:**

1. Do not add members to group 88. Do not grant write access on either Workbench model to
   any user who is not an allowlisted actor with the same Hub permissions.
2. Before any membership or ACL change, re-run the read-only check above and treat the
   change as a security decision that needs its own approval.
3. Keep `ODOO_OPERATOR_REQUEST_ACTORS` minimal. Every allowlisted actor's permissions are
   effectively available to every Odoo user who can write the rows.

**Rejected: `write_uid` cross-check.** `write_uid` is set by the ORM, but the Hub's own
projection refresh and acknowledgement rewrite the row as the Hub user. After a crash
between the Hub commit and the Odoo acknowledgement, the check would turn a completed
request into a false *Reddedildi*. A plain `write_uid` comparison would also be ambiguous
while the Hub user and the operator are the same uid.

**Recommended future improvement (identity binding, needs its own design and approval).**
Record the submission in a separate, **append-only** Studio model, for example
`x_ipp_wb_request_event` (target model, target row id, `requested_at`, snapshot version).

* The button *creates* one event row. Operators get **create and read only**: no write, no
  unlink.
* `create_uid` and `create_date` are set by the ORM and cannot be supplied or changed by a
  client.
* The Hub accepts a request only when an event for that row and `requested_at` exists, and
  then uses the event's `create_uid` as the requester, ignoring `requested_by`.
* The Hub's own writes never touch event rows, so crash, re-acknowledgement and refresh
  keep working.

This applies to both channels and closes the gap without custom Python. An alternative is
mail tracking on the ready field, which needs the chatter on the Studio model.

**Hub-owned fields.** Any operator write to a projection field is overwritten by the next
projection sync. The acknowledgement writes only the request result fields. A contract
test forbids mapping a request field onto a projection field.

## 4. Idempotency, retry and conflicts

| Situation | Outcome |
| --- | --- |
| Double click before the tick | Only the latest snapshot is read; one request. |
| Odoo acknowledgement fails after the Hub commit | The ledger is terminal, so the next tick **only re-acknowledges** (no second write, no second reclassification). |
| Transient failure after the supplierinfo write | `RETRY_LATER`; the resume reuses the ledger's authorization and the use case reuses the identical mapping. Never a second supplierinfo. |
| Resubmit on a now-matched line with the old snapshot | *Güncel Değil* (version moved). |
| Resubmit with the refreshed version | *Reddedildi*: "… ürün eşleştirmesi gerekmiyor". |
| Parent and child requests for the same line at the same version in one tick | The child completes; the parent is *Güncel Değil*. Exactly one supplierinfo. |
| Two child lines of one review in one tick | The first completes and bumps the version; the second is *Güncel Değil* and must be resubmitted (known UX cost of the optimistic check). |
| Supplier code already mapped to another product | *Reddedildi*, no write. |
| Odoo user not in the actor directory | *Yetkisiz*, nothing runs. |
| Actor without `workbench_execute` | *Reddedildi* before any authorization or write. |
| One channel raises unexpectedly (for example a ledger failure) | The other channel is still processed; the shared sessions are rolled back; the tick is reported as failed. The crashed request stays ready and resumes from its ledger state on a later tick. |

The ledger records these requests as `action = 'product_line_mapping'`, with
`odoo_record_id` = the **child** row id. The request key includes the action, so it never
collides with a parent request on the same ids.

## 5. Studio contract (exact)

### 5.1 New fields on `x_ipp_wb_product_line` (8)

All fields: `store=True`, `copied=False`, no compute, no default, not required.

| # | Technical name | Label | `ttype` | Relation / extra | Owner |
| --- | --- | --- | --- | --- | --- |
| 1 | `x_studio_ipp_req_product` | Eşleştirilecek Ürün | many2one | `product.product`, `on_delete=set null` | operator |
| 2 | `x_studio_ipp_req_ready` | İstek Hazır | boolean | | button / Hub |
| 3 | `x_studio_ipp_req_version` | İnceleme Sürümü (istek) | integer | | button |
| 4 | `x_studio_ipp_req_requested_by` | İsteyen | many2one | `res.users`, `on_delete=set null` | button |
| 5 | `x_studio_ipp_req_requested_at` | İstek Zamanı | datetime | | button |
| 6 | `x_studio_ipp_req_result` | Sonuç | selection | `Tamamlandı`, `Güncel Değil`, `Reddedildi`, `Yetkisiz`, `Hata` (value = name = label) | Hub |
| 7 | `x_studio_ipp_req_message` | Sonuç Mesajı | text | | Hub |
| 8 | `x_studio_ipp_req_processed_at` | İşlenme Zamanı | datetime | | Hub |

The names deliberately equal the parent request field names. They live on another model.
Each can be overridden by `ODOO_WORKBENCH_PRODUCT_LINE_REQ_<NAME>_FIELD` (for example
`…_REQ_PRODUCT_FIELD`), for non-production setups only. A set-but-blank override is a
contract error.

### 5.2 Server action "Eşleştir"

`ir.actions.server`: `model_id` = `x_ipp_wb_product_line`, `state=code`,
`groups_id` = [group 88]. Like the parent "İşleme Gönder" (1026), it only copies values:

```python
for record in records:
    record.write(
        {
            "x_studio_ipp_req_version": record.x_studio_ipp_review_version,
            "x_studio_ipp_req_requested_by": env.user.id,
            "x_studio_ipp_req_requested_at": datetime.datetime.now(),
            "x_studio_ipp_req_result": False,
            "x_studio_ipp_req_message": False,
            "x_studio_ipp_req_ready": True,
        }
    )
```

### 5.3 View: editable product line tab

A new `ir.ui.view`: `type=form`, `model=x_ipp_import_workbench`, `inherit_id` = parent
form 3984, `priority` 223 (after 4210), `mode=extension`. It replaces only the PR B list.
View 4210 itself stays unchanged, so deactivating this view restores the read-only tab
exactly.

```xml
<data>
  <xpath expr="//field[@name='x_studio_ipp_product_line_ids']" position="replace">
    <field name="x_studio_ipp_product_line_ids" nolabel="1">
      <list editable="bottom" create="0" delete="0" default_order="x_studio_ipp_line_sequence asc">
        <field name="x_studio_ipp_line_sequence" column_invisible="1"/>
        <field name="x_studio_ipp_req_ready" column_invisible="1"/>
        <field name="x_studio_ipp_line_number" string="Satır" readonly="1"/>
        <field name="x_studio_ipp_seller_code" string="Satıcı Ürün Kodu" readonly="1"/>
        <field name="x_studio_ipp_description" string="Açıklama" readonly="1"/>
        <field name="x_studio_ipp_quantity" string="Miktar" readonly="1"/>
        <field name="x_studio_ipp_uom_code" string="Birim" readonly="1"/>
        <field name="x_studio_ipp_match_state" string="Durum" readonly="1"
               decoration-success="x_studio_ipp_match_state == 'matched'"
               decoration-warning="x_studio_ipp_match_state in ('product_not_found', 'product_ambiguous')"
               decoration-danger="x_studio_ipp_match_state in ('identifier_missing', 'supplier_unresolved')"
               decoration-muted="x_studio_ipp_match_state in ('resolved_without_product', 'no_evidence')"/>
        <field name="x_studio_ipp_product_id" string="Eşleşen Ürün" readonly="1"/>
        <field name="x_studio_ipp_req_product" string="Eşleştirilecek Ürün"
               domain="[('purchase_ok', '=', True)]"
               readonly="x_studio_ipp_match_state != 'product_not_found' or x_studio_ipp_req_ready"/>
        <button name="ACTION_ID_ESLESTIR" type="action" string="Eşleştir" icon="fa-link"
                invisible="x_studio_ipp_match_state != 'product_not_found' or not x_studio_ipp_req_product or x_studio_ipp_req_ready"/>
        <field name="x_studio_ipp_req_result" string="Sonuç" readonly="1" optional="show"/>
        <field name="x_studio_ipp_req_message" string="Sonuç Mesajı" readonly="1" optional="show"/>
      </list>
    </field>
  </xpath>
</data>
```

The view attributes are convenience only. The Hub re-validates everything (sections 2 and 3).

**Unproven on Odoo 19.3.** A row button in an editable x2many list is *expected* to save
the parent form (including the inline product selection) before running the action on
that child row. This is **not assumed**. It is an acceptance gate (section 7.A), proven
with the Hub gate **off**, so a failure leaves only an inert request. If it fails, the
rollout STOPs. Any alternative UI (for example the PR B child form, view 4209, with an
editable request field and a header button) is a new design that needs its own approval
and its own acceptance.

### 5.4 ACL

A new `ir.model.access` row. ACL 1369 stays read-only and unchanged, and ACL 1371 is not
touched:

| Name | Group | read | write | create | unlink |
| --- | --- | --- | --- | --- | --- |
| `ipp_wb_product_line operator request write` | IPP Workbench Operator (88) | 1 | 1 | 0 | 0 |

Nobody gets create or unlink. On 2026-10-09 the only member of group 88 is uid 2, which
already has rwc through group 4 (ACL 1371), so this row adds no effective writer today.
It does **not** make it safe to add operators. Every user with write access can
impersonate any allowlisted actor (section 3.1). Membership stays uid 2 only until identity
binding exists.

### 5.5 Hub configuration

| Env | Value |
| --- | --- |
| `ODOO_WORKBENCH_PRODUCT_LINE_REQUESTS_ENABLED` | absent / `false` (default): child request fields are never read. `true`: the child channel runs in the request tick. |
| prerequisites (startup check) | `ODOO_WORKBENCH_OPERATOR_REQUESTS_ENABLED=true`, `ODOO_WORKBENCH_PRODUCT_LINE_PROJECTION_ENABLED=true` |
| `ODOO_WORKBENCH_PRODUCT_LINE_REQ_*_FIELD` | **not set** in production (defaults = section 5.1) |

The actor directory is shared with the parent. The mapping actor needs
`workbench_review_decide` and `workbench_execute`. Production uid 2 already has both since
the #208 enablement.

## 6. Rollout (NOT executed in PR C; each step needs its own approval)

1. **Merge.**
2. **Deploy with the gate absent.**
   * Back up first.
   * Build, then run `alembic upgrade head` (0038 → 0039) through the established one-off
     container. The api does not auto-migrate.
   * Recreate api and poller.
   * Expect: no behavior change. The parent tick is identical, and a reconcile dry-run
     reports parents and children NO_CHANGE.
3. **Studio provisioning** (user-2 JSON-2 key), in this order: the 8 fields (5.1), the
   server action (5.2), the view (5.3, with the action id substituted), and the ACL (5.4).
   * Record every id in `provision.json`.
   * Read-only verification: field names and types, the 5 result selection labels, the
     action `groups_id`, the combined arch contains the button exactly once, and the
     ACL row.
4. **Row-button acceptance with the gate OFF** (section 7.A), separately approved.
   * The operator submits the acceptance line once.
   * The Hub does not read child requests yet, so the submission stays an inert pending
     request.
   * STOP on any failure (section 7.C).
5. **Enable the gate, which processes the accepted request** (section 7.B), separately
   approved, and only after 7.A passed.
   * Back up `.env.production`.
   * Add `ODOO_WORKBENCH_PRODUCT_LINE_REQUESTS_ENABLED=true`.
   * Recreate api, then poller (same image).
   * The first tick processes exactly the request verified in 7.A.

## 7. Controlled acceptance runbook: dfccd66e line 2 → product 394

Each part needs its own explicit approval. Never combine 7.A and 7.B in one approval.

### 7.0 Pre-checks (read-only)

Every expectation must hold; otherwise STOP.

| # | Check | Expect |
| --- | --- | --- |
| 1 | Hub review `review:dfccd66e-…` | `pending_review`; record version `V` (3 after #208's acceptance); reasons contain `PRODUCT_NOT_FOUND` for line `2` and not for line `1` |
| 2 | Child row 21 | key `ipp-pl:v1:1:review:dfccd66e-…:2`, parent 24, `is_current`, `product_not_found`, seller `100021`, review version `V`, **all request fields empty** |
| 3 | `product.supplierinfo` partner 24, `product_code = 100021` | 0 rows |
| 4 | `product.product` 394 | active, template 163, company empty or 1 |
| 5 | Product identity claims for (1, 24, `100021`) | none |
| 6 | Ledger `workbench_operator_requests` | no `in_progress` row for this review; no `product_line_mapping` row at all |
| 7 | Parent row 24 request fields | not ready (no competing parent request) |
| 8 | `ODOO_WORKBENCH_PRODUCT_LINE_REQUESTS_ENABLED` in api **and** poller | absent / `false` |
| 9 | Ready child rows (`x_studio_ipp_req_ready = True`) | 0 |
| 10 | Group 88 members, and internal users with write on either Workbench model (section 3.1) | uid 2 only |

### 7.A Row-button acceptance (gate OFF)

1. Back up the DB. Snapshot (read-only): child 21 and child 20 full rows; parent row 24;
   counts and max ids of `x_ipp_wb_product_line`, `x_ipp_import_workbench` and
   `product.supplierinfo`; ledger count.
2. Operator uid 2 opens Workbench row 24 → *Ürün Satırları*. On line 2, set *Eşleştirilecek
   Ürün* = "Microsoft 365 Business Standard" (394), then press the row's **Eşleştir**.
   Note any dialog verbatim.
3. Verify in Odoo (read-only). **All** of the following must hold:

   | Field (child 21) | Expect |
   | --- | --- |
   | `x_studio_ipp_req_ready` | `True` |
   | `x_studio_ipp_req_product` | 394 |
   | `x_studio_ipp_req_version` | `V` |
   | `x_studio_ipp_req_requested_by` | 2 |
   | `x_studio_ipp_req_requested_at` | set, after the snapshot time; **record the exact value `T`** |
   | `x_studio_ipp_req_result` / `_message` / `_processed_at` | empty |
   | projection fields | identical to the snapshot |

   Also required:
   * Child 20 is unchanged.
   * Parent row 24 is unchanged except `write_date`. Saving the form writes the parent.
   * No other row in either model changed, and the supplierinfo and ledger counts are
     unchanged.
4. **PASS** only if every check holds and no dialog appeared. The request is now pending
   and inert: nothing reads it while the gate is off.

**STOP conditions for 7.A.** On any of them: stop, change nothing else, keep the gate
OFF, report, and follow section 7.C.

| Observation | Meaning |
| --- | --- |
| *Eşleştir* not visible on line 2 although the product is selected | View, visibility or provisioning defect |
| Any error, access or validation dialog on save or click | Save or action failed; the row may be partially written |
| A "discard changes" prompt, or the product selection is lost after the click | Form save did not happen before the action |
| `req_ready` not `True`, or `req_requested_at` empty | The action did not run |
| `req_product` ≠ 394, `req_version` ≠ `V` or `req_requested_by` ≠ 2 | Wrong or partial request |
| Any change outside the expected fields above | Unexpected write |

### 7.B Enable the gate and process (gate ON)

Only after 7.A PASSED and a new approval.

1. **Pre-enable check (read-only).** STOP if any item fails:
   * The **only** ready child row is 21, with exactly the values verified in 7.A and the
     same `T`.
   * No parent row is ready.
   * The Hub review is still at `V`. Otherwise the request would be *Güncel Değil*: STOP
     and decide.
   * There are still no `product_line_mapping` ledger rows.
2. Enable (section 6, step 5). Within one tick (≤ 60 s) the poller logs
   `workbench.operator_request.processed` with `action=product_line_mapping` and
   `outcome=completed`. There must be no `channel_failed` and no `periodic_task_crashed`.
3. If the outcome is anything other than `completed`, STOP. The Hub has already written
   its own result to the row. Follow section 7.C (gate ON).

**Expected end state.**

* Ledger: one new row, `action=product_line_mapping`, `odoo_record_id=21`, `completed`,
  with an authorization id.
* Authorization `MAP_EXISTING_PRODUCT` at version `V`, consumed once.
* New `product.supplierinfo`: partner 24, `product_code` `100021`, template 163,
  variant 394, company 1, `product_name` = invoice line description.
* Reclassification `MASTER_DATA_CHANGED`, `V → V+1`. No `PRODUCT_NOT_FOUND` remains on
  either line.
* Child 21: `matched`, product 394, `matched_by = supplier_product_code`, supplier 24,
  review version `V+1`. Request: `Tamamlandı`, ready False, product empty.
  Child 20 is unchanged apart from its review version.
* Row 24: guidance moves past "Ürün Eşleştirmesi Yapılmalı"; *Tamamlananlar* lists both
  lines.
* Nothing else changes: other reviews, other supplierinfo, partners, products and moves.
  Compare the fingerprints.

**Follow-ups.**

* The next 2 ticks are idempotent (no new ledger row, no write).
* A reconcile dry-run reports NO_CHANGE for parents and children.

**Rollback of the mapping.** Only with a separate approval: archive the new supplierinfo
in Odoo and reclassify. It is master data the operator explicitly approved, so it is not
deleted automatically.

### 7.C STOP and recovery of pending request fields

**Principles.**

* No cleanup is done ad hoc, by script or "just in the UI".
* Nobody deletes child or parent rows, edits projection fields, inserts or deletes ledger
  rows, or reuses a snapshot.
* Leaving a pending request untouched **with the gate OFF** is always safe. The Hub never
  reads child request fields while the gate is off.

**R1: default after any 7.A STOP (gate OFF).**

* Leave every field exactly as found and keep the gate OFF.
* Take a read-only snapshot of the affected rows (ids, request fields, `write_date`) and
  report it.
* The gate must not be enabled while any ready child row exists that 7.A did not verify.
  The 7.B pre-enable check enforces this.

**R2: approved neutralization (gate OFF, explicit approval naming the row id and `T`).**

* Exactly one JSON-2 call per approved row, made as uid 2:
  1. Re-read the row.
  2. Proceed only if `x_studio_ipp_req_requested_at` still equals the approved `T` and
     the gate is still off in **both** containers.
  3. Write exactly `{"x_studio_ipp_req_ready": false, "x_studio_ipp_req_result":
     "Reddedildi", "x_studio_ipp_req_message": "Kabul testi durduruldu; istek
     işlenmedi.", "x_studio_ipp_req_processed_at": <now UTC>}`.
* This is the same guarded shape as the Hub's own rejection acknowledgement. It touches no
  projection field and leaves `req_product` as evidence.
* Afterwards: re-read and verify, and confirm that no ledger row exists for the row. None
  can exist while the gate was off.
* A new attempt needs a fresh 7.0 and 7.A.

**R3: failure while the gate is ON (7.B).**

* **No manual Odoo cleanup.** The Hub owns the outcome. It writes *Reddedildi*,
  *Güncel Değil* or *Hata* itself and finishes its ledger row.
* If anything unexpected was written, or the outcome is unclear (for example a
  `RETRY_LATER` request staying ready, or a `channel_failed` log):
  1. STOP.
  2. Disable the gate: remove the env key and recreate api and poller. Pending child rows
     become inert again.
  3. Resolve from the ledger and Hub state through a separately approved plan.
* Never clear `req_ready` on a row whose ledger entry is `in_progress`. The next tick with
  the gate on would then not resume it, and a recorded authorization would be orphaned.

## 8. Rollback / disable

* **Stop consuming child requests.** Remove `ODOO_WORKBENCH_PRODUCT_LINE_REQUESTS_ENABLED`
  and recreate api and poller. Ready child rows simply stay ready and are never read.
* **Hide the UI.** Set `active=False` on the 5.3 view. The PR B read-only tab returns
  unchanged.
* **Revoke operator writes.** Delete or deactivate the 5.4 ACL row.
* **Schema.** `alembic downgrade 202607170038` refuses while any `product_line_mapping`
  ledger row exists. Those rows are audit history and are never deleted automatically.
  The code rollback (redeploy the previous image) does not need a downgrade: the wider
  CHECK constraint is harmless to older code.
* The Studio fields may stay. Deleting them needs a separate explicit approval.

## 9. Known limits

* One line per request. Several lines of one review need sequential submissions; a second
  line submitted in the same tick is *Güncel Değil* and must be resubmitted.
* Requester impersonation: any Odoo user with write access to a Workbench row can submit
  as any allowlisted actor (section 3.1). The allowlist does not prevent this. Today only
  uid 2 can write the rows; identity binding is a recommended future change.
* Only `product_not_found` lines with a seller code can be mapped. Lines without a code,
  ambiguous lines and new products stay technical support (PR D/F).
* The parent "Ürün Eşleştir" remains until PR E. Both channels share one use case and one
  authorization type; the version check serializes them.
