# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Proje Özeti

**Nöbet Yapma Robotu Pro v2** — Nöbet çizelgeleme optimizasyon sistemi. Backend: Firebase Cloud Functions (Python 3.11), Frontend: monolitik vanilla JS. Tüm arayüz, değişken isimleri, yorumlar ve alan terminolojisi Türkçedir.

Firebase proje ID: `nobetyap-29acf`

## Derleme ve Dağıtım

```bash
# Tümünü dağıt (functions + hosting)
firebase deploy

# Sadece backend
firebase deploy --only functions

# Sadece frontend
firebase deploy --only hosting

# Yerel geliştirme için emülatör
firebase emulators:start

# Python bağımlılıkları
cd functions && pip install -r requirements.txt
```

Frontend için derleme adımı yoktur — `public/index.html` doğrudan sunulur. Test framework'ü yapılandırılmamıştır.

## Mimari

### Çift Motorlu Çözücü Stratejisi

Sistemde otomatik geri dönüşlü iki bağımsız çizelgeleme motoru bulunur:

1. **Greedy Motor** (`greedy_solver.py` → `NobetYoneticisi`) — Hızlı sezgisel çözücü, `/nobet_dagit` üzerinden hızlı önizleme için kullanılır
2. **OR-Tools CP-SAT Motor** (`ortools_solver.py` → `NobetSolver`) — Optimal çözüm için kısıt programlama çözücüsü, `/nobet_coz` üzerinden kullanılır

OR-Tools INFEASIBLE (çözümsüz) döndürürse, `solve_strategy.py` **tanılama tabanlı gevşetme döngüsü** çalıştırır: kök nedeni teşhis et → kısıtları otomatik gevşet (ör. `ara_gun` azalt) → tekrar dene. Hâlâ çözüm bulunamazsa `greedy_fallback.py` aracılığıyla greedy motora geri döner.

### 4 Cloud Function Endpoint'i (main.py)

| Endpoint | Amaç | Bellek | Zaman Aşımı |
|---|---|---|---|
| `nobet_dagit` | Greedy dağıtım | 1 GB | 540s |
| `nobet_kapasite` | Kapasite analizi | 512 MB | 60s |
| `nobet_hedef_hesapla` | Hedef hesaplama (OR-Tools) | 1 GB | 300s |
| `nobet_coz` | Optimal çözüm (OR-Tools + geri dönüş) | 2 GB | 540s |

### Backend Modül Haritası (functions/)

- **`main.py`** — Giriş noktası, 4 HTTP endpoint, Firebase başlatma
- **`ortools_solver.py`** — `NobetSolver`: Üçlü denge (sayı/saat/hafta sonu) ve ağırlıklı ceza yöntemiyle CP-SAT modeli
- **`greedy_solver.py`** — `NobetYoneticisi`: Gün tipi kotalarıyla sezgisel çizelgeleyici
- **`hedef_hesaplayici.py`** — `HedefHesaplayici`: OR-Tools kullanarak kişi başı adil nöbet hedefi hesaplar
- **`solve_strategy.py`** — Tanılama döngüsü: çöz → çözümsüzlüğü teşhis et → gevşet → tekrar dene → greedy geri dönüş
- **`parsers.py`** — Frontend JSON'unu backend veri modellerine dönüştürür; ID normalizasyonu yapar
- **`utils.py`** — Ortak yardımcılar: `normalize_id()` (SHA1 tabanlı), takvim fonksiyonları, gün tipi sabitleri
- **`models.py`** — Greedy çözücü veri sınıfları: `GorevTanim`, `Personel`
- **`solver_models.py`** — OR-Tools veri sınıfları: `SolverPersonel`, `SolverGorev`, `SolverKural`, `SolverAtama`; ceza ağırlık sabitleri
- **`greedy_fallback.py`** — OR-Tools sonuç formatını greedy formatına dönüştürür
- **`excel_export.py`** — OpenPyXL tabanlı Excel rapor üretimi
- **`kapasite.py`** — Personel müsaitliği ve slot kapasitesi analizi
- **`http_helpers.py`** — CORS preflight, JSON/hata yanıt yardımcıları

### Frontend (public/index.html)

Tüm CSS, JS ve HTML'i içeren 8.500+ satırlık tek monolitik dosya. Firebase SDK v9.6.1 (auth, Firestore, storage) kullanır. 6 adımlı sihirbaz arayüzü. Durum LocalStorage ile saklanır.

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

## Kritik Kalıplar

**ID Normalizasyonu:** Tüm varlık ID'leri (personel, görev) `utils.py` içindeki `normalize_id()` fonksiyonundan geçer. int/float/string'i tutarlı bir int'e dönüştürür. Sayısal olmayan string'ler SHA1 ile hash'lenir. Bu kritiktir — frontend ile backend arasındaki ID uyumsuzlukları tekrarlayan bir hata kaynağıdır.

**Tembel OR-Tools İçe Aktarma:** OR-Tools, Firebase soğuk başlatma zaman aşımlarını önlemek için thread-safe kilitlemeyle tembel yüklenir (`ortools_solver.py`).

**Ağırlıklı Ceza Sabitleri** (`solver_models.py` içinde): `WEIGHT_GOREV_KOTA=1000`, `WEIGHT_GUN_TIPI=500`, vb. Bunlar CP-SAT modelindeki yumuşak kısıtlar arasındaki dengeyi kontrol eder. Değiştirilmesi çözüm kalitesini etkiler.

**Gün Tipi Mantığı:** `utils.py` içindeki `gun_tipi_hesapla()` tarihten gün tipini belirler. Perşembe ve Cuma özeldir çünkü hafta sonuna köprü oluştururlar (Cuma nöbetleri gece nöbeti nedeniyle 16 saat taşır).
