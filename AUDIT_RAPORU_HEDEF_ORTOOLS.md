# OR-Tools hedef, WE/WD ve fizibilite denetim raporu

**Denetlenen taban sürüm:** `c7a85a2` (`Keep preparation plan source consistent`) + çalışma ağacındaki 1 Kasım düzeltmeleri

**Denetim tarihi:** 1 Kasım 2026

## Sonuç

İlk denetimde iki temel sorun bulundu: aynı girdide hedef modelinin birden fazla
kez çalışması ve kapasite/nihai çözüm durumlarının birbirini maskelemesi. P0
düzeltmeleri uygulandı. Hedef endpoint'i ortak hedef/plan kontratını üretir;
kapasite hazırlığı bu snapshot'ı tüketir (eski istemcilerde kendisi üretir);
nihai endpoint geçerli snapshot'ı doğrulayıp aynı hedefleri tüketir. Eksik veya
bozuk snapshot 409 ile reddedilir. Manuel hedef hücresi düzenlemesi açık hedef
kilidine dönüşür ve eski planı geçersiz kılar.

Güncel doğrulama: `_regression_test.py` **83/83**, `_smoke_test.py` başarılı,
Python derleme kontrolü başarılı ve inline JavaScript `node --check` başarılıdır.
Hedef modeli ile tam görev-slotu modeli hâlâ farklı katmanlardır; bu yüzden
hedef sayımı geçse bile görev/havuz/bina/birlikte çakışması nihai çözümü
`INFEASIBLE` yapabilir. Bu ayrım raporda bilinçli olarak korunmuştur.

## Gerçek çalışma zinciri

```text
Frontend hedef hesapla
        |
        v
/nobet_hedef_hesapla
        |
        +-- ortak_plan_uret
        |      |
        |      +-- HedefHesaplayici (CP-SAT)
        |      +-- GunIskeletPlanlayici
        |
        +-- FEASIBLE ise planKontrati + hedefler frontend'e taşınır
        |      (hedef ekranı burada kapasite beklemez)
        |
        +-- liste oluşturma öncesi /nobet_kapasite
        |      |
        |      +-- aynı planKontrati doğrulanır ve tam görev-slotu modeli çalışır
        |      +-- gerekirse minimum-boşluk / core / what-if analizleri
        |
        +-- final /nobet_coz
               |
               +-- planKontrati doğrulanırsa ortak_plan_uret atlanır
               +-- snapshot yoksa geriye uyumlu yeniden hesaplama yolu
```

Bu değişiklik hedef ekranını tam kapasite analizinin bekleme süresinden ayırır ve
aynı hedef snapshot'ının kapasite ile finalde tüketilmesini sağlar. Kapasite
endpoint'inin tam görev-slotu kanıtı ve final çizelgeleme yine ayrı CP-SAT
geçişleridir; sonuçları ayrı aşama olarak raporlanır.

## Gün tipi, saat ve WE/WD tanımı

| Kod | Günler | Saat | Sınıf |
|---|---|---:|---|
| `hici` | Pazartesi–Çarşamba | 8 | WD / hafta içi |
| `prs` | Perşembe | 8 | WD / hafta içi |
| `cum` | Cuma | 16 | WE / hafta sonu grubu |
| `cmt` | Cumartesi | 24 | WE / hafta sonu grubu |
| `pzr` | Pazar | 16 | WE / hafta sonu grubu |

Kodda WE = `cum + cmt + pzr`, WD = `hici + prs` olarak tanımlıdır. Cuma
hafta içi sayılmıyor; 16 saatlik köprü nöbeti olarak WE grubuna giriyor.

## Hedef modelinin gerçekten zorunlu tuttuğu şeyler

`HedefHesaplayici` içinde şu kısıtlar hard'dır:

- mazeret, izin, rapor, eğitim ve nöbet izni olan günler aday havuzundan çıkarılır;
- kişi aynı gün en fazla bir kez atanır;
- `ara_gun` kadar yakın günler birlikte kullanılamaz;
- her gün gereken slot sayısı tam karşılanır;
- her gün tipi için toplam slot sayısı tam karşılanır;
- bütün personelin hedef toplamları toplam slot sayısına eşitlenir;
- manuel atama, min/max, birlikte/ayrı ve görev havuzu kısıtları uygulanır.

