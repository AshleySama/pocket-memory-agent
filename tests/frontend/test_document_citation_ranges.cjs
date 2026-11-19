/* Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.resolve(__dirname,'../../frontend/app.js'),'utf8');
const ctx = vm.createContext({});
vm.runInContext(source.slice(source.indexOf('function escapeHtml('), source.indexOf('function queryString(')),ctx);
vm.runInContext(source.slice(source.indexOf('function answerSourceLink('), source.indexOf('function renderAnswer(payload')),ctx);
vm.runInContext(source.slice(source.indexOf('function renderDocumentCitationText('),source.indexOf('async function openImportedDocument(')),ctx);
const chunk={id:7,text:'😀前言。AQS 是多线\n程同步器。尾注。',text_revision:'abc'};
const chars=Array.from(chunk.text), quote='AQS 是多线\n程同步器。', start=chars.indexOf('A');
const span={field:'document_text',document_id:3,chunk_id:7,char_start:start,char_end_exclusive:start+Array.from(quote).length,quote,source_revision:'abc'};
function render(spans, c=chunk, doc=3){ctx.args=[c,doc,spans];return vm.runInContext('renderDocumentCitationText(...args)',ctx);}
assert.equal(render([span]), '😀前言。<mark>'+quote+'</mark>尾注。');
assert.equal(render([span],{...chunk,id:8}),chunk.text);
assert.equal(render([span],{...chunk,text_revision:'changed'}),chunk.text);
assert.equal(render([{...span,quote:'伪造'}]),chunk.text);
assert.equal(render([{...span,char_start:-1}]),chunk.text);
assert.equal(render([]),chunk.text);
assert.equal(render([span],chunk,4),chunk.text);
assert(!render([], {...chunk,text:'<script>bad</script>'}).includes('<script>'));
ctx.src={index:1,title:'PDF',url:'/api/documents/3/file?chunk_id=7',source_type:'document',display_evidence:quote,display_context:chunk.text,display_document_spans:[span],display_evidence_kind:'literal_sentence_focus'};
const card=vm.runInContext('renderAnswerSource(src)',ctx);
assert(card.includes('data-doc-exact="true"'));
assert(card.includes('data-doc-ranges='));
assert(card.includes('查看完整上下文'));
assert(card.includes('source-context'));
ctx.link={dataset:{docExact:'true',docRanges:JSON.stringify([span])}};
assert.equal(vm.runInContext('documentLinkOptions(link).documentSpans[0].quote',ctx),quote);
ctx.src={...ctx.src,display_evidence:'',display_document_spans:[],display_evidence_kind:'context_only'};
const nofocus=vm.runInContext('renderAnswerSource(src)',ctx);
assert(!nofocus.includes('<strong>证据句</strong>'));
assert(nofocus.includes('未定位到具体句'));
console.log('Document exact ranges: focus, Unicode, wrong chunk/document, stale revision, tampering, no-focus and escaping passed');
