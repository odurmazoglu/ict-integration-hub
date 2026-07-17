# Architecture

```text
Uyumsoft SOAP/WSDL
        |
        v
ICT Integration Hub
- Connector adapters
- UBL-TR parser
- Idempotency / ETTN
- Mapping and validation
- Retry / audit log
- PostgreSQL
        |
        v
Odoo Online JSON-2 API
```

## Sorumluluklar

### Integration Hub

- Uyumsoft kimlik doğrulama ve SOAP çağrıları
- Gelen/giden fatura listesinin alınması
- XML/PDF belgelerinin indirilmesi
- UBL-TR ayrıştırma
- ETTN bazlı tekillik
- Partner/ürün/vergi eşleştirme kararlarının uygulanması
- Odoo API çağrıları
- Hata, retry ve audit kayıtları

### Odoo

- `res.partner`, `product.product`, `account.tax`, `account.journal` kaynak kayıtları
- Standart `account.move` ve `account.move.line`
- Finans kullanıcısının taslak kontrolü ve muhasebeleştirmesi
- Customer Asset ve satış/maliyet ilişkileri

## Aşamalı canlılaştırma

1. Bağlantı ve WSDL keşfi
2. Salt-okunur listeleme
3. Normalize metadata'nın Integration Hub veritabanına idempotent kaydı
4. Ham XML/PDF saklama
5. Parse ve doğrulama
6. Odoo'ya taslak fatura
7. Eşleştirme ekranları
8. Zamanlanmış senkronizasyon
9. Kontrollü canlı Uyumsoft geçişi

## Kritik kararlar

- Odoo Online nedeniyle entegrasyon harici servis olarak çalışır.
- Uyumsoft test ortamı MVP boyunca zorunludur.
- Durum değiştiren SOAP operasyonları varsayılan olarak kapalıdır.
- Odoo faturaları otomatik post edilmez.

## Uyumsoft invoice listing connector

- `app/connectors/uyumsoft/client.py` yalnız sağlayıcı çağrılarını yönetir ve FastAPI bağımlılığı içermez.
- `GetInboxInvoiceList` ve `GetOutboxInvoiceList` salt-okunur kabul edilir; durum değiştiren operasyonlar uygulanmaz.
- SOAP yanıtları `app/connectors/uyumsoft/invoice_mapping.py` içinde normalize edilir.
- API ve servis katmanlarına SOAP nesneleri taşınmaz; `app/schemas/uyumsoft_invoices.py` DTO modelleri kullanılır.
- Listeleme tarih aralığı, sayfalama, timeout ve geçici transport hatası retry desteği sağlar.
- Retry yalnız transport katmanı için yapılır; SOAP fault yanıtları tekrar denenmez.
- Uyumsoft test WSDL adresi: `https://efatura-test.uyumsoft.com.tr/Services/Integration?wsdl`.
- `GetInboxInvoiceList` input modeli `InboxInvoiceListQueryModel`, `GetOutboxInvoiceList` input modeli `OutboxInvoiceListQueryModel` olarak keşfedildi.
- Listeleme connector'ı Zeep tip fabrikasıyla WSDL tipli sorgu nesnesi üretir; sözlük payload varsayımı kullanılmaz.
- Her iki sorgu modelinde kullanılan alanlar: `ExecutionStartDate`, `ExecutionEndDate`, `PageIndex`, `PageSize`, `IncludeTagList`.
- Inbox sorgu modelinde ayrıca WSDL'de `CreateStartDate`, `CreateEndDate`, `Status`, `InvoiceIds`, `InvoiceNumbers`, `StatusInList`, `StatusNotInList`, `SortColumn`, `SortMode`, `IsArchived`, `TargetTitle`, `TargetTcknVkn`, `Tags`, `OrderDocumentId`, `OnlyNewestInvoices` alanları bulunur.
- Outbox sorgu modelinde ayrıca WSDL'de `CreateStartDate`, `CreateEndDate`, `Status`, `InvoiceIds`, `InvoiceNumbers`, `StatusInList`, `StatusNotInList`, `SortColumn`, `SortMode`, `IsArchived`, `TargetTitle`, `TargetTcknVkn`, `Tags`, `OrderDocumentId`, `Scenario` alanları bulunur.
- `InboxInvoiceListResponse` ve `OutboxInvoiceListResponse` yapısı `Value`, `IsSucceded`, `Message` alanlarından oluşur. `Value` içinde `Items`, `PageIndex`, `PageSize`, `TotalCount`, `TotalPages` bulunan paged response döner.
- Inbox item alanları `InvoiceId`, `DocumentId`, `Type`, `TypeCode`, `TargetTcknVkn`, `TargetTitle`, `EnvelopeIdentifier`, `Status`, `StatusCode`, `EnvelopeStatus`, `EnvelopeStatusCode`, `Message`, `CreateDateUtc`, `ExecutionDate`, `PayableAmount`, `TaxTotal`, `TaxExclusiveAmount`, `DocumentCurrencyCode`, `ExchangeRate`, VAT tutarları, `OrderDocumentId`, `IsArchived`, `InvoiceTipType`, `InvoiceTipTypeCode`, `IsNew`, `IsSeen` olarak keşfedildi.
- Outbox item alanları inbox alanlarına ek olarak `Scenario`, `ScenarioCode`, `LocalDocumentId`, `ExtraInformation` içerir.
- Provider-specific alanlar `extra_fields` içinde korunur; credential, token, büyük metin, binary ve XML benzeri içerikler normalize edilmeden önce redakte edilir.

