# Sprint 01 — Read-only Uyumsoft Invoice Retrieval

## Amaç

Uyumsoft test servisinden gelen ve giden fatura listelerini salt-okunur olarak çekmek, normalize etmek ve Integration Hub veritabanına idempotent biçimde kaydetmek.

## Kapsam

- `InboxInvoiceListQueryModel` ve `OutboxInvoiceListQueryModel` şemalarının kodla keşfi
- `TestConnection`, `WhoAmI`, `GetSystemDate` endpointleri
- `GetInboxInvoiceList` ve `GetOutboxInvoiceList` istemcileri
- Tarih aralığı ve sayfalama
- Normalize edilmiş fatura zarfı modeli
- ETTN tekillik kontrolü
- Dry-run ve salt-okunur güvenlik bariyerleri
- Birim testleri ve mock SOAP cevapları

## Kapsam dışı

- Odoo faturası oluşturma
- PDF/XML indirme
- `SetInvoicesTaken`
- Fatura gönderme/iptal
- Canlı Uyumsoft ortamı

## Kabul kriterleri

- Son 1 günlük gelen fatura listesi test ortamından okunabiliyor.
- Son 1 günlük giden fatura listesi test ortamından okunabiliyor.
- Aynı ETTN ikinci çalıştırmada yeni kayıt oluşturmuyor.
- SOAP fault kullanıcıya güvenli ve anlaşılır biçimde dönüyor.
- Hiçbir durum değiştiren Uyumsoft operasyonu çağrılmıyor.
- `pytest` ve `ruff check` başarılı.

## Bilinen doğrulanmış operasyonlar

- `GetInboxInvoiceList(query: InboxInvoiceListQueryModel)`
- `GetInboxInvoiceData(invoiceId: string)`
- `GetInboxInvoicePdf(invoiceId: string)`
- `GetOutboxInvoiceList(query: OutboxInvoiceListQueryModel)`
- `TestConnection()`
- `WhoAmI()`
- `GetSystemDate()`
