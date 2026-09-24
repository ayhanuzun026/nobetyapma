import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const html = readFileSync('public/index.html', 'utf8');
const script = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)]
  .map(match => match[1]).join('\n');
const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const UID = 'same-user';
const MONTH = `users/${UID}/months/2026_9`;
const WORKSPACE = `users/${UID}/settings/workspace`;

function monthData(revision = 4) {
  return { ayarlar: { yil: '2026', ay: '9', gunluk: '2', aragun: '2', kurumProfili: '112', maxAraGun: '6' },
    personelListesi: [{ id: 'person-1', ad: 'Test personel' }], gorevler: [], step: 4,
    _meta: { revision, updatedAt: 100 } };
}

function cloud(initial = {}) {
  const store = { docs: new Map(Object.entries(initial).map(([key, value]) => [key, clone(value)])), reads: [], writes: [], failReads: new Set(), failWrites: false, writeBarrier: null };
  const snapshot = path => ({ exists: store.docs.has(path), id: path.split('/').at(-1), data: () => clone(store.docs.get(path)) });
  function reference(path) {
    return { path, collection(name) { return reference(`${path}/${name}`); }, doc(id) { return reference(`${path}/${id}`); },
      orderBy(field, direction) { assert.equal(field, '_meta.updatedAt'); assert.equal(direction, 'desc'); return this; },
      limit(count) { assert.equal(count, 1); return this; },
      async get(options) {
        store.reads.push({ path, options: clone(options) });
        if (store.failReads.has(path)) throw new Error('Simulated network read failure');
        if (path.endsWith('/months')) {
          const docs = [...store.docs.entries()].filter(([key]) => key.startsWith(`${path}/`))
            .sort((a, b) => Number(b[1]._meta?.updatedAt || 0) - Number(a[1]._meta?.updatedAt || 0)).slice(0, 1).map(([key]) => snapshot(key));
          return { docs, empty: docs.length === 0 };
        }
        return snapshot(path);
      },
      async set(value) { store.docs.set(path, clone(value)); store.writes.push({ path, value: clone(value) }); }
    };
  }
  store.db = { collection: reference, async runTransaction(callback) {
    if (store.failWrites) throw new Error('Simulated network write failure');
    if (store.writeBarrier) await store.writeBarrier;
    const pending = []; const result = await callback({ get: async ref => snapshot(ref.path), set: (ref, value) => pending.push({ path: ref.path, value: clone(value) }) });
    for (const write of pending) { store.docs.set(write.path, write.value); store.writes.push(write); } return result;
  } };
  return store;
}

function browser(store, local = {}) {
  const values = new Map(Object.entries(local)); const elements = new Map(); const timers = new Map(); const alerts = []; const statuses = [];
  const defaults = { 'inp-yil': '2025', 'inp-ay': '12', 'inp-gunluk': '5', 'inp-aragun': '2', 'inp-max-aragun': '5', 'inp-kurum-profili': 'genel' };
  function element(id) { if (!elements.has(id)) elements.set(id, { value: defaults[id] ?? '', style: {}, textContent: '', innerHTML: '', checked: false, classList: { add() {}, remove() {} }, addEventListener() {}, remove() {}, querySelector: selector => element(`${id} ${selector}`), querySelectorAll: () => [], appendChild() {}, insertBefore() {}, setAttribute() {}, click() {} }); return elements.get(id); }
  const auth = { currentUser: { uid: UID, isAnonymous: false }, getRedirectResult: async () => null, onAuthStateChanged() {}, signOut: async () => {} };
  const firestore = Object.assign(() => store.db, { FieldValue: { serverTimestamp: () => 200 } });
  const context = vm.createContext({ firebase: { auth: () => auth, firestore }, document: { getElementById: element, querySelector: element, querySelectorAll: () => [], addEventListener() {}, createElement: element },
    localStorage: { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, String(value)), removeItem: key => values.delete(key) }, window: { addEventListener() {} }, location: { reload() {} }, console: { log() {}, warn() {}, error() {} }, alert: text => alerts.push(text), confirm: () => true,
    setTimeout(callback) { const id = timers.size + 1; timers.set(id, callback); return id; }, clearTimeout: id => timers.delete(id), URL, Blob, Date, _testStatuses: statuses });
  vm.runInContext(script, context, { filename: 'public/index.html' });
  const renderers = ['tatilListesiGuncelle', 'kuralListesiGuncelle', 'gorevKisitlamaListesiGuncelle', 'havuzMevcutListeGuncelle', 'manuelAtamaListesiGuncelle', 'gorevleriGuncelle', 'personelTablosuGuncelle', 'ozetTablosuGuncelle', 'selectleriGuncelle', 'mazeretGridOlustur', 'hedefTablosuGuncelle', 'ozelGorevSectionGuncelle', 'onizlemeGuncelle', 'solverTeshisGuncelle'];
  vm.runInContext(`${renderers.map(name => `${name} = () => {};`).join('\n')} updateSyncStatus = (status, detail) => _testStatuses.push({status, detail});`, context);
  return { run: expression => vm.runInContext(expression, context), element, local: values, timers, alerts, statuses, auth };
}

