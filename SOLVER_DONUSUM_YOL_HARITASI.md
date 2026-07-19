# Nöbet Solver Dönüşüm Yol Haritası

## 1. Amaç

Mevcut `plan -> gün iskeleti -> CP-SAT` yapısı korunacaktır. Dönüşümün amacı,
aylık eşitlik arayan bir çözücüden aşağıdaki bilgileri birlikte değerlendiren,
denetlenebilir bir karar sistemine geçmektir:

- seçili adalet dönemindeki geçmiş nöbet, saat, WE/WD ve gün tipi yükü,
- kişinin iş yükü katsayısı ve min/max sınırları,
- mazeret, izin ve açıkça onaylanmış manuel istisnalar,
- kişi-gün-görev yetkisi ve görev havuzu,
- bina tabanlı ayrı tutma,
- strict birlikte grupları,
- kullanıcı onaylı, solver tarafından doğrulanmış tamir seçenekleri.

Temel ilke şudur:

> Hedef, yerleşimden bağımsız bir sayı tablosu değildir. Hedef; geçmiş borç,
> gerçek kişi-gün-görev kapasitesi ve hard kurallar altında uygulanabilir bir
> aylık yük önerisidir. Nihai kişi-gün-görev kararı ortak CP-SAT modelindedir.

## Uygulama Durumu — 19 Temmuz 2026

Bu çalışma kapsamında ilk güvenli dönüşüm paketi uygulanmıştır.

### Tamamlanan P0 çekirdeği

- Seçilen yılın Ocak ayından seçilen aydan önceki aya kadar ortak geçmiş dönem
  özeti hedef ve final isteklerinde kullanılmaktadır.
- Toplam, saat, WE/WD ve beş gün tipi için geçmiş+aylık borç modeli; iş yükü
  katsayısı, min/max nöbet ve eşitlemeden muafiyetle birlikte çalışmaktadır.
- Hedef modeli kişi-gün değişkenleriyle mazeret, manuel gün, ara-gün ve hard
  birlikte grubunun gerçek ortak tarih fizibilitesini doğrulamaktadır.
- Kapasitesi olmayan veya zorunlu/muaf yük alan personelden kalan yük, uygun
  aktif personele katsayı oranında yeniden dağıtılmaktadır.
- Görev havuzu açıkça gönderilmişse authoritative'dir; boş havuz `deny-all`
  anlamına gelir. Kota görev yetkisi sayılmaz.
- Kritik görev yetkisi, mazeret, manuel kilit, aynı gün tek görev, hard birlikte
  ve aynı bina ayrı kuralları otomatik gevşetilemez.
- `ignoreManualConflicts` toplu bypass'ı API ve frontend sözleşmesinden
  kaldırılmıştır.
- Açık hedef kilitleri ile hesaplanan hedefler ayrılmıştır. Strict politika veya
  açık hedef kilidi varken plan toleransları otomatik yükseltilmez.
- Contract v2 strict sonuçta boş slot kalırsa başarı dönmez;
  `PARTIAL_REPAIR_REQUIRED` ve korunmuş kısmi atamalar/tanılar döner.
- Frontend'e iş yükü, min/max, muafiyet, adalet grubu, yetkili görevler; göreve
  bina, kritiklik ve istisna politikası alanları eklenmiştir.
- Adalet önce/sonra devri, sınırlar, kullanılan geçmiş ve solver durumları audit
  çıktısına eklenmiştir.

### Doğrulanan kalite kapıları

- Python kabul/regresyon paketi: `25/25`.
- Solver smoke paketi: strict/onaysız ve açık onaylı tamir senaryoları dahil
  tamamı başarılı.
- Frontend ve yapılandırma paketi: `15/15`.
- Python derleme ve `git diff --check`: başarılı.
- 50 personel, 31 gün ve 6 günlük slotluk hedef stresinde 186 hedefin tamamı
  yaklaşık 10 saniyede üretilmiştir. Toplam yük geçişi optimal; detay adalet
  geçişi süre sınırında uygulanabilir sonuç vermiştir.

### Bilinçli olarak sonraki fazda kalanlar

