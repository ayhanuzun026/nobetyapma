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

### Tek Motorlu Çözücü + İki Aşamalı Planlama

Sistem, iki AYRI OR-Tools CP-SAT modeli kullanır (eski greedy motor kaldırılmıştır):

1. **Hedef Modeli** (`hedef_hesaplayici.py` → `HedefHesaplayici`) — "kim kaç nöbet tutacak" sorusunu (kişi başı adil hedef sayıları) çözen CP-SAT modeli
2. **Çizelge Modeli** (`ortools_solver.py` → `NobetSolver`) — hedefleri gerçek takvime yerleştiren CP-SAT modeli ("kim, hangi gün, hangi slot")

Arada `gun_iskelet_planlayici.py` (sezgisel, OR-Tools kullanmaz) hedefleri günlere/rollere dağıtıp `PlanKontrati` üretir; bu, çizelge modeline yumuşak/sert "iskelet" kısıtları olarak beslenir. Üç endpoint de aynı planı `planlayici.ortak_plan_uret()` üzerinden üretir.

`nobet_coz` INFEASIBLE dönerse, `solve_strategy.py` **tanılama tabanlı gevşetme döngüsü** çalıştırır: kök nedeni teşhis et → kısıtları otomatik gevşet (ör. `ara_gun` azalt; exclusive/ayrı/birlikte kurallarını kaldır) → tekrar dene. **Greedy geri dönüş YOKTUR**; son çare tüm yumuşak kısıtların kaldırılmasıdır (`tum_soft_kaldir`).

### 5 Cloud Function Endpoint'i (main.py)

| Endpoint | Amaç | Bellek | Zaman Aşımı |
|---|---|---|---|
| `nobet_dagit` | OR-Tools hızlı önizleme (Excel + imzalı URL üretir) | 1 GB | 540s |
| `nobet_kapasite` | Kapasite analizi | 512 MB | 60s |
| `nobet_hedef_hesapla` | Hedef hesaplama (OR-Tools) | 1 GB | 300s |
| `nobet_coz` | Optimal çözüm (OR-Tools + gevşetme döngüsü) | 2 GB | 540s |
| `debug_event_log` | Frontend debug event → Firestore | 256 MB | 10s |

Not: Frontend şu an yalnızca `nobet_coz` ve `nobet_hedef_hesapla`'yı çağırır; `nobet_dagit` ve `nobet_kapasite` deploy edilir ama frontend'den kullanılmaz. Girdi boyut üst sınırları (`main.py`): `MAX_SLOT_SAYISI=50`, `MAX_PERSONEL=1000`, `MAX_GOREV=300` (OOM/DoS koruması).

### Backend Modül Haritası (functions/)

- **`main.py`** — Giriş noktası, 5 HTTP endpoint, Firebase başlatma, girdi boyut doğrulama
- **`ortools_solver.py`** — `NobetSolver`: Üçlü denge (sayı/saat/hafta sonu) ve ağırlıklı ceza yöntemiyle CP-SAT çizelge modeli
- **`hedef_hesaplayici.py`** — `HedefHesaplayici`: Ayrı bir CP-SAT modeliyle kişi başı adil nöbet hedefi hesaplar
- **`gun_iskelet_planlayici.py`** — `GunIskeletPlanlayici`: Hedefleri günlere/rollere dağıtan sezgisel iskelet planlayıcı (Faz 3); OR-Tools kullanmaz
- **`planlayici.py`** — `ortak_plan_uret()`: Hedef + iskeleti `PlanKontrati`'ye paketleyen orkestratör (3 endpoint aynı planı kullansın diye)
- **`solve_strategy.py`** — Tanılama döngüsü: çöz → çözümsüzlüğü teşhis et → gevşet → tekrar dene (greedy geri dönüş YOK)
- **`parsers.py`** — Frontend JSON'unu backend veri modellerine dönüştürür; ID normalizasyonu yapar
- **`utils.py`** — Ortak yardımcılar: `normalize_id()` (SHA1 tabanlı), takvim fonksiyonları, gün tipi sabitleri
- **`solver_models.py`** — OR-Tools veri sınıfları: `SolverPersonel`, `SolverGorev`, `SolverKural`, `SolverAtama`, `PlanKontrati`; ceza ağırlık sabitleri
- **`excel_export.py`** — OpenPyXL tabanlı Excel rapor üretimi
- **`kapasite.py`** — Personel müsaitliği ve slot kapasitesi analizi
- **`http_helpers.py`** — CORS preflight, JSON/hata yanıt yardımcıları
- **`firestore_logger.py`** — Her backend çağrısını `debug_sessions`'a kaydeder (PII maskeli, 30 gün TTL)

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

## Kritik Kalıplar

**ID Normalizasyonu:** Tüm varlık ID'leri (personel, görev) `utils.py` içindeki `normalize_id()` fonksiyonundan geçer. int/float/string'i tutarlı bir int'e dönüştürür. Sayısal olmayan string'ler SHA1 ile hash'lenir. Bu kritiktir — frontend ile backend arasındaki ID uyumsuzlukları tekrarlayan bir hata kaynağıdır.

**Tembel OR-Tools İçe Aktarma:** OR-Tools, Firebase soğuk başlatma zaman aşımlarını önlemek için thread-safe kilitlemeyle tembel yüklenir (`ortools_solver.py`).

**Ağırlıklı Ceza Sabitleri** (`solver_models.py` içinde): `WEIGHT_GOREV_KOTA=1000`, `WEIGHT_GUN_TIPI=500`, vb. Bunlar CP-SAT modelindeki yumuşak kısıtlar arasındaki dengeyi kontrol eder. Değiştirilmesi çözüm kalitesini etkiler.

**Gün Tipi Mantığı:** `utils.py` içindeki `gun_tipi_hesapla()` tarihten gün tipini belirler. Perşembe ve Cuma özeldir çünkü hafta sonuna köprü oluştururlar (Cuma nöbetleri gece nöbeti nedeniyle 16 saat taşır).
