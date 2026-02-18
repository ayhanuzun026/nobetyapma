# Nobet Yapma - Kod Incelemesi ve Iyilestirme Plani
**Tarih:** 2026-02-18

---

## Projenin Guclu Yonleri

### 1. Cift Motorlu Mimari (Mukemmel Tercih)
- **Greedy Engine** (`NobetYoneticisi` - main.py): Hizli, sezgisel cozum
- **OR-Tools CP-SAT Engine** (`NobetSolver` - ortools_solver.py): Matematiksel optimal cozum
- Ikili yapi: hizli onizleme + garanti cozum

### 2. Zengin Kural Seti
- Gun tipi bazli kotalar (hici/prs/cum/cmt/pzr)
- Birlikte tutma / ayri tutma kurallari
- Gorev kisitlamasi (exclusive gorevler)
- Manuel atamalar
- Mazeret/izin yonetimi
- Yillik dengeleme
- Devir (onceki aydan kalan)

### 3. OR-Tools Solver'in Uclu Dengeleme Sistemi
- Sayi dengesi (kelepce), saat dengesi, hafta sonu dengesi

---

## Tespit Edilen Hatalar

### Kritik Hata 1: ID Tipi Karmasasi (int vs float)
**Dosya:** `main.py` satir 962-966
```python
# Gereksiz cift donusum
pid = float(raw_id) if isinstance(raw_id, (int, float, str)) else float(len(personeller))
try:
    pid = float(raw_id)  # <- Zaten yukarida yapildi, tekrar yazilmis
except:
    pid = float(len(personeller))
```
- `NobetYoneticisi` int ID kullanirken, `NobetSolver` float ID kullaniyor
- `normalize_id`, `ids_match`, `find_matching_id` fonksiyonlari bu sorunu cozmek icin yazilmis ama asil sorun ID standardizasyonu
- **Cozum:** Tum ID'leri int'e standardize et

### Kritik Hata 2: nobet_dagit ve nobet_coz Uyumsuzlugu
- `nobet_dagit` -> `NobetYoneticisi` (greedy) kullaniyor, hedefleri frontend'den aliyor
- `nobet_coz` -> `NobetSolver` (OR-Tools) kullaniyor, hedefleri kendi otomatik hesapliyor (satir 1367-1387)
- `nobet_hedef_hesapla` zaten ayri bir endpoint olarak hedef hesapliyor - yani `nobet_coz` hedefi iki kez hesapliyor!
- **Cozum:** Hedef hesaplamayi tek yerde yap, her iki engine'e ayni hedefleri ver

### Hata 3: Bare except Kullanimi (30+ yer)
```python
except:  # <- Hangi hatayi yakaliyorsun? TypeError? ValueError?
    pass
```
- Debug'i imkansizlastirir
- **Cozum:** Her except'e spesifik exception tipi ekle

### Hata 4: gun_adi_bul - Persembe Tatili Destegi Eksik
```python
def gun_adi_bul(yil, ay, gun, resmi_tatiller):
    for rt in resmi_tatiller:
        if tip == "pzr": return "Pazar"
        if tip == "cmt": return "Cumartesi"
        if tip == "cum": return "Cuma"
        # <- Persembe tatili ("prs") yok!
```
- **Cozum:** `prs` tipini de ekle

### Hata 5: blob.make_public() - Guvenlik Riski
```python
blob.make_public()  # <- Excel dosyalari herkesin erisime aciliyor!
```
- **Cozum:** Signed URL kullan

---

## Algoritma Iyilestirme Onerileri

### 1. Greedy Engine'de Backtracking (En Buyuk Eksik)
Mevcut greedy algoritma geri donus yapmiyor. Bir atama yapildiktan sonra eger ileride tikanikliga sebep olursa, geri alip denemiyor.

**Oneri:** Basit bir backtracking mekanizmasi ekle:
- Eger bir gune hic aday bulunamiyorsa, o gunden onceki 2-3 atamayi geri al ve farkli adaylarla tekrar dene

### 2. OR-Tools Solver'da Degisken Sayisi Optimizasyonu
```python
# Mevcut: O(personel x gun x slot) = 20 x 31 x 6 = 3,720 bool degisken
x[p.id, g, s] = model.NewBoolVar(...)
```
- 50+ personel ve 10+ slot oldugunda 111,600 degisken oluyor
- **Oneri:** Mazeret gunlerindeki gunler icin degisken olusturma (zaten 0'a fix ediyorsun, hic olusturma)

### 3. Homojen Dagilim Algoritmasinin Guclendirilmesi
Mevcut haftalik pencere yaklasimi iyi ama yetersiz.

**Oneri:** Ardisik nobetler arasi minimum-maximum aralik penceresi ekle:
- Min aralik: `ara_gun` (zaten var, HARD)
- Max aralik: `gun_sayisi / hedef_nobet + tolerans` (YENI, SOFT)
- Bu sayede "ayin basinda 3 nobet, sonunda 0" durumu engellensin

### 4. Saat Dengesi Formulunun Gelistirilmesi
```python
SAAT_DEGERLERI = {'hici': 8, 'prs': 8, 'cum': 16, 'cmt': 24, 'pzr': 16}
```
- **Oneri:** Saat degerlerini configurable yapip frontend'den ayarlanabilir hale getir

### 5. Birlikte Tutma Kuralinda Permutasyon Patlamasi
```python
for sira in itertools.permutations(grup_uyeleri):  # O(n!)
```
- 3 kisilik grupta sorun yok (3! = 6) ama 5 kisilik grupta 5! = 120
- **Oneri:** Permutasyon yerine Hungarian Algorithm kullanarak optimal eslestirme yap - O(n^3) vs O(n!)

### 6. Cozum Kalitesi Metrikleri Ekle
```python
kalite_skoru = {
    'denge_puani': (max_nobet - min_nobet) / ortalama * 100,
    'saat_adaleti': std_dev(saatler) / ortalama_saat * 100,
    'homojenlik': std_dev(nobet_araliklari),
    'doluluk': doldurulan_slot / toplam_slot * 100,
    'kural_uyumu': ihlal_edilen_kural / toplam_kural * 100
}
```

### 7. Otomatik Fallback Mekanizmasi
Mevcut: Cozum bulunamazsa hata dondur.

**Oneri:** Otomatik kademeli gevseme:
1. OR-Tools ile dene (ara_gun = istenilen)
2. Bulamazsa ara_gun -= 1 ile tekrar dene
3. Hala bulamazsa greedy engine'e fallback
4. Greedy de tamamlayamazsa, son_vurus ile kalan bosluklari doldur

---

## Uygulama Onceligi

| Oncelik | Gorev | Etki |
|---------|-------|------|
| P0 | ID tipi standardizasyonu (int) | Kritik bug fix |
| P0 | Bare except temizligi | Debug kolayligi |
| P1 | Backtracking mekanizmasi | Dagitim basarisi artisi |
| P1 | Homojen dagilim penceresi | Adalet artisi |
| P2 | Cozum kalitesi metrikleri | Kullanici deneyimi |
| P2 | Otomatik fallback | Robustness |
| P3 | Hungarian algorithm | Performans |
| P3 | Signed URL | Guvenlik |
