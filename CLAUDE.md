# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Proje Özeti

**Nöbet Yapma Robotu Pro v2** — Nöbet çizelgeleme optimizasyon sistemi. Backend: Firebase Cloud Functions (Python 3.11), Frontend: monolitik vanilla JS. Tüm arayüz, değişken isimleri, yorumlar ve alan terminolojisi Türkçedir.

Firebase proje ID: `nobetyap-29acf`

## Derleme ve Dağıtım

Standart Firebase CLI ile dağıtılır (`firebase deploy`, `--only functions|hosting`, `firebase emulators:start`). Frontend için derleme adımı yoktur — `public/index.html` doğrudan sunulur. Test framework'ü yapılandırılmamıştır.

## Mimari

### Tek Motorlu Çözücü + İki Aşamalı Planlama

Sistem, iki AYRI OR-Tools CP-SAT modeli kullanır (eski greedy motor kaldırılmıştır):

1. **Hedef Modeli** (`hedef_hesaplayici.py` → `HedefHesaplayici`) — "kim kaç nöbet tutacak" sorusunu (kişi başı adil hedef sayıları) çözen CP-SAT modeli
2. **Çizelge Modeli** (`ortools_solver.py` → `NobetSolver`) — hedefleri gerçek takvime yerleştiren CP-SAT modeli ("kim, hangi gün, hangi slot")

Arada `gun_iskelet_planlayici.py` (sezgisel, OR-Tools kullanmaz) hedefleri günlere/rollere dağıtıp `PlanKontrati` üretir; bu, çizelge modeline yumuşak/sert "iskelet" kısıtları olarak beslenir. Üç endpoint de aynı planı `planlayici.ortak_plan_uret()` üzerinden üretir.

`nobet_coz` INFEASIBLE dönerse, `solve_strategy.py` **tanılama tabanlı gevşetme döngüsü** çalıştırır: kök nedeni teşhis et → kısıtları otomatik gevşet (ör. `ara_gun` azalt; exclusive/ayrı/birlikte kurallarını kaldır) → tekrar dene. **Greedy geri dönüş YOKTUR**; son çare tüm yumuşak kısıtların kaldırılmasıdır (`tum_soft_kaldir`).

⚠️ Bu döngü **yalnız çizelge modeli** içindir. **Hedef modeli** (`HedefHesaplayici`) çözümsüz kalırsa gevşetme yoktur — plan üretilmez, `nobet_coz` çözüme hiç başlamaz. Bu yüzden hedef modeline eklenen her kısıt ya güvenli bir üst sınır kesidi olmalı ya da soft/cezalı olmalıdır. Çözümsüzlükte `hedef_teshis.hedef_infeasible_insan_dili()` kök nedeni (`kapasite_yetersiz` / `gun_tipi_yetersiz` / `kurallar_celisiyor`) insan diline çevirip somut aksiyon önerir (ör. yeterli `ara_gun` değerini hesaplayıp söyler); ham CP-SAT debug'ı yalnız `hedef_tanisi.debug` içinde kalır, kullanıcıya gösterilmez.

### Cloud Function Endpoint'leri (main.py)

5 endpoint vardır: `nobet_dagit`, `nobet_kapasite`, `nobet_hedef_hesapla`, `nobet_coz`, `debug_event_log` (bellek/zaman aşımı ayarları `main.py` dekoratörlerinde). Not: Frontend şu an yalnızca `nobet_coz` ve `nobet_hedef_hesapla`'yı çağırır; `nobet_dagit` ve `nobet_kapasite` deploy edilir ama frontend'den kullanılmaz. Girdi boyut üst sınırları (`main.py`): `MAX_SLOT_SAYISI=50`, `MAX_PERSONEL=1000`, `MAX_GOREV=300` (OOM/DoS koruması).

### Frontend (public/index.html)

Tüm CSS, JS ve HTML'i içeren ~9.900 satırlık tek monolitik dosya. Firebase SDK v9.6.1 (auth, Firestore) kullanır. 6 adımlı sihirbaz arayüzü. Durum çift katmanlı saklanır: LocalStorage (senkron) + Firestore bulut senkronizasyonu (`users/{uid}/months/{yil}_{ay}`, 2sn debounce; yalnızca Google kullanıcıları). Ayrı bir `public/admin.html` debug paneli `debug_sessions`'ı okur (Google giriş + `firestore.rules` admin allowlist ile korumalı).

## Alan Kavramları

- **Nöbet** = vardiya/görev nöbeti; **Nöbetçi** = nöbetteki kişi
- **Görev** = atama slotu; **Görev Havuzu** = görev grubu
- **Personel** = personel/çalışan
- **Mazeret** = belirli bir gün için müsait olmama durumu
- **Ara gün** = bir kişinin nöbetleri arasında gerekli minimum gün sayısı
- **Kota** = gün tipine göre nöbet sayısı hedefi
- **Birlikte kuralı** = birlikte çizelgelenmesi gereken personeller
- **Ayrı kuralı** = aynı anda çizelgelenmemesi gereken personeller
- **Gün tipi** = `hici` (hafta içi), `prs` (Perşembe), `cum` (Cuma), `cmt` (Cumartesi), `pzr` (Pazar)
- **Saat değerleri** = gün tipine göre saat: `{hici: 8, prs: 8, cum: 16, cmt: 24, pzr: 16}`
- **Hedef** = kişi başı dengeli nöbet sayısı hedefi
- **Ayrı bina** = görevlerdeki ayrı bina kısıt bayrağı
- **Kurum profili** = `kurumProfili` bayrağı (`genel` | `112`); 112'ye özel kurallar yalnız `112` seçilince aktif
- **İzin türü** = `izin_turleri` (gün→tür): `izin` (yıllık), `egitim`, `rapor`, `nobet_izni`, `mazeret`. `mazeret_gunleri` bunların birleşimidir
- **İş günü** = hafta içi (`hici`/`prs`/`cum`); hafta sonu (`cmt`/`pzr`) ve resmi tatil hariç
- **Max ara gün** = 112'de iki nöbet arası izin verilen üst sınır (soft; `min ara_gun`'ün ikizi değil, ceza)

