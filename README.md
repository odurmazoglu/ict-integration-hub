# ICT Integration Hub

ICT Teknoloji'nin Odoo Online ERP ortamı ile harici servisler arasında çalışan entegrasyon katmanı.

İlk connector: **Uyumsoft e-Fatura**

## Mevcut durum

- Temiz FastAPI bootstrap oluşturuldu.
- Odoo Online JSON-2 API için salt-okunur probe eklendi.
- Uyumsoft test SOAP/WSDL için `TestConnection`, `WhoAmI`, `GetSystemDate` istemcileri eklendi.
- Uyumsoft WSDL operasyon keşfi yalnız geliştirme ortamında açıldı.
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

## Migration

```bash
alembic upgrade head
```

Ayrıntılar için `docs/` ve Codex çalışma kuralları için `AGENTS.md` dosyasına bakın.