WE, WD, saat, gün tipi ve geçmiş nöbet dengesi ise esas olarak ceza fonksiyonu
ile optimize edilir. Çıktıdaki `hedef_we` ve `hedef_wd`, bulunan tip hedeflerinin
toplamıdır; ayrı bir “her kişide WE = WD” hard eşitliği değildir. Saat cezası
aylık modelde WE/WD cezasından da daha düşük ağırlıktadır (`saat=2`, `we=40`,
`wd=40`; tarihselde `20/400/400`). Geçmiş nöbet de hard kota değildir; yalnızca
geçmiş verisi karşılaştırılabilir, en az iki kişilik adalet gruplarında objective'e
girer.

## Nihai çizelgenin hedefi birebir uygulamamasının nedeni

Plan kontratı varsayılanları şöyledir:

```text
toplam_hard          = false
gun_tipi_toleransi   = 1
gorev_kota_toleransi = 1
```

Bu nedenle kilitli hedef yoksa final solver:

- toplamı hedefin üzerine çıkarmaz, ancak hedefin altında kalabilir;
- her gün tipi hedefini varsayılan olarak ±1 aralıkta uygular;
- görev kotalarını varsayılan olarak ±1 aralıkta uygular.

Kilitli hedeflerde tolerans 0'a iner. Otomatik gevşetme izinliyse kesin
`INFEASIBLE` sonrasında tolerans daha da açılabilir. Bu, ekrandaki “hedef” ile
son çizelgedeki gerçekleşenin her zaman aynı olmasını engeller.

Önceki sürümde hedef snapshot'ı taşınmadığı için bu ayrışma oluşabiliyordu.
Güncel akışta kapasite FEASIBLE ise `planKontrati` frontend'de saklanır ve final
`nobet_coz` isteğine gönderilir; backend kişi setini, hedef tiplerini, toplamı,
görev kotalarını, `ara_gun` değerini ve varsa kontrat içerik hash'ini doğrular.
Geçerli snapshot kullanıldığında ortak hedef üretimi atlanır. Snapshot eksikse
geriye uyumlu yeniden hesaplama yapılır; bozuk snapshot sessizce kabul edilmez.

## Cuma mazereti ve Cuma–Pazar takası

Bir kişinin bütün Cumaları mazeretliyse geçmişte Cuma nöbeti fazla/az olmuş
olması bu mazereti ezmez. O kişiye Cuma yazılmaz. Eğer ayın Cuma slotlarını
dolduracak başka uygun personel yoksa hedef modeli, gün tipi toplamı hard olduğu
için `INFEASIBLE` olur. Başka personel yeterliyse model çalışabilir ve WE
adaleti için o kişiye Pazar/Cumartesi hedefi önerebilir; bu garanti edilmiş bir
“Cuma yerine Pazar” kuralı değildir.

Final solver'da 16 saatlik `cum <-> pzr` eşdeğer geçişi düşük ceza ile tercih
edilebilir. Ancak bu geçiş, S2 gün-tipi alt sınırını kaldırmaz. Örneğin hedef
Cuma sayısı 3 ve varsayılan tolerans 1 ise final model en az 2 Cuma ister;
kişinin bütün Cumaları mazeretliyse Pazar'a kayarak bu hard alt sınırı aşamaz.
Bu yalnız final atamasındaki soft bir tercihtir; hedef modelinin hard Cuma
toplamını çözemediği durumda geriye dönüp otomatik Pazar takası üretemez.
`takas_onerileri` ise boş slot tanısı için yerel, doğrulama gerektiren adaylar
(`dogrulanmis:false`, `otomatik_uygulanabilir:false`) üretir; genel bir gün tipi
değişim motoru değildir. Mazeret nedeniyle Cuma'ya hiç aday olamayan kişiye bu
kanaldan güvenilir bir Cuma–Pazar takası çıkmaz.

Geçmiş nöbet çetelesi de ikiye ayrılmalı. Geçmişteki gün-tipi sayıları
(`hici/prs/cum/cmt/pzr`) en az iki karşılaştırılabilir kişi bulunan, muaf
olmayan ve `gecmisVeriDurumu` `tam/kismi` olan gruplarda yalnız soft tarihsel
adalet cezasına girer. Geçmiş görev çetelesi (`gecmisGorevler`) tek başına Cuma
veya WE/WD borcu oluşturmaz. Bu nedenle “geçmişte Cuma az aldı, bu ay mutlaka
Cuma ver” kuralı mevcut modelde hard kural değildir.

## `INFEASIBLE` nedenleri

Hedef modeli her gün `slot_sayisi` kadar farklı kişi bulunmasını ve ay sonunda
tüm slotların toplamını hard ister. Nihai `NobetSolver` ise boş slot değişkeni
taşır ve kısmi çizelge üretebilir. Bu iki sözleşme farklı olduğu için hedef
katmanı “tam ay hedefi üretilemiyor” diyerek kesilirken final katmanı aslında
boş slotlu bir çizelge üretebilecek durumda olabilir. Kullanıcı kısmi çizelgeyi
seçebilecek olsa bile bu seçim hedef modeli başarısız olduktan sonra her zaman
ulaşılabilir değildir.

