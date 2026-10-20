# Odoo 335 İstisna Vergisi — Muhasebe Onay Formu

| | |
|---|---|
| Konu | KDV istisna kodu **335** için özel Odoo alış vergisinin oluşturulması |
| Sistem | Odoo 19 SaaS (Online), Şirket ID 1 |
| Hub sürümü (production) | `125cc4ca5db866e57e994c4a88c8c20c624b3bed` (T2-A) |
| Hub ayarı (production) | `VAT_EXEMPTION_PURCHASE_TAX_IDS` tanımsız (= `{}`) — otomatik 335 eşlemesi **devre dışı** |
| Belge durumu | **Muhasebe onayı bekleniyor** — K1–K5 için **önerilen yapılandırma** kaydedilmiştir; muhasebeci tarafından imzalanmamış ve onaylanmamıştır |
| Teknik referans | [VAT_EXEMPTION_335.md](VAT_EXEMPTION_335.md) |

> **Kapsam ve sınırlar**
> - Bu belge yalnızca bir **onay formudur**. Odoo'da kayıt oluşturmaz, değiştirmez ve vergi oluşturma
>   yetkisi **vermez**.
> - Bu formdaki "Önerilen yapılandırma" değerleri kullanıcı tarafından sağlanan muhasebe
>   yönlendirmesine ve Ek E'deki kaynaklara dayanır. **Muhasebeci onayı yerine geçmez.**
> - Verginin Odoo'da oluşturulması, bu form imzalandıktan sonra **ayrı bir onay** gerektirir.
> - Hub'da eşlemenin açılması (vergi ID'sinin ayara yazılması) ve faturaların yeniden sınıflandırılması
>   da **ayrı ayrı onay** gerektirir.
> - Yalnızca istisna kodu **335** kapsamdadır. **209, 351 ve diğer istisna kodları bu belgeyle
>   onaylanmaz** ve Hub'da bloke kalmaya devam eder.
> - Odoo'daki genel **vergi ID 6 ("0%") kullanılmamalıdır** (bkz. Bölüm 6).

---

## 1. Yönetici özeti — önerilen yapılandırma ve bekleyen muhasebe onayı

Hub'ın 335 istisnası için kullanacağı Odoo vergisinin adı, türü ve oranı iş tarafından onaylanmıştır
(Bölüm 2.1). Vergi oluşturulmadan önce muhasebe onayı gereken **beş konu** vardır. Bu konular için
kullanıcı tarafından sağlanan muhasebe yönlendirmesine dayanan bir **önerilen yapılandırma** aşağıda
kaydedilmiştir. Muhasebeci bu öneriyi onaylayana veya değiştirene kadar her karar **bekliyor**
durumundadır.

Bu konuların hiçbiri Hub tarafından okunamaz veya doğrulanamaz: Hub'ın Odoo erişimi; vergi dağılım
satırlarını, vergi gruplarını, hesapları ve vergi raporu etiketlerini kapsamaz. Bu nedenle doğruluklarının
tek güvencesi muhasebe onayıdır.

> **Önemli:** Mevcut genel %0 KDV raporlaması (vergi 6, PUR_BASE_0 / PUR_TAX_0) 335 istisnalı alımlar
> için **önerilmemektedir**. Önerilen yapılandırmada 335 vergisinin hiçbir KDV beyannamesi gridi/etiketi
> ve hiçbir vergi hesabı yoktur.

