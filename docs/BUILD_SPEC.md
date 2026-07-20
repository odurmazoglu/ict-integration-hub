# ICT Integration Hub — Clean Build Specification

## Amaç

Projeyi eski uygulama kodunu taşımadan, temiz bir mimariyle sıfırdan oluşturmak.

Eski projeden yalnızca yerel environment profile değerleri korunacaktır. Gerçek `.env.local`, `.env.test`, `.env.production`, `.env.live-readonly` veya benzeri secret-bearing dosyalar hiçbir koşulda Git'e eklenmeyecektir.

## Hedef mimari

```text
Uyumsoft SOAP
      |
      v
ICT Integration Hub (FastAPI)
      |
      v
Odoo Online JSON-2 API
```

## Teknoloji yığını

- Python 3.12
- FastAPI
- Pydantic Settings
- SQLAlchemy 2.x
- Alembic
- PostgreSQL
- Zeep
- httpx
- pytest
- Docker / Docker Compose

## İlk sürüm kapsamı

### Çekirdek

- Uygulama başlangıcı
- `/health` endpointi
- Ortam bazlı konfigürasyon
- Yapılandırılmış loglama
- PostgreSQL bağlantısı
- Alembic migration altyapısı

### Odoo bağlantısı

- Odoo Online JSON-2 istemcisi
- Salt-okunur bağlantı testi
- Şirket bilgisini döndüren probe endpointi
- Timeout ve kontrollü hata yönetimi

### Uyumsoft bağlantısı

- Test/production WSDL seçimi
- Zeep istemcisi
- `TestConnection`, `WhoAmI`, `GetSystemDate`
- WSDL operasyon ve tip keşfi
- Gelen/giden fatura sorgu modellerinin WSDL üzerinden okunması
- Salt-okunur gelen/giden fatura listeleme

### Veri modeli

- `providers`
- `invoices`
- `sync_runs`
- Gerekirse `connector_events`

Fatura tekillik anahtarı:

```text
provider + direction + ettn
```

### Güvenlik

- Secret değerler sadece environment üzerinden alınır
- Secret veya token loglanmaz
- Gerçek environment profile dosyaları repoya eklenmez
- İlk sprintte durum değiştiren SOAP operasyonları çağrılmaz
- `SetInvoicesTaken`, `SendInvoice`, `Cancel*`, retry ve status-change operasyonları yasaktır

## Beklenen klasör yapısı

```text
app/
  api/
    routers/
  connectors/
    odoo/
    uyumsoft/
  core/
  db/
  models/
  schemas/
  services/
  main.py
alembic/
tests/
  unit/
  integration/
  fixtures/
.env.local.example
.env.test.example
.env.production.example
.env.live-readonly.example
Dockerfile
docker-compose.yml
pyproject.toml
README.md
```

## API hedefleri

```text
GET /health
GET /api/v1/connectors/odoo/probe
GET /api/v1/connectors/uyumsoft/operations
GET /api/v1/connectors/uyumsoft/identity
GET /api/v1/invoices/incoming
GET /api/v1/invoices/outgoing
```

## Test gereksinimleri

- Gerçek secret gerektirmeyen unit testler
- Zeep response fixture'ları
- Odoo JSON-2 mock testleri
- Idempotency testleri
- DB hazır olmadan başlayan API için retry testi
- Docker healthcheck

## Teslim koşulları

- `docker compose up --build -d` başarılı olmalı
- `/health` 200 dönmeli
- Testler tek komutla çalışmalı
- README kurulum adımlarını içermeli
- Tüm değişiklikler ayrı branch ve PR üzerinden gelmeli
- PR açıklamasında test sonuçları ve güvenlik notları bulunmalı
