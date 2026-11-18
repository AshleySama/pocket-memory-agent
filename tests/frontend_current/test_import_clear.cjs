// Copyright 2026 AshleySama
// SPDX-License-Identifier: Apache-2.0

const assert = require('node:assert/strict');
const Queue = require('../../frontend/imports.js');
const file = (name='receipt.txt') => ({name, size:4, lastModified:1});
const failed = (id=4, updated_at='2026-11-17T01:00:00') => ({id, original_name:`${id}.txt`, status:'failed', error:'parse error', sha256:`sha-${id}`, updated_at});
const storage = () => {
  const entries = new Map();
  return {getItem:key => entries.get(key), setItem:(key, value) => entries.set(key, value)};
};
class Cache {
  constructor() { this.records = new Map(); }
  async list(scope) { return [...this.records.values()].filter(row => row.scope === scope); }
  async get(scope, key) { return this.records.get(`${scope}:${key}`); }
  async stage(scope, row) {
    const record = {scope, key:row.key, name:row.name, size:row.size, ext:row.ext, fingerprint:row.fingerprint, blob:row.file};
    this.records.set(`${scope}:${row.key}`, record);
    return record;
  }
  async acknowledge(scope, key, document) {
    const record = await this.get(scope, key);
    if (record) Object.assign(record, {blob:null, documentId:document.id, status:document.status, error:document.error || '', sha256:document.sha256});
  }
  async remove(scope, key) { this.records.delete(`${scope}:${key}`); }
  async removeDocument(scope, id) {
    for (const [key, row] of this.records) if (row.scope === scope && row.documentId === id && !row.blob) this.records.delete(key);
  }
}
const create = overrides => new Queue({api:async () => failed(), read:async () => 'original bytes', library:() => 'A', changed:() => {}, storage:storage(), ...overrides});
const tick = () => new Promise(resolve => setImmediate(resolve));
let passed = 0;
async function test(name, run) { await run(); console.log('PASS', name); passed++; }