| # | Karar | Önerilen yapılandırma | Dayanak | Muhasebecinin kararı | Durum |
|---|---|---|---|---|---|
| K1 | **335 istisnalı alımın KDV raporlamasındaki gösterimi** | Alıcı, istisnalı kitap alımının matrahını ve sıfır KDV'yi **KDV-1 beyannamesinde göstermez** | Vergi hukuku sonucu (Bölüm 1.2-A, Ek E) | | ☐ Bekliyor |
| K2 | **Fatura ve iade dağılımı — vergi hesabı** | Fatura ve iade vergi satırında hesap **boş**; **191000 kullanılmaz** | Odoo yapılandırma önerisi (Bölüm 1.2-B) | Fatura: ____ İade: ____ | ☐ Bekliyor |
| K3 | **Fatura ve iade dağılımı — matrah ve vergi gridleri/etiketleri** | Fatura ve iade, matrah ve vergi satırlarında grid/etiket **boş** | K1'in Odoo karşılığı (Bölüm 1.2-B) | Fatura matrah/vergi: ____ İade matrah/vergi: ____ | ☐ Bekliyor |
| K4 | **Vergi grubu** | Mevcut **"VAT %0" (grup ID 4)** | Odoo yapılandırma önerisi; muhasebeci tarafından ayrıca onaylanmamıştır | | ☐ Bekliyor |
| K5 | **Faturadaki etiket** (`invoice_label`) | **"KDV İstisna 335"** | Odoo yapılandırma önerisi | | ☐ Bekliyor |

Ayrıntılı alan bazlı karar tablosu: **Ek B**. Kaynaklar: **Ek E**.

### 1.1 Muhasebe onayı

Bu bölüm muhasebeci tarafından doldurulur. Önerilen yapılandırma bu alanlara **önceden yazılmamıştır**.

| Alan | Değer |
|---|---|
| Onaylayan (Ad Soyad) | |
| Unvan | |
| Tarih | |
| K1 — KDV raporlamasındaki gösterim | |
| K2 — Onaylanan vergi hesabı (fatura / iade) | |
| K3 — Onaylanan gridler (fatura matrah / vergi; iade matrah / vergi) | |
| K4 — Onaylanan vergi grubu | |
| K5 — Onaylanan faturadaki etiket | |
| Önerilen yapılandırma (Bölüm 1, Ek B) değişiklik olmadan kabul edildi mi? | ☐ Evet ☐ Hayır (farklı değerleri yukarıya yazın) |
| Ek B'deki diğer kararlar onaylandı mı? | ☐ Evet ☐ Hayır |
| İmza | |

Onay beyanı: *Yukarıdaki yapılandırmanın, yalnızca istisna kodu 335 (Basılı Kitap ve Süreli Yayınların
Teslimleri) kapsamındaki alımların muhasebe kaydı ve KDV raporlaması için doğru olduğunu onaylarım. Bu
onay, verginin Odoo'da oluşturulması için ayrıca verilecek onayın yerine geçmez ve 209 veya 351 kodlarını
kapsamaz.*

### 1.2 Vergi hukuku sonuçları ve Odoo yapılandırma önerileri

Aşağıdaki iki tablo birbirinden ayrıdır. **A** tablosu vergi mevzuatına ilişkin sonuçları, **B** tablosu
bu sonuçların Odoo'da nasıl yapılandırılmasının önerildiğini gösterir. B'deki öneriler A'daki sonuçlardan
türetilmiştir; mevzuat kaynağı değildir.

**A — Vergi hukuku sonuçları** (kaynak: Ek E)

| # | Sonuç | Kaynak |
|---|---|---|
| A1 | Basılı kitap ve süreli yayınların teslimi KDV'den istisnadır (3065 sayılı KDVK 13/n). | E2 |
| A2 | Bu teslimler **satıcı** tarafından KDV beyannamesinde 335 kodlu "Basılı Kitap ve Süreli Yayınların Teslimleri" satırında beyan edilir. 335 kodlu satır satıcının teslim beyanıdır. | E2 |
| A3 | **Alıcı**, istisnalı kitap alımının matrahını ve sıfır KDV'yi KDV-1 beyannamesinde göstermez. | E1, E3 |
| A4 | İstisnalı alımda alıcıya yansıtılmış KDV yoktur; dolayısıyla indirilecek KDV oluşmaz. | E1, E3 |

**B — Odoo yapılandırma önerileri**

