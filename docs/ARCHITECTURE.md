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
3. Ham XML/PDF saklama
4. Parse ve doğrulama
5. Odoo'ya taslak fatura
6. Eşleştirme ekranları
7. Zamanlanmış senkronizasyon
8. Kontrollü canlı Uyumsoft geçişi

## Kritik kararlar

- Odoo Online nedeniyle entegrasyon harici servis olarak çalışır.
- Uyumsoft test ortamı MVP boyunca zorunludur.
- Durum değiştiren SOAP operasyonları varsayılan olarak kapalıdır.
- Odoo faturaları otomatik post edilmez.
