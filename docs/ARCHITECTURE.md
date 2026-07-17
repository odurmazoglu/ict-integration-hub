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