| # | Öneri | Gerekçe |
|---|---|---|
| B1 | Fatura ve iade dağılımında matrah ve vergi gridleri/etiketleri **boş** | A3: alım KDV-1'e yansımamalıdır; boş grid, Odoo KDV raporuna hiçbir tutar taşımaz |
| B2 | Fatura ve iade vergi satırında vergi hesabı **boş**; **191000 kullanılmaz** | A4: indirilecek KDV yoktur; 191 hesabına sıfır tutarlı kayıt önerilmez |
| B3 | Vergi grubu: mevcut "VAT %0" (grup ID 4) | Yalnızca fatura üzerindeki vergi özet gösterimini etkiler; muhasebeci tarafından ayrıca onaylanmamıştır |
| B4 | Faturadaki etiket: "KDV İstisna 335" | Fatura üzerinde istisnanın açıkça görünmesi |
| B5 | Alış gider ve stok hesapları bu vergiden **bağımsızdır** | Gider/stok hesabı ürün, ürün kategorisi veya fatura satırı hesabından gelir; %0 vergi yapılandırması bu hesapları belirlemez veya değiştirmez. Odoo'da vergi satırının hesabı boş bırakıldığında vergi tutarı matrah satırının hesabına yazılır; tutar 0 olduğundan gider/stok hesabında fark oluşmaz |

---

## 2. Bilgilerin sınıflandırılması

### 2.1 Daha önce onaylanmış iş gereksinimleri

| Alan | Değer |
|---|---|
| İstisna kodu | **335** — Basılı Kitap ve Süreli Yayınların Teslimleri |
| Vergi adı (`name`) | **KDV İstisna — 335 — Basılı Kitap** |
| Vergi türü (`type_tax_use`) | Alışlar (`purchase`) |
| Vergi hesaplama (`amount_type`) | Fiyatın Yüzdesi (`percent`) |
| Tutar (`amount`) | **0 %** |
| Şirket | ID 1 |
| Fiyata dahil | **Hayır** |
| Alt vergiler | **Yok** |
| Onaylanmayan kodlar | 209 ve 351 (bloke kalır) |

### 2.2 Doğrulanmış mevcut Odoo ayarları

Kaynak: production Odoo'nun Hub istemcisiyle salt-okunur incelemesi, 19.10.2026. Hiçbir kayıt
değiştirilmedi.

| Konu | Doğrulanan durum |
|---|---|
| 335 / istisna adlı vergi | **Yok** |
| Vergi sayısı | 44 vergi (15 satış, 15 alış, 14 `none` türünde stopaj alt vergisi); 0 arşivlenmiş |
| Vergi 6 "0%" | Alış, yüzde, grup 4 "VAT %0"; **hiç kullanılmamış** (0 yevmiye satırı) |
| Vergi 34 (%20 alış) | Matrah PUR_BASE_20; vergi satırı 191000 İndirilecek KDV + PUR_TAX_20 |
| Varsayılan alış vergisi | Şirket varsayılanı 34; 161 ürün şablonunun tamamı yalnızca 34 kullanıyor |
| Mali pozisyonlar | 0 |
| Alış vergilerinde fiyata dahil | Hepsi hayır; şirket ayarı fiyat hariç (`tax_excluded`) |
| Hub'ın normal KDV eşlemesi | %1 → 36, %10 → 35, %20 → 34 |

**Doğrulanmamış (Hub erişimi dışında):**
- vergi 6'nın production'daki gerçek dağılım satırları, hesabı ve etiketleri;
- PUR_BASE_0 / PUR_TAX_0 etiketlerinin KDV raporundaki karşılığı;
- vergi gruplarının hesap ayarları.

Odoo'nun l10n_tr şablonunda vergi 6, PUR_BASE_0 + 191 hesabı + PUR_TAX_0 ile tanımlıdır. Ancak bu
yalnızca şablon bilgisidir ve production'da doğrulanmamıştır. Önerilen yapılandırma bu etiketleri ve 191
hesabını **kullanmadığı** için bu doğrulanmamış tanımlara bağımlı değildir.

### 2.3 Önerilen ayarlar (onay değildir)