- Hedef modeli kişi-gün düzeyinde exact olsa da görev havuzu ve görev yetkisini
  tarih seçimiyle tek `kişi-gün-görev` değişkeninde henüz birleştirmemektedir.
- `kismi` geçmiş, aktif ay/gün oranıyla normalize edilmemektedir; audit uyarısıyla
  ham dönemde karşılaştırılmaktadır.
- Yönlü görev alternatifi, gün tipi geçiş matrisi, iki/üç kişilik doğrulanmış
  takas ve kullanıcıya çoklu çözüm kartları henüz P1 kapsamındadır.
- Final yerleşim solver'ı hâlâ ağırlıklı amaç kullanmaktadır; tam leksikografik
  solve/fix zinciri uygulanmamıştır.
- Request `planHash` gönderilmekte ancak backend henüz stale-plan için 409
  doğrulaması yapmamaktadır.
- Kısmi alan yeniden çözümü, hücre/hafta/görev kilidi, istisna audit ekranı,
  Excel istisna özeti ve kesin sonucu geçmişe idempotent yazma P2/P3 işidir.

## 2. Mevcut Durumun Doğrulanmış Özeti

### Hazır olan parçalar

- Frontend hedef ve final payload'larında geçmiş dönem seçili yılın Ocak ayından
  seçili aydan önceki aya kadar toplanmaktadır.
- `yillikGerceklesen` ve `gecmisGorevler` backend modeline ulaşmaktadır.
- Manuel atama gün ve slotu hard constraint olarak kilitlenmektedir.
- Mazeret normal atamalarda hard'dır; açık `mazeretOnayli` manuel hücresi için
  sınırlı istisna altyapısı vardır.
- Görev havuzu, exclusive görev, ara gün, unsat-core ve kapasite ön analizi için
  temel altyapı bulunmaktadır.
- Hedef endpoint'i ile final endpoint'i ortak planlayıcıyı kullanmaktadır.
- Greedy fallback aktif değildir; güvenlik sözleşmelerini taşımayan eski fallback
  geri getirilmemelidir.

### Kök sorunlar

- Geçmiş veriler hedef matematiğinde kullanılmadığından tarihsel borç hedefi
  değiştirmemektedir.
- Final solver'daki yıllık dengeleme plan kontratı aktifken çalışmadığı için geçmiş
  sonuçta da fiilen etkisizdir.
- Hesaplanmış her pozitif frontend hedefi kullanıcı kilidi sayılmaktadır. Bu durum
  manuel atama veya geçmiş değişince backend yeniden hedef hesaplamasını engeller.
- Ayrı tutma aynı bina yerine aynı görev adı üzerinde uygulanmaktadır.
- Birlikte kuralı yalnız soft cezadır ve otomatik gevşetme akışı kuralı tamamen
  kaldırabilmektedir.
- Exclusive/havuz ve ayrı kuralları topluca kaldırabilen otomatik dallar vardır.
- Manuel atama bazı ara gün ve birlikte istisnalarını örtük olarak onaylamaktadır.
- Pozitif görev kotası bazı kontrollerde yetki gibi yorumlanmaktadır. Kota yetki
  değildir.
- Başarılı fakat boş slotlu sonuçta ara gün kullanıcı onayı olmadan azaltılabilir.
- Frontend özel görev kota dağıtımı bütün yılları karıştırabilmekte ve backend
  planından sonra yerel olarak planı değiştirebilmektedir.

## 3. Hedef Veri Sözleşmesi

### Personel

```json
{
  "id": 101,
  "ad": "Ayşe Kaya",
  "isYukuKatsayisi": 1.0,
  "minNobet": 0,
  "maxNobet": null,
  "esitlemedenMuaf": false,
  "adaletGrubu": "normal",
  "gecmisVeriDurumu": "tam",
  "yetkiliGorevler": ["AMATEM", "MAVİ KOD"]
}
```

`gecmisVeriDurumu` değerleri:

- `tam`: seçili adalet dönemi eksiksizdir,
- `kismi`: dönem kısmen bilinmektedir; raporda güven seviyesi düşürülür,
- `yeni`: kişi dönem içinde başlamıştır; bilinmeyen aylar sıfır borç sayılmaz,
- `bilinmiyor`: tarihsel karşılaştırmaya katılmaz, yalnız aylık katsayı uygulanır.

