/*
 * Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0
 */

// Focused clarification rendering; no browser, service, or model is started.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.resolve(__dirname, '../../frontend/app.js'), 'utf8');
const context = vm.createContext({});
vm.runInContext(source.slice(source.indexOf('function escapeHtml('), source.indexOf('function queryString(')), context);
vm.runInContext(source.slice(source.indexOf('function answerSourceLink('), source.indexOf('function renderAnswer(payload')), context);
const call = (name, ...args) => {
  context.args = args;
  return vm.runInContext(`${name}(...args)`, context);
};
const cases = [];
const check = (name, body) => {
  try { body(); cases.push({ name, passed: true }); }
  catch (error) { cases.push({ name, passed: false, error: error.message }); }
};
const reference = {
  index: 7, title: '对象场景清单', source_id: 42, parent_note_id: 41, tab_name: '场景表',
  url: '/api/notes/41?tab_id=42', evidence: '| 名称 | 核心功能 |\n| 对象甲 | 汇总 |',
  clarification_entities: ['对象甲'], reference_role: 'clarification',
};
check('Clarification cards are separate and do not introduce numbered factual citations', () => {
  const html = call('renderClarificationSources', [reference]);
  assert.match(html, /帮助确认对象的资料/);
  assert.match(html, /打开资料后，告诉我具体对象/);
  assert.match(html, /对象场景清单/);
  assert.match(html, /对象甲/);
  assert(!html.includes('[7]'));
  assert(!html.includes('证据句'));
  assert(!html.includes('answer-citation'));
  assert(!html.includes('含 1 处证据'));
});
check('Original tab URL and evidence remain available on the opening link', () => {
  const html = call('renderClarificationSources', [reference]);
  assert.match(html, /data-doc-url="\/api\/notes\/41\?tab_id=42"/);
  assert.match(html, /页签：场景表/);
  assert.match(html, /data-doc-evidence=/);
  assert.match(html, /<details class="source-context"><summary>查看相关原文<\/summary>/);
});
check('Same-document rows share one card and retain each original evidence link', () => {
  const second = { ...reference, index: 8, evidence: '| 名称 | 核心功能 |\n| 对象乙 | 归档 |',
    clarification_entities: ['对象乙', '对象甲'] };
  const before = JSON.stringify([reference, second]);
  const html = call('renderClarificationSources', [reference, second]);
  assert.equal((html.match(/class="answer-source clarification-source"/g) || []).length, 1);
  assert.match(html, /class="clarification-objects"><span>对象甲<\/span><span>对象乙<\/span>/);
  assert.equal((html.match(/>定位这段原文<\/a>/g) || []).length, 2);
  assert.match(html, /对象甲 \| 汇总/);
  assert.match(html, /对象乙 \| 归档/);
  assert.equal(JSON.stringify([reference, second]), before);
});
check('Same-titled sources on different tabs or documents are never merged', () => {
  const anotherTab = { ...reference, source_id: 43, tab_name: '复盘表', url: '/api/notes/41?tab_id=43' };
  const anotherDocument = { ...reference, source_type: 'document', url: '/api/documents/42/file?chunk_id=9' };
  const html = call('renderClarificationSources', [reference, anotherTab, anotherDocument]);
  assert.equal((html.match(/class="answer-source clarification-source"/g) || []).length, 3);
  assert.match(html, /data-doc-url="\/api\/notes\/41\?tab_id=42"/);
  assert.match(html, /data-doc-url="\/api\/notes\/41\?tab_id=43"/);
  assert.match(html, /data-doc-url="\/api\/documents\/42\/file\?chunk_id=9"/);
});
check('Missing references produce no section; a plain answer stays unchanged', () => {
  assert.equal(call('renderClarificationSources', []), '');
  assert.equal(call('renderAnswerText', '请明确对象名称。', []), '请明确对象名称。');
});
check('User text is escaped and source metadata is not mutated', () => {
  const record = { ...reference, title: '<img src=x onerror=alert(1)>',
    clarification_entities: ['<script>bad()</script>'], evidence: '<b>原文</b>' };
  const before = JSON.stringify(record);
  const html = call('renderClarificationSources', [record]);
  assert(!/<(?:img|script)[\s>]/.test(html));
  assert.match(html, /&lt;b&gt;原文&lt;\/b&gt;/);
  assert.equal(JSON.stringify(record), before);
});
const result = { passed: cases.filter(item => item.passed).length, total: cases.length, cases };
console.log(JSON.stringify(result, null, 2));
if (process.argv[2]) fs.writeFileSync(process.argv[2], JSON.stringify(result, null, 2) + '\n');
process.exitCode = result.passed === result.total ? 0 : 1;
