# NobetYap Faz 5-6 Devam Plani

Son durum:
- Faz 1-5 tamamlandi.
- Faz 6A tamamlandi: unsat-core teshisi rapora eklendi, otomatik gevsetme sirasi degismedi.
- Faz 6B beklemede: otomatik gevsetme sirasi unsat-core sonucuna baglanacak, simdilik uygulanmayacak.
- Faz 7-8 tamamlandi.
- Test proseduru eklendi: `TEST_PROCEDURE.md`
- Tek komut kontrol eklendi: `scripts/run_checks.ps1`

## Faz 5 - Davranis Korumali Refaktor

Amac: Cozucu davranisini degistirmeden uzun ve tekrarli kodu daha okunabilir,
test edilebilir parcalara bolmek.

### 5.1 NobetSolver.coz() bolme

Mevcut durum:
- `functions/ortools_solver.py` icindeki `NobetSolver.coz()` model kurma, cozum,
  basarili sonuc cikarma ve basarisiz sonuc cikarma islerini tek metotta yapiyor.

Hedef:
- `coz()` orkestrator olarak kalsin.
- Model kurma `_build_model(...)` icine tasinsin.
- Solver calistirma `_solve(...)` icine tasinsin.
- Basarili sonuc cikarma `_extract_solution(...)` icine tasinsin.
- Basarisiz sonuc cikarma `_build_failure_result(...)` icine tasinsin.
- Model icinde gerekli paylasilan degerler icin kucuk bir context dataclass kullanilsin.

Risk kontrolu:
- Constraint ve penalty ifadeleri aynen korunacak.
- Solver parametreleri aynen korunacak.
- `SolverSonuc` payload anahtarlari aynen korunacak.
- Refaktor oncesi/sonrasi `functions/_smoke_test.py` calisacak.

### 5.2 HedefHesaplayici teshis bloğunu ayirma

Mevcut durum:
- `functions/hedef_hesaplayici.py` icindeki `hesapla()` basarisiz CP-SAT durumunda
  uzun izolasyon/debug modellerini inline kuruyor.

Hedef:
- Bu blok `functions/hedef_teshis.py` icine tasinacak.
- `hesapla()` sadece gerekli context degerlerini verip debug mesaji alacak.
- Donen mesaj icerigi korunacak.

Risk kontrolu:
- Hata mesaj formati mumkun oldugunca ayni kalacak.
- Ek modül Firebase import zincirini agirlastirmayacak.

### 5.3 Endpoint tekrarlarini azaltma

Mevcut durum:
- `nobet_dagit` ve `nobet_coz` yil/ay/slot/araGun parse, ortak planlama,
  solver cagirma, cizelge olusturma ve preflight analizinde benzer kod tasiyor.

Hedef:
- Kucuk yardimcilarla tekrar azalt:
  - ortak tarih/slot parametre parse
  - ortak `cizelge` olusturma
  - ortak preflight analiz ekleme
  - ortak planlama girdisi hazirlama mumkunse sinirli kapsamda
- Endpoint response semasi korunacak.

Risk kontrolu:
- HTTP status ve JSON alanlari degismeyecek.
- Auth/CORS ve log_session akisina dokunulmayacak.

### 5.4 Ortak tekrar yardimcilari

Mevcut durum:
- Musaitlik ve kısıt analizi bazi dosyalarda tekrar ediyor.

Hedef:
- Sadece risksiz, lokal tekrarlar yardimciya alinacak.
- Faz 6 oncesi solver semantigini etkileyebilecek genis soyutlama yapilmayacak.

## Faz 6 - Unsat-Core Teshis

Amac: Elle yazilmis sezgisel INFEASIBLE teshisini CP-SAT assumptions temelli
daha dogrudan bir teshis sistemiyle desteklemek.

Plan:
1. Faz 5 bitip testler yesil olduktan sonra baslanacak.
2. Soft/hard kural gruplari icin assumption literal tasarimi cikacak.
3. Sadece hata yolunda `sufficient_assumptions_for_infeasibility()` calisacak.
4. Assumptions yolu `num_search_workers=1` kullanacak.
5. Ilk adimda mevcut `_diagnose_infeasible` tamamen silinmeyecek; yeni sistemle
   yan yana dogrulanacak.
6. Kullanici ile adim adim gidilecek; her adimdan sonra test/sonuc kontrol edilecek.

Faz 6 baslamadan once beklenen durum:
- `NobetSolver.coz()` parcali hale gelmis.
- Hedef teshis bloğu ayri modülde.
- Smoke testler geciyor.
