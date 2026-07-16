# AGENTS.md

## Proje amacı

ICT Integration Hub, Odoo Online ile Uyumsoft ve ileride eklenecek diğer harici servisler arasında güvenli bir entegrasyon katmanıdır.

## Şu anki öncelik

Yalnızca Uyumsoft test ortamından gelen/giden e-Fatura verilerini salt-okunur çekmek ve Integration Hub veritabanında güvenli biçimde saklamak.

## Değişmez kurallar

- Gerçek kimlik bilgilerini, API anahtarlarını, VKN dışındaki gizli verileri veya `.env` dosyasını commit etme.
- Uyumsoft test ortamını kullan.
- `SetInvoicesTaken`, `SendInvoice`, `Cancel*`, `RetrySendInvoices`, `MoveToDraftStatus` gibi durum değiştiren operasyonları uygulama veya çağırma.
- Odoo'da ilk aşamada yalnız taslak kayıt oluştur; `action_post` çağırma.
- ETTN için idempotency zorunludur.
- API ve SOAP hatalarını yutma; yapılandırılmış log ve anlaşılır hata üret.
- Yeni bağımlılık eklerken gerekçesini PR açıklamasında belirt.
- Her iş için test ekle.

## Mimari sınırlar

- `app/connectors/uyumsoft`: SOAP/WSDL erişimi ve sağlayıcıya özgü DTO dönüşümü
- `app/connectors/odoo`: Odoo JSON-2 API erişimi
- `app/services`: iş akışları, idempotency ve eşleştirme
- `app/models`: kalıcı Integration Hub kayıtları
- `app/api`: HTTP endpointleri; iş mantığı burada tutulmaz
- `tests`: birim ve entegrasyon testleri

## Kod standartları

- Python 3.12 type hints kullan.
- Fonksiyonlar küçük ve tek amaçlı olsun.
- `ruff` ve `pytest` başarılı olmalı.
- SOAP yanıtlarını doğrudan uygulama geneline yayma; normalize edilmiş dataclass/Pydantic modellerine dönüştür.
- Tarih ve saatlerde timezone-aware değer kullan.
- Parasal değerlerde `Decimal` kullan.

## PR teslim kriterleri

- Amaç ve kapsam açıklanmış olmalı.
- Güvenlik etkisi belirtilmeli.
- Test komutları ve sonuçları yazılmalı.
- Migration gerekiyorsa Alembic migration eklenmeli.
- Geri alma yöntemi belirtilmeli.
