# Odoo Workbench V1 operator buttons

V1 replaces the generic *Yapılacak İşlem* dropdown and the *İşleme Gönder* button with one
Turkish button per workflow step. It is a Studio-only change on top of the existing
operator request protocol ([ODOO_WORKBENCH_OPERATOR_UI.md](ODOO_WORKBENCH_OPERATOR_UI.md)):
each button's server action sets the same `x_studio_ipp_req_action` label the dropdown
set, then stamps the request exactly as action 1026 does. The Hub reads the same fields,
dispatches to the same use cases and enforces every existing rule (purchase-purpose gate,
RESALE / INTERNAL_USE pins, UBL exchange rate, authorization, idempotency, draft-only).

Nothing here has been applied to production. Applying it is a separately approved step.

Artifacts: [`docs/odoo_studio/v1_operator_buttons/`](odoo_studio/v1_operator_buttons/)

| File | Odoo object |
| --- | --- |
| `view.xml` | `ir.ui.view` form extension of 3984, priority 260, "ICT V1 Workbench operator buttons" |
| `actions/supplier.py` | server action "ICT V1 Tedarikçiyi Kaydet" |
| `actions/purpose.py` | server action "ICT V1 Satın Alma Amacını Kaydet" |
| `actions/accounting.py` | server action "ICT V1 Muhasebeyi Kaydet" |
| `actions/decision.py` | server action "ICT V1 Kararı Gönder" |
| `actions/execute.py` | server action "ICT V1 Taslak Alış Faturası Oluştur" |
| `actions/open_bill.py` | server action "ICT V1 Taslak Faturayı Aç" (read-only) |
| `provision.py` | `validate` (read-only) / `apply` / `rollback` |
| `production_form_combined_20261021.xml` | combined production form (`get_views`) the view is tested against |

## Buttons

All buttons: group *IPP Workbench Operator* (88, user 2 only), hidden while a request is
queued (`x_studio_ipp_req_ready`). Server actions are created with `group_ids = [88]` and
are not bound to the Action menu.

| Sıradaki Adım (`x_studio_ipp_next_action`) | Button | Queues `İşlem` | Server-side validation (besides the common checks) |
| --- | --- | --- | --- |
| Tedarikçi Doğrulanmalı | Tedarikçiyi Kaydet | Tedarikçi Çözümü | Tedarikçi İşlemi; Mevcut Tedarikçi when "Mevcut Tedarikçiyi Seç" |
| Satın Alma Amacı Seçilmeli | Satın Alma Amacını Kaydet | Satın Alma Amacı | Satın Alma Amacı |
| Karar Verilmeli (amaç seçiliyse, ikincil) | Satın Alma Amacını Kaydet | Satın Alma Amacı | Satın Alma Amacı |
| Muhasebe İşlemi Seçilmeli | Muhasebeyi Kaydet | Muhasebe İşlemi | Muhasebe İşlemi; Gider: hesap + kategori; Demirbaş: hesap (izinli listede) + amortisman modeli |
| Karar Verilmeli | Kararı Gönder (onay sorar) | Karar | Karar; Submit Decision: İş Akışı; Dismiss: Karar Notu |
| Fatura Oluşturulmalı | Taslak Alış Faturası Oluştur (onay sorar) | Fatura Oluştur | durum Decision Submitted; taslak fatura yok |
| (taslak fatura varsa) | Taslak Faturayı Aç | — (yazma yok) | fatura bağlantısı var |

Common checks in every queuing action (each raises a Turkish `UserError` and writes nothing):
exactly one record; no request already queued (double-click / second tab); the row is still
at the button's step ("Sayfayı yenileyin"); the row has a Hub review version. The only write
is the request stamp: action label, version, requester, time, cleared result, `ready=True`.

Decision and bill creation remain two separate operator steps; no button posts, pays or
reconciles. The Hub still refuses anything the use cases refuse (e.g. a product-coded
decision without RESALE / INTERNAL_USE purpose) and shows the reason in *Sonuç Mesajı*.

## Form changes (view 260)

- Header: *İşleme Gönder* (1026) hidden, step buttons added.
- *İşlem* group renamed *Bilgileri Girin*; the *Yapılacak İşlem* field is hidden.
- *Not* is optional and hidden at the decision and bill steps.
- Dropdown-era hints (`ipp_golive_resale_hint`, `ipp_internal_use_purpose_hint`) hidden;
  the accounting hint and the step instruction shortened to one sentence each.
