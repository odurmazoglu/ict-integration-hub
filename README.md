# ICT Integration Hub

ICT Teknoloji'nin Odoo Online ERP ortamı ile harici servisler arasında çalışan entegrasyon katmanı.

İlk connector: **Uyumsoft e-Fatura**

## Mevcut durum

- Temiz FastAPI bootstrap oluşturuldu.
- Odoo Online JSON-2 API için salt-okunur probe eklendi.
- Uyumsoft test SOAP/WSDL için `TestConnection`, `WhoAmI`, `GetSystemDate` istemcileri eklendi.
- Uyumsoft WSDL operasyon keşfi yalnız geliştirme ortamında açıldı.
- Uyumsoft `GetInboxInvoiceList` ve `GetOutboxInvoiceList` listeleme çağrıları gerçek test WSDL model adlarıyla salt-okunur çalışacak şekilde eklendi.
- Fatura senkronizasyonu, Uyumsoft durum değiştiren operasyonları ve Odoo yazma operasyonları uygulanmadı.

## MVP kapsamı

1. Uyumsoft gelen ve giden fatura listelerini çekme
2. UBL-TR XML ve PDF belgelerini indirme
3. ETTN bazlı mükerrer kayıt engelleme
4. Partner, ürün, vergi ve journal eşleştirme
5. Odoo'da taslak tedarikçi/müşteri faturası oluşturma
6. Hata, retry ve audit kayıtları
7. Customer Asset ve maliyet ilişkileri

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

Ayrıntılar için `docs/` ve Codex çalışma kuralları için `AGENTS.md` dosyasına bakın.
