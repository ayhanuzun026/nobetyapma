> **✅ UYGULANDI / ESKİMİŞ.** Bu plan hayata geçirildi (`tasma_gorevi` alanı, H7 taşma
> slotları vb. kodda mevcut). Ayrıca aşağıdaki dosya referansları güncel değildir:
> `greedy_solver.py` artık YOKTUR ve `SolverPersonel` dataclass'ı `solver_models.py`
> içindedir. Tarihsel referans olarak saklanmaktadır.

# Taşma/Yedek Sistemi Algoritma Entegrasyonu

## Problem

Frontend'de taşma/yedek (overflow/fallback) sistemi tam implementedir:
- UI'da tasmaGorevi seçimi yapılabiliyor
- Hedef hesaplamada taşma kotası hesaplanıyor (kapasiteyi ana görev + taşma görevi toplamı üzerinden hesaplıyor)
- Frontend greedy solver'da taşma ataması yapılıyor
- Backend'e `tasmaGorevi` alanı gönderiliyor

Ancak backend bu veriyi **tamamen yok sayıyor**:
- `parsers.py:parse_gorev_kisitlamalari()` → sadece `personelId` ve `gorevAdi` alıyor, `tasmaGorevi` yok
- `parsers.py:parse_personeller_for_solver()` → `kisitli_gorev` setliyor ama `tasma_gorevi` yok
- `SolverPersonel` dataclass → `tasma_gorevi` field'ı yok
- OR-Tools H7 constraint → kısıtlı kişi sadece `izinli_slotlar`'a atanabiliyor, taşma slotları yok
- Greedy solver (Python) → `kisi_uygun_mu()` taşma bilgisi kullanmıyor

**Sonuç**: "Aşil → Acil nöbeti, dolarsa Mavi Kod'a taşsın" dendiğinde:
- Frontend hedef hesaplama doğru çalışıyor (Acil + Mavi Kod kapasitesi birleşik hesaplanıyor)
- Backend çözücüler Aşil'i sadece Acil'e atıyor, Mavi Kod'a hiç bakmiyor
- Acil dolunca Aşil'in hedefi doldurulamıyor

## Çözüm Stratejisi

**Yaklaşım**: Taşma görevini, kısıtlı kişinin "ikinci izinli görevi" olarak modellemek.
H7 constraint'te `izinli_slotlar` listesine taşma görevinin slotlarını da eklemek yeterli.
Greedy solver'da da aynı mantıkla taşma görevine atama izni vermek.

## Değişiklikler

### Adım 1: `SolverPersonel` dataclass'ına `tasma_gorevi` ekle
**Dosya**: `nobetyapma/functions/ortools_solver.py:34-44`

```python
@dataclass
class SolverPersonel:
    id: int
    ad: str
    mazeret_gunleri: Set[int] = field(default_factory=set)
    kisitli_gorev: Optional[str] = None
    tasma_gorevi: Optional[str] = None          # <-- YENİ
    hedef_tipler: Dict[str, int] = field(default_factory=dict)
    gorev_kotalari: Dict[str, int] = field(default_factory=dict)
    musait_gunler: Set[int] = field(default_factory=set)
    musait_tipler: Dict[str, int] = field(default_factory=dict)
    yillik_gerceklesen: Dict[str, int] = field(default_factory=dict)
```

### Adım 2: `parse_gorev_kisitlamalari` → taşma bilgisini de döndür
**Dosya**: `nobetyapma/functions/parsers.py:396-404`

Mevcut fonksiyon `Dict[int, str]` döndürüyor (`{pid: gorev_adi}`).
Bunu `Dict[int, dict]` yapacağız: `{pid: {"gorevAdi": str, "tasmaGorevi": str|None}}`