### 1. Gerçek matematiksel kapasite yetersizliği

31 gün, 5 slot ve `ara_gun=2` için talep 155 atamadır. Bir kişi yakın gün
kısıtı nedeniyle ay boyunca yaklaşık 11 gün kullanılabilir; mazeretler bu sayıyı
daha da düşürür. Örneğin 10 kişilik kadronun üst sınırı yaklaşık 110 olur ve
155 talep gerçek anlamda imkânsızdır. Sentetik denemede bu tip örnekler yaklaşık
5 saniyede hedef modeli tarafından reddedildi. Personel sayısı arttırıldığında
aynı yapı çözülebiliyor.

### 2. Hedef sayımı mümkün, günlük rol yerleşimi mümkün değil

Hedef modeli kişi/gün/gün tipi sayımı ve görev havuzu için transport sınırları
kuruyor. Nihai model ise aynı anda kişi + gün + görev + bina + manuel atama +
ayrı/birlikte kurallarını çözüyor. Günlük Hall tipi kesişim çatışmaları hedef
modelinde görünmeyip final tam modelinde `INFEASIBLE` olabilir.

### 3. Kesin sonuç yerine zaman aşımı

Kapasite endpoint'i `maxSure` değerini en fazla 20 saniye kabul ediyor ve bu
bütçeyi yaklaşık olarak şöyle bölüyor:

```text
tam doluluk kanıtı       1–3 sn
minimum boşluk modeli    kalan bütçenin bir bölümü
unsat-core / what-if     varsa kalan süre
```

Büyük örneklerde ilk model `UNKNOWN` dönebilir. `UNKNOWN`, “imkânsız” demek
değildir; solver süresinde karar veremedi demektir. Kapasite endpoint'i tam
model için bu ayrımı koruyor. Ortak hedef planı başarısız olduğunda üst zarf da
`durum`, `karar_durumu`, `asama: HEDEF` ve `kapsam.asama` alanlarını korur;
kesin `INFEASIBLE` ile süre kaynaklı `UNKNOWN` birbirine çevrilmez. Frontend bu
durumu fiziksel kapasite kanıtı gibi göstermez.

## Performans kanıtı

- Hedef modeli her çalışmada önce yaklaşık 1 saniyelik projection, ardından
  örnek boyutuna göre en az 7 saniyelik detay adalet geçişi çalıştırıyor;
  üst sınır 30 saniye.
- 12×31×4 ve 20×31×4 sentetik örneklerinde hedef hesabı yaklaşık 7–11 saniye
  sürdü; daha büyük örneklerde 17–22 saniyeye çıktı.
- 20 personel × 31 gün × 6 slot gibi kısıtsız örneklerde kapasite modeli süre
  sınırı içinde farklı çalıştırmalarda `FEASIBLE` veya `UNKNOWN` dönebildi;
  bu, kararsızlığın bir bölümünün kanıt süresi ve çok iş parçacıklı CP-SAT
  aramasından geldiğini gösteriyor.
- Timeout'lar da aynı sınırda değil: kapasite Cloud Function 120 saniye,
  tarayıcı 150 saniye; hedef 300/310 saniye; final çözüm 540/330 saniye
  (backend/tarayıcı). Tarayıcı final isteğini 330 saniyede keserken Cloud
  Function çalışmaya devam edebilir; bu gereksiz arka plan maliyeti ve üst üste
  istek riski oluşturur.
- Smoke test geçti; `_regression_test.py` 83/83 geçti. Bu testler küçük/izole
  davranışları doğrular, büyük aylık modelin kanıt süresini doğrulamaz.

## Uygulanan P0 düzeltmeleri ve kalan yol haritası

### Tamamlanan P0 düzeltmeleri

