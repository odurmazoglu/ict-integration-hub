# KDV İstisna 335 — Muhasebe Onay Özeti

| | |
|---|---|
| Konu | KDV istisna kodu **335** için özel Odoo alış vergisi |
| Şirket | ICT Teknoloji — Odoo Şirket ID 1 |
| Belge durumu | **Muhasebe onayı bekleniyor** — aşağıdaki kararların hiçbiri henüz onaylanmamıştır |
| Ayrıntılı form | [ODOO_TAX_335_ACCOUNTING_APPROVAL.md](ODOO_TAX_335_ACCOUNTING_APPROVAL.md) |

> Bu özet yalnızca muhasebe kararlarının toplanması içindir. Odoo'da vergi oluşturma yetkisi
> **vermez**. Verginin oluşturulması ve Hub'da kullanıma açılması ayrı onaylar gerektirir.
> Yalnızca istisna kodu **335** kapsamdadır; 209, 351 ve diğer kodlar bu özetle onaylanmaz.
>
> 335 istisna vergisinin otomatik eşleştirilmesi ICT Integration Hub production ortamında şu anda
> devre dışıdır. Muhasebe onayı, vergi oluşturma ve aktivasyon işlemleri ayrı ayrı tamamlanmadan
> özellik etkinleştirilmeyecektir.

## 1. Neden gerekli?

ICT Teknoloji'ye gelen KDV istisnalı (335 — Basılı Kitap ve Süreli Yayınların Teslimleri) alış
faturaları, Odoo'da uygun bir vergi olmadığı için bugün Hub'da bloke edilmekte ve manuel incelemeye
düşmektedir. Bu faturaların Odoo'ya doğru kaydedilebilmesi için **tek bir özel alış vergisi**
gereklidir:

**KDV İstisna — 335 — Basılı Kitap**

## 2. Daha önce onaylanmış teknik gereksinimler

| Alan | Değer |
|---|---|
| Vergi türü | Alış vergisi |
| Oran | %0 |
| Şirket | Şirket ID 1 |
| Fiyata dahil | Hayır |
| Alt vergiler | Yok |
| Kullanım | Yalnızca istisna kodu 335'e özel |

## 3. Uyarılar

> ⚠️ **Odoo'daki genel vergi ID 6 ("0%") yeniden kullanılmamalıdır.** Bu amaçla yeniden
> adlandırılmamalı veya değiştirilmemelidir; 335 için yeni ve ayrı bir vergi oluşturulmalıdır.

> ⚠️ **PUR_BASE_0, PUR_TAX_0 ve 191000 hesabı otomatik olarak onaylı değildir.** Aşağıda yalnızca
> seçenek olarak yer alırlar. PUR_BASE_0 / PUR_TAX_0 etiketlerinin production Odoo'daki fiili tanımı ve
> KDV raporundaki karşılığı doğrulanmamıştır; 335 istisnalı alımlar için uygunluğu muhasebe kararıdır.

> ⚠️ Bu vergi hiçbir ürüne, ürün kategorisine, şirket varsayılanına veya mali pozisyona varsayılan
> vergi olarak atanmamalıdır.

## 4. Referans fatura

| Alan | Değer |
|---|---|
| Fatura no | **EFF2026000001009** |
| Fatura türü / Profil | ISTISNA / TICARIFATURA |
| Satır | ODTÜ More To Read I (Birinci Kitap) |
| Vergi kodu / İstisna kodu | 0015 / 335 |
| Matrah / KDV | 419,49 / 0 |
| Tevkifat | Yok |

## 5. Muhasebenin karar vermesi gereken konular

Aşağıdaki seçenekler kaynak formdaki önerilerdir; **onay değildir**. Hub bu alanları Odoo'dan okuyup
doğrulayamaz; doğruluklarının tek güvencesi muhasebe onayıdır.

### K1 — KDV beyannamesinde raporlama

335 istisnalı alımın KDV raporlamasında (beyannamede) nerede ve nasıl gösterileceği.

- Seçenekler: Kaynak formda hazır seçenek yoktur — **muhasebe belirler**.

**Muhasebenin kararı:** ______________________________________________

### K2 — Fatura ve iade vergi hesapları

Tutarı 0 olan vergi satırının hangi hesaba bağlanacağı (fatura ve iade için ayrı ayrı).

- (a) 191000 İndirilecek KDV
- (b) Hesapsız (boş)
- (c) Diğer

**Muhasebenin kararı:** Fatura: ____________________ İade: ____________________

### K3 — Fatura ve iade vergi gridleri / etiketleri

Matrah ve vergi satırlarının KDV raporunda hangi grid/etiketlerle izleneceği (fatura ve iade için).

- (a) PUR_BASE_0 / PUR_TAX_0 (iadede iade yönü)
- (b) İstisnaya özel grid
- (c) Etiketsiz

**Muhasebenin kararı:**
Fatura — matrah: ______________ vergi: ______________
İade — matrah: ______________ vergi: ______________

### K4 — Vergi grubu

Verginin hangi vergi grubuna bağlanacağı.

- (a) Mevcut "VAT %0" (grup 4)
- (b) Yeni özel istisna grubu (ör. "KDV İstisna")

**Muhasebenin kararı:** ______________________________________________

### K5 — Faturadaki etiket

Faturada vergi satırında görünecek metin (`invoice_label`).

- (a) "KDV İstisna 335"
- (b) "%0 KDV (335 İstisna)"
- (c) Diğer

**Muhasebenin kararı:** ______________________________________________

## 6. Muhasebe onayı

| Alan | Değer |
|---|---|
| Muhasebeci (Ad Soyad) | |
| Unvan | |
| Tarih | |
| K1–K5 kararları yukarıda eksiksiz dolduruldu | ☐ Evet ☐ Hayır |
| Onay | ☐ Onaylıyorum ☐ Onaylamıyorum |
| İmza | |

*Yukarıdaki kararların yalnızca istisna kodu 335 kapsamındaki alımların muhasebe kaydı ve KDV
raporlaması için doğru olduğunu onaylarım. Bu onay, verginin Odoo'da oluşturulması için ayrıca verilecek
onayın yerine geçmez ve 209 veya 351 kodlarını kapsamaz.*