```python
def parse_gorev_kisitlamalari(data: Dict, personeller) -> Dict[int, dict]:
    """Görev kısıtlamalarını dict formatında parse et {personel_id: {gorevAdi, tasmaGorevi}}"""
    gorev_kisitlamalari = {}
    for k_data in data.get("gorevKisitlamalari", []):
        pid = _resolve_personel_id(k_data.get("personelId"), personeller, require_existing=True)
        gorev_adi = k_data.get("gorevAdi")
        if pid is not None and gorev_adi:
            gorev_kisitlamalari[pid] = {
                "gorevAdi": gorev_adi,
                "tasmaGorevi": k_data.get("tasmaGorevi")
            }
    return gorev_kisitlamalari
```

**DİKKAT**: Bu fonksiyonun dönüş tipini değiştirmek, onu kullanan tüm yerleri etkiler.
Kullanılan yerler:
- `main.py:247` → HedefHesaplayici'ya geçiriliyor
- `main.py:432` → HedefHesaplayici'ya geçiriliyor
- `ortools_solver.py:131` → HedefHesaplayici.__init__
- `ortools_solver.py:331` → `for pid, gorev_adi in self.gorev_kisitlamalari.items():`
- `ortools_solver.py:539` → aynı pattern

Tüm bu yerleri yeni format için uyarlamak lazım.

### Adım 3: `parse_personeller_for_solver` → taşma görevini SolverPersonel'e aktar
**Dosya**: `nobetyapma/functions/parsers.py:280-335`

`kisitli_gorev` setlenirken yanına `tasma_gorevi` de setlenecek:

```python
# Görev kısıtlaması
kisitli_gorev = None
tasma_gorevi = None           # <-- YENİ
for k in data.get("gorevKisitlamalari", []):
    k_pid = k.get("personelId")
    k_pid_matches = ids_match(k_pid, pid)
    if isinstance(k_pid, str) and k_pid.strip() == p_data.get("ad", ""):
        k_pid_matches = True
    if k_pid_matches:
        raw_gorev_adi = k.get("gorevAdi")
        kisitli_gorev = _normalize_gorev_adi(raw_gorev_adi)
        tasma_gorevi = _normalize_gorev_adi(k.get("tasmaGorevi"))  # <-- YENİ
        break

personeller.append(SolverPersonel(
    id=pid,
    ad=p_data.get("ad"),
    mazeret_gunleri=mazeretler,
    kisitli_gorev=kisitli_gorev,
    tasma_gorevi=tasma_gorevi,     # <-- YENİ
    hedef_tipler=hedef_tipler,
    gorev_kotalari=gorev_kotalari,
    yillik_gerceklesen=yillik_gerceklesen
))
```

### Adım 4: OR-Tools H7 constraint → taşma slotlarını izinli yap
**Dosya**: `nobetyapma/functions/ortools_solver.py:1344-1359`

```python
# H7. Kisitli gorev - kısıtlı kişi kendi görevine + taşma görevine gidebilir
for p in self.personel_listesi:
    if p.kisitli_gorev:
        izinli_slotlar = self.role_slots.get(p.kisitli_gorev, [])
        if not izinli_slotlar:
            for s, gorev in enumerate(self.gorevler):
                if gorev.ad == p.kisitli_gorev or gorev.base_name == p.kisitli_gorev:
                    izinli_slotlar.append(s)

        # TAŞMA GÖREVİ SLOTLARINI DA İZİNLİ YAP
        if p.tasma_gorevi:
            tasma_slotlar = self.role_slots.get(p.tasma_gorevi, [])
            if not tasma_slotlar:
                for s, gorev in enumerate(self.gorevler):
                    if gorev.ad == p.tasma_gorevi or gorev.base_name == p.tasma_gorevi:
                        tasma_slotlar.append(s)
            izinli_slotlar = list(set(izinli_slotlar + tasma_slotlar))

        for g in range(1, self.gun_sayisi + 1):
            allowed_exception_roles = self.kisitlama_istisna_map.get((p.id, g), set())
            for s in range(self.slot_sayisi):
                role = self._role_name_by_slot(s)
                if s not in izinli_slotlar and role not in allowed_exception_roles:
                    model.Add(x[p.id, g, s] == 0)
```