## Uyumsoft invoice metadata persistence

- Kalıcı metadata modeli `app/models/uyumsoft_invoice.py` içinde `UyumsoftInvoiceMetadata` olarak tanımlıdır.
- Tablo adı `uyumsoft_invoice_metadata` şeklindedir.
- Servis katmanı `app/services/invoice_persistence.py` içindedir; FastAPI veya connector nesnesine bağımlı değildir.
- Sync workflow `app/services/uyumsoft_invoice_sync.py` içinde yalnız read-only listing metotlarını çağırır.
- Manuel endpoint `POST /api/v1/sync/uyumsoft/invoices` yalnız Uyumsoft test ortamında ve `confirm_read_only=true` ile çalışır.
- Endpoint structured summary döndürür: run id, status, cursor state ve direction bazında pages_fetched, invoices_seen, created, updated, skipped.
- Incremental sync run kayıtları `app/models/uyumsoft_sync_run.py` içindeki `UyumsoftSyncRun` modelinde saklanır.
- Sync konfigürasyonu bounded tutulur: tarih aralığı en fazla 31 gün, page_size en fazla 100, max_pages en fazla 10 olabilir.
- Kısmi connector hatasında run `failed` olarak işaretlenir, tamamlanmış sayfaların cursor/summary bilgisi saklanır ve idempotent metadata kayıtları korunur.

### Schema

Tablo alanları:

- `id`
- `provider`
- `direction`
- `provider_invoice_id`
- `ettn`
- `identity_key`
- `identity_strategy`
- `invoice_number`
- `invoice_date`
- `sender_name`
- `sender_tax_number`
- `receiver_name`
- `receiver_tax_number`
- `currency`
- `total_amount`
- `provider_status`
- `raw_metadata`
- `first_seen_at`
- `last_seen_at`
- `created_at`
- `updated_at`

Index ve constraint'ler:

- `uq_uyumsoft_invoice_provider_direction_ettn`
- `uq_uyumsoft_invoice_provider_direction_identity`
- `ix_uyumsoft_invoice_provider_direction`
- `ix_uyumsoft_invoice_ettn`
- `ix_uyumsoft_invoice_invoice_date`

### Idempotency

- ETTN varsa birincil idempotency anahtarı `provider + direction + ettn` şeklindedir.
- Aynı ETTN tekrar geldiğinde yeni kayıt oluşturulmaz.
- Provider metadata değişirse mevcut kayıt güncellenir, `first_seen_at` korunur ve `last_seen_at` yenilenir.
- Metadata değişmezse kayıt skipped sayılır; yine de `last_seen_at` son görülme zamanı olarak güncellenir.
- ETTN yoksa fallback identity stratejisi kullanılır.

### Fallback identity

ETTN eksik olduğunda `identity_strategy = fallback_v1` olur. `identity_key`, aşağıdaki alanların canonical JSON çıktısından üretilen SHA-256 digest ile oluşturulur:

- direction
- provider_invoice_id
- invoice_number
- invoice_date
- tax_number
- currency
- total_amount

Bu strateji deterministiktir ve `provider + direction + identity_key` unique constraint ile veritabanında korunur.

### Sync run tracking schema

`uyumsoft_sync_runs` tablosu her manuel incremental sync denemesinin güvenli operasyon kaydını tutar:

- `provider`
- `status`
- `requested_directions`
- `from_date`, `to_date`
- `page_size`, `max_pages`
- `current_direction`, `current_page`
- `pages_fetched`, `invoices_seen`
- `created_count`, `updated_count`, `skipped_count`
- `cursor_state`
- `summary`
- `failure_message`, `failure_detail`
- `started_at`, `finished_at`, `created_at`, `updated_at`

Index'ler:

- `ix_uyumsoft_sync_runs_provider_status`
- `ix_uyumsoft_sync_runs_window`
- `ix_uyumsoft_sync_runs_started_at`

### Read-only sync flow

1. API veya ilerideki scheduler dar tarih aralığı, direction, page_size ve max_pages ile workflow'u çağırır.
2. Workflow yalnız `GetInboxInvoiceList` ve/veya `GetOutboxInvoiceList` çağırır.
3. Connector SOAP yanıtını `UyumsoftInvoiceSummary` DTO'larına normalize eder.
4. Persistence service kayıtları idempotent şekilde insert/update/skip eder.
5. Sync run repository current direction/page ve aggregate counts bilgisini günceller.
6. API yalnız aggregate summary döndürür; secret, XML/PDF veya tam fatura payload'ı loglanmaz.

