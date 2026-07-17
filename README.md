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
- Uyumsoft durum değiştiren operasyonları, XML/PDF indirme ve Odoo yazma operasyonları uygulanmadı.

## MVP kapsamı

1. Uyumsoft gelen ve giden fatura metadata listesini çekme
2. ETTN bazlı mükerrer kayıt engelleme
3. Hata, retry ve audit kayıtları
4. Sonraki fazlarda UBL-TR XML/PDF işleme
5. Sonraki fazlarda Partner, ürün, vergi ve journal eşleştirme
6. Sonraki fazlarda Odoo'da taslak tedarikçi/müşteri faturası oluşturma
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

## Uygulanan endpointler

- `GET /health`
- `GET /api/v1/connectors/odoo/probe`
- `GET /api/v1/connectors/uyumsoft/test-connection`
- `GET /api/v1/connectors/uyumsoft/identity`
- `GET /api/v1/connectors/uyumsoft/system-date`
- `GET /api/v1/connectors/uyumsoft/operations`
- `GET /api/v1/connectors/uyumsoft/inbox?from=<iso-datetime>&to=<iso-datetime>&page=1&page_size=50`
- `GET /api/v1/connectors/uyumsoft/outbox?from=<iso-datetime>&to=<iso-datetime>&page=1&page_size=50`
- `POST /api/v1/sync/uyumsoft/invoices?from=<iso-datetime>&to=<iso-datetime>&direction=Both&page_size=50&max_pages=1&confirm_read_only=true`

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

## Uyumsoft WSDL keşfi

Güvenli WSDL şema keşfi kimlik bilgisi gerektirmez ve yalnız operasyon/model meta verisi yazar:

```bash
python3 scripts/inspect_uyumsoft_wsdl.py \
  --wsdl-url https://efatura-test.uyumsoft.com.tr/Services/Integration?wsdl
```

Bu keşfe göre listeleme sorguları `ExecutionStartDate`, `ExecutionEndDate`, `PageIndex`, `PageSize` alanlarını içeren WSDL tipli `InboxInvoiceListQueryModel` ve `OutboxInvoiceListQueryModel` nesneleriyle oluşturulur.

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

Ayrıntılar için `docs/` ve Codex çalışma kuralları için `AGENTS.md` dosyasına bakın.