1. **Tek plan kontratı:** `/nobet_hedef_hesapla` ortak hedef/plan kontratını üretir; kapasite ve final endpoint'leri FEASIBLE snapshot'ı doğrudan tüketir. Aynı girdide hedef modeli ikinci kez çalıştırılmaz. Final `/nobet_coz` geçerli snapshot'ı doğrular ve aynı hedef/kotaları tüketir.
2. **Bayatlık ve bütünlük kontrolü:** `planHash` kapasite ve finalde aynı `plan_girdi_hash` sözleşmesine dayanır. Kişi seti, beş gün tipi hedefi, toplam, görev kotaları, `ara_gun`, negatif değerler ve kontrat içerik hash'i doğrulanır. Geçersiz snapshot 409 `PlanSnapshotGecersiz` döner.
3. **Elle hedef değişikliği:** Kullanıcı hücre düzenlemeleri `kilitliHedefler` olarak saklanır; eski kontrat temizlenir. Otomatik üretilen hedefler yanlışlıkla hard kilit sayılmaz.
4. **Kesinlik ayrımı:** Kesin `INFEASIBLE` ile süre içinde karar verilemeyen `UNKNOWN` ayrılır. `UNKNOWN` sonrası otomatik gevşetme yapılmaz; aşama bilgisi `HEDEF`/tam görev-slotu kapsamıyla raporlanır.
5. **Cuma mazereti:** Mazeret, izin, rapor, eğitim ve nöbet izni hard aday filtresidir. Tüm Cumaları mazeretli kişi Cuma'ya atanmaz; yeterli aday yoksa hızlı tanı `GUN_BAZLI_MAZERET_KAPASITESI` veya `GUN_TIPI_MAZERET_KAPASITESI` koduyla döner. Geçmiş Cuma borcu bu hard filtreyi ezmez.
6. **Hızlı ön kontrol:** Günlük veya gün tipi uygun aday arzı slot talebinin altındaysa ağır hedef CP-SAT modeli çalıştırılmadan kesin `INFEASIBLE` tanısı döner. Cuma arzı için Pazar önerisi yalnız doğrulama gerektiren bir nottur; otomatik takas yapılmaz.

### Uygulanan P1 düzeltmeleri ve kalan işler

1. **Hedef/kapasite ayrımı uygulandı:** Hedef butonu artık doğrudan `/nobet_hedef_hesapla` çağırır ve plan kontratını ekrana taşır. Tam görev-slotu kapasite kontrolü çizelge oluşturma adımının hemen öncesinde aynı snapshot ile çalışır; hedef ekranı kapasite kararını beklemez.
2. **Aşama alanları eklendi:** API yanıtları `asama` (`HEDEF`, `KAPASITE`, `FINAL`), `durum` ve `karar_durumu` döndürür. Kesin `INFEASIBLE` ile `UNKNOWN` ayrımı korunur.
3. **Hızlı ön kontrol uygulandı:** Günlük ve gün tipi uygun aday arzı slot talebinin altındaysa CP-SAT öncesi kesin tanı döner. Bu kontrol Cuma mazereti örneğini milisaniyelik aşamaya indirir.
4. **Kalan iş — tam model sözleşmesi:** Hedef FEASIBLE ile tam görev-slotu FEASIBLE rozetleri arayüzde ayrı görünür hale getirilmeli; hedef katmanı görev/bina/havuz çakışmasının kanıtı değildir.
5. **Kalan iş — WE/WD politikası:** Varsayılan denge soft objective'tir; `WE = Cuma + Cumartesi + Pazar`, `WD = Hafta içi + Perşembe`. Kurum tam eşitlik istiyorsa oran/aralık veya hard kural açıkça seçilmelidir.
6. **Kalan iş — kanıtlı takas:** Cuma→Pazar önerisi yalnız aday kişi, ara-gün, görev/havuz, hedef toleransı ve WE toplamı yeniden doğrulanırsa uygulanabilir işaretlenmelidir; mevcut genel takas otomatik değildir.
7. **Kalan iş — cache ve izleme:** `plan_girdi_hash` anahtarında başarılı kontrat cache'i, CP-SAT wall time/conflict/branch metrikleri ve istek iptali eklenmelidir.

### Önerilen uygulama sırası

- **Sprint 1:** Hedef/kapasite UI ayrımı, aşama rozetleri ve hızlı ön kontrol.
- **Sprint 2:** Kanıtlı Cuma→Pazar what-if servisi, WE/WD kurum politikası, hedef cache'i.
- **Sprint 3:** Tam model ile hedef modelini tek CP-SAT akışında birleştirme veya hedefi açıkça advisory yapma; büyük aylar için performans ölçümü.

## İzlenmesi gereken alanlar

Gerçek kullanıcı vakasında şu alanlar birlikte kaydedilmelidir:

```text
durum
fizibilite.durum
istatistikler.status
istatistikler.karar_durumu
istatistikler.adalet.*
istatistikler.feasibility_debug
teshis.unsat_core
teshis.rol_sorunlari
teshis.ortak_butce_kapasitesi
planKontrati.uygulama
```

Bu alanlar hangi katmanın başarısız olduğunu ve sonucun kesin mi, süreye bağlı
`UNKNOWN` mı olduğunu ayırır.