(async () => {
await test('clear removes only local failure receipts and same server state stays hidden after refresh and reopen', async () => {
  const saved = storage(), cache = new Cache(), calls = [], document = Object.freeze(failed());
  const q = create({storage:saved, cache, api:async (url, options) => { calls.push([url, options.method]); return document; }});
  await q.add([file()]);
  assert.equal(q.rows[0].updated_at, document.updated_at);
  await q.clearCompleted();
  assert.equal(q.rows.length, 0);
  assert.equal(cache.records.size, 0);
  await q.activate(true);
  await q.reconcile([document]);
  assert.equal(q.rows.length, 0);
  const reopened = create({storage:saved, cache});
  await reopened.activate();
  await reopened.reconcile([document]);
  assert.equal(reopened.rows.length, 0);
  assert.deepEqual(calls, [['/api/documents', 'POST']]);
  assert.equal(document.status, 'failed');
  assert.equal(document.error, 'parse error');
});

await test('ready rejected and saved failed can clear; active retries and unconfirmed originals remain', async () => {
  const q = create();
  await q.activate();
  const retained = ['pending', 'extracting', 'indexing', 'queued', 'uploading', 'staging', 'staged', 'stage_failed', 'upload_failed', 'needs_file'];
  q.rows = retained.map((status, index) => ({key:`active-${index}`, name:`${status}.txt`, status, file:index < 3 ? null : file(), staged:index >= 6, documentId:index < 3 ? index + 30 : null}));
  q.rows.push({key:'retry', name:'retry.txt', documentId:9, status:'failed', retrying:true});
  q.rows.push({key:'unconfirmed', name:'unconfirmed.txt', status:'failed', file:file(), staged:true});
  q.rows.push({key:'ready', name:'ready.txt', documentId:10, status:'ready'});
  q.rows.push({key:'failed', name:'failed.txt', documentId:11, status:'failed'});
  q.rows.push({key:'rejected', name:'rejected.exe', status:'rejected', file:null});
  const pendingKeys = q.rows.slice(0, -3).map(row => row.key);
  await q.clearCompleted();
  assert.deepEqual(q.rows.map(row => row.key), pendingKeys);
  assert(q.rows.find(row => row.key === 'unconfirmed').file);
});

await test('new failed version and changed processing status both restore the receipt', async () => {
  const q = create();
  await q.add([file()]);
  await q.clearCompleted();
  await q.reconcile([failed(4, '2026-11-17T01:00:01')]);
  assert.equal(q.rows[0].status, 'failed');
  assert.equal(q.rows[0].updated_at, '2026-11-17T01:00:01');
  await q.clearCompleted();
  await q.reconcile([{...failed(4, '2026-11-17T01:00:01'), status:'extracting', error:''}]);
  assert.equal(q.rows[0].status, 'extracting');
  await q.reconcile([{...failed(4, '2026-11-17T01:00:02'), status:'ready', error:''}]);
  assert.equal(q.rows[0].status, 'ready');
});

await test('changed completed version is shown even when intermediate retry states were not polled', async () => {
  const q = create();
  await q.add([file()]);
  await q.clearCompleted();
  await q.reconcile([{...failed(4, '2026-11-17T01:00:01'), status:'ready', error:''}]);
  assert.equal(q.rows.length, 1);
  assert.equal(q.rows[0].status, 'ready');
});

await test('explicit retry reveal handles identical timestamps and permits retry from the saved original', async () => {
  const paths = [], q = create({api:async url => { paths.push(url); return url.endsWith('/reindex') ? {...failed(), status:'pending', error:''} : failed(); }});
  await q.add([file()]);
  await q.clearCompleted();
  q.revealDocument(4, 'A');
  await q.reconcile([failed()]);
  assert.equal(q.rows.length, 1);
  await q.retry(q.rows[0].key);
  assert.equal(q.rows[0].status, 'pending');
  assert.deepEqual(paths, ['/api/documents', '/api/documents/4/reindex']);
});

await test('selecting the same original again reveals a deduplicated same-version receipt', async () => {
  const q = create();
  await q.add([file()]);
  await q.clearCompleted();
  await q.add([file()]);
  await q.reconcile([failed()]);
  assert.equal(q.rows.length, 1);
  assert.equal(q.rows[0].documentId, 4);
});

await test('clear and explicit reveal are isolated by library even when document IDs are equal', async () => {
  let scope = 'A';
  const saved = storage(), q = create({storage:saved, library:() => scope});
  await q.add([file()]);
  await q.clearCompleted();
  scope = 'B';
  await q.activate();
  await q.reconcile([failed()]);
  assert.equal(q.rows.length, 1);
  await q.clearCompleted();
  scope = 'A';
  await q.activate();
  q.revealDocument(4, 'B');
  await q.reconcile([failed()]);
  assert.equal(q.rows.length, 0);
  scope = 'B';
  await q.activate();
  await q.reconcile([failed()]);
  assert.equal(q.rows.length, 1);
});

await test('cache removal failure preserves visible receipts and does not persist a hidden marker', async () => {
  const saved = storage(), cache = new Cache(), q = create({storage:saved, cache});
  await q.add([file()]);
  cache.removeDocument = async () => { throw Error('cache locked'); };
  await assert.rejects(q.clearCompleted(), /cache locked/);
  assert.equal(q.rows.length, 1);
  assert.equal(cache.records.size, 1);
  const reopened = create({storage:saved, cache});
  await reopened.activate();
  await reopened.reconcile([failed()]);
  assert.equal(reopened.rows.length, 1);
});

await test('failure to persist the hidden marker keeps a recoverable visible server receipt', async () => {
  const saved = storage(), cache = new Cache(), q = create({storage:saved, cache});
  await q.add([file()]);
  const write = saved.setItem;
  saved.setItem = (key, value) => { if (key.startsWith('pm-import-cleared:')) throw Error('quota'); write(key, value); };
  await assert.rejects(q.clearCompleted(), /无法保存清除记录/);
  assert.equal(q.rows.length, 1);
  assert.equal(q.storageError, true);
  const reopened = create({storage:saved, cache});
  await reopened.activate();
  await reopened.reconcile([failed()]);
  assert.equal(reopened.rows.length, 1);
});

await test('partially completed clear persists successful removals and retains the failed cleanup item', async () => {
  const saved = storage(), cache = new Cache();
  let id = 0;
  const q = create({storage:saved, cache, api:async () => failed(++id)});
  await q.add([file('one.txt'), file('two.txt')]);
  const remove = cache.removeDocument.bind(cache);
  cache.removeDocument = async (scope, documentId) => { if (documentId === 2) throw Error('cache locked'); await remove(scope, documentId); };
  await assert.rejects(q.clearCompleted(), /cache locked/);
  assert.deepEqual(q.rows.map(row => row.documentId), [2]);
  const reopened = create({storage:saved, cache});
  await reopened.activate();
  await reopened.reconcile([failed(1), failed(2)]);
  assert.deepEqual(reopened.rows.map(row => row.documentId), [2]);
});

await test('last unacknowledged staged bytes survive a clear attempt', async () => {
  const cache = new Cache();
  cache.acknowledge = async () => { throw Error('ack blocked'); };
  const q = create({cache});
  await q.add([file()]);
  assert(q.rows[0].cleanupError);
  await assert.rejects(q.clearCompleted(), /回执尚未确认/);
  assert.equal(q.rows.length, 1);
  assert((await cache.list('A'))[0].blob);
});

await test('retry beginning during async cache cleanup is not hidden or removed', async () => {
  let finishClear, finishRetry;
  const cache = new Cache(), q = create({cache, api:async url => url.endsWith('/reindex') ? new Promise(resolve => { finishRetry = resolve; }) : failed()});
  await q.add([file()]);
  cache.removeDocument = () => new Promise(resolve => { finishClear = resolve; });
  const clearing = q.clearCompleted();
  await tick();
  assert.equal(typeof finishClear, 'function');
  const retrying = q.retry(q.rows[0].key);
  await tick();
  assert.equal(q.rows[0].retrying, true);
  finishClear();
  await clearing;
  assert.equal(q.rows.length, 1);
  finishRetry({...failed(), status:'pending', error:''});
  await retrying;
  assert.equal(q.rows[0].status, 'pending');
  assert.equal(q.isCleared(failed()), false);
});

await test('switching libraries during async clear never hides another library receipt', async () => {
  let scope = 'A', finishClear;
  const cache = new Cache(), q = create({cache, library:() => scope});
  await q.add([file()]);
  cache.removeDocument = () => new Promise(resolve => { finishClear = resolve; });
  const clearing = q.clearCompleted();
  await tick();
  scope = 'B';
  await q.activate();
  await q.reconcile([failed()]);
  finishClear();
  await clearing;
  assert.equal(q.scope, 'B');
  assert.equal(q.rows.length, 1);
  assert.equal(q.isCleared(failed()), false);
});

console.log(`${passed} clear receipt checks passed`);
})().catch(error => { console.error(error); process.exitCode = 1; });