İkinci fazda `iseBaslamaTarihi` eklenerek `yeni` durumunun beklenen yükü aktif gün
veya aktif ay oranıyla otomatik ölçeklenecektir.

### Görev

```json
{
  "id": "amatem-1",
  "ad": "AMATEM",
  "baseName": "AMATEM",
  "binaId": "AMATEM_BINASI",
  "kritik": true,
  "exclusive": true,
  "istisnaPolitikasi": "asla",
  "alternatifGorevler": [
    {
      "gorev": "MAVİ KOD",
      "yon": "AMATEMDEN_MAVIYE",
      "kullaniciOnayi": true,
      "maliyet": 3
    }
  ]
}
```

`binaId` yoksa geriye uyumluluk politikası:

- normal görev: `ANA_BINA`,
- legacy `ayriBina=true`: `AYRI_BINA:<baseName>`.

### Kural

```json
{
  "tur": "ayri",
  "kisiler": [101, 202],
  "politika": "hard",
  "aslaGevsetme": true
}
```

Politikalar:

- `hard`: strict ve tamir aşamalarında kaldırılamaz,
- `kullanici_onayli`: strict modelde hard, yalnız açık istisna kaydıyla aşılabilir,
- `soft`: amaç fonksiyonunda cezadır.

### İstek üst bilgisi

```json
{
  "sozlesmeSurumu": 2,
  "gecmisDonem": {
    "politika": "yil_basi_onceki_ay",
    "baslangic": "2026-01",
    "bitis": "2026-06"
  },
  "tamirPolitikasi": {
    "mod": "strict",
    "araGunAzaltma": "onaysiz",
    "birlikteIstisnasi": "onaysiz"
  },
  "kilitliHedefler": {},
  "planHash": "..."
}
```

Hesaplanmış hedefler kullanıcı kilidi değildir. Yalnız `kilitliHedefler` hard
hedef kilidi oluşturur.

## 4. Tarihsel Adalet Matematiği

Kişi `p`, ölçüt `d` ve ölçeklenmiş iş yükü ağırlığı `w_p` olsun.

- `G_p,d`: seçili dönemde gerçekleşen geçmiş yük,
- `X_p,d`: bu ay için solver'ın ürettiği yük,
- `W`: adalet hesabındaki toplam ağırlık,
- `T_d`: geçmiş ve bu ay toplam ölçüt yükü.

Ölçeklenmiş borç:

```text
D_p,d = (G_p,d + X_p,d) * W - T_d * w_p
```

Solver `|D_p,d|` değerlerini küçültür. Bu formül:

- katsayısı `0.50` olan personelin beklenen payını yarıya indirir,
- geçmişte fazla cumartesi tutanın yeni cumartesi hedefini düşürür,
- geçmişte eksik kalan uygun kişiyi öne alır,
- ham sayıların farklı katsayılı personeller arasında yanlış eşitlenmesini önler.

Ölçütler:

1. toplam nöbet,
2. toplam saat,
3. WE ve WD,
4. `hici`, `prs`, `cum`, `cmt`, `pzr`,
5. yetkili olunan görev aileleri.

`gecmisVeriDurumu=bilinmiyor/yeni` olan kişi tarihsel sıfır nedeniyle yapay borçlu
sayılmaz; aylık katsayı adaletine katılmaya devam eder.

Hard kişi sınırları:

```text
alt sınır = max(manuel atama sayısı, minNobet)
üst sınır = min(ara-günlü gerçek kapasite, maxNobet varsa maxNobet)
```

Alt sınır üst sınırı aşarsa solver sessiz gevşetmez; açık teşhis döndürür.

## 5. Strict Model

Strict model aşağıdakileri asla otomatik değiştirmez:

- kritik görev yetkisi ve explicit görev havuzu,
- mazeret; yalnız onaylı manuel hücre istisnadır,
- manuel kişi-gün-görev kilidi,
- kişi başına günde en fazla bir görev,
- `hard/aslaGevsetme` ayrı kuralı,
- kullanıcı onayı olmayan birlikte istisnası,
- kullanıcı onayı olmayan ara gün istisnası.