| Alan | Öneri | Gerekçe |
|---|---|---|
| Dağılım yüzdeleri | Matrah %100, vergi %100 (fatura ve iade) | Standart; negatif faktör Hub tarafından reddedilir |
| Vergi hesabı (fatura ve iade vergi satırı) | **Boş** — 191000 kullanılmaz | Bölüm 1.2-B2 |
| Matrah ve vergi gridleri/etiketleri (fatura ve iade) | **Boş** | Bölüm 1.2-B1 |
| Vergi grubu (`tax_group_id`) | Mevcut "VAT %0" (grup ID 4) | Bölüm 1.2-B3 |
| Faturadaki etiket (`invoice_label`) | "KDV İstisna 335" | Bölüm 1.2-B4 |
| Vergi kapsamı (`tax_scope`) | Boş | Mevcut alış vergileriyle aynı |
| `is_base_affected` | İşaretli | Mevcut alış vergileriyle aynı |
| UBL/EDI istisna alanları | Boş | Odoo'daki AB VATEX listesi 335'i içermez |
| Açıklama (`description`) | "Basılı Kitap ve Süreli Yayınların Teslimleri (KDV istisna kodu 335)" | Bilgi amaçlı |
| Varsayılan vergi ataması | **Yok** — hiçbir ürün, kategori, şirket varsayılanı veya mali pozisyon | Bölüm 5 |
| Mali pozisyon / yerine geçen / orijinal vergi eşlemesi | **Yok** | Ek A |

### 2.4 Bekleyen muhasebe kararları

Bölüm 1'deki K1–K5 ve Ek B'deki ayrıntılı kararlar. Her biri için önerilen yapılandırma kaydedilmiştir;
hiçbiri muhasebeci tarafından onaylanmamıştır.

---

## 3. Amaç ve iş bağlamı

ICT Integration Hub, Uyumsoft'tan gelen e-Faturaları Odoo'da **taslak** tedarikçi faturasına dönüştürür.
KDV satırları, faturadaki oran ile mevcut Odoo alış vergilerine eşlenir.

KDV istisnalı faturalarda KDV tutarı 0'dır ve satırda istisna kodu bulunur. Bu faturalar bugün Hub'da
güvenli şekilde **bloke** edilir ve manuel incelemeye düşer (`UNSUPPORTED_INVOICE_CONTENT`). Bunun
nedeni, onaylanmış bir istisna vergisinin bulunmamasıdır.

Hub'ın 335 eşlemesi production'a **devre dışı** olarak yüklenmiştir (19.10.2026). Eşleme açılsa bile Hub
bu vergiyi bir faturaya yalnızca şu koşullar sağlanırsa uygular:

- orijinal UBL belgesi hash ve UUID ile doğrulanmıştır;
- fatura türü `ISTISNA`'dır;
- tüm satırlar `0015` / `335` / KDV tutarı 0 yapısındadır;
- belge yapısal olarak tamdır.

Ayrıca Hub her Vendor Bill işleminden önce vergiyi Odoo'dan yeniden okur ve Ek A'daki koşullarla
doğrular.

## 4. Referans fatura

| Alan | Değer |
|---|---|
| Fatura no | EFF2026000001009 |
| InvoiceTypeCode / ProfileID | ISTISNA / TICARIFATURA |
| Satır | 1 — ODTÜ More To Read I (Birinci Kitap) |
| TaxTypeCode / TaxExemptionReasonCode | 0015 / 335 |
| TaxExemptionReason (metin) | "Muafiyet uygulanmıştır" |
| Percent | Belgede yok |
| TaxableAmount / TaxAmount | 419,49 / 0 |
| TaxExclusiveAmount = TaxInclusiveAmount | 419,49 = 419,49 |
| Tevkifat | Yok |

Hub kontrolü (salt-okunur, 19.10.2026): belge doğrulandı, **yapısal olarak uygun**. Production'daki 66
belgeden yalnızca bu fatura uygundur:
- 209 kodlu fatura: kod onaylı değil;
- 351 kodlu fatura: türü SATIS.

Not: bu fatura ayrıca tedarikçi ve ürün eşleşmesi eksik olduğu için (`SUPPLIER_NOT_FOUND`,
`PRODUCT_NOT_FOUND`) vergi oluşturulsa bile tek başına yürütülebilir olmayacaktır.

