// Copyright 2026 AshleySama
// SPDX-License-Identifier: Apache-2.0

// Citation coordinates must select the cited occurrence, not every equal phrase.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const app = fs.readFileSync(path.resolve(__dirname, '../../frontend/app.js'), 'utf8');
const ctx = vm.createContext({state:{allNotes:[]}, crypto:crypto.webcrypto, TextEncoder});
vm.runInContext(app.slice(app.indexOf('function escapeHtml('), app.indexOf('function queryString(')), ctx);
vm.runInContext(app.slice(app.indexOf('async function validatedNoteCitationSpans('), app.indexOf('async function openDoc(')), ctx);
const body = '😀前言。\n风险一：相同文字。\n\n风险二：相同文字。\n末尾条件不变。';
const note = {id:8,parent_note_id:2,tab_name:'核对页',content:body};
const text = '相同文字。', start = Array.from(body.slice(0,body.lastIndexOf(text))).length;
const span = {source_id:8,parent_note_id:2,tab_name:'核对页',field:'content',char_start:start,char_end_exclusive:start+Array.from(text).length,quote:text,source_revision:crypto.createHash('sha256').update(body).digest('hex')};
async function check(n,spans) {
  ctx.note=n;ctx.spans=spans;
  const checked=await vm.runInContext('validatedNoteCitationSpans(note,spans)',ctx);
  ctx.checked=checked;
  const html=vm.runInContext('renderNoteContent(note.content,["风险"],{citationSpans:checked})',ctx);
  return {checked,html};
}
(async()=>{
  const good=await check(note,[span]);
  assert.equal(good.checked.length,1);
  assert.equal((good.html.match(/<mark>/g)||[]).length,1);
  assert(good.html.includes('风险二：<mark>相同文字。</mark>'));
  assert(good.html.includes('风险一：相同文字。'));
  assert(good.html.includes('末尾条件不变。'));
  for (const spans of [[null,span],[{...span,source_id:9}],[{...span,tab_name:'另一页'}],[{...span,field:'ocr_text'}],[{...span,source_revision:'stale'}],[{...span,quote:'错文字'}],[span,{...span,char_start:start+1,quote:text.slice(1)}]]) {
    const bad=await check(note,spans);
    assert.equal(bad.checked.length,0);assert(!bad.html.includes('<mark>'));
  }
  const stale=await check({...note,content:body+'新增'},[span]);
  assert.equal(stale.checked.length,0);assert(!stale.html.includes('<mark>'));
  const duplicate=await check(note,[span,span]);assert.equal(duplicate.checked.length,1);
  console.log('PASS note citation spans: unique occurrence, Unicode, no keyword fallback, source/tab/field/revision/quote, malformed/overlap, stale, dedup');
})().catch(e=>{console.error(e);process.exitCode=1;});
