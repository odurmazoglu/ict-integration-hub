# Odoo Workbench Operator UI (ADR-0013)

This document is the implementation and provisioning plan for operating Import Workbench
reviews entirely from Odoo Online, while the Hub stays the only workflow authority. The
architecture decision is [ADR-0013](adr/ADR-0013-odoo-online-workbench-operator-requests.md),
which extends [ADR-0011](adr/ADR-0011-odoo-online-import-workbench-projection.md).

Nothing in this document has been applied to production. Every Studio change, env change
and enablement below is a separate, explicitly approved step (see [Rollout](#rollout)).

## How it works

```text
Operator fills the "Yapılması Gerekenler" inputs on the Workbench row
  -> presses "İşleme Gönder" (Studio server action: copies values only)
       expected version := projected review version
       requested by     := current user
       requested at     := now
       ready            := true, previous result cleared
  -> Odoo shows "İşlem gönderildi — Hub sırada" while ready = true
  -> Hub poller tick (default every 60 s) reads ready rows (JSON-2, read-only)
  -> Hub maps the request to one existing use case and runs it unchanged
  -> Hub refreshes the projection (status, guidance, next action)
  -> Hub writes the result + message and clears ready (same request only)
  -> Odoo shows the result and the next required action
```

Code map:

| Concern | Module |
| --- | --- |
| Request DTO, request key, actor directory, ingestion workflow | `app/application/workbench/operator_request_ingestion.py` |
| One handler per action -> existing use case | `app/application/workbench/operator_request_handlers.py` |
| Guidance (next action, to-do, completed summary) | `app/application/workbench/operator_guidance.py` |
| Odoo request reader + result acknowledger | `app/erp/odoo/workbench_operator_request_reader.py` |
| Guidance fields in the projection publisher | `app/erp/odoo/workbench_projection_publisher.py` |
| Request ledger (table `workbench_operator_requests`) | `app/models/workbench_operator_request.py`, `app/persistence/workbench_operator_request_ledger.py`, migration `202607170036` |
| Composition + single-flight tick | `app/composition/operator_requests.py` |
| Scheduler (poller process) | `app/workers/uyumsoft_inbound_poller.py` (`PeriodicTaskScheduler`) |

## Supported operator actions

Each action maps to exactly one existing use case and requires the same permission as its
REST endpoint. No new domain choice exists.

| Action (Studio label) | Existing use case / endpoint | Inputs | Permission |
| --- | --- | --- | --- |
| Tedarikçi Çözümü | `ResolveWorkbenchSupplierUseCase` / `POST …/supplier-resolution` | supplier mode, partner (MATCH_EXISTING) | `workbench_review_decide`; writing modes also `workbench_execute` (narrow authorization) |
| Satın Alma Amacı | `SubmitPurchasePurposeUseCase` / `POST …/purchase-purpose` | purpose | `workbench_review_decide` |
| Muhasebe İşlemi | `SubmitReviewAccountingResolutionUseCase` / `POST …/accounting-resolution` | treatment; expense account + category, or asset account + depreciation model | `workbench_review_decide` |
| Karar | `SubmitReviewDecisionUseCase` via the existing decision candidate reader / `POST …/decision` | existing decision fields + allocation child rows | `workbench_review_decide` |
| Fatura Oluştur | `CreateWriteAuthorizationUseCase` (`EXECUTE_VENDOR_BILL`) + `WorkbenchAcceptedDecisionExecutionDispatcher` (EXECUTE) / `POST …/write-authorizations` + `POST …/execute` | none (the accepted decision is the input) | `workbench_execute` |

Supplier modes: `Mevcut Tedarikçiyi Seç` (match_existing), `Kalıcı Tedarikçi Oluştur`
(create_permanent_supplier), `Tek Seferlik Tedarikçi` (one_off_vendor). The code also
accepts `Tek Seferlik Niyet (Ertelenmiş)` (use_one_off_supplier, intent only, never
executed); keep it out of the operator selection unless the business asks for it.

Purposes: `Şirket İçi Kullanım`, `Yeniden Satış`, `Müşteri Projesi`, `Diğer İşletme Gideri`.
Treatments: `Gider Hesabı`, `Sabit Kıymet / Demirbaş`.

Re-submitting *Fatura Oluştur* for a decision whose Vendor Bill already exists issues no
authorization: the handler checks the runtime's replay identity
(`CompletedVendorBillExecutionProbe`) and returns the stored `ALREADY_EXECUTED` result.

Authorizations (G) are not a separate operator click: a write that needs one (creating a
supplier partner, executing a Vendor Bill) issues the existing narrow, single-use, 15-minute
authorization for exactly that review/version/operation, with the mapped Hub actor as
`authorized_by`, records it on the request ledger, and consumes it in the same request.

Not covered in this slice (guidance shows **Teknik Destek Gerekli**): product remediation
(`CREATE_NEW_PRODUCT`, per-line inputs), tax/line resolutions, RESALE/CUSTOMER_PROJECT
accounting (the accounting endpoint itself rejects them), customer quotation execution.

## Request schema (Odoo-owned)

All fields live on the existing parent model `x_ipp_import_workbench`. Odoo users write
them only through the form; the Hub only reads them.

| Label | Technical name (suggested) | Type | Env key `ODOO_WORKBENCH_REQUEST_…` |
| --- | --- | --- | --- |
| İşlem | `x_studio_ipp_req_action` | Selection (labels above) | `ACTION_FIELD` |
| İnceleme Sürümü (istek) | `x_studio_ipp_req_version` | Integer, readonly in view | `EXPECTED_VERSION_FIELD` |
| İsteyen | `x_studio_ipp_req_requested_by` | Many2one `res.users`, readonly in view | `REQUESTED_BY_FIELD` |
| İstek Zamanı | `x_studio_ipp_req_requested_at` | Datetime, readonly in view | `REQUESTED_AT_FIELD` |
| İstek Hazır | `x_studio_ipp_req_ready` | Boolean, invisible | `READY_FIELD` |
| Tedarikçi İşlemi | `x_studio_ipp_req_supplier_mode` | Selection | `SUPPLIER_MODE_FIELD` |
| Tedarikçi | `x_studio_ipp_req_partner` | Many2one `res.partner` | `PARTNER_FIELD` |
| Satın Alma Amacı | `x_studio_ipp_req_purpose` | Selection | `PURCHASE_PURPOSE_FIELD` |
| Muhasebe İşlemi | `x_studio_ipp_req_treatment` | Selection | `TREATMENT_FIELD` |
| Gider Hesabı | `x_studio_ipp_req_expense_account` | Many2one `account.account`, domain `[('account_type','=','expense')]` | `EXPENSE_ACCOUNT_FIELD` |
| Gider Kategorisi | `x_studio_ipp_req_expense_category` | Char (uppercase code) | `EXPENSE_CATEGORY_FIELD` |
| Demirbaş Hesabı | `x_studio_ipp_req_asset_account` | Many2one `account.account`, domain `[('id','in',x_studio_ipp_eligible_asset_account_ids)]` | `ASSET_ACCOUNT_FIELD` |
| Amortisman Modeli | `x_studio_ipp_req_depreciation_model` | Many2one `account.depreciation.model`, domain `[('active','=',True)]` | `DEPRECIATION_MODEL_FIELD` |
| Not | `x_studio_ipp_req_note` | Text | `NOTE_FIELD` |

Also required: `PARENT_MODEL`, `REVIEW_ID_FIELD` (`x_studio_review_id`),
`COMPANY_ID_FIELD` (`x_studio_company`).

Studio view domains only narrow the choices for convenience; the Hub re-validates every
selected id (eligibility allowlist, account type, company, activity) in the use case.

## Hub-owned result and guidance fields

Request result (written by `OdooOperatorRequestAcknowledger`):

| Label | Technical name (suggested) | Type | Env key `ODOO_WORKBENCH_REQUEST_…` |
| --- | --- | --- | --- |
| Sonuç | `x_studio_ipp_req_result` | Selection: `Tamamlandı`, `Güncel Değil`, `Reddedildi`, `Yetkisiz`, `Hata` | `RESULT_FIELD` |
| Sonuç Mesajı | `x_studio_ipp_req_message` | Text | `MESSAGE_FIELD` |
| İşlenme Zamanı | `x_studio_ipp_req_processed_at` | Datetime | `PROCESSED_AT_FIELD` |

Guidance (written by the projection publisher, full-snapshot, idempotent):

| Label | Technical name (suggested) | Type | Env key `ODOO_WORKBENCH_PUBLISHER_…` |
| --- | --- | --- | --- |
| Yapılması Gereken | `x_studio_ipp_next_action` | Selection: `Tedarikçi Doğrulanmalı`, `Satın Alma Amacı Seçilmeli`, `Muhasebe İşlemi Seçilmeli`, `Karar Verilmeli`, `Fatura Oluşturulmalı`, `Teknik Destek Gerekli`, `Tamamlandı` | `NEXT_ACTION_FIELD` |
| Yapılması Gerekenler | `x_studio_ipp_todo` | Html (readonly) | `TODO_FIELD` |
| Tamamlananlar | `x_studio_ipp_completed` | Html (readonly) | `COMPLETED_FIELD` |
| Uygun Demirbaş Hesapları | `x_studio_ipp_eligible_asset_account_ids` | Many2many `account.account`, invisible | `ELIGIBLE_ASSET_ACCOUNTS_FIELD` |

*Tamamlananlar* shows accounting in operator language, e.g. `Muhasebe: Sabit Kıymet /
Demirbaş — <hesap kodu> — <hesap adı> — <amortisman modeli>` or `Muhasebe: Gider — <hesap
kodu> — <hesap adı> — <kategori>`. Names are read through the existing read-only
`FixedAssetAccountingReader` port (no cache, no new source of truth). If Odoo cannot answer,
the summary shows "hesap bilgisi şu an okunamadı" / "amortisman modeli bilgisi şu an
okunamadı"; raw ids remain only in the *Teknik / Denetim* decision-basis lines. A
temporarily unreadable label can make one reconcile run report the summary as changed.

The eligible asset accounts are exactly `ODOO_FIXED_ASSET_ACCOUNT_IDS`; no account or
depreciation model is hard-coded in Odoo views or Hub code. Unmapped guidance fields are
neither read nor written, so the existing projection is unchanged until they are mapped.

## "İşleme Gönder" server action

Model `x_ipp_import_workbench`, type *Update Record* (or *Execute Code* restricted to the
lines below). It only copies values; it contains no business rule.

```python
for record in records:
    record.write(
        {
            "x_studio_ipp_req_version": record.x_studio_review_version,
            "x_studio_ipp_req_requested_by": env.user.id,
            "x_studio_ipp_req_requested_at": datetime.datetime.now(),
            "x_studio_ipp_req_result": False,
            "x_studio_ipp_req_message": False,
            "x_studio_ipp_req_ready": True,
        }
    )
```

## Screen structure

Three tabs only: **İnceleme**, **İş Bağlamı** (only when business context is required),
**Teknik / Denetim**. Workflow stages are not tabs.

```xml
<form>
  <header>
    <button name="ACTION_ID_ISLEME_GONDER" type="action" string="İşleme Gönder" class="btn-primary"
            invisible="x_studio_ipp_req_ready or x_studio_ipp_next_action in ('Tamamlandı', 'Teknik Destek Gerekli')"/>
    <field name="x_studio_review_status" widget="statusbar"/>
  </header>
  <sheet>
    <div class="alert alert-info" invisible="not x_studio_ipp_req_ready">
      İşlem gönderildi — Hub sırada. Sonuç genellikle bir dakika içinde görünür.
    </div>
    <div class="alert alert-warning" invisible="x_studio_ipp_req_result not in ('Güncel Değil', 'Reddedildi', 'Yetkisiz', 'Hata')">
      <field name="x_studio_ipp_req_message" readonly="1"/>
    </div>
    <notebook>
      <page string="İnceleme">
        <group string="Fatura">
          <field name="x_studio_invoice_number" readonly="1"/>
          <field name="x_studio_supplier" readonly="1"/>
          <field name="x_studio_invoice_date" readonly="1"/>
          <field name="x_studio_invoice_total" readonly="1"/>
          <field name="x_studio_currency" readonly="1"/>
          <field name="x_studio_review_status" readonly="1"/>
        </group>
        <group string="Yapılması Gerekenler">
          <field name="x_studio_ipp_next_action" readonly="1"/>
          <field name="x_studio_ipp_todo" readonly="1" nolabel="1"/>
        </group>
        <!-- supplier step -->
        <group invisible="x_studio_ipp_next_action != 'Tedarikçi Doğrulanmalı'">
          <field name="x_studio_ipp_req_supplier_mode"/>
          <field name="x_studio_ipp_req_partner" invisible="x_studio_ipp_req_supplier_mode != 'Mevcut Tedarikçiyi Seç'"/>
        </group>
        <!-- purpose step -->
        <group invisible="x_studio_ipp_next_action != 'Satın Alma Amacı Seçilmeli'">
          <field name="x_studio_ipp_req_purpose"/>
        </group>
        <!-- accounting step -->
        <group invisible="x_studio_ipp_next_action != 'Muhasebe İşlemi Seçilmeli'">
          <field name="x_studio_ipp_req_treatment"/>
          <field name="x_studio_ipp_req_expense_account" invisible="x_studio_ipp_req_treatment != 'Gider Hesabı'"
                 domain="[('account_type', '=', 'expense')]"/>
          <field name="x_studio_ipp_req_expense_category" invisible="x_studio_ipp_req_treatment != 'Gider Hesabı'"/>
          <field name="x_studio_ipp_eligible_asset_account_ids" invisible="1"/>
          <field name="x_studio_ipp_req_asset_account" invisible="x_studio_ipp_req_treatment != 'Sabit Kıymet / Demirbaş'"
                 domain="[('id', 'in', x_studio_ipp_eligible_asset_account_ids)]"/>
          <field name="x_studio_ipp_req_depreciation_model"
                 invisible="x_studio_ipp_req_treatment != 'Sabit Kıymet / Demirbaş'"/>
        </group>
        <!-- decision step: existing decision fields -->
        <group invisible="x_studio_ipp_next_action != 'Karar Verilmeli'">
          <field name="x_studio_decision"/>
          <field name="x_studio_selected_workflow"/>
          <field name="x_studio_decision_comment"/>
        </group>
        <group invisible="x_studio_ipp_next_action == 'Tamamlandı'">
          <field name="x_studio_ipp_req_action"/>
          <field name="x_studio_ipp_req_note"/>
        </group>
        <group string="Tamamlananlar">
          <field name="x_studio_ipp_completed" readonly="1" nolabel="1"/>
          <field name="x_studio_vendor_bill" readonly="1" invisible="not x_studio_vendor_bill"/>
        </group>
      </page>
      <page string="İş Bağlamı" invisible="x_studio_business_context_required != 'Required'">
        <field name="x_studio_allocation_list"/>
      </page>
      <page string="Teknik / Denetim" groups="base.group_system">
        <group>
          <field name="x_studio_review_reasons" readonly="1"/>
          <field name="x_studio_review_version" readonly="1"/>
          <field name="x_studio_ipp_req_version" readonly="1"/>
          <field name="x_studio_ipp_req_requested_by" readonly="1"/>
          <field name="x_studio_ipp_req_requested_at" readonly="1"/>
          <field name="x_studio_ipp_req_result" readonly="1"/>
          <field name="x_studio_ipp_req_processed_at" readonly="1"/>
          <field name="x_studio_execution_message" readonly="1"/>
          <field name="x_studio_last_sync_at" readonly="1"/>
        </group>
      </page>
    </notebook>
  </sheet>
</form>
```

The *İşlem* selection should default to the next action. Studio cannot compute that
without code; operators pick it (one choice per step). Choosing the wrong action is
harmless: the Hub rejects it with a readable message.

List view (compact):

```xml
<list>
  <field name="x_studio_invoice_number" string="Fatura No"/>
  <field name="x_studio_supplier" string="Tedarikçi"/>
  <field name="x_studio_invoice_date" string="Fatura Tarihi"/>
  <field name="x_studio_invoice_total" string="Toplam"/>
  <field name="x_studio_currency" string="Para Birimi"/>
  <field name="x_studio_review_status" string="Durum"/>
  <field name="x_studio_ipp_next_action" string="Yapılması Gereken"/>
</list>
```

## Access control

- Hub-owned fields (projection, guidance, result) are readonly in every view.
- Request fields are editable only on the form, by the Workbench reviewer group.
- Restrict JSON-2 write access on the model to the Hub integration user and reviewers.
  `requested by` is set by the button; a user with raw API write access could forge it,
  which is why the Hub additionally requires an `ODOO_OPERATOR_REQUEST_ACTORS` mapping.
- The *Teknik / Denetim* page is limited to administrators.

## Hub configuration

```text
ODOO_WORKBENCH_OPERATOR_REQUESTS_ENABLED=false            # enable is a separate step
ODOO_WORKBENCH_OPERATOR_REQUESTS_INTERVAL_SECONDS=60
ODOO_WORKBENCH_OPERATOR_REQUESTS_COMPANY_ID=1
ODOO_OPERATOR_REQUEST_ACTORS={"<odoo user id>": {"actor": "<name>", "permissions": ["workbench_review_decide", "workbench_execute"]}}
ODOO_WORKBENCH_REQUEST_*                                  # request field mapping above
ODOO_WORKBENCH_PUBLISHER_NEXT_ACTION_FIELD / _TODO_FIELD / _COMPLETED_FIELD / _ELIGIBLE_ASSET_ACCOUNTS_FIELD
```

Startup fails closed when the tick is enabled without a company id, without
`ODOO_WORKBENCH_PROJECTION_PUBLISH_ENABLED=true`, with an incomplete request mapping, or
with malformed actor JSON.

## Concurrency, idempotency, failure behaviour

- The expected version is the version the operator saw (snapshotted by the button). It is
  passed unchanged; a moved review yields *Güncel Değil* with the message "Bu inceleme siz
  işlem yaparken değişti. Güncel bilgiler yüklendi; lütfen işlemi tekrar kontrol edin."
- Request key = hash of company, review, row, action, version, values, requester, time.
  Terminal ledger rows are only re-acknowledged; in-progress rows are resumed through the
  use cases' own replay (`already_applied`, decision idempotency key, `ALREADY_EXECUTED`).
- Transient Odoo/ERP failures leave the request pending (retried next tick, up to 5
  attempts, then *Hata*). Use-case refusals are *Reddedildi* with the use case's safe
  reason. Unexpected errors are logged with traceback and shown as *Hata*.
- The acknowledgement clears `ready` only when the row still carries the same request
  time, so a newer submission is never lost.

## Rollout

Each step needs its own explicit approval; none is part of this PR.

1. Merge; deploy Hub (migration `202607170036`, flag still `false`). Reconcile dry-run.
2. Studio: add the request, result and guidance fields with the exact selection labels.
3. Env: map `ODOO_WORKBENCH_PUBLISHER_*` guidance fields; reconcile dry-run shows UPDATE
   for guidance only; apply reconcile.
4. Studio: form/list views, server action, ACLs.
5. Env: request mapping, actors, company id; enable the tick; recreate the poller.
6. Acceptance on one non-critical review; watch `workbench.operator_request.processed` logs.

## Rollout risks

- Production reconcile dry-runs on 2026-10-06 showed intermittent "Odoo request timed out"
  errors on different reviews per run. They predate this change. Timeouts make a request
  wait for a later tick (retry, then *Hata* after 5 attempts) and can delay guidance
  refreshes. Odoo timeout/retry hardening is a separate operational item.

## Rollback

- Disable: `ODOO_WORKBENCH_OPERATOR_REQUESTS_ENABLED=false`, recreate the poller. Pending
  Odoo requests stay untouched.
- Guidance: unset the publisher guidance env keys (fields stop being written).
- Schema: `alembic downgrade 202607170035` drops only the request ledger (audit history);
  no business table is touched.
- Studio: deactivate the added views/action; fields can stay (inert).