test('two computers resume the same cloud period despite different local periods', async () => {
  const server = cloud({ [MONTH]: monthData(), [WORKSPACE]: { lastPeriod: { yil: 2026, ay: 9 } } });
  const pcA = browser(server, { [`nobet_last_yil_${UID}`]: '2025', [`nobet_last_ay_${UID}`]: '12' }); const pcB = browser(server, { [`nobet_last_yil_${UID}`]: '2026', [`nobet_last_ay_${UID}`]: '1' });
  await pcA.run('yukle()'); await pcB.run('yukle()');
  for (const pc of [pcA, pcB]) { assert.deepEqual(clone(pc.run('aktifDonem')), { yil: 2026, ay: 9 }); assert.equal(pc.run('personelListesi[0].id'), 'person-1'); assert.equal(pc.run('currentStep'), 4); assert.equal(pc.element('inp-kurum-profili').value, '112'); assert.equal(pc.element('inp-max-aragun').value, '6'); assert.equal(pc.timers.size, 0); }
  assert.equal(server.writes.length, 0);
  assert.ok(server.reads.some(read => read.path === WORKSPACE && read.options?.source === 'server'));
});

test('legacy account resumes its most recently saved cloud month', async () => {
  const server = cloud({ [MONTH]: monthData() }); const pc = browser(server); assert.equal(pc.run(`getFirestoreWorkspaceRef('${UID}').path`), WORKSPACE); assert.deepEqual(clone(await pc.run(`bulutSonDonemOku('${UID}')`)), { yil: 2026, ay: 9 }); assert.ok(server.reads.some(read => read.path === `users/${UID}/months`));
});

test('failed period read preserves local fallback without uploading stale or empty data', async () => {
  const server = cloud({ [MONTH]: monthData(), [WORKSPACE]: { lastPeriod: { yil: 2026, ay: 9 } } }); server.failReads.add(MONTH); const localData = monthData(1); localData.personelListesi[0].ad = 'Local unsynced work';
  const pc = browser(server, { [`nobet_v2_${UID}_2026_9`]: JSON.stringify(localData) }); await pc.run('yukle()'); assert.equal(pc.run('personelListesi[0].ad'), 'Local unsynced work'); assert.equal(server.writes.length, 0); assert.equal(pc.timers.size, 0); assert.equal(await pc.run('kaydetDonem(2026, 9, true)'), false); assert.equal(server.docs.get(MONTH).personelListesi[0].ad, 'Test personel'); assert.equal(server.writes.length, 0); assert.ok(pc.statuses.some(status => status.status === 'error'));
});

test('revision guard rejects a second computer overwriting a newer cloud revision', async () => {
  const server = cloud({ [MONTH]: monthData(), [WORKSPACE]: { lastPeriod: { yil: 2026, ay: 9 } } }); const pcA = browser(server); const pcB = browser(server); await pcA.run('yukle()'); await pcB.run('yukle()'); pcA.run("personelListesi[0].ad = 'Newer computer A work'"); assert.equal(await pcA.run('kaydetDonem(2026, 9, true)'), true); assert.equal(server.docs.get(MONTH)._meta.revision, 5); pcB.run("personelListesi[0].ad = 'Stale computer B work'"); assert.equal(await pcB.run('kaydetDonem(2026, 9, true)'), false); assert.equal(server.docs.get(MONTH).personelListesi[0].ad, 'Newer computer A work'); assert.equal(server.docs.get(MONTH)._meta.revision, 5); assert.equal(pcB.run('personelListesi[0].ad'), 'Stale computer B work'); assert.ok(pcB.statuses.some(status => status.status === 'conflict'));
});

test('unknown revision and changed user refuse a cloud write', async () => {
  const server = cloud({ [MONTH]: monthData() }); const pc = browser(server); assert.equal(await pc.run(`firestoreKaydet({}, '${UID}', 2026, 9)`), false); await pc.run('yukle({yil: 2026, ay: 9})'); pc.auth.currentUser.uid = 'another-user'; assert.equal(await pc.run(`firestoreKaydet({}, '${UID}', 2026, 9)`), false); assert.equal(server.writes.length, 0);
});