## 112 Kurum Profili (Faz 2)

112/Ambulans birimlerine özel domain kuralları `kurumProfili="112"` bayrağı arkasında toplanır. **Genel hastane profili hiç etkilenmez — tüm kurallar `kurum_profili=="112"` ile kapılıdır, geriye tam uyumludur.** Bayrak `parse_kurum_profili()` (`parsers.py`) ile normalize edilir; frontend'de sihirbaz adım 1'inde seçilir (localStorage'da kalıcı) ve `nobet_coz`/`nobet_hedef_hesapla` payload'ında akar.

**Kritik ilke:** Ara gün / yerleşim ile ilgili yeni kurallar **asla kör-HARD kısıt değildir** — ya soft ceza ya kullanıcı onaylı öneridir. Aksi halde `solve_strategy.py`'deki gevşetme döngüsü (yalnız `ara_gun_azalt` kolunu bilir) göremediği bir kısıt yüzünden yanlış yeri gevşetir.

Uygulanan kurallar:
- **Mesai-bazlı min nöbet** (`hedef_hesaplayici._mesai_min_nobet`): `min = ceil(net_iş_günü / 3)` (8/24 saat). İzin/eğitim/rapor iş gününe denk gelince borçtan düşer (normal mazeret ve nöbet izni **düşürmez**). **Gerçek soft:** `t[pid]` alt sınırına HİÇ girmez; 4b bloğunda ceza değişkenine bağlanır (`WEIGHT_MESAI_MIN`) ve aynı ceza projeksiyon modeline de eklenir (yoksa `oncelikli_objective <= projection_objective_degeri` bağı tutarsız kalır). Açık, çözümden sonra **gerçekleşen** `t` değerinden ölçülür.
  - ⚠️ Alt sınır olarak dayatmak yasak: mesai borçlarının toplamı slot arzını aşabilir (`personel > toplam_slot / min` olan her gerçek 112 kadrosunda aşar) ve `sum(t) == toplam_slot` hard eşitliğiyle çakışıp modeli kırar. Kişi bazında kırpmak yetmez — bu tam olarak `acb6350` öncesi canlı hatanın kök nedeniydi.
  - Açık `min_nobet_aciklari` ile "kim/ne kadar eksik + neden + nasıl tamamlanır" olarak raporlanır; frontend `minNobetAcigiOnayiAl()` ile çözümden önce kullanıcı onayı ister.
- **Max ara gün** (`ortools_solver` S5b, `WEIGHT_MAX_ARA`): her `max_ara_gun` (default 5) günlük pencerede ≥1 nöbet tercih edilir (soft). `maxAraGun` payload'ından ayarlanır.
- **12/12 son çare doldurma** (`_bos_slot_takas_onerileri` → `yari_vardiya`): boş kalan slot için gündüz+gece 12'şer saat bölme önerisi (kullanıcı onaylı). Gece adayı önceki gün nöbette olamaz (sabah çıkan o akşam yazılamaz). Normal nöbet 24s kalır.
- **Yıllık izin öncesi/sonrası yerleşim** (`ortools_solver` S5c, `WEIGHT_IZIN_YERLESIM`): yalnız yıllık izin için — öncesi 2 gün boşluk tercihi, sonrası ilk iş gününe nöbet tercihi (ikisi de soft).
- **112 yetkinlik rolleri** (şoför/ATT/paramedik + çoklu yetkinlik): ayrı kod yok; mevcut `gorevHavuzlari` + `yetkiliGorevler` altyapısıyla yapılandırılır (bir kişiyi birden çok havuza ekle).

## Kritik Kalıplar

**ID Normalizasyonu:** Tüm varlık ID'leri (personel, görev) `utils.py` içindeki `normalize_id()` fonksiyonundan geçer. int/float/string'i tutarlı bir int'e dönüştürür. Sayısal olmayan string'ler SHA1 ile hash'lenir. Bu kritiktir — frontend ile backend arasındaki ID uyumsuzlukları tekrarlayan bir hata kaynağıdır.

**Tembel OR-Tools İçe Aktarma:** OR-Tools, Firebase soğuk başlatma zaman aşımlarını önlemek için thread-safe kilitlemeyle tembel yüklenir (`ortools_solver.py`).

**Ağırlıklı Ceza Sabitleri** (`solver_models.py` içinde): `WEIGHT_GOREV_KOTA=1000`, `WEIGHT_GUN_TIPI=500`, vb. Bunlar CP-SAT modelindeki yumuşak kısıtlar arasındaki dengeyi kontrol eder. Değiştirilmesi çözüm kalitesini etkiler.

**Gün Tipi Mantığı:** `utils.py` içindeki `gun_tipi_hesapla()` tarihten gün tipini belirler. Perşembe ve Cuma özeldir çünkü hafta sonuna köprü oluştururlar (Cuma nöbetleri gece nöbeti nedeniyle 16 saat taşır).
