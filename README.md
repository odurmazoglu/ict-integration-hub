# ICT Integration Hub

ICT Teknoloji'nin Odoo Online ERP ortamı ile harici servisler arasında çalışan entegrasyon katmanı.

İlk connector: **Uyumsoft e-Fatura**

## Mevcut durum

- Temiz FastAPI bootstrap oluşturuldu.
- Odoo Online JSON-2 API için salt-okunur probe eklendi.
- Uyumsoft test SOAP/WSDL için `TestConnection`, `WhoAmI`, `GetSystemDate` istemcileri eklendi.
- Uyumsoft WSDL operasyon keşfi yalnız geliştirme ortamında açıldı.
- Uyumsoft `GetInboxInvoiceList` ve `GetOutboxInvoiceList` listeleme çağrıları gerçek test WSDL model adlarıyla salt-okunur çalışacak şekilde eklendi.
- Uyumsoft gelen/giden fatura metadata'sı Integration Hub veritabanına ETTN idempotency ile salt-okunur kaydedilir.
- Manuel incremental sync run'ları `uyumsoft_sync_runs` tablosunda pencere, direction, page cursor, durum, özet ve güvenli hata bilgisiyle izlenir.
- Persist edilmiş Uyumsoft fatura metadata kayıtları için UBL XML doküman indirme, yerel dosya saklama ve metadata kaydı eklendi.
- Saklanan `UBL_XML` dokümanları provider-independent normalize invoice modellerine local parser ile ayrıştırılır.
- Normalize invoice modellerinden Odoo taslak fatura payload preview üretimi eklendi.
- Mapping Preview verisinden mevcut Odoo kayıtlarını salt-okunur ve deterministik eşleyen Odoo Resolution Engine eklendi.
- Onaylanmış Mapping Preview verisinden idempotent Odoo taslak vendor bill oluşturma eklendi.
- Üretim hazırlığı için explicit production gate, readiness/liveness endpointleri, safe config validation ve operasyon runbook'ları eklendi.
- Uyumsoft durum değiştiren operasyonları, PDF/XSLT/ZIP indirme ve Odoo `action_post` uygulanmadı.

## MVP kapsamı

1. Uyumsoft gelen ve giden fatura metadata listesini çekme
2. ETTN bazlı mükerrer kayıt engelleme
3. Hata, retry ve audit kayıtları
4. Sonraki fazlarda UBL-TR XML/PDF işleme
5. Partner, ürün, vergi, currency ve journal eşleştirme önizlemesi
6. Sonraki fazlarda Odoo'da kontrollü taslak tedarikçi/müşteri faturası oluşturma
7. Sonraki fazlarda Customer Asset ve maliyet ilişkileri

## Roadmap

Repository work should follow:

```text
Issue -> Milestone -> Labels -> Branch -> Draft PR -> CI -> Review -> Merge
```

Milestones:

- `Foundation`: bootstrap, CI/CD, Docker, WSDL discovery, read-only connector probes, read-only listing, and metadata persistence. This milestone can be closed once the merged foundation work is accepted as complete.
- `Invoice Sync`: incremental sync engine and safe recurring metadata refresh.
- `Attachments`: XML/UBL download, storage, parsing, and parse diagnostics.
- `Odoo Integration`: read-only mapping preview and controlled Odoo draft invoice creation.
- `Production Ready`: production gates, runbooks, monitoring, rollback, and go-live checklist.

Roadmap issues:

- [Incremental Sync Engine](https://github.com/odurmazoglu/ict-integration-hub/issues/12) -> `Invoice Sync`
- [XML / UBL Download](https://github.com/odurmazoglu/ict-integration-hub/issues/13) -> `Attachments`
- [UBL Parser](https://github.com/odurmazoglu/ict-integration-hub/issues/14) -> `Attachments`
- [Odoo Mapping Preview](https://github.com/odurmazoglu/ict-integration-hub/issues/15) -> `Odoo Integration`
- [Odoo Draft Invoice Creation](https://github.com/odurmazoglu/ict-integration-hub/issues/16) -> `Odoo Integration`
- [Odoo Resolution Engine](https://github.com/odurmazoglu/ict-integration-hub/issues/24) -> `Odoo Integration`
- [Production Readiness](https://github.com/odurmazoglu/ict-integration-hub/issues/17) -> `Production Ready`

Expected labels are documented in `CONTRIBUTING.md`: `feature`, `bug`, `enhancement`, `refactor`, `documentation`, `security`, `database`, `uyumsoft`, `odoo`, `ci`, `testing`, and `blocked`.

Current automation note: issue creation is available through the GitHub connector, but milestone and label creation are not exposed by the available tools. If the labels or milestones are missing in GitHub, create them manually and assign them to the roadmap issues above.

## Teknoloji

- Python 3.12
- FastAPI
- PostgreSQL
- SQLAlchemy
- Zeep SOAP client
- Odoo JSON-2 API
- Docker Compose

## Güvenlik

- `.env` ve gerçek kimlik bilgileri repoya gönderilmez.
- İlk aşamada otomatik muhasebeleştirme yapılmaz.
- Uyumsoft test ortamında kalınır.
- Odoo'da yalnız taslak kayıt oluşturulur.
- `SetInvoicesTaken`, gönderme ve iptal operasyonları açıkça onaylanmadan kullanılmaz.

## Çalıştırma

```bash
cp .env.example .env
docker compose up --build -d
curl http://localhost:8000/health
```

Docker Compose uses the bundled PostgreSQL service on port `5432` and sets the API container database URL to `postgresql+psycopg://ict:ict@db:5432/ict_integration_hub`. Local `.env` remains for runtime connector configuration and is not committed.

## Lokal geliştirme

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check .
uvicorn app.main:app --reload
```

Yerel `.env` dosyası yalnız runtime konfigürasyonu içindir; içeriği repoya eklenmez, testlerde basılmaz ve loglanmaz.

## Üretim hazırlığı

Production access tek bir environment değişikliğiyle açılamaz. `APP_ENV=production` yanında `PRODUCTION_OPERATIONS_ENABLED=true`, `PRODUCTION_APPROVAL_ACK=APPROVED_FOR_PRODUCTION`, production Uyumsoft environment, onaylı endpoint hostları ve placeholder olmayan credential değerleri gerekir. Unsafe veya çelişkili production config startup/readiness sırasında güvenli hata mesajıyla reddedilir.

Operasyon dokümanları:

- [Production Readiness](docs/PRODUCTION_READINESS.md): production gates, environment separation, readiness, logging/redaction, timeout/retry policy, deployment/rollback/incident runbooks, backup/restore, permissions and go-live checklist.
- [Integration Flow](docs/INTEGRATION_FLOW.md): Uyumsoft -> incremental sync -> document download -> UBL parser -> mapping preview -> Odoo resolution -> draft vendor bill creation stage boundaries.
- [Testing Documentation](docs/testing/README.md): integration testing, UAT, failure injection, performance validation, and go-live validation package.

## Uygulanan endpointler

- `GET /health`
- `GET /health/live`
- `GET /health/ready`
- `GET /api/v1/connectors/odoo/probe`
- `GET /api/v1/connectors/uyumsoft/test-connection`
- `GET /api/v1/connectors/uyumsoft/identity`
- `GET /api/v1/connectors/uyumsoft/system-date`
- `GET /api/v1/connectors/uyumsoft/operations`
- `GET /api/v1/connectors/uyumsoft/inbox?from=<iso-datetime>&to=<iso-datetime>&page=1&page_size=50`
- `GET /api/v1/connectors/uyumsoft/outbox?from=<iso-datetime>&to=<iso-datetime>&page=1&page_size=50`
- `POST /api/v1/sync/uyumsoft/invoices?from=<iso-datetime>&to=<iso-datetime>&direction=Both&page_size=50&max_pages=1&confirm_read_only=true`
- `POST /api/v1/documents/uyumsoft/invoices/download`
- `POST /api/v1/odoo/mapping-preview`
- `POST /api/v1/odoo/resolution`
- `POST /api/v1/odoo/draft-invoices`

## Uyumsoft metadata persistence

`uyumsoft_invoice_metadata` tablosu normalize edilmiş Uyumsoft test ortamı fatura metadata'sını saklar. Saklanan ana alanlar:

- provider, direction, provider_invoice_id
- ettn, invoice_number, invoice_date
- sender_name, sender_tax_number
- receiver_name, receiver_tax_number
- currency, total_amount, provider_status
- raw_metadata
- first_seen_at, last_seen_at, created_at, updated_at

Idempotency stratejisi:

- ETTN varsa `provider + direction + ettn` unique constraint ile korunur.
- Her kayıt için ayrıca `provider + direction + identity_key` unique constraint bulunur.
- ETTN varsa `identity_key = ettn:<ettn>` kullanılır.
- ETTN yoksa fallback identity `fallback_v1:<sha256>` olarak üretilir.
- Fallback hash girdileri deterministiktir: direction, provider_invoice_id, invoice_number, invoice_date, tax_number, currency ve total_amount.
- Tekrarlanan sync aynı kaydı günceller veya metadata değişmediyse skipped sayar; yeni kayıt oluşturmaz.

Manuel sync yalnız Uyumsoft test ortamında çalışır ve açık onay ister:

```bash
curl -X POST "http://localhost:8000/api/v1/sync/uyumsoft/invoices?from=2026-07-16T00:00:00%2B00:00&to=2026-07-17T00:00:00%2B00:00&direction=Both&page_size=10&max_pages=1&confirm_read_only=true"
```

Bu endpoint yalnız `GetInboxInvoiceList` ve `GetOutboxInvoiceList` çağırır. XML/PDF indirme, acknowledgement, status mutation, Uyumsoft write operasyonu veya Odoo write işlemi yapmaz.

Incremental sync davranışı:

- `direction=Inbox`, `direction=Outbox` veya `direction=Both` desteklenir.
- Her sync run bounded çalışır: tarih aralığı en fazla 31 gün, `page_size` en fazla 100, `max_pages` en fazla 10 olabilir.
- `uyumsoft_sync_runs` her run için requested directions, from/to window, page size, max pages, current direction/page, fetched page count, invoice counts, created/updated/skipped summary ve failure metadata saklar.
- Başarılı run `completed`, connector hatası alan run `failed` durumuna geçer. Kısmi başarıda tamamlanmış sayfaların metadata kayıtları idempotent biçimde korunur; aynı pencere tekrar çalıştırıldığında ETTN/fallback identity duplicate oluşturmaz.
- Structured log kayıtları yalnız aggregate run bilgisini içerir; secret, tam SOAP payload, XML/PDF veya fatura içeriği loglanmaz.

## Invoice document storage

UBL XML indirme yalnız daha önce `uyumsoft_invoice_metadata` tablosuna kaydedilmiş seçili invoice id'leri için, açık read-only onayla çalışır:

```bash
curl -X POST "http://localhost:8000/api/v1/documents/uyumsoft/invoices/download" \
  -H "Content-Type: application/json" \
  -d '{"invoice_ids":[1],"document_type":"UBL_XML","confirm_read_only":true}'
```

Bu endpoint yalnız Uyumsoft test ortamında çalışır ve sadece `GetInboxInvoiceData` / `GetOutboxInvoiceData` çağırır. PDF, XSLT, ZIP, acknowledgement, status mutation, Uyumsoft write operasyonu veya Odoo write işlemi yapmaz.

Doküman davranışı:

- Desteklenen tek doküman tipi `UBL_XML` değeridir.
- Batch input bounded tutulur: en fazla 20 invoice id kabul edilir.
- XML bytes veritabanına yazılmaz; dosya içeriği `DOCUMENT_STORAGE_ROOT` altındaki yerel dosya sisteminde saklanır.
- `invoice_documents` tablosu invoice linki, provider, direction, document type, storage backend/key, SHA-256 hash, MIME type, byte size ve indirme zamanını saklar.
- Aynı invoice ve document type tekrar indirildiğinde içerik hash'i aynıysa idempotent `existing` sonucu döner; içerik değişmişse doküman overwrite edilmez ve conflict hatası üretilir.
- Structured log kayıtları yalnız aggregate sonuç içerir; XML içeriği, SOAP payload, credential veya secret loglanmaz.

## UBL parser

Saklanan `UBL_XML` dokümanları `app/services/document_parser.py` içinde local olarak parse edilir. Parser Uyumsoft SOAP DTO'larını, Odoo modellerini veya transport detaylarını bilmez; çıktı `app/schemas/normalized_invoice.py` içindeki provider-independent typed modellerdir.

Parse edilen başlıca alanlar:

- Invoice header: invoice number, ETTN/UUID, invoice type code, profile id, timezone-aware issue datetime, currency, notes ve references.
- Parties: supplier ve customer tax id, party name, address, tax office ve contact alanları.
- Monetary totals: line extension, tax exclusive/inclusive, allowance, charge ve payable amounts.
- Taxes: total tax amount, subtotals, tax category, percent, taxable amount, tax amount ve exemption reason/code.
- Lines: line id, item name/description, quantity, unit code, unit price, line extension amount, allowance/charge ve line-level tax bilgisi.

Parser yalnız local XML parse eder, DOCTYPE/entity kullanımını reddeder, network erişimi yapmaz, malformed XML ve geçersiz Decimal/date-time alanlarını açık ve güvenli hatalarla bildirir. Normalized parse sonucu şu aşamada veritabanına yazılmaz; Odoo mapping Issue #15 kapsamındadır.

## Odoo mapping preview

`POST /api/v1/odoo/mapping-preview` endpoint'i `app/schemas/normalized_invoice.py` içindeki provider-independent normalize invoice modelini kabul eder ve `app/services/odoo_mapping_preview.py` ile Odoo taslak fatura payload preview üretir.

Preview çıktısı:

- `invoice`: `move_type`, `invoice_date`, `currency`, journal adayı, partner adayı, invoice lines, taxes, references, notes, invoice number ve ETTN alanlarını içeren taslak payload.
- `lines`: product adayı, sequence, description, quantity, unit price, unit of measure, line taxes ve line extension amount alanlarını içeren satır payload'ları.
- `warnings`: desteklenmeyen veya türetilen alanlara ait güvenli uyarılar.
- `missing_fields`: partner, currency, tax, quantity, unit price, invoice number, ETTN veya timezone gibi zorunlu eşleme eksiklerini listeler.
- `mapping_status`: eksik alan yoksa `ready`, aksi durumda `needs_review`.

Bu katman Odoo JSON-2 API çağırmaz, connector kullanmaz, kayıt oluşturmaz ve veritabanına yazmaz. Partner/product/tax/journal adayları deterministik preview olarak üretilir; otomatik Odoo eşleştirme ve Odoo taslak fatura oluşturma sonraki issue kapsamındadır. Structured log yalnız invoice id, mapping status, warning count ve line count içerir; XML, SOAP payload, credential, secret veya tam fatura payload'ı loglanmaz.

## Odoo resolution engine

`POST /api/v1/odoo/resolution` endpoint'i Mapping Preview çıktısını alır ve mevcut Odoo kayıtlarını yalnız `search_read` çağrılarıyla çözer. Bu katman provider-independent çalışır; Uyumsoft SOAP modellerini, UBL parser implementation detaylarını veya XML içeriğini bilmez.

Deterministik eşleştirme stratejisi:

- Partner: önce exact VAT/VKN, bulunamazsa exact normalized name.
- Product: önce `default_code`, bulunamazsa exact normalized name.
- Tax: purchase usage, percent amount, company, `price_include` ve active alanlarıyla exact match.
- Currency: ISO code için exact active `res.currency.name`.
- Journal: yalnız explicit configured purchase journal id veya code.

Çıktı `resolved`, `unresolved`, `ambiguous`, `invalid` ve `not_required` durumlarını ayrı ayrı raporlar; ambiguous veya missing eşleşmeler otomatik seçilmez. Reviewed preview içinde yalnız bulunan Odoo id'leri işlenir. Endpoint kayıt oluşturmaz, güncellemez, silmez, draft invoice create çağırmaz ve veritabanına yazmaz. Structured log yalnız invoice id, ETTN, entity type, status, match method, candidate count, duration ve güvenli hata kategorisi içerir.

## Odoo draft invoice creation

`POST /api/v1/odoo/draft-invoices` endpoint'i reviewed Mapping Preview çıktısını alır ve mevcut Odoo JSON-2 client ile yalnız `account.move/create` çağırarak taslak vendor bill oluşturur.

Güvenlik ve doğrulama:

- `confirm_create_draft=true` zorunludur.
- `APP_ENV=production` iken endpoint kapalıdır.
- Mapping Preview `mapping_status=ready` olmalı ve `missing_fields` boş olmalıdır.
- Preview içinde `partner.odoo_id`, `currency_id`, `journal.odoo_id`, her satır için `product.odoo_id` ve tax `odoo_id` değerleri bulunmalıdır.
- Partner, product, tax, currency, journal veya payment term lookup/creation yapılmaz.
- `action_post`, `unlink`, mevcut invoice update, payment registration ve reconciliation çağrıları yoktur.

Idempotency:

- `odoo_draft_invoices` tablosu ETTN için unique constraint uygular.
- Aynı ETTN daha önce başarıyla oluşturulduysa endpoint mevcut `account.move` referansını döndürür ve ikinci Odoo create çağrısı yapmaz.
- Başarısız denemeler `failed` durumuyla güvenli hata kategorisi/mesajı ve attempt count saklar; retry aynı ETTN kaydını günceller.
- Structured log yalnız Integration Hub invoice id, ETTN, operation status, Odoo move id, duration, error category ve attempt count içerir; tam Odoo payload, XML, SOAP payload, credential veya secret loglanmaz.

## Uyumsoft WSDL keşfi

Güvenli WSDL şema keşfi kimlik bilgisi gerektirmez ve yalnız operasyon/model meta verisi yazar:

```bash
python3 scripts/inspect_uyumsoft_wsdl.py \
  --wsdl-url https://efatura-test.uyumsoft.com.tr/Services/Integration?wsdl
```

Bu keşfe göre listeleme sorguları `ExecutionStartDate`, `ExecutionEndDate`, `PageIndex`, `PageSize` alanlarını içeren WSDL tipli `InboxInvoiceListQueryModel` ve `OutboxInvoiceListQueryModel` nesneleriyle oluşturulur. UBL XML indirme için `GetInboxInvoiceData(invoiceId: string)` ve `GetOutboxInvoiceData(invoiceId: string)` operasyonları `InvoiceDataResponse.Value.Data` alanından bytes döndürür.

## Opsiyonel canlı smoke testi

Canlı smoke testi varsayılan olarak kapalıdır. Yalnız Uyumsoft test ortamında, dar tarih aralığıyla, `page_size=1` kullanarak `GetInboxInvoiceList` ve `GetOutboxInvoiceList` çağırır. Kimlik bilgisi veya fatura XML/PDF içeriği yazdırmaz; yalnız güvenli özet döndürür.

```bash
ICT_UYUMSOFT_ENABLE_LIVE_SMOKE=1 python3 scripts/uyumsoft_readonly_smoke.py \
  --from 2026-07-16T00:00:00+00:00 \
  --to 2026-07-17T00:00:00+00:00 \
  --page-size 1
```

## Uyumsoft authentication diagnostic

Uyumsoft test ortamı SOAP güvenlik doğrulaması için HTTPS + WS-Security `UsernameToken` kullanır. Connector, WSDL'deki zero-argument read-only operasyonlara kullanıcı adı/parola body parametresi geçmez; credential bilgisi SOAP header içinde `PasswordText` formatıyla gönderilir.

Canlı authentication diagnostic varsayılan olarak kapalıdır. Yalnız güvenli metadata üretir; credential, token, XML/PDF veya fatura içeriği yazdırmaz.

```bash
ICT_UYUMSOFT_ENABLE_LIVE_SMOKE=1 python3 scripts/diagnose_uyumsoft_auth.py \
  --from 2026-07-16T00:00:00+00:00 \
  --to 2026-07-17T00:00:00+00:00
```

Yorumlama:

- `a:InvalidSecurity`: WS-Security header, binding veya password formatı client tarafında tekrar incelenmelidir.
- `s:Client` ve yetki mesajı: SOAP security envelope provider tarafından işlenmiştir; credential, hesap yetkisi, IP allowlist veya test ortamı aktivasyonu Uyumsoft tarafında doğrulanmalıdır.
- `Missing credentials`: runtime ayarlarında gerçek `UYUMSOFT_USERNAME` / `UYUMSOFT_PASSWORD` yoktur.

## Migration

```bash
alembic upgrade head
```

Rollback:

```bash
alembic downgrade -1
```

Issue #16 rollback'i `odoo_draft_invoices` tablosunu ve ilişkili index/constraint'leri kaldırır. Odoo tarafında oluşturulmuş taslak kayıtlar veritabanı rollback'iyle silinmez; operasyonel rollback sırasında Odoo taslak kayıtları ayrıca manuel değerlendirilmelidir.

Ayrıntılar için `docs/` ve Codex çalışma kuralları için `AGENTS.md` dosyasına bakın.
