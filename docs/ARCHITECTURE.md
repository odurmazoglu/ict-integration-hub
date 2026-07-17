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