## 5. Uyarı — Varsayılan vergi olarak atanmamalı

> ⚠️ Bu vergi **hiçbir** ürüne, ürün kategorisine, şirket varsayılan alış vergisine veya mali
> pozisyona **varsayılan alış vergisi olarak atanmamalıdır.** Yalnızca Hub tarafından, doğrulanmış 335
> istisnalı faturalarda kullanılmak üzere tanımlanır. Varsayılan olarak atanırsa, istisnası olmayan
> alımlar yanlışlıkla %0 ile kaydedilebilir.

## 6. Uyarı — Genel vergi 6 kullanılmamalı

> ⚠️ Odoo'daki genel **vergi ID 6 ("0%")** 335 istisnası için **kullanılmamalı**. Bu amaçla yeniden
> adlandırılmamalı veya değiştirilmemelidir. Hub, vergi 6'yı istisna vergisi olarak kodda kesin olarak
> reddeder. 335 için Bölüm 2.1'deki adla **yeni ve ayrı** bir vergi oluşturulmalıdır.

---

## Ek A — Hub'ın teknik gereksinimleri

Hub vergiyi tam ID ile okur. Aşağıdaki koşullardan biri sağlanmazsa vergiyi reddeder ve fatura bloke
kalır. Bunlar muhasebe kararı değildir; vergi oluşturulurken uyulması gereken teknik koşullardır.

| Alan | Gerekli değer |
|---|---|
| `id` | 6 olmamalı; başka bir istisna koduyla paylaşılmamalı |
| `name` | Ayrı bir sözcük olarak `335` içermeli — **onaylı adı değiştirmeyin** |
| `active` | Evet |
| `company_id` | 1 |
| `country_code` | TR |
| `type_tax_use` / `amount_type` / `amount` | `purchase` / `percent` / 0 |
| `children_tax_ids` | Boş |
| `price_include` | Hayır |
| `price_include_override` | Varsayılan (boş) veya Vergi Hariç |
| `include_base_amount` | Hayır |
| `tax_exigibility` | Faturaya göre (`on_invoice`) |
| Negatif dağılım faktörü (`has_negative_factor`) | Olmamalı |
| `fiscal_position_ids`, `original_tax_ids`, `replacing_tax_ids` | Boş |

Hub'ın **doğrulayamadığı** alanlar: dağılım satırları, vergi raporu gridleri/etiketleri, vergi hesabı,
vergi grubu ve grubun hesapları. Bunlar yalnızca muhasebe onayı ile güvence altındadır.

## Ek B — Ayrıntılı karar tablosu

"Önerilen yapılandırma" sütunu kullanıcı tarafından sağlanan muhasebe yönlendirmesine dayanır.
"Muhasebecinin seçtiği değer" ve "Onay durumu" sütunları muhasebeci tarafından doldurulur.