Ayrı kuralı:

```text
aynı gün + aynı bina  -> yasak
aynı gün + farklı bina -> izinli
```

Birlikte kuralı strict aşamada:

- üyeler aynı gün çalışır,
- aynı/eşdeğer görev ailesinde kalır,
- açık kişi-gün istisnası olmayan üye otomatik ayrılmaz.

Strict aşama ya tam çizelge üretir ya da `INFEASIBLE` ve doğrulanabilir nedenler
döndürür. Boş slotu olan sonuç “tam strict çözüm” sayılmaz.

## 6. Tamir Motoru

Tamir motoru öneri ile uygulamayı ayırır.

1. Solver strict modeli çalıştırır.
2. Unsat-core ve kişi-gün-görev aday analizi kök nedeni çıkarır.
3. Her aday değişiklik ayrı bir senaryo olarak gerçek solver ile doğrulanır.
4. Güvenlik politikası otomatik değişikliğe izin vermiyorsa yalnız öneri döner.
5. Kullanıcı seçerse dar kapsamlı istisna kaydı oluşturulur ve solver yeniden çalışır.

Önerilen sıra:

1. aynı gün tipinde başka tarih,
2. aynı görev için başka yetkili kişi,
3. iki kişilik görev takası,
4. üç kişilik zincirleme takas,
5. `hici <-> prs`,
6. `cum <-> pzr`,
7. birlikte grubu topluca başka güne taşıma,
8. tanımlı yönlü taşma görevi,
9. kullanıcı onaylı cumartesi geçişi,
10. tek atama için kullanıcı onaylı ara gün istisnası,
11. kişi-gün bazlı kullanıcı onaylı birlikte istisnası,
12. boş bırakma.

Exclusive bayraklarını, görev havuzlarını veya bütün kuralları topluca silen bir
tamir adımı bulunmamalıdır.

## 7. Leksikografik Amaç

Tek ağırlıklı toplam yerine sıralı solve/fix yaklaşımı hedeflenir:

1. boş slot sayısını minimize et ve optimumu sabitle,
2. onaysız güvenlik ihlalini sıfırda sabitle,
3. strict birlikte uyumunu optimize et,
4. tarihsel toplam/saat/WE-WD borcunu optimize et,
5. gün tipi borcunu optimize et,
6. görev ailesi borcunu optimize et,
7. homojen yayılım ve düşük önemdeki tercihleri optimize et.

Her geçişte önceki optimum constraint olarak sonraki modele eklenir. Böylece düşük
önemli kota kazancı için daha yüksek önemdeki kural bozulmaz.

## 8. Fazlar

### P0-A — Güvenlik ve sözleşme

- personel adalet alanlarını geriye uyumlu parse et,
- görev `binaId/kritik/istisnaPolitikasi` alanlarını parse et,
- hesaplanmış hedef ile açık kullanıcı kilidini ayır,
- ayrı kuralını bina tabanlı yap,
- kritik/exclusive/havuz toplu otomatik gevşetmesini kapat,
- manuel örtük istisnaları kaldır,
- birlikte kuralını strict aşamada hard yap,
- onaysız ara gün azaltmayı durdur.

### P0-B — Tarihsel hedef

- iş yükü katsayılı geçmiş+aylık toplam borcu,
- saat ve WE/WD borcu,
- gün tipi borcu,
- min/max ve manuel alt sınır,
- gerçek ortak tarih kesişimi,
- hedef öncesi veri kalite ve kapasite teşhisi,
- önce/sonra devir raporu.

### P1 — Ortak kişi-gün-görev kapasitesi

- hedef hesaplayıcıya görev havuzlarını geçir,
- kişi-gün-görev uygunluk kümesini tek ortak yardımcıda üret,
- ara gün etkili gerçek üst kapasite,
- yönlü görev alternatif matrisi,
- gün tipi geçiş matrisi ve maliyetleri,
- iki ve üç kişilik doğrulanmış takas üreticisi,
- hafta sonu ve kritik görev kıtlık puanı.

