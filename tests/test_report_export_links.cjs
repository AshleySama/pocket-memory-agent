/* Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0 */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const {test}=require('node:test');
const renderer=fs.readFileSync(path.join(__dirname,'..','pocket_memory','report_workspace.py'),'utf8');
// The exported script is a fixed Python string; its sole escaped character is
// the regex backslash. Execute that exact script after Python's unescaping.
const script=renderer.match(/<script data-report-source-links>([\s\S]*?)<\/script>/)[1].replace(/\\\\/g,'\\');

function harness(hash){
  const outer={tagName:'DETAILS',open:false,parentElement:null};
  const inner={tagName:'DETAILS',open:false,parentElement:outer};
  const target={parentElement:inner,scrollCalls:0,scrollIntoView(){this.scrollCalls++;}};
  const events={},location={hash};
  vm.runInNewContext(script,{location,document:{getElementById:id=>id==='source-3'?target:null,addEventListener:(name,fn)=>{events[name]=fn;}},window:{addEventListener:(name,fn)=>{events[name]=fn;}},setTimeout:fn=>fn()}, {timeout:1000});
  return {outer,inner,target,events,location};
}

test('loading a citation hash opens every collapsed ancestor and scrolls to its passage',()=>{
  const h=harness('#source-3');assert.equal(h.outer.open,true);assert.equal(h.inner.open,true);assert.equal(h.target.scrollCalls,1);
});
test('clicking another citation or the same citation after folding reveals it again',()=>{
  const h=harness('');h.location.hash='#source-3';h.events.hashchange();assert.equal(h.inner.open,true);
  h.inner.open=false;h.outer.open=false;h.events.click({target:{closest:()=>({hash:'#source-3'})}});
  assert.equal(h.outer.open,true);assert.equal(h.inner.open,true);assert.equal(h.target.scrollCalls,2);
});
test('unknown or non-citation hashes do not unfold source material',()=>{
  const h=harness('#report-document');assert.equal(h.outer.open,false);h.location.hash='#source-999';h.events.hashchange();assert.equal(h.inner.open,false);assert.equal(h.target.scrollCalls,0);
});
