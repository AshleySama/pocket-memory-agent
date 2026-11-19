/* Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0 */
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const app=fs.readFileSync(path.join(__dirname,'../../frontend/app.js'),'utf8');
const code=app.slice(app.indexOf('async function request(url,'),app.indexOf('\nfunction syncCachedNote'));
function setup(fetch,pending=true){
  const timers=[];const ctx={window:{PocketStartup:{pending}},state:{activeAsk:null,updatePendingWrites:0},draftVault:{library:null},AbortController,fetch,setTimeout:(fn,ms)=>{const t={fn,ms};timers.push(t);return t},clearTimeout:t=>{if(t)t.cleared=true}};
  vm.createContext(ctx);vm.runInContext(code,ctx);return {request:ctx.request,timers,state:ctx.state};
}
const aborted=signal=>new Promise((_,reject)=>{if(signal.aborted)reject(new Error('aborted'));else signal.addEventListener('abort',()=>reject(new Error('aborted')),{once:true})});
const deadline=setTimeout(()=>{console.error('Request contract test did not complete');process.exit(1)},3000);
(async()=>{
  let s=setup(async(_,options)=>aborted(options.signal)),p=s.request('/api/notes?_t=1');
  assert.equal(s.timers[0].ms,20000);s.timers[0].fn();await assert.rejects(p,/初次读取资料超时/);assert.ok(s.timers[0].cleared);
  s=setup(async(_,options)=>({ok:true,json:()=>aborted(options.signal)}));p=s.request('/api/drafts');await Promise.resolve();s.timers[0].fn();await assert.rejects(p,/初次读取资料超时/);
  let passedSignal; s=setup(async(_,options)=>{passedSignal=options.signal;return {ok:true,json:async()=>({saved:true})}});await s.request('/api/notes',{method:'POST'});assert.equal(s.timers.length,0);assert.equal(passedSignal,undefined);assert.equal(s.state.updatePendingWrites,0);
  s=setup(async()=>({ok:true,json:async()=>[]}),false);await s.request('/api/notes');assert.equal(s.timers.length,0);
  s=setup(async(_,options)=>aborted(options.signal));const caller=new AbortController();p=s.request('/api/settings',{signal:caller.signal});caller.abort();await assert.rejects(p,/^Error: aborted$/);assert.ok(s.timers[0].cleared);
  s=setup(async()=>({ok:false,status:403,json:async()=>({error:'denied'})}));await assert.rejects(s.request('/api/drafts'),e=>e.message==='denied'&&e.status===403);assert.ok(s.timers[0].cleared);
  console.log(JSON.stringify({passed:6,cases:['initial GET timeout','timeout covers body read','no timeout or retry on saves','no change after startup','caller cancellation preserved','HTTP error and timer cleanup preserved']},null,2));
})().catch(e=>{console.error(e);process.exitCode=1}).finally(()=>clearTimeout(deadline));
