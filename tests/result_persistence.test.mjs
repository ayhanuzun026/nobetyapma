import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const html = readFileSync('public/index.html', 'utf8');

function functionSource(name) {
  const start = html.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} function not found`);
  const braceStart = html.indexOf('{', start);
  let depth = 0;
  let quote = null;
  let escaped = false;
  for (let i = braceStart; i < html.length; i++) {
    const ch = html[i];
    if (quote) {
      if (escaped) escaped = false;
      else if (ch === '\\') escaped = true;
      else if (ch === quote) quote = null;
      continue;
    }
    if (ch === '/' && html[i + 1] === '/') {
      i = html.indexOf('\n', i);
      continue;
    }
    if (ch === '/' && html[i + 1] === '*') {
      i = html.indexOf('*/', i + 2) + 1;
      continue;
    }
    if (ch === "'" || ch === '"' || ch === '`') {
      quote = ch;
      continue;
    }
    if (ch === '{') depth++;
    if (ch === '}' && --depth === 0) return html.slice(start, i + 1);
  }
  throw new Error(`${name} is not balanced`);
}

function evaluate(name, context) {
  const source = functionSource(name);
  const sandbox = {
    ...context,
    Object,
    Number,
    String,
    Boolean,
    JSON,
    Set,
    Math
  };
  vm.runInNewContext(`${source}; globalThis.result = ${name};`, sandbox);
  return [sandbox.result, sandbox];
}

test('result snapshots omit transient solver data and normalize assignment IDs', () => {
  const [create] = evaluate('cizelgeSonucSnapshotOlustur', {
    hesaplananListe: {
      atanan: { 1: { 0: 7 } },
      siraliGorevler: [{ id: 3, ad: 'A', baseName: 'A' }],
      _girdiImzasi: 'sig',
      _kismiCozum: true,
      _optimumKanitlandi: false
    },
    cozumGecerliMi: () => true,
    cozumGirdiImzasiOlustur: () => 'sig',
    getYilAy: () => ({ yil: 2026, ay: 2 }),
    getGunSayisi: () => 1
  });
  const snapshot = create();
  assert.equal(snapshot.surum, 1);
  assert.equal(snapshot.atanan['1']['0'], '7');
  assert.deepEqual(JSON.parse(JSON.stringify(snapshot.siraliGorevler)), [{ id: '3', ad: 'A', baseName: 'A' }]);
  assert.equal(snapshot.kismiCozum, true);
  assert.equal('kisiAtama' in snapshot, false);
  assert.equal('solverSorunRaporu' in snapshot, false);
});

test('snapshot signatures compare nested object keys independent of Firestore map order', () => {
  const [equal] = evaluate('cozumImzasiEsdegerMi', {});
  const a = JSON.stringify({ personeller: [{ id: 'p1', hedef: { hici: 1, prs: 2 } }, { id: 'p2' }], ay: 9 });
  const b = JSON.stringify({ ay: 9, personeller: [{ hedef: { prs: 2, hici: 1 }, id: 'p1' }, { id: 'p2' }] });
  assert.equal(equal(a, b), true);
  assert.equal(equal(a, JSON.stringify({ ...JSON.parse(a), ay: 10 })), false);
  assert.equal(equal(a, JSON.stringify({ ...JSON.parse(a), personeller: [...JSON.parse(a).personeller].reverse() })), false);
  assert.equal(equal(a, '{bozuk-imza'), false);
});

test('result restore rebuilds summaries from valid assignments and rejects stale/duplicate data', () => {
  const [equal] = evaluate('cozumImzasiEsdegerMi', {});
  const guncelImza = JSON.stringify({ yil: 2026, ay: 2, hedef: { hici: 1 } });
  const kaydedilenImza = JSON.stringify({ hedef: { hici: 1 }, ay: 2, yil: 2026 });
  const context = {
    GUN_TIPLERI: ['hici'],
    gorevler: [{ id: 1, ad: 'A' }, { id: 2, ad: 'B' }],
    personelListesi: [{ id: 'p1', ad: 'Bir', hedef: { hici: 1 } }, { id: 'p2', ad: 'Iki', hedef: { hici: 1 } }],
    getGunSayisi: () => 2,
    cozumGirdiImzasiOlustur: () => guncelImza,
    getOzelGorevler: () => [],
    getGunTipi: () => 'hici',
    cozumImzasiEsdegerMi: equal,
    hesaplananListe: null,
    aktifCozumIstekImzasi: null,
    atananlar: {}
  };
  const [restore, sandbox] = evaluate('cizelgeSonucSnapshotGeriYukle', context);
  const snapshot = {
    surum: 1,
    girdiImzasi: kaydedilenImza,
    siraliGorevler: [{ id: '1', ad: 'A' }, { id: '2', ad: 'B' }],
    atanan: { '1': { '0': 'p1' }, '2': { '1': 'p2' } }
  };
  assert.equal(restore(snapshot, 2026, 2), true);
  assert.equal(sandbox.hesaplananListe._girdiImzasi, guncelImza);
  assert.equal(sandbox.aktifCozumIstekImzasi, guncelImza);
  assert.equal(sandbox.hesaplananListe.atanan[1]['0'], 'p1');
  assert.deepEqual([...sandbox.hesaplananListe.kisiAtama.p1.gunler], [1]);
  assert.deepEqual([...sandbox.hesaplananListe.kisiAtama.p2.gunler], [2]);

  const duplicate = structuredClone(snapshot);
  duplicate.atanan['1']['1'] = 'p1';
  assert.equal(restore(duplicate, 2026, 2), false);
  assert.equal(restore({ ...snapshot, girdiImzasi: 'stale' }, 2026, 2), false);
});

test('period settings persist institution profile and maximum gap', () => {
  assert.match(html, /kurumProfili: getKurumProfili\(\)/);
  assert.match(html, /maxAraGun: document\.getElementById\('inp-max-aragun'\)/);
  assert.match(html, /data\.ayarlar\.kurumProfili === '112'/);
  assert.match(html, /data\.ayarlar\.maxAraGun/);
});