## Tekrarlanabilirlik ve hedef sözleşmesi kanıtı

Yerel denetimde aynı girdiyi üç kez çalıştırdım: 14 gün, 5 personel, 1 slot/gün,
`ara_gun=0`, aynı `gun_tipleri` ve aynı personel kayıtları. Üç çalışmanın tümü
`FEASIBLE` döndü ve toplam hedef 14 olarak kaldı; fakat kişi bazlı hedef tipleri
ve kişi WE/WD değerleri her çalışmada farklı personellere dağıldı. Örnek olarak
bir çalışmada bir kişi `WE=2, WD=1`, başka çalışmada aynı kişi `WE=1, WD=2`
aldı. Süreler yaklaşık 7.15–8.78 saniye aralığındaydı. Bu bir kapasite hatası
değil, CP-SAT'in dört iş parçacıklı ve zaman bütçeli eşit amaç değerli çözümler
arasında farklı geçerli çözüm seçmesidir.

Bu gözlem kod akışındaki sözleşme açığını doğruluyor:

- `hedef_hesaplayici.py:567-570` WE/WD sınıflarını tanımlar; `826-848` toplam
  slot ve gün tipi arzını hard eşitler; `947-951` ve devamındaki adalet cezaları
  ise kişi bazlı WE/WD'yi objective olarak kullanır.
- `hedef_hesaplayici.py:1203-1205`, `1368-1405` hedef modeli için önce 1 saniye
  projeksiyon, sonra `_detay_cozum_sure_saniye()` (taban 7, tavan 30 saniye)
  geçişi çalıştırır.
- Frontend `public/index.html` kapasite hazırlığından dönen `planKontrati` ve
  hedefleri FEASIBLE durumda doğrudan kullanır; eski/eksik backend cevabında
  hedef endpoint'ine geriye uyumlu fallback vardır. Final isteği kontratı da
  taşır.
- Backend `main.py` geçerli snapshot geldiğinde `ortak_plan_uret` çağrısını
  atlar; snapshot yoksa eski yolu çalıştırır. `planlayici.py` içindeki
  `plan_girdi_hash` otomatik üretilen `hedef_tipler` ve `gorev_kotalari` alanlarını
  hash'ten özellikle çıkarır; bu hash girdinin bayatlığını denetler. Snapshot'ın
  içeriği ayrıca `plan_hash` ile doğrulanır. Kullanıcı hedef hücresini elle
  düzenlediğinde frontend bunu `kilitliHedefler` olarak kaydeder ve eski kontratı
  geçersizleştirir.

Sonuç: WE/WD toplam arzı doğru hesaplanıyor; kişi bazındaki hedef dağılımı
artık önizleme snapshot'ı ile finalde aynı kontrat üzerinden tüketiliyor. WE/WD
ve geçmiş adaleti yine soft objective olduğu için, mazeret ve görev arzı izin
vermiyorsa matematiksel eşitlik garanti edilmez; sapma istatistiklerde raporlanır.
# Güncel durum notu (1 Kasım 2026)

Bu denetim turunda iki ek düzeltme uygulandı: hedef butonu artık kapasite
analizini beklemeden doğrudan hedef endpoint'ini çalıştırıyor; kapasite kontrolü
çizelge oluşturma öncesine taşınıyor ve hedef snapshot'ını tüketiyor. Ayrıca
günlük/gün tipi aday arzı slot talebinin altındaysa CP-SAT öncesi hızlı kesin
tanı dönüyor. Regresyon paketi bu değişikliklerden sonra **83/83** geçti.

Bu raporun ilk denetiminde tarif edilen “hedefin finalde yeniden hesaplanması”
riski bu çalışma içinde giderildi. `/nobet_kapasite` FEASIBLE yanıtında ortak
`planKontrati` döner; frontend bunu saklar ve `/nobet_coz` isteğine `planKontrati`
ile birlikte taşır. Backend geçerli snapshot'ı kişi kümesi, beş gün tipi hedefi,
toplam hedef ve `ara_gun` bakımından doğrular; geçerliyse `ortak_plan_uret`
çağrısını atlayıp aynı hedef/kota snapshot'ını kullanır. Snapshot eksikse eski
istemciler için yeniden üretim korunur; snapshot yapısal olarak geçersizse 409
`PlanSnapshotGecersiz` döner. Kontratın `plan_hash` değeri de içerikle
karşılaştırılır. Bu nedenle aşağıdaki eski “snapshot taşınmıyor” ve “final her
zaman ortak_plan_uret çağırıyor” ifadeleri tarihsel bulgu olarak okunmalıdır;
mevcut akışta yalnız fallback durumunu tarif eder.
