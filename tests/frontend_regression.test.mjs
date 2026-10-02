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
  throw new Error(`${name} function is not balanced`);
}

function evaluateFunctions(names, expression) {
  const source = names.map(functionSource).join('\n');
  return vm.runInNewContext(`(() => { ${source}; return (${expression}); })()`);
}

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
  assert.match(html, /const tani = result\.hedefTanisi \|\| result\.istatistikler\?\.hedef_tanisi/);
  assert.match(html, /hata\.hedefTanisi = tani;/);
  assert.match(html, /let sonHedefHatasi = null/);
  // Hazirlik paneli yapılandırılmış hedef tanısını ve öneriyi kaybetmez.
  assert.match(html, /hazirlikHedefModeliHatasiniKaydet\(result, tani\)/);
  const renderer = functionSource('hazirlikKapasiteSonucuGoster');
  assert.match(renderer, /escapeHtml\(fizibilite\.oneri\)/);
  // Gercek hata jenerik mesajla ezilmiyor.
  assert.match(html, /throw sonHedefHatasi\s*\n?\s*\|\|/);
  // Oneriler listelenir, ham debug yalniz konsola gider.
  assert.match(html, /satirlar\.push\('', 'Ne yapabilirsiniz:'\)/);
  assert.match(html, /console\.warn\('\[HEDEF\]\[debug\]', tani\.debug\)/);
});

test('target UNKNOWN is never relabeled as proven infeasibility', () => {
  const normalize = evaluateFunctions(
    ['hazirlikHedefDurumuNormalizeEt'],
    'hazirlikHedefDurumuNormalizeEt'
  );
  assert.equal(normalize({ karar_durumu: 'INFEASIBLE' }), 'INFEASIBLE');
  assert.equal(normalize({ status: 'UNKNOWN' }), 'UNKNOWN');
  assert.equal(normalize({ status: 'MODEL_INVALID' }), 'UNKNOWN');
  assert.equal(normalize({}), 'UNKNOWN');

  const renderer = functionSource('hazirlikKapasiteSonucuGoster');
  assert.match(renderer, /hedefModeliHatasi[\s\S]*Hedef\/plan modeli için kesin hüküm verilemedi/);
  assert.match(renderer, /bu hata fiziksel kapasite kanıtı sayılmaz/);
});

