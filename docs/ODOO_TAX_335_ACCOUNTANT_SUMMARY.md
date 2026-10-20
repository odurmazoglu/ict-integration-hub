# KDV İstisna 335 — Muhasebe Onay Özeti

| | |
|---|---|
| Konu | KDV istisna kodu **335** için özel Odoo alış vergisi |
| Şirket | ICT Teknoloji — Odoo Şirket ID 1 |
| Belge durumu | **Muhasebe onayı bekleniyor** — K1–K5 için önerilen yapılandırma kaydedilmiştir; hiçbiri muhasebeci tarafından onaylanmamıştır |
| Ayrıntılı form | [ODOO_TAX_335_ACCOUNTING_APPROVAL.md](ODOO_TAX_335_ACCOUNTING_APPROVAL.md) |

> Bu özet yalnızca muhasebe kararlarının toplanması içindir. Odoo'da vergi oluşturma yetkisi
> **vermez**. Verginin oluşturulması ve Hub'da kullanıma açılması ayrı onaylar gerektirir.
> Yalnızca istisna kodu **335** kapsamdadır; 209, 351 ve diğer kodlar bu özetle onaylanmaz.
>
> "Önerilen yapılandırma" değerleri kullanıcı tarafından sağlanan muhasebe yönlendirmesine ve aşağıdaki
> kaynaklara dayanır; **muhasebeci onayı yerine geçmez**.
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

> ⚠️ **PUR_BASE_0, PUR_TAX_0 ve 191000 hesabı önerilmemektedir.** Önerilen yapılandırmada 335
> vergisinin hiçbir KDV beyannamesi gridi/etiketi ve hiçbir vergi hesabı yoktur.

> ⚠️ Bu vergi hiçbir ürüne, ürün kategorisine, şirket varsayılanına veya mali pozisyona varsayılan
> vergi olarak atanmamalıdır. Mali pozisyon, yerine geçen veya orijinal vergi eşlemesi eklenmemelidir.

## 4. Referans fatura

| Alan | Değer |
|---|---|
| Fatura no | **EFF2026000001009** |
| Fatura türü / Profil | ISTISNA / TICARIFATURA |
| Satır | ODTÜ More To Read I (Birinci Kitap) |
| Vergi kodu / İstisna kodu | 0015 / 335 |
| Matrah / KDV | 419,49 / 0 |
| Tevkifat | Yok |

## 5. Vergi hukuku sonuçları ve Odoo yapılandırma önerileri

**Vergi hukuku sonuçları** (kaynak: Bölüm 8)

- Basılı kitap ve süreli yayınların teslimi KDV'den istisnadır (KDVK 13/n). Bu teslimler **satıcı**
  tarafından KDV beyannamesinde 335 kodlu satırda beyan edilir.
- **Alıcı**, istisnalı kitap alımının matrahını ve sıfır KDV'yi **KDV-1 beyannamesinde göstermez**.
- İstisnalı alımda indirilecek KDV oluşmaz.

**Odoo yapılandırma önerileri** (yukarıdaki sonuçlardan türetilmiştir; mevzuat kaynağı değildir)

- Fatura ve iade dağılımında matrah ve vergi gridleri **boş**.
- Fatura ve iade vergi satırında vergi hesabı **boş**; **191000 kullanılmaz**.
- Alış gider ve stok hesapları bu vergiden bağımsızdır; ürün, ürün kategorisi veya fatura satırı
  hesabından gelir. %0 vergi yapılandırması bu hesapları değiştirmez.

## 6. Muhasebenin onaylaması gereken konular

Her konu için **önerilen yapılandırma** yazılmıştır. "Muhasebenin kararı" alanları muhasebeci tarafından
doldurulur; öneri kabul ediliyorsa aynı değer yazılabilir.

### K1 — KDV beyannamesinde raporlama

335 istisnalı alımın KDV raporlamasında (beyannamede) nerede ve nasıl gösterileceği.

- **Önerilen yapılandırma:** Alıcı, istisnalı kitap alımının matrahını ve sıfır KDV'yi KDV-1
  beyannamesinde göstermez. *(Vergi hukuku sonucu.)*

**Muhasebenin kararı:** ______________________________________________

### K2 — Fatura ve iade vergi hesapları

Tutarı 0 olan vergi satırının hangi hesaba bağlanacağı (fatura ve iade için ayrı ayrı).

- **Önerilen yapılandırma:** Fatura: **boş** · İade: **boş** — 191000 kullanılmaz. *(Odoo yapılandırma
  önerisi.)*

**Muhasebenin kararı:** Fatura: ____________________ İade: ____________________

### K3 — Fatura ve iade vergi gridleri / etiketleri

Matrah ve vergi satırlarının KDV raporunda hangi grid/etiketlerle izleneceği (fatura ve iade için).

- **Önerilen yapılandırma:** Fatura matrah/vergi: **boş / boş** · İade matrah/vergi: **boş / boş**.
  PUR_BASE_0 / PUR_TAX_0 kullanılmaz. *(K1'in Odoo karşılığı.)*

**Muhasebenin kararı:**
Fatura — matrah: ______________ vergi: ______________
İade — matrah: ______________ vergi: ______________

### K4 — Vergi grubu

Verginin hangi vergi grubuna bağlanacağı.

- **Önerilen yapılandırma:** Mevcut **"VAT %0" (grup ID 4)**. *(Odoo yapılandırma önerisi; muhasebeci
  tarafından ayrıca onaylanmamıştır.)*

**Muhasebenin kararı:** ______________________________________________

### K5 — Faturadaki etiket

Faturada vergi satırında görünecek metin (`invoice_label`).

- **Önerilen yapılandırma:** **"KDV İstisna 335"**. *(Odoo yapılandırma önerisi.)*

**Muhasebenin kararı:** ______________________________________________

## 7. Muhasebe onayı

| Alan | Değer |
|---|---|
| Muhasebeci (Ad Soyad) | |
| Unvan | |
| Tarih | |
| K1–K5 kararları yukarıda eksiksiz dolduruldu | ☐ Evet ☐ Hayır |
| Önerilen yapılandırma değişiklik olmadan kabul edildi | ☐ Evet ☐ Hayır |
| Onay | ☐ Onaylıyorum ☐ Onaylamıyorum |
| İmza | |

*Yukarıdaki kararların yalnızca istisna kodu 335 kapsamındaki alımların muhasebe kaydı ve KDV
raporlaması için doğru olduğunu onaylarım. Bu onay, verginin Odoo'da oluşturulması için ayrıca verilecek
onayın yerine geçmez ve 209 veya 351 kodlarını kapsamaz.*

## 8. Kaynaklar

1. **İSMMMO danışmanlık cevabı** — 335 istisnalı kitap alımları (alıcı tarafı). Kullanıcı tarafından
   referans verilmiştir; metin bu belgede çoğaltılmamıştır.
2. **GİB** — 3065 sayılı KDV Kanunu 13/n ve KDV Genel Uygulama Tebliği'nde Değişiklik Yapılmasına Dair
   Tebliğ (Seri No: 25): basılı kitap ve süreli yayın teslimlerinin 335 kodlu satırda beyanı (satıcı
   tarafı).
3. **Kullanıcı tarafından sağlanan muhasebe yönlendirmesi** (20.10.2026) — muhasebeci imzalı onay
   değildir.
