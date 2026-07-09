# NobetYap Test Proseduru

Bu prosedur Faz 5 refaktor ve Faz 6A unsat-core teshisi sonrasi sistemi kontrol etmek
icin kullanilir.

## 1. Tek Komut Kontrol

PowerShell:

```powershell
.\scripts\run_checks.ps1
```

Beklenen:
- Python derleme kontrolu hatasiz biter.
- `functions\_smoke_test.py` tum senaryolari gecer.
- `git diff --check` whitespace hatasi vermez.

## 2. Smoke Test Senaryolari

`functions\_smoke_test.py` su kritik kontrolleri yapar:

- Temel cozum: 4 personel, 1 gorev, 7 gun feasible olmali.
- Eksik hedef: hedefi olmayan kisiye nobet yazilmamali.
- Unsat-core: ara gun + plan hard hedef cakismasinda core icinde
  `H4_ARA_GUN` ve `S3_TOPLAM_HEDEF_PLAN` gorunmeli.

Manuel calistirma:

```powershell
python functions\_smoke_test.py
```

## 3. Gercek Uygulama Uzerinden Kisa Test

Web arayuzunde kucuk bir veri seti ile deneyin:

- 4 personel
- 1 gorev
- 7 gun
- mazeret yok
- ara gun 2

Beklenen:
- Hedef hesaplama basarili.
- Nobet cozumu `OPTIMAL` veya `FEASIBLE`.
- Aynı kisi ara gun kuralini bozacak sekilde atanmaz.
- Bos slot sayisi beklenen sinirda kalir.

## 4. Bilerek Kilitleme Testi

Sistemin hata yolunu test etmek icin:

- Personel sayisini azaltin.
- Ara gunu sert tutun.
- Kilitli hedefleri veya gorev kisitlarini artirin.

Beklenen:
- Sistem cokmeden `INFEASIBLE` veya gevsetilmis cozum dondurur.
- Response icinde `teshis`, `teshis.unsat_core`, `tani_mesajlari` ve
  `hazirlikAnalizi` alanlari gorunur.

## 5. Faz 6B Notu

Faz 6B beklemede tutulmali. Unsat-core su anda sadece teshis/rapor olarak kullanilir;
otomatik gevsetme sirasi henuz unsat-core sonucuna baglanmamistir.
