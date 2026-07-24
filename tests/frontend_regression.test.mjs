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

test('period changes load the selected period instead of copying global state', () => {
  assert.match(html, /function donemDegisti\(\)/);
  assert.match(html, /addEventListener\('change', donemDegisti\)/);
  assert.match(html, /donemVerisiEslesiyor/);
  assert.match(html, /await kaydetDonem\(oncekiDonem\.yil, oncekiDonem\.ay, true\)/);
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