- Draft warning uses the approved text: "Taslak alış faturası oluşturuldu. Faturayı Odoo'da
  kontrol edin. Muhasebe geçiş kararı olmadan ONAYLAMAYIN; ödeme veya mutabakat yapmayın."
- *Teknik / Denetim* (system group) is untouched; no field is deleted.

## Hub change in the same PR (no technical ids in operator text)

Operator messages no longer show Odoo record ids or review versions:
"Karar kaydedildi.", "Taslak alış faturası oluşturuldu. …" (no "(Odoo kayıt N)"),
"Taslak alış faturası daha önce oluşturulmuştu; tekrar oluşturulmadı.", completed summary
"Karar: Tedarikçi Faturası" and "Taslak alış faturası oluşturuldu (onaylanmadı)", product
mapping / creation messages without "#id". Ids stay in the request ledger, execution
evidence and the *Teknik / Denetim* tab. The Studio change does not depend on this deploy.
After the deploy, rows whose completed summary changes are updated by the normal runtime
projection or a separately approved reconcile.

## Provisioning (separately approved)

```bash
# 1. read-only validation against production (fields, selections, group, anchors)
python3 docs/odoo_studio/v1_operator_buttons/provision.py --bundle \
  | ssh ict-hub-prod "docker exec -i -w /app config-api-1 sh -c \
    'ODOO_API_KEY=\"\$(cat /run/secrets/odoo_api_key)\" exec python - validate'"
# expect: "problems": [], no existing V1 objects, digest 615303b6c916d405

# 2. apply (creates 6 server actions, then the view; post-check; auto-deactivates the
#    view if the post-check fails)
python3 docs/odoo_studio/v1_operator_buttons/provision.py --bundle \
  | ssh ict-hub-prod "docker exec -i -w /app -e V1_CONFIRM=615303b6c916d405 config-api-1 sh -c \
    'ODOO_API_KEY=\"\$(cat /run/secrets/odoo_api_key)\" exec python - apply'"
```

## Rollback

1. `rollback` (`V1_CONFIRM=rollback`) sets the V1 view `active=False`. The form is exactly
   the current one again: dropdown and *İşleme Gönder* (1026) return.
2. The six server actions are inert without the view (not in any menu); they may stay or be
   archived later.
3. Queued requests are unaffected: the Hub processes them the same either way.
4. Hub message change: revert the PR / redeploy the previous image.

## Operator acceptance scenarios (production, each run separately approved)

Each scenario uses one real pending review; steps that create a draft bill need approval.

| # | Workflow | Steps | Expected |
| --- | --- | --- | --- |
| 1 | Unknown supplier | Tedarikçi İşlemi = Mevcut Tedarikçiyi Seç, choose partner → Tedarikçiyi Kaydet | blue "Hub sırada" banner, buttons hidden; then Sonuç Tamamlandı, next step advances |
| 2 | Product-coded RESALE | Satın Alma Amacı = Yeniden Satış → Satın Alma Amacını Kaydet → Karar = Submit Decision, İş Akışı = Direct Vendor Bill → Kararı Gönder (confirm) → Taslak Alış Faturası Oluştur (confirm) → Taslak Faturayı Aç | draft bill on the RESALE-pinned category account; UBL rate; draft state |
| 3 | Product-coded INTERNAL_USE | Amaç = Şirket İçi Kullanım → Muhasebe = Gider Hesabı + hesap + kategori → Muhasebeyi Kaydet → Karar → Fatura | draft bill on the pinned internal expense account |
| 4 | Identifier-free operating expense | Amaç → Muhasebe (Gider) → Karar → Fatura | draft bill on the selected expense account |
| 5 | Fixed asset | Amaç = Şirket İçi Kullanım → Muhasebe = Sabit Kıymet / Demirbaş (eligible account + model) → Karar → Fatura | draft bill on the asset account |
| 6 | Product not found | Ürün Satırları tab (unchanged: Eşleştir / Yeni Ürün Oluştur) | no header button at this step |
| 7 | Negative | empty required field; second click; another tab after the step moved; product-coded decision without purpose | Turkish UserError and no write; Hub "Reddedildi" with reason for the purpose gate |

## Known limitations

- Studio cannot know whether the invoice is product-coded, so the purpose field offers all
  four purposes; the Hub rejects unsupported ones with a readable reason.
- *İş Akışı* still lists all workflows; allocation-requiring ones are validated by the Hub.
- Odoo's own display name of a draft bill contains its id (e.g. "Taslak Fatura (* 76)").
- Dismiss stays available through *Karar = Dismiss* with a mandatory note; it is terminal.
