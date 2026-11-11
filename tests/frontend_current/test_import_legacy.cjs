const assert = require('node:assert/strict');
const Queue = require('../../frontend/imports.js');
const file = (name='test.txt', size=4) => ({name,size,lastModified:1});
const storage = () => { const m=new Map(); return {getItem:k=>m.get(k),setItem:(k,v)=>m.set(k,v)}; };
let passed=0;
async function test(name, fn) { await fn(); console.log('PASS',name); passed++; }
const create = (api,extra={}) => new Queue({api,read:async f=>f.name,library:()=> 'lib-a',changed:()=>{},storage:storage(),...extra});
(async()=>{
await test('mixed batch preserves per-file reason; only failed item resubmitted',async()=>{
 let calls=[];let fail=true;
 const q=create(async(u,o)=>{const n=JSON.parse(o.body).name;calls.push(n);if(n==='bad.txt'&&fail)throw Error('offline');return {id:n==='bad.txt'?2:1,status:'ready'};});
 await q.add([file('ok.txt'),file('bad.txt'),file('blocked.exe'),file('empty.txt',0),file('large.pdf',11*1024*1024)]);
 assert.equal(calls.length,2);assert.equal(q.rows.filter(x=>x.status==='rejected').length,3);assert(q.hasUnsaved());
 fail=false;await q.retry(q.rows.find(x=>x.name==='bad.txt').key);assert.deepEqual(calls,['ok.txt','bad.txt','bad.txt']);assert(!q.hasUnsaved());
});
await test('lost response retry reuses server ID and drops file handle only after receipt',async()=>{
 let fail=true;const q=create(async()=>{if(fail)throw Error('reply lost');return {id:3,status:'ready',reused:true};});
 await q.add([file()]);assert(q.rows[0].file);fail=false;await q.retry(q.rows[0].key);assert.equal(q.rows.length,1);assert.equal(q.rows[0].reused,true);assert.equal(q.rows[0].file,null);
});
await test('processing retry uses saved original without reading file',async()=>{
 let paths=[];const q=create(async u=>{paths.push(u);return {id:8,status:'extracting'};},{read:()=>{throw Error('must not read')}});
 await q.activate();await q.reconcile([{id:8,status:'failed',original_name:'saved.pdf',error:'bad format'}]);await q.retry('saved:8');assert.deepEqual(paths,['/api/documents/8/reindex']);assert.equal(q.rows[0].status,'extracting');
});
await test('reload restores unresolved receipts but asks to reselect absent browser file',async()=>{
 const s=storage();const q=create(async()=>{throw Error('offline')},{storage:s});await q.add([file()]);
 const restored=create(async()=>({id:1,status:'ready'}),{storage:s});await restored.activate();assert.equal(restored.rows[0].status,'needs_file');assert.equal(restored.rows[0].file,null);await restored.add([file()]);assert.equal(restored.rows.length,1);assert.equal(restored.rows[0].status,'ready');
});
await test('library change during file reading cannot send to another library',async()=>{
 let scope='A', calls=0;const q=create(async()=>{calls++;},{library:()=>scope,read:async()=>{scope='B';return 'data';}});await q.add([file()]);assert.equal(calls,0);assert.equal(q.scope,'A');await q.activate();assert.equal(q.scope,'B');assert.equal(q.rows.length,0);
});
await test('same name/size/mtime is not treated as proof of duplicate bytes',async()=>{
 let n=0;const q=create(async()=>({id:++n,status:'ready'}));await q.add([file()]);await q.add([file()]);assert.equal(n,2);assert.equal(q.rows.length,2);
});
await test('double selection while upload running is serialized',async()=>{
 let release;let calls=0;const pause=new Promise(r=>release=r);const q=create(async()=>{calls++;await pause;return{id:calls,status:'ready'}});const f=file();const pending=q.add([f]);while(!calls) await new Promise(r=>setImmediate(r));await q.add([f,file('next.txt')]);release();await pending;assert.equal(calls,2);assert.equal(q.busy,false);
});
await test('saved failed item remains visible on reload and clear does not drop pending',async()=>{
 const s=storage();const q=create(async()=>({id:4,status:'failed',error:'parse error'}),{storage:s});await q.add([file()]);await q.clearCompleted();const next=create(async()=>{}, {storage:s});await next.activate();assert.equal(next.rows[0].documentId,4);assert.equal(next.rows[0].status,'failed');
});
await test('disabled browser storage is explicit, not successful persistence',async()=>{
 const q=create(async()=>({id:1,status:'ready'}),{storage:{getItem:()=>null,setItem:()=>{throw Error('quota')}}});await q.add([file()]);assert.equal(q.storageError,true);assert.equal(q.rows[0].status,'ready');
});
await test('lost reply is confirmed by original byte hash and type, never just filename',async()=>{
 const q=create(async()=>{throw Error('reply lost')},{hash:async()=> 'original-bytes'});await q.add([file()]);
 await q.reconcile([{id:1,sha256:'different',file_type:'txt',status:'ready',original_name:'test.txt'}]);assert.equal(q.rows[0].status,'upload_failed');
 await q.reconcile([{id:2,sha256:'original-bytes',file_type:'txt',status:'ready',original_name:'test.txt'}]);assert.equal(q.rows.length,1);assert.equal(q.rows[0].documentId,2);assert(!q.hasUnsaved());
});
await test('unsupported and empty files stay rejected after reopening',async()=>{
 const s=storage();const q=create(async()=>{throw Error('not sent')},{storage:s});await q.add([file('empty.txt',0),file('wrong.exe')]);const next=create(async()=>{},{storage:s});await next.activate();assert(next.rows.every(x=>x.status==='rejected'));assert(next.rows[0].error.includes('为空'));
});
await test('optional browser hashing cannot block import; dismiss never deletes saved records',async()=>{
 const q=create(async()=>({id:5,status:'ready'}),{hash:async()=>{throw Error('unavailable')}});await q.add([file()]);assert.equal(q.rows[0].documentId,5);await q.dismiss(q.rows[0].key);assert.equal(q.rows.length,1);
 await q.add([file('bad.exe')]);await q.dismiss(q.rows.find(x=>x.status==='rejected').key);assert.equal(q.rows.length,1);
});
console.log(`${passed} import queue checks passed`);
})().catch(e=>{console.error(e);process.exitCode=1;});


