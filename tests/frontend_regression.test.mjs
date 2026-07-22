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
