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
- Connector katmanı FastAPI `HTTPException` üretmemeli; sağlayıcı/domain exception üretmeli, HTTP eşlemesi API katmanında yapılmalı.
- Dış servis istemcileri mock edilebilir ve dependency injection ile değiştirilebilir olmalı.

## Definition of Done — zorunlu teslim akışı

Bir görev yalnızca kod yazıldığında bitmiş sayılmaz. Codex, ayrıca aşağıdaki teslim akışını tamamlamalıdır:

1. İlgili issue ve proje dokümanlarını okumalıdır.
2. Ana branch üzerinde doğrudan çalışmamalı; görev için ayrı bir branch oluşturmalıdır.
3. Kodu, migrationları, testleri ve gerekli dokümantasyonu tamamlamalıdır.
4. Lokal doğrulama komutlarını kendisi çalıştırmalıdır.
5. Docker tabanlı projelerde sistemi kendisi ayağa kaldırmalı ve health check yapmalıdır.
6. Test, lint, startup, migration veya Docker hatalarını kendisi düzeltmelidir.
7. Değişiklikleri anlamlı commit mesajlarıyla commit etmelidir.
8. Branch'i origin'e push etmelidir.
9. `main` branch'ine karşı bir draft pull request oluşturmalıdır.
10. PR açıklamasına özet, test sonuçları, güvenlik sınırları, bilinen kısıtlar, migration/rollback bilgisi ve ilgili issue bağlantısını eklemelidir.
11. Kullanıcıya yalnızca PR bağlantısı, yapılanların özeti, doğrulama sonuçları ve kalan gerçek risklerle dönmelidir.

Aşağıdakilerden biri eksikse görev tamamlanmış sayılmaz:

- testler çalıştırılmamışsa,
- lint başarısızsa,
- Docker uygulaması ayağa kalkmıyorsa,
- health check başarısızsa,
- branch push edilmemişse,
- draft PR oluşturulmamışsa.

Codex yalnızca şu durumlarda teslim akışını tamamlamadan durabilir:

- kullanıcıdan zorunlu bir iş kararı gerekiyorsa,
- erişim veya yetki eksikliği varsa,
- gerçek secret değeri olmadan ilerlemek teknik olarak imkânsızsa,
- geri döndürülemez veya üretim verisini etkileyen bir işlem için açık onay gerekiyorsa.

Bu durumda yalnızca engeli, nedenini ve kullanıcıdan gereken tek net aksiyonu bildirmelidir. Yapabileceği diğer işleri yine tamamlamalıdır.

## Zorunlu doğrulamalar

Projede mevcut komutlara göre eşdeğerleri kullanılabilir; varsayılan beklenti:

```bash
ruff check .
pytest

docker compose down --remove-orphans
docker compose up --build -d
docker compose ps
curl --fail http://localhost:8000/health
```

Migration içeren işlerde ayrıca:

```bash
alembic upgrade head
```

Gerekli servisler container içindeyse test ve lint komutları uygun biçimde `docker compose exec` ile çalıştırılmalıdır.

## PR teslim kriterleri

- Amaç ve kapsam açıklanmış olmalı.
- Güvenlik etkisi belirtilmeli.
- Test komutları ve sonuçları yazılmalı.
- Migration gerekiyorsa Alembic migration eklenmeli.
- Geri alma yöntemi belirtilmeli.
- Bilinen kısıtlar ve kalan riskler açıkça yazılmalı.
- PR ilgili issue'yu `Closes #<issue>` ile bağlamalı.
- PR oluşturulmadan görev tamamlandı denmemeli.
