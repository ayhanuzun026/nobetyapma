import { after, before, test } from 'node:test';
import { readFileSync } from 'node:fs';

import {
  assertFails,
  assertSucceeds,
  initializeTestEnvironment,
} from '@firebase/rules-unit-testing';
import { doc, getDoc, setDoc } from 'firebase/firestore';
import { getBytes, ref, uploadString } from 'firebase/storage';

const projectId = 'demo-nobetyap';
const adminUid = 'TJg6pfV9NdU4exBfRrqeaYRWigC3';
let testEnv;

before(async () => {
  testEnv = await initializeTestEnvironment({
    projectId,
    firestore: { rules: readFileSync('firestore.rules', 'utf8') },
    storage: { rules: readFileSync('storage.rules', 'utf8') },
  });

  await testEnv.withSecurityRulesDisabled(async (context) => {
    await setDoc(doc(context.firestore(), 'debug_sessions/session-1'), { endpoint: 'nobet_coz' });
    await uploadString(ref(context.storage(), 'sonuclar/private.xlsx'), 'private');
  });
});

after(async () => {
  await testEnv.clearFirestore();
  await testEnv.clearStorage();
  await testEnv.cleanup();
});

test('kullanici yalniz kendi ay belgesini okuyup yazabilir', async () => {
  const alice = testEnv.authenticatedContext('alice').firestore();
  const bob = testEnv.authenticatedContext('bob').firestore();

  await assertSucceeds(setDoc(doc(alice, 'users/alice/months/2026_7'), { value: 1 }));
  await assertSucceeds(getDoc(doc(alice, 'users/alice/months/2026_7')));
  await assertFails(getDoc(doc(bob, 'users/alice/months/2026_7')));
});

test('debug session yalniz allowlist admin tarafindan okunabilir', async () => {
  const admin = testEnv.authenticatedContext(adminUid).firestore();
  const user = testEnv.authenticatedContext('user-1').firestore();

  await assertSucceeds(getDoc(doc(admin, 'debug_sessions/session-1')));
  await assertFails(getDoc(doc(user, 'debug_sessions/session-1')));
  await assertFails(setDoc(doc(user, 'debug_frontend_logs/log-1'), { logs: ['x'] }));
});

test('storage istemci okuma ve yazmaya tamamen kapalidir', async () => {
  const anonymousStorage = testEnv.unauthenticatedContext().storage();
  const authenticatedStorage = testEnv.authenticatedContext('alice').storage();

  await assertFails(getBytes(ref(anonymousStorage, 'sonuclar/private.xlsx')));
  await assertFails(uploadString(ref(anonymousStorage, 'sonuclar/new.xlsx'), 'x'));
  await assertFails(getBytes(ref(authenticatedStorage, 'sonuclar/private.xlsx')));
  await assertFails(uploadString(ref(authenticatedStorage, 'sonuclar/new.xlsx'), 'x'));
});
