# KAPASİTE KONTROL GELİŞTİRME YOL HARİTASI

**Oluşturma:** 2026-08-20
**Son doğrulama:** 2026-10-01 (kodla karşılaştırıldı)
**Durum:** ✅ Faz 1-5 TAMAMLANDI. Kalan iş aşağıdaki "Açık işler" bölümünde.

> ⚠️ Bu dosyanın 2026-08-20 tarihli ilk hâli, henüz yazılmamış kodun
> taslak Python/JS bloklarını içeriyordu. O bloklar `a978c6d`
> ("align preflight target and solver constraints") ile hayata geçti ve
> gerçek implementasyon taslaktan farklı/daha ileri oldu. Bu sürüm
> taslakları atıp **kodda doğrulanmış** son durumu kaydeder.

---

## 📌 BAŞLANGIÇ SORUNU (çözüldü)

Kapasite kontrolü INFEASIBLE dönünce akış duruyordu ve kullanıcı ne
yapacağını bilmiyordu: detay yok, öneri yok, kısmi çözüm yok, iteratif
düzeltme yok.

**Hedef:** "INFEASIBLE → DUR" yerine "SIKINTILI NOKTALARI GÖSTER → ÖNER →
KULLANICI DÜZELTSİN".

---

## ✅ TAMAMLANAN FAZLAR (kodda doğrulandı)

### Faz 1 — Kısmi çözüm modu
Tam doluluk imkânsızsa boşluk minimize edilip "18/21 doldurulabilir"
raporlanıyor; INFEASIBLE artık ölü son değil.

- `kapasite.py:600` `_tam_doluluk_fizibilite_kontrolu` — minimum boşluk
  modelini koşar, `optimum_kanitlandi` ile kanıt/tahmin ayrımı yapar.
- `kapasite.py:186` `_kismi_cozum_payload` — `doldurulan_slot`,
  `bos_slot`, `objective_bound`, `solver_status`, `bos_slot_detaylari`.
- Frontend: `hazirlikKapasiteSonucuGoster` panelinde doldurulan/boş/alt
  sınır kutuları + `hazirlikKismiCizelgeTablosu` ile salt-okunur önizleme.

### Faz 1.2 — Boş slot neden analizi
Taslaktaki iki kategoriden (mazeret / ara gün) **yedi** kategoriye çıktı.

- `ortools_solver.py:1075` `_bos_slot_aciklamalari` — her boş (gün, slot)
  için aday başına TEK birincil engel: `mazeret`, `gorev_uygun_degil`,
  `zaten_atandi`, `hedef_dolu`, `ara_gun`, `ayri`, `serbest`.
  Hazır `aciklama` cümlesi + `sebepler` kırılımı döner.
- `kapasite.py:222` bunu `bos_slot_detaylari` olarak dışa verir.

### Faz 2 — Ara gün pencere analizi
- `kapasite.py:1265` `ara_gun_pencere_aciklari` — her (başlangıç, bitiş)
  penceresi için talep/üst kapasite/eksik; en kötü 10 pencere sıralı.
- `_pencere_kisi_ust_kapasitesi` (`kapasite.py:20`) ara gün istisnalarını
  hesaba katar.
- Frontend panelde `teshis.pencereler` olarak listelenir.
- NOT: Taslaktaki `darbogaz_personeller` (pencere başına kişi kırılımı)
  **yapılmadı** — boş slot neden analizi kişi bazlı bilgiyi zaten daha
  isabetli veriyor. Açık iş olarak aşağıda duruyor.

### Faz 3 — 12/12 bölme önerisi (112 profili)
- `ortools_solver.py:1285` `_yari_vardiya_onerisi` → `tur: 'yari_vardiya'`.
  Gece adayı önceki gün nöbette olamaz (sabah çıkan o akşam yazılmaz).
- Frontend: `hazirlikYariVardiyaMi` ile what-if/takas listelerinden
  ayrıştırılıp kendi kartında gösterilir.

### Faz 4 — İteratif test & düzeltme döngüsü
Taslaktaki "mazeret silinince 500ms sonra otomatik yeniden koş"
yaklaşımı yerine **girdi imzası** tabanlı, daha sağlam bir yol seçildi:

- `cozumGirdiImzasiOlustur()` + `cozumuGecersizKil(neden)` — kapasiteyi
  etkileyen her düzenleme önceki sonucu bayatlatır.
- Panelde `data-hazirlik-action="retry"` ("Tekrar analiz et") ve
  `"edit"` ("Verilere dön ve düzelt") butonları.
