const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = fs.readFileSync(path.resolve(__dirname, '../../frontend/app-update.js'), 'utf8');

async function run(state, resume) {
  const nodes = new Map(), saved = new Map(), redirects = [], toasts = [], requests = [], blobs = [];
  if (resume) saved.set('pocket-memory-update-resume-v1', JSON.stringify(resume));
  const node = () => ({dataset:{}, textContent:'', hidden:false, disabled:false, open:false, inert:false,
    classList:{contains:()=>false,toggle:()=>{}}, events:{}, addEventListener(name,fn){this.events[name]=fn;},
    removeAttribute(){}, replaceChildren(...children){this.children=children;}, click(){return this.events.click?.();}, close(){}});
  const query = selector => {if (!nodes.has(selector)) nodes.set(selector,node());return nodes.get(selector);};
  class LocalURL extends URL {}
  LocalURL.createObjectURL = blob => {blobs.push(blob);return 'blob:fixture';}; LocalURL.revokeObjectURL=()=>{};
  const storage = {getItem:k=>saved.get(k)||null,setItem:(k,v)=>saved.set(k,v),removeItem:k=>saved.delete(k)};
  const context = {document:{querySelector:query,querySelectorAll:()=>[],body:node(),createElement:node},
    window:{PocketMemoryUpdateHooks:{busyReason:()=>'',prepare:async()=>{}},addEventListener(){},showToast:(...x)=>toasts.push(x)},
    location:{origin:'http://127.0.0.1:1234',pathname:'/',search:'',hash:'',replace:x=>redirects.push(x)},
    localStorage:storage,sessionStorage:storage,URL:LocalURL,Blob,AbortController,
    setTimeout:()=>1,clearTimeout(){},console, fetch:async(url,options)=>{requests.push([url,options.method]);return {ok:true,json:async()=>state};}};
  vm.runInNewContext(code,context);
  await new Promise(resolve=>setImmediate(resolve));
  return {nodes,saved,redirects,toasts,requests,blobs};
}
(async()=>{
  const waiting={phase:'waiting',previous:'1.0.16',target:'1.0.17',address:'/',at:Date.now()};
  const healthy={state:'current',current_version:'1.0.17',installation_integrity:{status:'ok'},last_result:{state:'complete',target_version:'1.0.17',message:'updated'}};
  for (const extra of [{state:'error'}, {retry_blocked:true,recovery:{action:'pause_and_diagnose',steps:[]}},
    {transaction_recovery:{state:'invalid'}}, {installation_integrity:{status:'missing'}}]) {
    const state={...healthy,...extra};
    for (const resume of [waiting,{...waiting,phase:'complete',installed:'1.0.17'}]) {
      const r=await run(state,resume);
      assert.equal(r.redirects.length,0);assert.equal(r.toasts.length,0);
    }
  }
  const paused={...healthy,state:'error',retry_blocked:true,can_update:false,has_update:true,message:'hash mismatch',
    failure_record_saved:true,recovery:{action:'pause_and_diagnose',steps:['keep data']},
    diagnostic:{schema_version:1,app:'PocketMemory',failure:{code:'integrity_mismatch',stage:'verify'}},
    install_dir:'C:/Users/PRIVATE',health_token:'SECRET'};
  const r=await run(paused,waiting);
  assert.equal(r.nodes.get('#app-update-start').disabled,true);
  assert.equal(r.nodes.get('#app-update-last-result').hidden,true);
  assert.equal(r.nodes.get('#app-update-check').textContent,'重新核对状态');
  await r.nodes.get('#app-update-check').click();
  assert(r.requests.every(([url])=>!url.endsWith('/start')));
  r.nodes.get('#app-update-diagnostic').click();
  assert.deepEqual(JSON.parse(await r.blobs[0].text()),paused.diagnostic);
  assert(!String(await r.blobs[0].text()).includes('PRIVATE'));
  assert(!String(await r.blobs[0].text()).includes('SECRET'));
  const ok=await run(healthy,waiting);assert.equal(ok.redirects.length,1);
  const toast=await run(healthy,{...waiting,phase:'complete',installed:'1.0.17'});assert.equal(toast.toasts.length,1);
  const unknown=await run({...healthy,last_result:null},waiting);assert.equal(unknown.redirects.length,0);
  const stale=await run({...healthy,last_result:{state:'complete',target_version:'1.0.16'}},waiting);assert.equal(stale.redirects.length,0);
  const active=await run({...healthy,state:'verifying'},{...waiting,phase:'complete',installed:'1.0.17'});assert.equal(active.toasts.length,0);
  console.log('PASS: 8 blocked-reconnect cases, paused controls/diagnostic export, confirmed success, unconfirmed version change, stale success version, active update');
})().catch(e=>{console.error(e);process.exitCode=1;});