| # | Alan | Önerilen yapılandırma | Muhasebecinin seçtiği değer | Onay durumu |
|---|---|---|---|---|
| 1 | Vergi grubu (`tax_group_id`) — K4 | Mevcut "VAT %0" (grup ID 4) | | ☐ Bekliyor |
| 2 | Faturadaki etiket (`invoice_label`) — K5 | "KDV İstisna 335" | | ☐ Bekliyor |
| 3 | Açıklama (`description`) | Bölüm 2.3'teki öneri | | ☐ Bekliyor |
| 4 | Fatura dağılımı — Matrah % | 100 % | | ☐ Bekliyor |
| 5 | Fatura dağılımı — Vergi % | 100 % (tutar 0 olduğundan satır 0 TL) | | ☐ Bekliyor |
| 6 | Fatura dağılımı — Vergi hesabı — K2 | **Boş** (191000 kullanılmaz) | | ☐ Bekliyor |
| 7 | Fatura dağılımı — Matrah gridi/etiketi — K3 | **Boş** | | ☐ Bekliyor |
| 8 | Fatura dağılımı — Vergi gridi/etiketi — K3 | **Boş** | | ☐ Bekliyor |
| 9 | İade dağılımı — Matrah % | 100 % | | ☐ Bekliyor |
| 10 | İade dağılımı — Vergi % | 100 % | | ☐ Bekliyor |
| 11 | İade dağılımı — Vergi hesabı — K2 | **Boş** (191000 kullanılmaz) | | ☐ Bekliyor |
| 12 | İade dağılımı — Matrah gridi/etiketi — K3 | **Boş** | | ☐ Bekliyor |
| 13 | İade dağılımı — Vergi gridi/etiketi — K3 | **Boş** | | ☐ Bekliyor |
| 14 | Sıfır tutarlı vergi satırında 191000 kullanılsın mı? — K2 | **Hayır** | | ☐ Bekliyor |
| 15 | PUR_BASE_0 / PUR_TAX_0 bu istisnalı alım için uygun mu? — K1/K3 | **Hayır** — alıcı KDV-1'de göstermez; gridler boş kalır | | ☐ Bekliyor |
| 16 | Vergi kapsamı (`tax_scope`) | Boş | | ☐ Bekliyor |
| 17 | UBL/EDI istisna alanları (`ubl_cii_tax_category_code`, `ubl_cii_tax_exemption_reason_code`) | Boş | | ☐ Bekliyor |

## Ek C — Odoo 19 oluşturma kontrol listesi (alan alan)

Yalnızca muhasebe onayı (Bölüm 1.1) ve vergi oluşturma için ayrıca verilen onaydan sonra uygulanır.
Değerler muhasebecinin Bölüm 1.1 / Ek B'de **onayladığı** değerlerdir; muhasebeci önerilen yapılandırmadan
farklı bir değer seçtiyse onaylanan değer kullanılır.

Yol: **Muhasebe → Yapılandırma → Vergiler → Yeni**. Türkçe arayüz etiketleri sürüme göre küçük
farklılık gösterebilir; parantez içinde teknik alan adları verilmiştir.

**Ana sekme**
- [ ] Vergi Adı (`name`): `KDV İstisna — 335 — Basılı Kitap`
- [ ] Vergi Türü (`type_tax_use`): Alışlar
- [ ] Vergi Hesaplama (`amount_type`): Fiyatın Yüzdesi
- [ ] Aktif (`active`): işaretli
- [ ] Tutar (`amount`): 0,0000 %
- [ ] Vergi Kapsamı (`tax_scope`): Ek B #16 (önerilen: boş)
- [ ] Şirket (`company_id`): Şirket ID 1

**Dağılım — Faturalar (`invoice_repartition_line_ids`)**
- [ ] Matrah satırı: % = Ek B #4 (100); Etiket = Ek B #7 (önerilen: boş)
- [ ] Vergi satırı: % = Ek B #5 (100, pozitif); Hesap = Ek B #6 (önerilen: boş, 191000 değil);
      Etiket = Ek B #8 (önerilen: boş)
- [ ] Ek satır **eklenmemeli**; negatif faktör **olmamalı**

**Dağılım — İadeler (`refund_repartition_line_ids`)**
- [ ] Matrah satırı: % = Ek B #9 (100); Etiket = Ek B #12 (önerilen: boş)
- [ ] Vergi satırı: % = Ek B #10 (100, pozitif); Hesap = Ek B #11 (önerilen: boş, 191000 değil);
      Etiket = Ek B #13 (önerilen: boş)
- [ ] Ek satır **eklenmemeli**; negatif faktör **olmamalı**