test('manual save waits for the server before confirming success', async () => {
  const server = cloud({ [MONTH]: monthData(), [WORKSPACE]: { lastPeriod: { yil: 2026, ay: 9 } } }); const pc = browser(server); await pc.run('yukle()'); let release; server.writeBarrier = new Promise(resolve => { release = resolve; }); const saved = pc.run('kaydetVeBildir()'); await Promise.resolve(); await Promise.resolve(); assert.equal(pc.alerts.length, 0); assert.equal(server.writes.length, 0); release(); await saved; assert.equal(server.docs.get(MONTH)._meta.revision, 5); assert.equal(pc.alerts.length, 1); assert.match(pc.alerts[0], /bulut/i);
});

test('pending local draft survives reload when the server revision is unchanged', async () => {
  const server = cloud({ [MONTH]: monthData(), [WORKSPACE]: { lastPeriod: { yil: 2026, ay: 9 } } });
  const pcA = browser(server);
  await pcA.run('yukle()');
  pcA.run("personelListesi[0].ad = 'Offline draft'");
  server.failWrites = true;
  assert.equal(await pcA.run('kaydetDonem(2026, 9, true)'), false);
  const key = `nobet_v2_${UID}_2026_9`;
  const syncMeta = JSON.parse(pcA.local.get(`${key}_sync`));
  assert.equal(syncMeta.pending, true);
  assert.equal(syncMeta.revision, 4);

  server.failWrites = false;
  const pcB = browser(server, Object.fromEntries(pcA.local));
  await pcB.run('yukle()');
  assert.equal(pcB.run('personelListesi[0].ad'), 'Offline draft');
  assert.equal(server.docs.get(MONTH).personelListesi[0].ad, 'Test personel');
  assert.equal(server.writes.length, 0);
});

test('pending draft with a newer server revision stays local but is blocked from upload', async () => {
  const server = cloud({ [MONTH]: monthData(), [WORKSPACE]: { lastPeriod: { yil: 2026, ay: 9 } } });
  const pcA = browser(server);
  await pcA.run('yukle()');
  pcA.run("personelListesi[0].ad = 'Stale local draft'");
  await pcA.run('kaydetDonem(2026, 9, false)');
  server.docs.set(MONTH, { ...monthData(5), personelListesi: [{ id: 'person-1', ad: 'Remote newer work' }] });

  const pcB = browser(server, Object.fromEntries(pcA.local));
  await pcB.run('yukle()');
  assert.equal(pcB.run('personelListesi[0].ad'), 'Stale local draft');
  assert.equal(await pcB.run('kaydetDonem(2026, 9, true)'), false);
  assert.equal(server.docs.get(MONTH).personelListesi[0].ad, 'Remote newer work');
  assert.equal(server.writes.length, 0);
  assert.ok(pcB.statuses.some(status => status.status === 'conflict'));
  // A rejected save must retain the original base revision in local metadata;
  // otherwise a reload could make the stale draft look current and permit an
  // eventual overwrite of the remote revision.
  const pcC = browser(server, Object.fromEntries(pcB.local));
  await pcC.run('yukle({yil: 2026, ay: 9})');
  assert.equal(pcC.run('personelListesi[0].ad'), 'Stale local draft');
  assert.equal(await pcC.run('kaydetDonem(2026, 9, true)'), false);
  assert.equal(server.docs.get(MONTH).personelListesi[0].ad, 'Remote newer work');
});

test('failed month read removes a previously trusted revision before fallback', async () => {
  const server = cloud({ [MONTH]: monthData(), [WORKSPACE]: { lastPeriod: { yil: 2026, ay: 9 } } });
  const pc = browser(server);
  await pc.run('yukle()');
  assert.equal(pc.run(`_donemBulutRevizyonlari.get('${`nobet_v2_${UID}_2026_9`}')`), 4);
  server.failReads.add(MONTH);
  await pc.run('yukle({yil: 2026, ay: 9})');
  assert.equal(pc.run(`_donemBulutRevizyonlari.has('${`nobet_v2_${UID}_2026_9`}')`), false);
  assert.equal(await pc.run('kaydetDonem(2026, 9, true)'), false);
  assert.equal(server.writes.length, 0);
});

test('period switch aborts when saving the previous period is rejected', async () => {
  const server = cloud({
    [MONTH]: monthData(),
    [WORKSPACE]: { lastPeriod: { yil: 2026, ay: 9 } },
    [`users/${UID}/months/2026_10`]: { ...monthData(1), ayarlar: { ...monthData(1).ayarlar, ay: '10' } }
  });
  const pc = browser(server);
  await pc.run('yukle()');
  // Make the current month's trusted revision stale before changing period.
  server.docs.set(MONTH, { ...monthData(5), personelListesi: [{ id: 'person-1', ad: 'Remote newer work' }] });
  pc.element('inp-yil').value = '2026';
  pc.element('inp-ay').value = '10';
  await pc.run('donemDegisti()');
  assert.deepEqual(clone(pc.run('aktifDonem')), { yil: 2026, ay: 9 });
  assert.equal(pc.element('inp-ay').value, '9');
  assert.equal(server.docs.get(`users/${UID}/months/2026_10`).ayarlar.ay, '10');
});

