import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

const firebaseConfig = JSON.parse(readFileSync('firebase.json', 'utf8'));
const firestoreIndexes = JSON.parse(readFileSync('firestore.indexes.json', 'utf8'));
const storageRules = readFileSync('storage.rules', 'utf8');
const firestoreRules = readFileSync('firestore.rules', 'utf8');
const requirements = readFileSync('functions/requirements.txt', 'utf8');
const mainPy = readFileSync('functions/main.py', 'utf8');
const packageJson = JSON.parse(readFileSync('package.json', 'utf8'));

test('storage is deny-by-default and frontend debug writes are closed', () => {
  assert.match(storageRules, /allow read, write: if false/);
  assert.match(firestoreRules, /allow create, update, delete: if false/);
  assert.doesNotMatch(firestoreRules, /^\s*allow read, write: if request\.auth != null;\s*$/m);
});

test('debug collections and payload chunks have TTL policies', () => {
  const ttlGroups = new Set(
    firestoreIndexes.fieldOverrides
      .filter((entry) => entry.fieldPath === 'expireAt' && entry.ttl === true)
      .map((entry) => entry.collectionGroup),
  );
  for (const group of ['debug_sessions', 'payload', 'debug_events', 'debug_frontend_logs']) {
    assert.ok(ttlGroups.has(group), `${group} TTL policy missing`);
  }
});

test('hosting emits baseline browser security headers', () => {
  const headers = Object.fromEntries(firebaseConfig.hosting.headers[0].headers.map((entry) => [entry.key, entry.value]));
  for (const name of ['Content-Security-Policy', 'X-Content-Type-Options', 'Referrer-Policy', 'Permissions-Policy']) {
    assert.ok(headers[name], `${name} missing`);
  }
  assert.match(headers['Content-Security-Policy'], /object-src 'none'/);
  assert.match(headers['Content-Security-Policy'], /frame-ancestors 'none'/);
});

test('runtime dependencies are exact-version pinned', () => {
  const lines = requirements.split(/\r?\n/).filter((line) => line && !line.startsWith('#'));
  assert.ok(lines.length >= 4);
  lines.forEach((line) => assert.match(line, /^[a-z0-9_-]+==[^=]+$/i));
  Object.values(packageJson.devDependencies).forEach((version) => {
    assert.doesNotMatch(version, /^[~^*]|x$/i);
  });
});

test('responses use the final relaxed plan contract and targets', () => {
  assert.match(mainPy, /def _sonuc_plan_ve_hedefler/);
  assert.match(mainPy, /son_plan_kontrati, aktif_hedefler = _sonuc_plan_ve_hedefler/);
  assert.match(mainPy, /ara_gun=kullanilan_ara_gun/);
});

test('Firebase Storage SDK is lazy-loaded outside module initialization', () => {
  assert.doesNotMatch(mainPy, /from firebase_admin import initialize_app, storage/);
  assert.match(mainPy, /from firebase_admin import storage/);
});