**Gelişmiş Seçenekler sekmesi**
- [ ] Faturadaki Etiket (`invoice_label`): Ek B #2 (önerilen: "KDV İstisna 335")
- [ ] Vergi Grubu (`tax_group_id`): Ek B #1 (önerilen: "VAT %0", grup ID 4)
- [ ] Ülke (`country_id`): Türkiye
- [ ] Fiyata Dahil (`price_include_override`): Varsayılan / Vergi Hariç
- [ ] Sonraki Vergilerin Matrahını Etkiler (`include_base_amount`): **işaretsiz**
- [ ] Önceki Vergilerden Etkilenen Matrah (`is_base_affected`): işaretli (Bölüm 2.3)
- [ ] Vergi Vadesi (`tax_exigibility`, yalnızca nakit esası açıksa görünür): Faturaya göre
- [ ] Açıklama (`description`): Ek B #3
- [ ] UBL/EDI istisna alanları: Ek B #17 (boş)
- [ ] Alt vergi **eklenmemeli**
- [ ] Mali pozisyon, yerine geçen veya orijinal vergi eşlemesi **eklenmemeli**

**Kaydet** → oluşan **vergi ID'sini** not edin (URL'deki `id=` değeri): __________

## Ek D — Oluşturma sonrası doğrulama kontrol listesi

**Odoo'da (muhasebe)**
- [ ] Ad tam olarak `KDV İstisna — 335 — Basılı Kitap`
- [ ] Tür Alışlar, hesaplama Yüzde, tutar 0, aktif, şirket 1, ülke Türkiye
- [ ] Fatura ve iade dağılımı satırları Bölüm 1.1 / Ek B'deki onaylı değerlerle birebir aynı
- [ ] Onaylı değerler önerilen yapılandırmayla aynıysa: hiçbir dağılım satırında grid/etiket **yok**
- [ ] Onaylı değerler önerilen yapılandırmayla aynıysa: vergi satırlarında hesap **boş**; 191000 veya başka
      bir 191 hesabı **atanmamış**
- [ ] Vergi grubu ve faturadaki etiket onaylı değerlerle aynı
- [ ] Hiçbir ürün, ürün kategorisi, şirket varsayılanı veya mali pozisyon bu vergiyi kullanmıyor (Bölüm 5)
- [ ] Vergi 6 "0%" değiştirilmedi (Bölüm 6)
- [ ] Bu vergiyle hiçbir fatura kaydedilmedi (test faturası dahil)

**Hub'da (BT, her adım ayrı onayla)**
- [ ] Vergi ID'sinin 6 olmadığı doğrulandı
- [ ] Hub'ın salt-okunur doğrulaması Ek A'daki tüm koşullardan geçti
- [ ] Ancak bundan sonra, ayrı onayla: `VAT_EXEMPTION_PURCHASE_TAX_IDS={"335": <vergi ID>}` ayarlanır ve
      `api` ile `poller` yeniden başlatılır
- [ ] Referans faturanın yeniden sınıflandırılması ayrı bir onayla yapılır

Geri alma: Hub ayarının yeniden boşaltılması, istisna eşlemesini kapatır ve istisna vergili tüm Vendor
Bill işlemlerini bloke eder. Odoo vergisinin silinmesi veya arşivlenmesi gerekmez.

## Ek E — Kaynaklar

| # | Kaynak | Kullanıldığı yer | Not |
|---|---|---|---|
| E1 | İSMMMO danışmanlık cevabı — 335 istisnalı kitap alımları | A3, A4 | Kullanıcı tarafından referans verilmiştir. Metin bu belgede çoğaltılmamış ve Hub tarafından bağımsız olarak doğrulanmamıştır. |
| E2 | GİB — 3065 sayılı KDV Kanunu 13/n ve KDV Genel Uygulama Tebliği'nde Değişiklik Yapılmasına Dair Tebliğ (Seri No: 25): basılı kitap ve süreli yayın teslimlerinin 335 kodlu satırda beyanı | A1, A2 | Satıcının beyanına ilişkindir; alıcı tarafının raporlamasını düzenlemez. |
| E3 | Kullanıcı tarafından sağlanan muhasebe yönlendirmesi (20.10.2026): alıcı KDV-1'de göstermez; gridler boş; 191000 kullanılmaz; Odoo vergi hesabı boş | A3, A4, B1, B2 | Muhasebeci imzalı onay **değildir**. |

Bölüm 1.2-B'deki Odoo yapılandırma önerileri bu kaynaklardan türetilmiştir. Hiçbir kaynak doğrudan bir
Odoo ayarı öngörmez.