Bu akışta aşağıdakiler uygulanmaz: `SetInvoicesTaken`, `SendInvoice`, `Cancel*`, `RetrySendInvoices`, `MoveToDraftStatus`, invoice acknowledgement, status mutation, XML/PDF download, Odoo create/write/unlink/action_post.

### Rollback

Migration rollback için:

```bash
alembic downgrade -1
```

Bu rollback `uyumsoft_invoice_metadata` tablosunu ve ilişkili index/constraint'leri kaldırır.

Issue #12 migration rollback'i yalnız `uyumsoft_sync_runs` tablosunu ve ilişkili index'leri kaldırır. Bir önceki metadata migration'ı ayrıca rollback edilmedikçe invoice metadata tablosu korunur.

## Uyumsoft authentication and security

- Uyumsoft test Integration WSDL'i `BasicHttpBinding_IIntegration` için SOAP 1.1 + HTTPS binding yayınlar.
- WSDL policy içinde `TransportBinding`, `HttpsToken`, `SignedSupportingTokens`, `UsernameToken`, `IncludeTimestamp`, `Basic256` ve `Wss10` assertion'ları bulunur.
- Zeep operation signature'larına göre `TestConnection`, `WhoAmI` ve `GetSystemDate` body parametresi almaz; kullanıcı adı/parola SOAP body argümanı olarak gönderilmez.
- Connector kimlik doğrulamayı SOAP header seviyesinde WS-Security `UsernameToken` ile yapar.
- Canlı testte `UsernameToken` `PasswordText` formatı WCF güvenlik doğrulamasını geçip sağlayıcı yetkilendirme katmanına ulaşmıştır.
- Canlı testte `PasswordDigest` formatı `a:InvalidSecurity` SOAP fault üretmiştir; bu nedenle varsayılan istemci yapılandırması `PasswordText` kullanır.
- Zeep otomatik olarak SOAPAction değerlerini WSDL binding'inden kullanır:
  - `http://tempuri.org/IIntegration/TestConnection`
  - `http://tempuri.org/IIntegration/WhoAmI`
  - `http://tempuri.org/IIntegration/GetInboxInvoiceList`
  - `http://tempuri.org/IIntegration/GetOutboxInvoiceList`
- Endpoint URL `https://efatura-test.uyumsoft.com.tr/Services/Integration` olarak WSDL service port'undan alınır.
- Gerekli runtime ayarları:
  - `UYUMSOFT_ENVIRONMENT=test`
  - `UYUMSOFT_TEST_WSDL_URL=https://efatura-test.uyumsoft.com.tr/Services/Integration?wsdl`
  - `UYUMSOFT_USERNAME`
  - `UYUMSOFT_PASSWORD`
- `.env` içeriği loglanmaz; provider fault mesajlarında kullanıcı adı benzeri alanlar connector sınırında redakte edilir.

### Common authentication failures

- WS-Security header yoksa Uyumsoft SOAP fault: `a:InvalidSecurity` / `An error occurred when verifying security for the message.`
- Kullanıcı adı/parola body argümanı olarak gönderilirse Zeep signature hatası oluşur; WSDL bu operasyonlarda body credential alanı tanımlamaz.
- `PasswordDigest` kullanılırsa Uyumsoft test ortamı yine `a:InvalidSecurity` döndürür.
- `PasswordText` ile güvenlik doğrulaması geçip `s:Client` ve `Bu sisteme erişmek için gerekli yetkiniz yok` dönerse istek provider authorization katmanına ulaşmıştır; credential, hesap yetkisi, IP allowlist veya test ortamı aktivasyonu provider tarafında doğrulanmalıdır.
- `UYUMSOFT_USERNAME` veya `UYUMSOFT_PASSWORD` placeholder ise canlı smoke testi provider authorization sonucuna ulaşsa bile doğrulanmış başarılı auth sayılmaz.

### Troubleshooting

1. `python3 scripts/inspect_uyumsoft_wsdl.py --wsdl-url https://efatura-test.uyumsoft.com.tr/Services/Integration?wsdl` ile WSDL erişimini ve operation signature'larını doğrula.
2. `ICT_UYUMSOFT_ENABLE_LIVE_SMOKE=1 python3 scripts/diagnose_uyumsoft_auth.py --from <iso> --to <iso>` ile güvenli authentication diagnostic çalıştır.
3. Diagnostic çıktısında `has_ws_security=true`, `has_username_token=true`, `password_type=PasswordText`, doğru SOAPAction ve doğru endpoint olduğunu doğrula.
4. Fault code `a:InvalidSecurity` ise client-side security header veya password formatı tekrar incelenmelidir.
5. Fault code `s:Client` ve provider yetki mesajı varsa client security envelope kabul edilmiştir; provider credential/yetki/IP/test ortamı aktivasyonu gereklidir.