### P2 — Kullanıcı kontrollü yeniden çözüm

- hücre, kişi, gün, hafta ve görev ailesi kilitleri,
- yalnız boş slotları çözme,
- seçili alanı yeniden çözme,
- doğrulanmış alternatifleri karşılaştırma,
- istisna onay/audit ekranı,
- kesinleştirilmiş çizelgeyi geçmişe idempotent ekleme.

### P3 — Raporlama ve operasyon

- Excel'e uygulanan geçiş ve istisna özeti,
- adalet önce/sonra tablosu,
- grup birlikte çalışma oranı,
- görev sapmaları,
- solver süre/kalite trendleri,
- gölgeli çalıştırma ve eski-yeni sonuç karşılaştırması.

## 9. Kabul Kapıları

P0 tamamlanmış sayılmadan aşağıdaki senaryolar otomatik test olmalıdır:

1. Geçmiş cumartesi `8/3` ise uygun ve borçlu kişi daha yüksek cumartesi hedefi alır.
2. Katsayı `0.50/1.00` olan eşit kapasiteli kişiler yaklaşık `1/2` yük oranına gider.
3. `minNobet/maxNobet` ve manuel alt sınır birlikte korunur.
4. Bilinmeyen geçmişi olan yeni personel yapay olarak en borçlu kişi sayılmaz.
5. Ortak cuma kümeleri `3/3`, kesişim `1` ise grup kapasitesi `1` raporlanır.
6. Ayrı kişiler farklı görevlerde ama aynı binadaysa aynı gün atanmaz.
7. Ayrı kişiler farklı binalardaysa aynı gün atanabilir.
8. Yetkisiz kişi kritik göreve kota, manuel atama veya tamirle giremez.
9. Mazeret yalnız açıkça onaylanmış manuel hücre için aşılabilir.
10. Manuel ara gün çakışması açık istisna yoksa reddedilir.
11. Birlikte grup strict çözümde ayrılmaz.
12. Exclusive/havuz/ayrı/birlikte kuralları otomatik topluca kaldırılmaz.
13. Onaysız ara gün azaltılmaz; öneri olarak raporlanır.
14. Hesaplanmış hedef kullanıcı kilidi sayılmaz; yalnız açık kilit korunur.
15. Manuel atama değişince hedef yeniden hesaplanır.
16. Final payload hedef payload'ıyla aynı saat ve geçmiş dönem sözleşmesini kullanır.
17. Aynı isimli iki personel sonuçta `personel_id` ile doğru eşleşir.
18. Kesinleştirme geçmişe aynı çizelgeyi ikinci kez eklemez.

## 10. Yayına Alma Stratejisi

1. Sözleşme v2 alanlarını önce geriye uyumlu ve kapalı özellik olarak yayınla.
2. Eski kayıtları yükleme sırasında varsayılanlarla migrate et; veriyi topluca
   geri yazma.
3. Tarihsel hedefi önce gölgeli çalıştır; eski ve yeni hedef farklarını logla.
4. Bina kimliği olmayan görevleri kullanıcıya `ANA_BINA` olarak göster ve
   kesinleştirmesini iste.
5. Strict modu varsayılan yap; tamir önerilerini uygulamadan yalnız raporla.
6. Onay/audit ekranı hazır olduktan sonra dar kapsamlı tamir uygulamalarını aç.
7. P0 kabul testleri ve regresyon kapıları geçmeden otomatik deploy yapma.

## 11. Ölçülecek Metrikler

- tam doluluk oranı,
- strict çözüm oranı,
- kullanıcı onayı isteyen çözüm oranı,
- kural türüne göre öneri/istisna sayısı,
- kişi bazında tarihsel borç önce/sonra,
- toplam saat ve WE/WD makası,
- gün tipi ve görev ailesi maksimum sapması,
- birlikte çalışma oranı,
- sıfır adaylı kişi-gün-görev sayısı,
- solve süresi ve timeout oranı,
- plan hash uyumsuzluğu ve bayat hedef sayısı.

Bu metrikler yalnız çözüm kalitesini değil, hangi kural veya veri alanının sistemi
darboğaza soktuğunu da görünür kılmalıdır.
