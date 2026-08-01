import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const html = readFileSync('public/index.html', 'utf8');

test('doctype is the first token and inline scripts parse', () => {
  assert.match(html, /^<!DOCTYPE html>/i);
  const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)];
  assert.ok(scripts.length > 0);
  scripts.forEach((match, index) => new vm.Script(match[1], { filename: `inline-${index}.js` }));
});

test('external scripts are version pinned and protected with SRI', () => {
  assert.doesNotMatch(html, /read-excel-file@5\.x/);
  const externalScripts = [...html.matchAll(/<script\s+[^>]*src="https:[^"]+"[^>]*><\/script>/gi)];
  assert.ok(externalScripts.length >= 5);
  for (const [tag] of externalScripts) {
    assert.match(tag, /integrity="sha384-[^"]+"/);
    assert.match(tag, /crossorigin="anonymous"/);
  }
});

test('ExcelJS avoids its CSP-blocked dynamic evaluation fallback', () => {
  const runtimeBinding = html.indexOf('<script>var regeneratorRuntime;</script>');
  const excelJs = html.indexOf('exceljs/4.3.0/exceljs.min.js');
  assert.ok(runtimeBinding >= 0, 'regeneratorRuntime binding missing');
  assert.ok(excelJs > runtimeBinding, 'regeneratorRuntime must be declared before ExcelJS');
});

test('task count invalidation stays inside the scope that computes it', () => {
  const updateStart = html.indexOf('function gorevleriGuncelle()');
  const changeStart = html.indexOf('function gorevDegisti()', updateStart);
  const nextStart = html.indexOf('function getOzelGorevler()', changeStart);
  assert.ok(updateStart >= 0 && changeStart > updateStart && nextStart > changeStart);

  const updateBody = html.slice(updateStart, changeStart);
  const changeBody = html.slice(changeStart, nextStart);
  assert.match(updateBody, /const gorevSayisiDegisti\s*=/);
  assert.match(updateBody, /if \(gorevSayisiDegisti\) hedefleriBayatIsaretle/);
  assert.doesNotMatch(changeBody, /gorevSayisiDegisti/);
});

test('period changes load the selected period instead of copying global state', () => {
  assert.match(html, /function donemDegisti\(\)/);
  assert.match(html, /addEventListener\('change', donemDegisti\)/);
  assert.match(html, /donemVerisiEslesiyor/);
  assert.match(html, /await kaydetDonem\(oncekiDonem\.yil, oncekiDonem\.ay, true\)/);
});

test('sign-in failures surface the auth code and fall back to redirect', () => {
  // Jenerik "tekrar deneyin" yerine kod + aciklama.
  assert.match(html, /function girisHatasiMetni\(kod\)/);
  assert.match(html, /'auth\/unauthorized-domain':/);
  assert.match(html, /Hata kodu: \$\{kod \|\| 'bilinmiyor'\}/);
  // Popup engellenirse yonlendirmeli girise dusulur.
  assert.match(html, /kod === 'auth\/popup-blocked'/);
  assert.match(html, /await auth\.signInWithRedirect\(saglayici\)/);
  assert.match(html, /auth\.getRedirectResult\(\)\.catch/);
  // Kullanici kendi kapattiysa sessiz kalinir.
  assert.match(html, /kod === 'auth\/popup-closed-by-user'/);
});

test('guest mode warns that data never reaches the cloud', () => {
  // Bulut senkronu !isAnonymous sartina bagli — misafir verisi yalniz yerelde.
  assert.match(html, /auth\.currentUser && !auth\.currentUser\.isAnonymous/);
  // Giris oncesi acik onay.
  assert.match(html, /Misafir olarak devam edilsin mi\?/);
  assert.match(html, /if \(!onay\) return;/);
  // Oturum boyunca kalici bant.
  assert.match(html, /function misafirUyarisiGuncelle\(misafirMi\)/);
  assert.match(html, /misafirUyarisiGuncelle\(user\.isAnonymous\)/);
  assert.match(html, /bant\.textContent = 'Misafir modundasınız/);
});

test('unmet 112 duty minimums require explicit user confirmation before solving', () => {
  assert.match(html, /function minNobetAcigiOnayiAl\(\)/);
  // Acik yoksa hic sorulmaz (genel profil etkilenmez).
  assert.match(html, /if \(!Array\.isArray\(aciklar\) \|\| aciklar\.length === 0\) return true/);
  assert.match(html, /if \(toplamAcik <= 0\) return true/);
  // Kim/ne kadar eksik gosterilir ve onay alinir.
  assert.match(html, /nöbet gerekiyordu, /);
  assert.match(html, /Yine de listeyi oluşturayım mı\?/);
  assert.match(html, /return confirm\(satirlar\.join\('\\n'\)\)/);
  // Onaylanmazsa cozum baslatilmaz.
  assert.match(html, /if \(!minNobetAcigiOnayiAl\(\)\) \{/);
  const cagri = html.indexOf('if (!minNobetAcigiOnayiAl())');
  const onKontrol = html.indexOf('const onKontrol = ortoolsOnKontrolYap', cagri);
  assert.ok(cagri > 0 && onKontrol > cagri, 'onay cozum baslamadan once sorulmali');
});

test('target-calculation failures show a human diagnosis, not raw solver debug', () => {
  // Backend insan dili tani uretir; frontend onu jenerik metinle degistirmemeli.
  assert.match(html, /function hedefHatasiniGoster\(error\)/);
  assert.match(html, /hata\.hedefTanisi\s*=\s*result\.hedefTanisi/);
  assert.match(html, /let sonHedefHatasi = null/);
  // Gercek hata jenerik mesajla ezilmiyor.
  assert.match(html, /throw sonHedefHatasi\s*\n?\s*\|\|/);
  // Oneriler listelenir, ham debug yalniz konsola gider.
  assert.match(html, /satirlar\.push\('', 'Ne yapabilirsiniz:'\)/);
  assert.match(html, /console\.warn\('\[HEDEF\]\[debug\]', tani\.debug\)/);
});

test('stale solver results cannot be previewed or downloaded', () => {
  assert.match(html, /function cozumGirdiImzasiOlustur\(\)/);
  assert.match(html, /function cozumGecerliMi\(\)/);
  assert.match(html, /function cozumuGecersizKil\(/);
  assert.match(html, /Çizelge güncel değil\. Listeyi yeniden oluşturun!/);
});

test('OR-Tools result conversion returns the schedule instead of overwriting it with undefined', () => {
  const start = html.indexOf('function isleOrtoolsSonucu');
  const end = html.indexOf('function onizlemeGuncelle', start);
  assert.ok(start >= 0 && end > start);
  const body = html.slice(start, end);
  assert.match(body, /return\s*{[\s\S]*atanan,[\s\S]*kisiAtama,[\s\S]*siraliGorevler/);
  assert.doesNotMatch(body, /hesaplananListe\s*=/);
  assert.match(html, /hesaplananListe\s*=\s*isleOrtoolsSonucu\(result, gunSayisi\)/);
});

test('targeted inline-handler XSS patterns and raw debug persistence are absent', () => {
  assert.doesNotMatch(html, /onclick="gorevHavuzuSil\('/);
  assert.doesNotMatch(html, /onblur="gorevKapasiteDegistir\('/);
  assert.doesNotMatch(html, /onblur="gorevKotasiDegistir\(/);
  assert.doesNotMatch(html, /collection\('debug_frontend_logs'\)/);
  assert.match(html, /escapeHtml\(x\.gorev\)/);
});

test('backend requests have abortable timeouts', () => {
  assert.match(html, /const controller = new AbortController\(\)/);
  assert.match(html, /timeoutMs: 330000/);
  assert.match(html, /timeoutMs: 310000/);
});

test('preparation capacity is an exact gate before target and schedule calculation', () => {
  assert.match(html, /async function hazirlikKapasiteKontrolEt\(requestData\)/);
  assert.match(html, /authFetch\(BACKEND_URL_KAPASITE/);
  const targetStart = html.indexOf('async function hedefHesaplaOrtools()');
  const targetEnd = html.indexOf('async function listeOlustur()', targetStart);
  const targetBody = html.slice(targetStart, targetEnd);
  const capacityCheck = targetBody.indexOf('await hazirlikKapasiteKontrolEt(requestData)');
  const targetRequest = targetBody.indexOf('authFetch(BACKEND_URL_HEDEF');
  assert.ok(capacityCheck >= 0 && targetRequest > capacityCheck);
  assert.match(html, /durum !== 'FEASIBLE'/);
  assert.match(html, /Hedef ve çizelge hesaplaması engellendi\./);
  assert.match(html, /Hazırlık kapasite durumu \$\{durum\}/);
});

test('capacity-affecting edits invalidate the previous preparation result', () => {
  const reasons = [
    'Resmi tatil eklendi.',
    'Görev havuzu değişti.',
    'Personel kuralı eklendi.',
    'Görev kısıtlaması eklendi.',
    'Mazeret veya izin değişti.',
    'Tüm mazeret ve izinler temizlendi.',
    'Ara gün değeri değişti.',
    'Kurum profili değişti.',
    'Geçmiş Excel verisi yüklendi.',
    'Geçmiş gün tipi verisi değişti.',
    'Saat değeri değişti.'
  ];
  for (const reason of reasons) {
    assert.ok(html.includes(`hedefleriBayatIsaretle('${reason}')`), `missing invalidation: ${reason}`);
  }
  assert.match(html, /hazirlikPaneli\.innerHTML = ''/);
  assert.match(html, /INFEASIBLE: Hazırlık kapasitesi yetersiz/);
});

test('strict contract sends authority fields without global manual bypass', () => {
  assert.match(html, /function normalizeYetkiliGorevler\(value\)/);
  assert.match(html, /yetkiliGorevler:\s*normalizeYetkiliGorevler\(p\.yetkiliGorevler\)/);
  assert.doesNotMatch(html, /ignoreManualConflicts/);
  assert.doesNotMatch(html, /listeOlusturOrtools\([^\n]*,\s*true\)/);
  assert.doesNotMatch(html, /çakışmalar kullanıcı onayıyla yok sayılıyor/i);
});

test('stale plan (409/PlanBayat) is handled distinctly and refreshes the hash', () => {
  // İstemci solve isteğinde tuttuğu planHash'i gönderir.
  assert.match(html, /planHash:\s*aktifPlanHash/);
  // 409 + PlanBayat özel olarak yakalanmalı (genel hataya karışmadan).
  assert.match(html, /response\.status === 409 && result\.error_type === 'PlanBayat'/);
  // Güncel hash benimsenmeli ki aynı verilerle tekrar deneme uyuşsun.
  assert.match(html, /aktifPlanHash = result\.guncelPlanHash/);
});

test('empty-slot swap suggestions (takas_onerileri) are surfaced in the diagnostic panel', () => {
  // Backend'in ürettiği eyleme dönük öneriler rapora alınmalı ve render edilmeli.
  assert.match(html, /takasOnerileri:\s*ist\.takas_onerileri \|\| \[\]/);
  assert.match(html, /Boş Slot İçin Uygulanabilir Öneriler/);
  assert.match(html, /o\.tur === 'dogrudan_atama'/);
});

test('12/12 half-shift fill suggestions (yari_vardiya) are rendered distinctly', () => {
  // Son çare 12/12 önerisi takas kartında ayrı badge + saat ikonu ile görünür.
  assert.match(html, /o\.tur === 'yari_vardiya' \? 'badge-warn'/);
  assert.match(html, /o\.tur === 'yari_vardiya' \? '⏱️ '/);
});

test('max ara gun (112) is configurable and sent to the solve endpoint', () => {
  assert.match(html, /id="inp-max-aragun"/);
  assert.match(html, /maxAraGun:\s*parseInt\(document\.getElementById\('inp-max-aragun'\)\?\.value\) \|\| 0/);
});

test('min nobet shortfall (112) is warned with completion suggestion', () => {
  // Backend istatistikleri min_nobet_aciklari uretir; frontend uyari panelinde gosterir.
  assert.match(html, /window\.ortoolsIstatistikler\?\.min_nobet_aciklari \|\| \[\]/);
  assert.match(html, /kişi min nöbetine ulaşamadı/);
  // Kim/ne kadar eksik + somut oneri (escape'li — XSS guvenli).
  assert.match(html, /escapeHtml\(a\.personel_ad\)/);
  assert.match(html, /escapeHtml\(a\.oneri\)/);
  assert.match(html, /a\.acik/);
});

test('leave types egitim/rapor are first-class and consistently gate availability', () => {
  // Ayrı Eğitim + Rapor giriş butonları.
  assert.match(html, /mazeretUygula\('egitim'\)/);
  assert.match(html, /mazeretUygula\('rapor'\)/);
  // Tek kaynak müsaitlik yardımcısı beş türü de kapsamalı.
  assert.match(html, /function personelIzinliMi\(p, gun\)/);
  assert.match(html, /p\?\.egitimler\?\.includes\(gun\)/);
  assert.match(html, /p\?\.raporlar\?\.includes\(gun\)/);
  // Grid görünürlüğü + CSS.
  assert.match(html, /cell\.classList\.add\('cal-egitim'\)/);
  assert.match(html, /cell\.classList\.add\('cal-rapor'\)/);
  assert.match(html, /\.cal-egitim\s*{/);
  assert.match(html, /\.cal-rapor\s*{/);
  // Her iki endpoint payload'ında egitimler+raporlar (hedef + coz = 2'şar).
  assert.ok([...html.matchAll(/egitimler:\s*p\.egitimler \|\| \[\]/g)].length >= 2);
  assert.ok([...html.matchAll(/raporlar:\s*p\.raporlar \|\| \[\]/g)].length >= 2);
});

test('kurum profili (112) selection persists and is sent to both endpoints', () => {
  // Adım 1'de profil seçimi UI'si (Genel / 112).
  assert.match(html, /id="inp-kurum-profili"/);
  assert.match(html, /112 \/ Ambulans/);
  // Saf yardımcı: yalnız "112" -> "112", geri kalan her şey "genel" (geriye uyumlu default).
  assert.match(html, /function getKurumProfili\(\)/);
  assert.match(html, /deger === '112' \? '112' : 'genel'/);
  // Kalıcı saklama: localStorage anahtarı + değişimde kaydet + açılışta geri yükle.
  assert.match(html, /const KURUM_PROFILI_KEY = 'nobet_kurum_profili'/);
  assert.match(html, /localStorage\.setItem\(KURUM_PROFILI_KEY, getKurumProfili\(\)\)/);
  assert.match(html, /kurumProfiliniGeriYukle\(\)/);
  // Her iki endpoint payload'ında da gönderilmeli.
  const coz = [...html.matchAll(/kurumProfili:\s*getKurumProfili\(\)/g)];
  assert.ok(coz.length >= 2, 'kurumProfili hem hedef hem coz payloadinda olmali');
});
