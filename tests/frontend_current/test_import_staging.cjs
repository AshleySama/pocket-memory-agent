const assert=require('node:assert/strict');
const Queue=require('../../frontend/imports.js');
const files=(name='draft.txt',body='saved bytes')=>({name,size:body.length,lastModified:1,body});
const storage=()=>{const m=new Map();return{getItem:k=>m.get(k),setItem:(k,v)=>m.set(k,v)}};
class Cache {
 constructor(){this.records=new Map();this.failStage=false;this.failAck=false;}
 id(s,k){return s+':'+k}
 async list(s){return [...this.records.values()].filter(x=>x.scope===s)}
 async get(s,k){return this.records.get(this.id(s,k))}
 async stage(s,r){if(this.failStage)throw Error('disk full');let v=await this.get(s,r.key);if(v)return v;v={scope:s,key:r.key,name:r.name,size:r.size,ext:r.ext,fingerprint:r.fingerprint,blob:r.file};this.records.set(this.id(s,r.key),v);return v;}
 async acknowledge(s,k,d){if(this.failAck)throw Error('cleanup denied');const v=await this.get(s,k);if(v)this.records.set(this.id(s,k),{...v,blob:null,documentId:d.id,status:d.status,sha256:d.sha256});}
 async remove(s,k){this.records.delete(this.id(s,k))}
 async removeDocument(s,id){for(const [k,v] of this.records)if(v.scope===s&&v.documentId===id&&!v.blob)this.records.delete(k)}
}
let passed=0;
async function test(name,fn){await fn();passed++;console.log('PASS',name)}
const create=(cache,extra={})=>new Queue({cache,storage:storage(),api:async()=>{throw Error('offline')},library:()=> 'A',read:async f=>f.body,changed:()=>{},...extra});
(async()=>{
await test('offline original survives reopen without localStorage or File reselection',async()=>{
 const cache=new Cache();let original=create(cache);await original.add([files()]);assert(original.rows[0].staged);assert(!original.hasUnsaved());
 let posts=[];let restored=create(cache,{api:async(u,o)=>{posts.push(JSON.parse(o.body));return{id:7,status:'ready'}}});await restored.activate();assert.equal(restored.rows[0].status,'staged');assert.equal(posts.length,0);await restored.resume();assert.equal(posts[0].data_url,'saved bytes');assert.equal(restored.rows[0].documentId,7);assert.equal((await cache.list('A'))[0].blob,null);
});
await test('file is not submitted or called staged before persistence finishes',async()=>{
 const cache=new Cache();let release;const wait=new Promise(r=>release=r);const stage=cache.stage.bind(cache);cache.stage=async(...a)=>{await wait;return stage(...a)};
 let calls=0;const q=create(cache,{api:async()=>{calls++;return{id:1,status:'ready'}}});const pending=q.add([files()]);await new Promise(r=>setImmediate(r));assert.equal(calls,0);assert(q.hasUnsaved());assert(!q.rows[0].staged);release();await pending;assert.equal(calls,1);
});
await test('quota or transaction failure retains File and has no false durable state',async()=>{
 const cache=new Cache();cache.failStage=true;const q=create(cache);await q.add([files()]);assert.equal(q.rows[0].status,'stage_failed');assert(q.rows[0].file);assert(q.hasUnsaved());assert.equal(cache.records.size,0);cache.failStage=false;await q.retry(q.rows[0].key);assert(q.rows[0].staged);assert.equal(q.rows[0].status,'upload_failed');
});
await test('library isolation and different same-metadata bytes are preserved',async()=>{
 const cache=new Cache();const q=create(cache);await q.add([files('same.txt','AAAA'),files('same.txt','BBBB')]);assert.equal((await cache.list('A')).length,2);assert.notEqual(q.rows[0].key,q.rows[1].key);const b=create(cache,{library:()=> 'B'});await b.activate();assert.equal(b.rows.length,0);await b.add([files('same.txt','CCCC')]);assert.equal((await cache.list('A')).length,2);assert.equal((await cache.list('B')).length,1);
});
await test('two windows stage distinct records without overwriting, confirmed receipt prevents replay',async()=>{
 const cache=new Cache();const a=create(cache),b=create(cache);await Promise.all([a.add([files('a.txt')]),b.add([files('b.txt')])]);assert.equal(cache.records.size,2);
 const c=create(cache,{api:async()=>({id:9,status:'ready'})});await c.activate();await c.retry(a.rows[0].key);let calls=0;a.api=async()=>{calls++;throw Error('must not replay confirmed upload')};await a.retry(a.rows[0].key);assert.equal(calls,0);assert.equal(a.rows[0].documentId,9);
});
await test('server acknowledgement cleanup failure retains bytes for safe replay',async()=>{
 const cache=new Cache();cache.failAck=true;const q=create(cache,{api:async()=>({id:7,status:'ready'})});await q.add([files()]);assert(q.rows[0].cleanupError);assert((await cache.list('A'))[0].blob);cache.failAck=false;
 const restored=create(cache,{api:async()=>({id:7,status:'ready',reused:true})});await restored.activate();await restored.resume();assert.equal(restored.rows[0].reused,true);assert.equal((await cache.list('A'))[0].blob,null);
});
await test('missing server original never clears the last staged copy',async()=>{
 const cache=new Cache();const q=create(cache,{hash:async()=> 'hash'});await q.add([files()]);await q.reconcile([{id:3,sha256:'hash',file_type:'txt',status:'failed',error:'FileNotFoundError'}]);assert(q.rows[0].file);assert((await cache.list('A'))[0].blob);
});
await test('stale read for old library cannot reconcile another library',async()=>{
 let scope='A';const q=create(new Cache(),{library:()=>scope});await q.add([files()]);scope='B';await q.activate();await q.reconcile([{id:3,status:'failed',original_name:'old file'}],'A');assert.equal(q.rows.length,0);
});
await test('removing cached file deletes only selected local temporary original',async()=>{
 const cache=new Cache(),q=create(cache);await q.add([files('one.txt'),files('two.txt')]);const removed=q.rows[0].key;await q.dismiss(removed);assert.equal(q.rows.length,1);assert.equal(cache.records.size,1);const reopened=create(cache);await reopened.activate();assert.equal(reopened.rows[0].name,'two.txt');
});
await test('unreadable cache is visible and does not silently erase cached records',async()=>{
 const cache=new Cache();const q=create(cache);await q.add([files()]);cache.list=async()=>{throw Error('blocked')};const restored=create(cache);await restored.activate();assert(restored.cacheError.includes('无法读取'));assert.equal(cache.records.size,1);
});
await test('clearing merged receipts never resurrects duplicate completed records',async()=>{
 const cache=new Cache(),q=create(cache,{api:async()=>({id:71,status:'ready'})});
 await q.add([files('same.txt')]);await q.add([files('same.txt')]);assert.equal(q.rows.length,1);assert.equal(cache.records.size,2);
 await q.clearCompleted();assert.equal(cache.records.size,0);const reopened=create(cache);await reopened.activate();assert.equal(reopened.rows.length,0);
});
await test('deleting document clears all hidden receipts in that library only',async()=>{
 const cache=new Cache(),a=create(cache,{api:async()=>({id:72,status:'ready'})}),b=create(cache,{library:()=> 'B',api:async()=>({id:72,status:'ready'})});
 await a.add([files()]);await a.add([files()]);await b.add([files()]);const empty=create(cache);await empty.forget(72,'A');assert.equal((await cache.list('A')).length,0);assert.equal((await cache.list('B')).length,1);
});
console.log(passed+' staging checks passed');
})().catch(e=>{console.error(e);process.exitCode=1});