test('guest immediate save reports local success and never touches Firestore', async () => {
  const server = cloud({ [MONTH]: monthData() });
  const pc = browser(server);
  pc.auth.currentUser.isAnonymous = true;
  await pc.run('yukle({yil: 2026, ay: 9})');
  assert.equal(await pc.run('kaydetDonem(2026, 9, true)'), true);
  assert.equal(server.writes.length, 0);
});

test('immediate save cancels an older pending timer and writes only the latest draft', async () => {
  const server = cloud({ [MONTH]: monthData(), [WORKSPACE]: { lastPeriod: { yil: 2026, ay: 9 } } });
  const pc = browser(server);
  await pc.run('yukle()');
  pc.run("personelListesi[0].ad = 'Old scheduled draft'");
  await pc.run('kaydetDonem(2026, 9, false)');
  const oldCallbacks = [...pc.timers.values()];
  pc.run("personelListesi[0].ad = 'Latest immediate draft'");
  assert.equal(await pc.run('kaydetDonem(2026, 9, true)'), true);
  for (const callback of oldCallbacks) callback();
  await Promise.resolve();
  assert.equal(server.writes.length, 2); // month + workspace transaction
  assert.equal(server.docs.get(MONTH).personelListesi[0].ad, 'Latest immediate draft');
});

test('period switch preserves the previous snapshot and advances the shared period pointer', async () => {
  const nextMonth = `users/${UID}/months/2026_10`;
  const server = cloud({
    [MONTH]: monthData(),
    [WORKSPACE]: { lastPeriod: { yil: 2026, ay: 9 } },
    [nextMonth]: { ...monthData(0), ayarlar: { ...monthData(0).ayarlar, ay: '10' }, personelListesi: [{ id: 'person-1', ad: 'October data' }] }
  });
  const pc = browser(server);
  await pc.run('yukle()');
  pc.run("personelListesi[0].ad = 'September snapshot'");
  pc.element('inp-yil').value = '2026';
  pc.element('inp-ay').value = '10';
  await pc.run('donemDegisti()');
  assert.deepEqual(clone(pc.run('aktifDonem')), { yil: 2026, ay: 10 });
  assert.equal(pc.run('personelListesi[0].ad'), 'October data');
  assert.equal(server.docs.get(MONTH).personelListesi[0].ad, 'September snapshot');
  assert.deepEqual(server.docs.get(WORKSPACE).lastPeriod, { yil: 2026, ay: 10 });
});

test('previous-month copy prefers a newer cloud record over stale local data', async () => {
  const nextMonth = `users/${UID}/months/2026_10`;
  const stale = { ...monthData(1), personelListesi: [{ id: 'person-1', ad: 'Stale local previous month' }] };
  const server = cloud({
    [MONTH]: { ...monthData(4), personelListesi: [{ id: 'person-1', ad: 'Fresh cloud previous month' }] },
    [WORKSPACE]: { lastPeriod: { yil: 2026, ay: 10 } },
    [nextMonth]: { ...monthData(1), ayarlar: { ...monthData(1).ayarlar, ay: '10' }, personelListesi: [{ id: 'person-1', ad: 'Current month' }] }
  });
  const pc = browser(server, { [`nobet_v2_${UID}_2026_9`]: JSON.stringify(stale) });
  await pc.run('yukle()');
  await pc.run('oncekiAydanCek()');
  assert.equal(pc.run('personelListesi[0].ad'), 'Fresh cloud previous month');
  assert.ok(pc.alerts.length > 0);
});

test('deleted remote previous month does not resurrect a pending local copy', async () => {
  const nextMonth = `users/${UID}/months/2026_10`;
  const pending = { ...monthData(4), personelListesi: [{ id: 'person-1', ad: 'Deleted remote local draft' }] };
  const server = cloud({
    [WORKSPACE]: { lastPeriod: { yil: 2026, ay: 10 } },
    [nextMonth]: { ...monthData(1), ayarlar: { ...monthData(1).ayarlar, ay: '10' }, personelListesi: [{ id: 'person-1', ad: 'Current month' }] }
  });
  const key = `nobet_v2_${UID}_2026_9`;
  const pc = browser(server, {
    [key]: JSON.stringify(pending),
    [`${key}_sync`]: JSON.stringify({ pending: true, revision: 4 })
  });
  await pc.run('yukle()');
  await pc.run('oncekiAydanCek()');
  assert.equal(pc.run('personelListesi[0].ad'), 'Current month');
  assert.ok(pc.alerts.some(message => /veri.*bulunamad/i.test(message)));
  assert.equal(server.writes.length, 0);
});