test('PlanBos keeps its structured diagnosis and opens the preparation report', () => {
  const solve = functionSource('listeOlusturOrtools');
  assert.match(solve, /result\.error_type === 'PlanBos'/);
  assert.match(solve, /hata\.hedefTanisi = result\.hedefTanisi/);
  assert.match(solve, /hata\.hedefModeliHatasi = true/);
  assert.match(solve, /hazirlikHedefModeliHatasiniKaydet\(result, hata\.hedefTanisi\)/);
  const outerStart = html.indexOf('async function listeOlustur()');
  const outerEnd = html.indexOf('async function listeOlusturOrtools(', outerStart);
  assert.ok(outerStart >= 0 && outerEnd > outerStart, 'listeOlustur source not found');
  const outer = html.slice(outerStart, outerEnd);
  assert.match(outer, /if \(error\?\.hedefModeliHatasi\) \{[\s\S]*hedefHatasiniGoster\(error\);[\s\S]*return;/);
});

test('direct target-model failures stay on the structured diagnosis path', () => {
  const target = functionSource('hedefHesaplaOrtools');
  assert.match(
    target,
    /hata\.hedefTanisi = tani;\s*hata\.hedefModeliHatasi = true;\s*throw hata;/
  );
});

test('stale solver results cannot be previewed or downloaded', () => {
  assert.match(html, /function cozumGirdiImzasiOlustur\(\)/);
  assert.match(html, /function cozumGecerliMi\(\)/);
  assert.match(html, /function cozumuGecersizKil\(/);
  assert.match(html, /Çizelge güncel değil\. Listeyi yeniden oluşturun!/);
  assert.match(html, /kurumProfili: getKurumProfili\(\)/);
  assert.match(html, /maxAraGun: parseInt\(document\.getElementById\('inp-max-aragun'\)\?\.value\) \|\| 0/);
  assert.match(html, /hazirlikKismiCozumOnayi = null/);
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
  const renderer = functionSource('hazirlikKapasiteSonucuGoster');
  assert.match(renderer, /hazirlikKismiCizelgeTablosu\(kismiCozum\)/);
  assert.match(renderer, /escapeHtml\(hazirlikWhatIfMetni\(x\)\)/);
  assert.match(renderer, /escapeHtml\(hazirlikBosSlotMetni\(detay\)\)|escapeHtml\(x\)/);
  assert.doesNotMatch(renderer, /onclick=/);
  assert.match(renderer, /addEventListener\('click'/);
});

test('same-name staff remain distinct by ID and ambiguous legacy responses are rejected', () => {
  const context = vm.createContext({
    gorevler: [{ ad: 'Acil' }, { ad: 'Servis' }],
    personelListesi: [{ id: 1, ad: 'Ali', hedef: {} }, { id: 2, ad: 'Ali', hedef: {} }],
    getOzelGorevler: () => [], getYilAy: () => ({ yil: 2026, ay: 9 }),
    getGunTipi: () => 'hici', console: { log() {}, warn() {} }
  });
  vm.runInContext(functionSource('isleOrtoolsSonucu'), context);
  const result = context.isleOrtoolsSonucu({ atamalar: [
    { personel_id: 2, gun: 1, slot_idx: 0 }, { personel_id: 1, gun: 1, slot_idx: 1 }
  ], cizelge: { 1: ['Ali', 'Ali'] } }, 1);
  assert.equal(result.atanan[1][0], 2);
  assert.equal(result.atanan[1][1], 1);
  assert.throws(() => context.isleOrtoolsSonucu({ cizelge: { 1: ['Ali'] } }, 1), /benzersiz/);
  assert.throws(() => context.isleOrtoolsSonucu({ atamalar: [
    { personel_id: 99, gun: 1, slot_idx: 0 }
  ], cizelge: { 1: ['Ali'] } }, 1), /doğrulanamadı/);
});

test('history confirmation replaces one month idempotently and rejects partial or stale schedules', async () => {
  const types = ['hici', 'prs', 'cum', 'cmt', 'pzr'];
  const context = vm.createContext({
    GUN_TIPLERI: types, gorevler: [{ ad: 'Acil #1', baseName: 'Acil' }],
    personelListesi: [{ id: 1, yillikGecmis: { 2026: { 1: { cmt: 7 }, 2: { hici: 40 } } } }],
    hesaplananListe: { atanan: { 1: { 0: 1 }, 2: { 0: 1 } } },
    getYilAy: () => ({ yil: 2026, ay: 2 }), getGunSayisi: () => 2,
    getGunTipi: () => 'hici', cozumGecerliMi: () => true,
    cozumGirdiImzasiOlustur: () => 'current', confirm: () => true,
    alert() {}, ozetTablosuGuncelle() {}, saves: []
  });
  vm.runInContext(functionSource('cizelgeGecmisKayitlariniOlustur'), context);
  vm.runInContext('async ' + functionSource('cizelgeyiGecmiseKaydet'), context);
  context.kaydetDonem = async () => context.saves.push(JSON.stringify(context.personelListesi));
  await context.cizelgeyiGecmiseKaydet();
  await context.cizelgeyiGecmiseKaydet();
  assert.equal(context.saves.length, 2);
  assert.equal(context.saves[0], context.saves[1]);
  assert.equal(context.personelListesi[0].yillikGecmis[2026][2].hici, 2);
  assert.equal(context.personelListesi[0].yillikGecmis[2026][2].gorevler.Acil, 2);
  assert.equal(context.personelListesi[0].yillikGecmis[2026][1].cmt, 7);
  context.hesaplananListe._kismiCozum = true;
  await context.cizelgeyiGecmiseKaydet();
  assert.equal(context.saves.length, 2);
  context.hesaplananListe._kismiCozum = false;
  context.cozumGecerliMi = () => false;
  await context.cizelgeyiGecmiseKaydet();
  assert.equal(context.saves.length, 2);
});

test('historical summary includes only earlier months of the selected year', () => {
  const context = vm.createContext({
    GUN_TIPLERI: ['hici', 'prs', 'cum', 'cmt', 'pzr'], GECMIS_DONEM_POLITIKASI: 'secili_yil'
  });
  vm.runInContext(functionSource('gecmisOzetOlustur'), context);
  const summary = context.gecmisOzetOlustur({ yillikGecmis: {
    2025: { 1: { cmt: 100 } },
    2026: { 1: { cmt: 3, gorevler: { Acil: 2 } }, 2: { cmt: 1 }, 3: { cmt: 99 } }
  } }, 2026, 3);
  assert.equal(summary.yillikGerceklesen.cmt, 4);
  assert.equal(summary.gecmisGorevler.Acil, 2);
});

test('backend requests have abortable timeouts', () => {
  assert.match(html, /const controller = new AbortController\(\)/);
  assert.match(html, /timeoutMs: 330000/);
  assert.match(html, /timeoutMs: 310000/);
});

test('preparation capacity awaits an explicit decision before target calculation', () => {
  assert.match(html, /async function hazirlikKapasiteKontrolEt\(requestData\)/);
  assert.match(html, /authFetch\(BACKEND_URL_KAPASITE/);
  const targetStart = html.indexOf('async function hedefHesaplaOrtools()');
  const targetEnd = html.indexOf('async function listeOlustur()', targetStart);
  const targetBody = html.slice(targetStart, targetEnd);
  const capacityCheck = targetBody.indexOf('await hazirlikKapasiteKontrolEt(requestData)');
  const targetRequest = targetBody.indexOf('authFetch(BACKEND_URL_HEDEF');
  assert.ok(capacityCheck >= 0 && targetRequest > capacityCheck);
  assert.match(html, /const eylem = await kararPromise/);
  assert.match(html, /hazirlikKullaniciKarariBekle\(girdiImzasi\)/);
  assert.match(html, /if \(eylem === 'retry'\) continue/);
  assert.match(html, /if \(eylem === 'partial_adopted'\) return/);
  assert.match(html, /Açık kullanıcı kararı olmadan hedef veya yeni çizelge üretimine geçilmez/);
});

test('capacity response normalization accepts nested and top-level partial contracts', () => {
  const normalize = evaluateFunctions(
    ['hazirlikKapasiteYanitiNormalizeEt'],
    'hazirlikKapasiteYanitiNormalizeEt'
  );
  const nestedPartial = { solver_status: 'OPTIMAL', bos_slot: 2 };
  const nestedDiagnosis = { pencereler: [{ baslangic: 1, bitis: 3 }] };
  const nested = normalize({
    fizibilite: { durum: 'infeasible', kismi_cozum: nestedPartial, teshis: nestedDiagnosis }
  });
  assert.equal(nested.durum, 'INFEASIBLE');
  assert.equal(nested.kismiCozum.bos_slot, 2);
  assert.equal(nested.teshis.pencereler.length, 1);

  const top = normalize({
    durum: 'INFEASIBLE',
    kismi_cozum: { solver_status: 'FEASIBLE', bos_slot: 4 },
    teshis: { rol_sorunlari: [{ gorev: 'Ambulans' }] },
    fizibilite: { durum: 'INFEASIBLE' }
  });
  assert.equal(top.kismiCozum.bos_slot, 4);
  assert.equal(top.teshis.rol_sorunlari[0].gorev, 'Ambulans');
});

test('partial proof labels require both OPTIMAL status and explicit proof flag', () => {
  const proof = evaluateFunctions(
    ['hazirlikKismiOptimumKanitlandi'],
    'hazirlikKismiOptimumKanitlandi'
  );
  assert.equal(proof({ solver_status: 'OPTIMAL', optimum_kanitlandi: true }), true);
  assert.equal(proof({ solver_status: 'OPTIMAL', optimum_kanitlandi: false }), false);
  assert.equal(proof({ solver_status: 'FEASIBLE', optimum_kanitlandi: true }), false);
  assert.equal(proof({ solver_status: 'UNKNOWN', optimum_kanitlandi: false }), false);
});

test('pending preparation decision is a real promise and invalidation resolves it', async () => {
  const source = [
    'let bekleyenHazirlikKarari = null;',
    functionSource('hazirlikBekleyenKarariBitir'),
    functionSource('hazirlikKullaniciKarariBekle'),
    'return ({ wait: hazirlikKullaniciKarariBekle, finish: hazirlikBekleyenKarariBitir });'
  ].join('\n');
  const api = vm.runInNewContext(`(() => { ${source}; })()`);
  const pending = api.wait('signature-1');
  assert.equal(api.finish('stale'), true);
  assert.equal(await pending, 'stale');
  assert.equal(api.finish('retry'), false);
});

test('a stale in-flight capacity response is rejected before it restores panel state', () => {
  const body = functionSource('hazirlikKapasiteKontrolEt');
  const staleCheck = body.indexOf("girdiImzasi !== cozumGirdiImzasiOlustur()");
  const stateWrite = body.indexOf('hazirlikKapasiteSonucu = {');
  assert.ok(staleCheck >= 0 && stateWrite > staleCheck);
  const invalidation = functionSource('hedefleriBayatIsaretle');
  assert.match(invalidation, /hazirlikBekleyenKarariBitir\('stale'\)/);
  assert.match(invalidation, /hazirlikKismiCozumOnayi = null/);
});

test('conscious partial adoption uses full-model assignments and skips target API', () => {
  const adoption = functionSource('hazirlikKismiCozumuKabulEt');
  assert.match(adoption, /hazirlikKismiAtamalariniDogrula\(kismiCozum, gunSayisi\)/);
  assert.match(functionSource('hazirlikKismiAtamalariniDogrula'), /hazirlikKismiAtamalariNormalizeEt\(kismiCozum\)/);
  assert.match(adoption, /isleOrtoolsSonucu\(\{ atamalar, cizelge \}, gunSayisi\)/);
  assert.match(adoption, /donusenAtamaSayisi !== atamalar\.length/);
  assert.match(adoption, /hesaplananListe\._girdiImzasi = girdiImzasi/);
  assert.match(adoption, /hesaplananListe\._kismiCozum = true/);
  assert.match(adoption, /Kalite\/adalet optimizasyonu çalıştırılmadı/);
  assert.doesNotMatch(adoption, /authFetch/);

  const targetBody = html.slice(
    html.indexOf('async function hedefHesaplaOrtools()'),
    html.indexOf('async function listeOlustur()', html.indexOf('async function hedefHesaplaOrtools()'))
  );
  const adoptedCheck = targetBody.indexOf("kapasiteKarari?.action === 'partial_adopted'");
  const targetRequest = targetBody.indexOf('authFetch(BACKEND_URL_HEDEF');
  assert.ok(adoptedCheck >= 0 && targetRequest > adoptedCheck);
  assert.match(targetBody.slice(adoptedCheck, targetRequest), /return\s*\{/);
});

test('partial assignment contract rejects unknown, duplicate, and count-mismatched records', () => {
  const source = [
    'const personelListesi = [{ id: 1, ad: "A" }, { id: 2, ad: "B" }];',
    'const gorevler = [{ id: 1, ad: "G1" }, { id: 2, ad: "G2" }];',
    functionSource('hazirlikDizi'),
    functionSource('hazirlikAtamalari'),
    functionSource('hazirlikIlkSayi'),
    functionSource('hazirlikKismiAtamalariNormalizeEt'),
    functionSource('hazirlikKismiAtamalariniDogrula'),
    'return hazirlikKismiAtamalariniDogrula;'
  ].join('\n');
  const validate = vm.runInNewContext(`(() => { ${source} })()`);
  const base = {
    solver_status: 'OPTIMAL', doldurulan_slot: 2, toplam_slot: 3, bos_slot: 1,
    atamalar: [
      { personel_id: 1, gun: 1, slot_idx: 0 },
      { personel_id: 2, gun: 1, slot_idx: 1 }
    ]
  };
  assert.equal(validate(base, 31).gecerli, true);
  assert.equal(validate({ ...base, atamalar: [base.atamalar[0], { personel_id: 2, gun: 1, slot_idx: 0 }] }, 31).gecerli, false);
  assert.equal(validate({ ...base, atamalar: [base.atamalar[0], { personel_id: 1, gun: 1, slot_idx: 1 }] }, 31).gecerli, false);
  assert.equal(validate({ ...base, atamalar: [base.atamalar[0], { personel_id: 99, gun: 1, slot_idx: 1 }] }, 31).gecerli, false);
  assert.equal(validate({ ...base, atamalar: [base.atamalar[0], { personel_id: 99, personel_ad: 'B', gun: 1, slot_idx: 1 }] }, 31).gecerli, false);
  assert.equal(validate({ ...base, doldurulan_slot: 3 }, 31).gecerli, false);
  assert.equal(validate({ ...base, atamalar: [base.atamalar[0], { personel_id: 2, gun: 32, slot_idx: 1 }] }, 31).gecerli, false);
});

test('partial preview is read-only and separate from conscious adoption', () => {
  const action = functionSource('hazirlikKapasiteEylemiUygula');
  const previewBranch = action.slice(action.indexOf("eylem === 'preview'"), action.indexOf("eylem === 'partial'"));
  assert.match(previewBranch, /alan\.style\.display/);
  assert.doesNotMatch(previewBranch, /hesaplananListe|isleOrtoolsSonucu|hazirlikKismiCozumOnayi/);
  assert.match(html, /Kısmi çizelgeyi salt-okunur önizle/);
  assert.match(html, /Bilinçli olarak kısmi çizelge oluştur/);
  const table = functionSource('hazirlikKismiCizelgeTablosu');
  assert.match(table, /Array\.from\(\{ length: gunSayisi \}/);
  assert.match(table, /gorevler\.map/);
  assert.match(table, /escapeHtml\(personelAdi \|\| '—'\)/);
  assert.doesNotMatch(table, /hesaplananListe\s*=|atananlar\s*=|hazirlikKismiCozumOnayi\s*=/);
});

test('what-if cards show occupancy delta and distinguish proof from a found effect', () => {
  const format = evaluateFunctions(
    ['hazirlikKayitMetni', 'hazirlikWhatIfMetni'],
    'hazirlikWhatIfMetni'
  );
  const text = format({
    aciklama: 'Ara günü azalt', onceki_bos_slot: 12, bos_slot: 6,
    doldurulan_slot: 87, toplam_slot: 93, delta_bos_slot: 6
  });
  assert.match(text, /81\/93 → 87\/93/);
  assert.match(text, /6 boşluk kapandı/);
  const full = format({
    aciklama: 'Havuza kişi ekle', onceki_bos_slot: 1, bos_slot: 0,
    doldurulan_slot: 93, toplam_slot: 93, delta_bos_slot: 1,
    tam_doluluk_saglandi: true
  });
  assert.match(full, /tam dolu çizelge tanığı bulundu/);
  assert.match(html, /deltaKanitli \? 'Kanıtlı etki'/);
  assert.match(html, /karsilastirmaVar \? 'Bulunan iyileşme' : 'Geçerli çözüm tanığı'/);
  assert.match(html, /'Tam dolu çizelge tanığı'/);
});

test('preparation cancel and stale errors stay silent in target UI', () => {
  const body = functionSource('hedefleriHesapla');
  const catchStart = body.indexOf('} catch (error)');
  const catchBody = body.slice(catchStart);
  assert.match(catchBody, /if \(!error\?\.hazirlikKullaniciIptali\) hedefHatasiniGoster\(error\)/);
});

test('UNKNOWN partial can be previewed but cannot be adopted', () => {
  const flow = functionSource('hazirlikKapasiteKontrolEt');
  const clearIndex = flow.indexOf('cozumuGecersizKil(`Hazırlık kapasite durumu ${durum}`)');
  const waitIndex = flow.indexOf('hazirlikKullaniciKarariBekle(girdiImzasi)');
  assert.ok(clearIndex >= 0 && waitIndex > clearIndex);
  const renderer = functionSource('hazirlikKapasiteSonucuGoster');
  assert.match(renderer, /kismiOnizlemeVar = Boolean\(kismiCozum && atamalar\.length\)/);
  assert.match(renderer, /kismiBenimsemeVar = Boolean\(durum === 'INFEASIBLE' && kismiOnizlemeVar\)/);
  assert.match(renderer, /kismiOnizlemeVar \? '<button[^']+data-hazirlik-action="preview"/);
  assert.match(renderer, /kismiBenimsemeVar[\s\S]*data-hazirlik-action="partial"/);
  const action = functionSource('hazirlikKapasiteEylemiUygula');
  assert.match(action, /if \(normalize\.durum !== 'INFEASIBLE'\)/);
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
  assert.match(html, /Tam doluluğun imkânsız olduğu kanıtlandı/);
  assert.match(html, /Maksimum ara gün değeri değişti\./);
  const invalidation = functionSource('hedefleriBayatIsaretle');
  assert.match(invalidation, /if \(hesaplananListe \|\| window\.sonExcelUrl\)/);
  assert.match(invalidation, /cozumuGecersizKil/);
});

test('task capacity and quota edits invalidate the right plan state', () => {
  assert.match(functionSource('gorevKapasiteDegistir'), /hedefleriBayatIsaretle\('Görev kapasite hedefi değişti\.'\)/);
  assert.match(functionSource('ozelGorevKotalariniYenidenDagit'), /planHedefiniGecersizKil\('Görev kotaları yeniden dağıtıldı\.'\)/);
  assert.match(functionSource('gorevKotasiDegistir'), /planHedefiniGecersizKil\('Kişi görev kotası değişti\.'\)/);
  const planInvalidation = functionSource('planHedefiniGecersizKil');
  assert.match(planInvalidation, /aktifPlanKontrati = null/);
  assert.match(planInvalidation, /aktifPlanHash = null/);
  assert.match(planInvalidation, /KULLANICI_TARAFINDAN_DEGISTIRILDI_FINAL_MODELE_AKTARILACAK/);
  assert.match(planInvalidation, /cozumuGecersizKil/);
});

test('strict contract sends authority fields without global manual bypass', () => {
  assert.match(html, /function normalizeYetkiliGorevler\(value\)/);
  assert.match(html, /yetkiliGorevler:\s*normalizeYetkiliGorevler\(p\.yetkiliGorevler\)/);
  assert.doesNotMatch(html, /ignoreManualConflicts/);
  assert.doesNotMatch(html, /listeOlusturOrtools\([^\n]*,\s*true\)/);
  assert.doesNotMatch(html, /çakışmalar kullanıcı onayıyla yok sayılıyor/i);
});

test('stale plan (409/PlanBayat) invalidates targets instead of adopting an unseen plan', () => {
  // İstemci solve isteğinde tuttuğu planHash'i gönderir.
  assert.match(html, /planHash:\s*aktifPlanHash/);
  // 409 + PlanBayat özel olarak yakalanmalı (genel hataya karışmadan).
  assert.match(html, /response\.status === 409 && result\.error_type === 'PlanBayat'/);
  assert.doesNotMatch(html, /aktifPlanHash = result\.guncelPlanHash/);
  assert.match(functionSource('listeOlusturOrtools'), /hedefleriBayatIsaretle\('Plan girdileri değişti\.'/);
});

test('empty-slot swap suggestions are surfaced as non-mutating candidates', () => {
  // Backend'in ürettiği eyleme dönük öneriler rapora alınmalı ve render edilmeli.
  assert.match(html, /takasOnerileri:\s*ist\.takas_onerileri \|\| \[\]/);
  assert.match(html, /Boş Slot İçin Düzeltme Adayları/);
  assert.match(html, /o\.tur === 'dogrudan_atama'/);
  assert.match(html, /tam modelde yeniden çözülmeden kesin çözüm sayılmaz/i);
});

test('12/12 half-shift is clearly unverified and never auto-applied', () => {
  assert.match(html, /12\/12 — model dışı taslak/);
  assert.match(html, /Doğrulanmamıştır; açık alt-slot modeli gerekir ve otomatik uygulanmaz/);
  assert.doesNotMatch(html, /manuelAtamalar\.push\([\s\S]{0,500}yari_vardiya/);
});

test('max ara gun (112) is configurable and sent to the solve endpoint', () => {
  assert.match(html, /id="inp-max-aragun"/);
  assert.match(html, /maxAraGun:\s*parseInt\(document\.getElementById\('inp-max-aragun'\)\?\.value\) \|\| 0/);
  assert.ok([...html.matchAll(/maxAraGun:\s*parseInt\(document\.getElementById\('inp-max-aragun'\)\?\.value\) \|\| 0/g)].length >= 3);
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

test('infeasibility details render the human-readable sentence, not raw IDs', () => {
  // Saf yardimci: backend'in urettigi neden.detay.aciklama cumlesini cikarir.
  const metin = evaluateFunctions(
    ['hazirlikNedenAciklamasi'],
    'hazirlikNedenAciklamasi'
  );
  assert.equal(
    metin({ detay: { aciklama: 'Ayhan için 17. gün manuel atama var.' } }),
    'Ayhan için 17. gün manuel atama var.'
  );
  // Aciklama yoksa isimli alanlardan cumle kurar; ham ID'ye dusmez.
  assert.match(
    metin({ detay: { personel_ad: 'Veli', gun: 3 } }),
    /Veli/
  );
  assert.match(
    metin({ detay: { mazeretli_adlar: ['Ayhan', 'Veli'], gun: 5 } }),
    /Ayhan, Veli/
  );
  assert.match(
    metin({ detay: { grup_adlari: ['Ayhan', 'Can'] } }),
    /Ayhan, Can/
  );
  // Hicbir insan-dili alani yoksa bos doner (jenerik mesaj devrede kalir).
  assert.equal(metin({ detay: { slot_idx: 2 } }), '');
  assert.equal(metin({}), '');
  assert.equal(metin(null), '');

  // Panel bu cumleyi neden nesnesinden turetip escape ederek basar.
  const renderer = functionSource('hazirlikKapasiteSonucuGoster');
  assert.match(renderer, /const nedenAciklamasi = hazirlikNedenAciklamasi\(neden\)/);
  assert.match(renderer, /\$\{escapeHtml\(nedenAciklamasi\)\}/);
});

test('cross-task contention diagnosis reaches the rendered UI (no dead field)', () => {
  // Backend yeni alanlar üretir: teshis.aday_kesisimi_aciklari ve
  // teshis.ortak_butce_kapasitesi. Bunlar ekrana da basılmalı; aksi halde
  // INFEASIBLE raporunda kullanıcı yine boş "rol_sorunlari: []" görür.
  const renderer = functionSource('hazirlikKapasiteSonucuGoster');
  assert.match(renderer, /const kesisimAciklari = hazirlikDizi\(teshis\?\.aday_kesisimi_aciklari\)/);
  assert.match(renderer, /ortakButce = teshis\?\.ortak_butce_kapasitesi/);
  // Her iki kaynak da "sorun kartları" listesine girmeli.
  assert.match(renderer, /\.\.\.\(ortakButce\?\.aciklama \? \[String\(ortakButce\.aciklama\)\] : \[\]\)/);
  assert.match(renderer, /\.\.\.kesisimAciklari\.map\(x => hazirlikKayitMetni\(x\)\)/);
  // Engel kategorisi de görünmeli, yoksa metin ekrana hiç dökülmez.
  const kategoriler = functionSource('hazirlikEngelKategorileri');
  assert.match(kategoriler, /normalize\.teshis\?\.aday_kesisimi_aciklari/);
  assert.match(kategoriler, /ortak_butce_kapasitesi\?\.eksik/);
  assert.match(kategoriler, /çekişme: 'Görevler arası çekişme'/);
});