- `hazirlikKullaniciKarariBekle(girdiImzasi)` — kullanıcı veriyi
  değiştirirse bekleyen karar düşer.

### Faz 5 — Kullanıcı onay akışı
Taslaktaki `confirm()` yerine panel içi eylem butonları:

- `data-hazirlik-action="partial"` → "Bilinçli olarak kısmi çizelge
  oluştur". `hazirlikKapasiteEylemiUygula` içinde **yalnız INFEASIBLE**
  için açık; UNKNOWN önizlenebilir ama benimsenemez (kanıt yok).
- `kismiCozumBenimsendi` bayrağı sonuç katmanına akar.
- Ayrıca `minNobetAcigiOnayiAl()` (112) çözüm öncesi min nöbet açığı için
  ayrı onay ister.
- Güvenlik: panel `onclick=` kullanmaz, `addEventListener` ile bağlanır
  (Node testi bunu `assert.doesNotMatch(renderer, /onclick=/)` ile korur).

### Faz 6 — İnsan dili hata detayları (2026-10-01, commit 672788b)
Fizibilite kontrolünün INFEASIBLE dalları ham ID döndürüyordu
(`personel_id: 1, gun: 17`). Yedi dalın hepsi artık personel ADI + tam
cümle döndürür.

- `kapasite.py` `_ad()` / `_adlar()` yardımcıları.
- `aciklama` + `*_ad` / `*_adlar` alanları: `PERSONEL_YETERSIZ`,
  `MANUEL_KISI_GUN_CAKISMASI`, `MANUEL_SLOT_CAKISMASI`,
  `MANUEL_MAZERET_CAKISMASI`, `MANUEL_ARA_GUN_CAKISMASI`,
  `GUNLUK_MAZERET_KAPASITESI` (müsait + mazeretli adlar),
  `BIRLIKTE_GRUP_SLOT_CAKISMASI`. Mevcut ham alanlar korundu (additive).
- Frontend `hazirlikNedenAciklamasi()` (`index.html:8616`): `detay.aciklama`
  varsa onu, yoksa isimli alanlardan cümle kurar, hiçbiri yoksa boş döner
  (jenerik mesaj devrede kalır). Panelde escape edilerek basılır.
- ⚠️ Bu cümle backendde `a978c6d`'den beri bir dalda üretiliyordu ama
  panel yalnız `neden.mesaj` basıyordu — yani **ölü koddu**. Render
  bağlantısı bu fazda kuruldu.

---

## 🔭 AÇIK İŞLER (opsiyonel, öncelik sırasıyla)

1. **Pencere başına darboğaz personel kırılımı** — `ara_gun_pencere_aciklari`
   şu an yalnız sayı döndürüyor (talep/üst kapasite/eksik). Taslaktaki
   "bu pencerede en çok kaybı kim yapıyor" tablosu yok. Boş slot neden
   analizi kısmen ikame ediyor.
2. **Yönlü `alternatif_gorevler` + 3 kişilik zincir takas** — takas motoru
   (`_bos_slot_takas_onerileri`) şu an 1-taşıma ve 2-kişi swap üretiyor.
3. **Frontend kilit UI** — `planlayici.kilitli_hucre_atamalari` backend
   tarafı hazır, hücre/hafta/görev kilidi için arayüz yok.
4. **Büyük örnekte OPTIMAL kanıtı** — 50x31x6 hedef detay geçişi adaptif
   süre bütçesiyle FEASIBLE dönüyor; OPTIMAL kanıtlanmıyor.

---

## 🔧 TEKNİK NOTLAR

### Geriye uyumluluk
Tüm yeni alanlar additive. Ham `personel_id` / `personel_ids` /
`grup_boyutu` alanları korundu; frontend `aciklama` yoksa jenerik mesaja
düşer. Genel hastane profili 112 kurallarından etkilenmez.

### Kalite kapıları
```
cd functions && python _regression_test.py     # 72/72
cd functions && python -m py_compile *.py
npm test                                        # 67 (frontend 41 + config 7 + sync 19)
```
`tests/test_target_exceptions.py` pytest ister; bu ortamda pytest kurulu
değil, kapı listesinde sayılmıyor.

### Ölü kod dersi
Faz 6, backendin ürettiği bir alanın frontendde hiç okunmadığı bir
durumu ortaya çıkardı. Yeni bir `detay` alanı eklerken render tarafının
da bağlandığını Node testiyle doğrula — aksi halde iş yapılmış görünür
ama kullanıcıya ulaşmaz.