### Adım 5: OR-Tools teshis/debug fonksiyonlarını güncelle
H7 ile aynı pattern kullanan diğer yerlerde de taşma kontrolü ekle:
- `ortools_solver.py:753` → slot uygunluk kontrolü
- `ortools_solver.py:890` → greedy fallback uygunluk
- `ortools_solver.py:1038` → kısıtlı kişi sayısı hesaplama

### Adım 6: HedefHesaplayici → taşma kapasitesini dahil et
**Dosya**: `nobetyapma/functions/ortools_solver.py:330-334`

`kisitli_kapasite` hesabında taşma görevinin kapasitesini de ekle:

```python
for pid, gorev_adi in self.gorev_kisitlamalari.items():
    # Yeni format: gorev_adi dict olabilir
    if isinstance(gorev_adi, dict):
        ana_gorev = gorev_adi["gorevAdi"]
        tasma = gorev_adi.get("tasmaGorevi")
    else:
        ana_gorev = gorev_adi
        tasma = None

    slot_sayisi = sum(1 for g in self.gorevler if g.base_name == ana_gorev or g.ad == ana_gorev)
    if tasma:
        slot_sayisi += sum(1 for g in self.gorevler if g.base_name == tasma or g.ad == tasma)

    if slot_sayisi > 0:
        kisitli_kapasite[pid] = slot_sayisi * self.gun_sayisi
```

### Adım 7: Greedy solver (Python) → `kisi_uygun_mu` taşma kontrolü
**Dosya**: `nobetyapma/functions/greedy_solver.py:132-139`

Kısıtlama kontrolünde, taşma görevi varsa onun da izinli olduğunu kontrol et:

```python
for kisit in self.gorev_kisitlamalari:
    if ids_match(kisit.get('personelId'), p.id):
        kisit_gorev = kisit.get('gorevAdi')
        tasma_gorev = kisit.get('tasmaGorevi')
        if kisit_gorev != gorev.ad and kisit_gorev != gorev.base_name:
            # Taşma görevi kontrolü
            if tasma_gorev and (tasma_gorev == gorev.ad or tasma_gorev == gorev.base_name):
                pass  # Taşma görevine atanabilir
            else:
                is_exclusive = kisit.get('exclusive', True)
                havuz_ids = kisit.get('havuzIds', [])
                if not is_exclusive or len(havuz_ids) > 0:
                    pass
                else:
                    return False
```

### Adım 8: Loglama ve debug bilgisi
- OR-Tools teshis çıktısında taşma bilgisini göster
- HedefHesaplayici istatistiklerine taşma bilgisini ekle

## Etki Analizi

| Bileşen | Değişiklik | Risk |
|---------|-----------|------|
| SolverPersonel | 1 yeni field | Düşük - optional field |
| parsers.py | 2 fonksiyon güncelleme | Orta - dönüş tipi değişiyor |
| OR-Tools solver | H7 constraint genişletme | Düşük - mevcut mantığın üstüne ekleme |
| Greedy solver | kisi_uygun_mu genişletme | Düşük - mevcut kontrol akışına ekleme |
| HedefHesaplayici | Kapasite hesabı güncelleme | Orta - dict format değişikliği |
| Frontend | Değişiklik yok | - |

## Test Senaryosu

1. Kişi A → Acil görevi (kısıtlı), taşma: Mavi Kod
2. Acil: 1 slot/gün, 30 gün → max 30 atama
3. 3 kişi Acil'e kısıtlı → her biri max 10 atama
4. Kişi A'nın hedefi 15 ise → 10 Acil + 5 Mavi Kod olmalı
5. OR-Tools ve Greedy'de aynı sonuç beklenilmeli
