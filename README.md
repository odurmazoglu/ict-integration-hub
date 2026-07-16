# ICT Integration Hub

ICT Teknoloji'nin Odoo Online ERP ortamı ile harici servisler arasında çalışan entegrasyon katmanı.

İlk connector: **Uyumsoft e-Fatura**

## Mevcut durum

- Odoo Online JSON-2 API bağlantısı doğrulandı.
- Uyumsoft test SOAP/WSDL bağlantısı doğrulandı.
- Odoo şirket kaydı okunabiliyor.
- Uyumsoft operasyon listesi okunabiliyor.
- Geliştirme test ortamında ve salt-okunur ilerliyor.

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
curl http://localhost:8080/health
```

Ayrıntılar için `docs/` ve Codex çalışma kuralları için `AGENTS.md` dosyasına bakın.
