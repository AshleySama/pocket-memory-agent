/* Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0 */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../../frontend/index.html'), 'utf8');
const code = html.match(/<script id="pocket-startup-guard">([\s\S]*?)<\/script>/)[1];
function setup({blockedStorage=false}={}) {
  const events={}, docEvents={}, timers=[], storage=new Map(); let reloads=0;
  function el(tag) {return {tagName:tag,style:{},dataset:{},classList:{contains:c=>c==='app-brand-logo'},children:[],events:{},addEventListener(n,f){(this.events[n]||=[]).push(f)},setAttribute(){},append(...v){this.children.push(...v)},remove(){this.removed=true},querySelector(){return this.children[0]}};}
  const document={body:el('BODY'),createElement:t=>el(t.toUpperCase()),addEventListener:(n,f)=>(docEvents[n]||=[]).push(f),removeEventListener(){}};
  const window={fetch:async()=>({ok:true}),addEventListener:(n,f)=>(events[n]||=[]).push(f),confirm:()=>false};
  const location={href:'http://127.0.0.1:1/',origin:'http://127.0.0.1:1',reload(){reloads++}};
  const sessionStorage={getItem:k=>{if(blockedStorage)throw Error('blocked');return storage.get(k)},setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
  vm.runInNewContext(code,{window,document,location,sessionStorage,URL,console:{warn(){}},setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length},clearTimeout:i=>{if(timers[i-1])timers[i-1].cleared=true}});
  return {window,document,timers,storage,el,get reloads(){return reloads},fire:(n,e={target:window})=>events[n]?.forEach(f=>f(e)),edit:()=>docEvents.input.forEach(f=>f()),banner:()=>document.body.children.find(e=>e.id==='startup-recovery')};
}
const cases=[];function check(name,fn){fn();cases.push(name)}
check('healthy readiness requires notes, drafts and complete resource loading',()=>{
  const s=setup();s.window.PocketStartup.step('notes',true);s.fire('load');assert.equal(s.window.__PM_DESKTOP_READY__,false);
  s.window.PocketStartup.step('drafts',true);assert.equal(s.window.__PM_DESKTOP_READY__,true);assert.equal(s.reloads,0);
});
check('transient critical script failure permits exactly one page retry',()=>{
  const s=setup(),script=s.el('SCRIPT');script.src='/drafts.js';s.fire('error',{target:script});s.fire('load');s.fire('load');assert.equal(s.reloads,1);assert.equal(s.storage.size,1);
});
check('persistent failure stops and shows recovery action',()=>{
  const s=setup();s.storage.set('pocket-memory-startup-retry-v1','1');const script=s.el('SCRIPT');script.src='/app.js';s.fire('error',{target:script});s.fire('load');assert.equal(s.reloads,0);assert.ok(s.banner());
});
check('user edits prevent automatic reload and manual cancel preserves page',()=>{
  const s=setup();s.edit();const script=s.el('SCRIPT');script.src='/app.js';s.fire('error',{target:script});s.fire('load');assert.equal(s.reloads,0);s.banner().children[1].onclick();assert.equal(s.reloads,0);
});
check('background draft writes also prevent automatic reload',()=>{
  const s=setup();s.window.fetch('/api/drafts',{method:'POST'});const script=s.el('SCRIPT');script.src='/app.js';s.fire('error',{target:script});s.fire('load');assert.equal(s.reloads,0);assert.ok(s.banner());
});
check('unavailable session storage cannot cause reload loop',()=>{
  const s=setup({blockedStorage:true}),script=s.el('SCRIPT');script.src='/app.js';s.fire('error',{target:script});s.fire('load');assert.equal(s.reloads,0);assert.ok(s.banner());
});
check('stylesheet retry is bounded and ready waits for recovery',()=>{
  const s=setup(),style=s.el('LINK');style.rel='stylesheet';style.href='/styles.css';s.fire('error',{target:style});s.window.PocketStartup.step('notes',true);s.window.PocketStartup.step('drafts',true);s.fire('load');assert.equal(s.window.__PM_DESKTOP_READY__,false);
  s.timers.find(t=>t.ms===300).fn();assert.match(style.href,/_startup_retry=1/);style.events.load.forEach(f=>f());assert.equal(s.window.__PM_DESKTOP_READY__,true);
});
check('persistent image failure stops after two attempts',()=>{
  const s=setup(),img=s.el('IMG');img.src='/brand/logo.png';for(let i=0;i<3;i++)s.fire('error',{target:img});assert.equal(s.timers.filter(t=>t.ms<1000).length,2);assert.ok(s.banner());assert.equal(s.reloads,0);
});
check('ordinary note attachments are not retried by startup guard',()=>{
  const s=setup(),img=s.el('IMG');img.classList.contains=()=>false;img.src='/attachments/a.png';s.fire('error',{target:img});assert.equal(s.timers.length,1);assert.equal(s.window.PocketStartup.status().failedResources.length,0);
});
check('runtime exception is visible and cannot mark desktop ready',()=>{
  const s=setup();s.fire('error',{filename:'http://127.0.0.1:1/app.js'});s.window.PocketStartup.step('notes',true);s.window.PocketStartup.step('drafts',true);s.fire('load');assert.equal(s.window.__PM_DESKTOP_READY__,false);assert.ok(s.banner());
});
check('read wait has a visible deadline without automatically discarding input',()=>{
  const s=setup();s.edit();s.timers.find(t=>t.ms===25000).fn();assert.ok(s.banner());assert.equal(s.reloads,0);
});
console.log(JSON.stringify({passed:cases.length,cases},null,2));
