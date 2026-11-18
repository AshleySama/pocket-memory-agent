// Copyright 2026 AshleySama
// SPDX-License-Identifier: Apache-2.0
// Real scope controls and SSE request path, with bounded DOM/network substitutes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '../..');
const app = fs.readFileSync(path.join(root, 'frontend/app.js'), 'utf8');
const scopeSource = fs.readFileSync(path.join(root, 'frontend/ask-scope.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'frontend/index.html'), 'utf8');
const plain = value => JSON.parse(JSON.stringify(value));
let context, nodes, state, requests, alerts, controls, metadata;

function node(selector) {
  if (!nodes.has(selector)) nodes.set(selector, {
    value: '', textContent: '', innerHTML: '', dataset: {}, hidden: false, disabled: false,
    isConnected: true, open: false, listeners: {},
    addEventListener(name, listener) { this.listeners[name] = listener; },
    focus() { context.document.activeElement = this; },
    showModal() { this.open = true; }, close() { this.open = false; },
    querySelectorAll() { return controls.filter(control => !control.disabled); },
  });
  return nodes.get(selector);
}

function call(name, ...args) {
  context.args = args;
  return vm.runInContext(`${name}(...args)`, context);
}

function sse(answer = '已核实的回答。') {
  let delivered = false;
  return { ok: true, body: { getReader: () => ({
    read: async () => delivered ? { done: true } : (delivered = true, {
      done: false, value: new TextEncoder().encode(`data: ${JSON.stringify({ type: 'done', answer, sources: [], kind: 'qa' })}\n\n`),
    }), cancel: async () => {},
  }) } };
}

function reset() {
  nodes = new Map(); requests = []; alerts = []; controls = [];
  metadata = { notes: [{ id: 32, title: '笔记说明', tab_count: 3 }, { id: 27, title: '<img src=x onerror=bad()>', tab_count: 1 }],
    documents: [{ id: 32, title: '导入预算表', original_name: 'budget.xlsx' }] };
  context = vm.createContext({ AbortController, TextDecoder, TextEncoder, crypto: crypto.webcrypto,
    performance: { now: () => 10 }, clearTimeout() {}, setTimeout: () => 1,
    document: { activeElement: null }, $: node,
    request: async url => { requests.push({ url }); return metadata; },
    fetch: async (url, options) => { requests.push({ url, body: JSON.parse(options.body), signal: options.signal }); return url.endsWith('/cancel') ? { ok: true } : sse(); },
    showAlert: async text => alerts.push(text), switchPane() {}, INTENT_LABELS: { qa: '智能问答' }, renderIntentData: () => '',
  });
  vm.runInContext(app.slice(0, app.indexOf('/* ---- 风格定义')), context);
  state = vm.runInContext('state', context);
  vm.runInContext(app.slice(app.indexOf('function escapeHtml('), app.indexOf('function queryString(')), context);
  vm.runInContext(scopeSource, context);
  vm.runInContext(app.slice(app.indexOf('function answerSourceLink('), app.indexOf('const INTENT_LABELS =')), context);
  vm.runInContext(app.slice(app.indexOf('function askIsCurrent('), app.indexOf('function renderAnswerStreaming(')), context);
  call('initializeAskScope');
  controls = [node('#ask-source-search'), node('#ask-source-cancel'), node('#ask-source-apply')];
  node('#search').value = '项目有哪些风险';
  node('#ask-scope-mode').focus();
}

const cases = [];
async function check(name, test) {
  reset();
  try { await test(); cases.push({ name, passed: true }); }
  catch (error) { cases.push({ name, passed: false, error: error.stack }); }
}

(async () => {
  await check('Default all scope sends the exact question and labels the final answer', async () => {
    await call('askWith', '/api/ask', '正在生成');
    assert.deepEqual(plain(requests[0].body), { question: '项目有哪些风险', stream: true, history: [], scope: { mode: 'all', note_ids: [], document_ids: [] } });
    assert.match(node('#answer-panel').innerHTML, /问答范围：全部资料/);
  });
  await check('A top search result is never an implicit current note', async () => {
    state.notes = [{ id: 27, title: '搜索首条' }]; state.allNotes = state.notes; state.selectedNoteId = 27;
    call('setAskScope', { mode: 'current', note_ids: [], document_ids: [] });
    assert.equal(node('#ask-scope-current').disabled, true);
    assert.equal(node('#ask-button').disabled, true);
    await call('askWith', '/api/ask', '正在生成');
    assert.equal(requests.length, 0); assert.equal(alerts.length, 1);
  });
  await check('An explicitly opened child uses its parent family and survives automatic search selection', async () => {
    state.allNotes = [{ id: 32, title: '笔记说明' }, { id: 27, title: '搜索首条' }];
    call('rememberAskCurrentNote', { id: 40, parent_note_id: 32, title: '子页', tab_name: '计划' }, 'reference');
    call('setAskScope', { mode: 'current', note_ids: [], document_ids: [] });
    state.selectedNoteId = 27;
    await call('askWith', '/api/ask', '正在生成');
    assert.deepEqual(plain(requests[0].body.scope), { mode: 'current', note_ids: [32], document_ids: [] });
    assert.equal(requests[0].body.question, '项目有哪些风险');
    assert.match(node('#answer-panel').innerHTML, /当前笔记：笔记说明（全部页签）/);
  });
  await check('Searchable metadata selector keeps note and document identities separate', async () => {
    await call('openAskSourcePicker');
    assert.equal(requests[0].url, '/api/ask/sources');
    assert.match(node('#ask-source-options').innerHTML, /note:32/);
    assert.match(node('#ask-source-options').innerHTML, /document:32/);
    node('#ask-source-search').value = '说明';
    node('#ask-source-search').listeners.input();
    assert.match(node('#ask-source-options').innerHTML, /笔记说明/);
    assert(!node('#ask-source-options').innerHTML.includes('导入预算表'));
    for (const key of ['note:32', 'document:32']) {
      const input = { checked: true, dataset: { askSource: key }, closest() { return this; } };
      node('#ask-source-options').listeners.change({ target: input });
    }
    node('#ask-source-apply').listeners.click();
    assert.equal(node('#ask-source-picker').open, false);
    assert.equal(context.document.activeElement, node('#ask-scope-mode'));
    await call('askWith', '/api/ask', '正在生成');
    assert.deepEqual(plain(requests.find(item => item.body?.question).body.scope), { mode: 'selected', note_ids: [32], document_ids: [32] });
    assert.match(node('#answer-panel').innerHTML, /笔记说明（全部页签）、导入预算表/);
  });
  await check('Cancel and Escape preserve the applied scope/history and return focus', async () => {
    state.askHistory = [{ role: 'user', content: '保留历史' }];
    await call('openAskSourcePicker'); state.askSourcePicker.selected.add('note:32');
    node('#ask-source-cancel').listeners.click();
    assert.equal(state.askScope.mode, 'all'); assert.equal(state.askHistory.length, 1);
    await call('openAskSourcePicker');
    let prevented = false, stopped = false;
    node('#ask-source-picker').listeners.keydown({ key: 'Escape', preventDefault() { prevented = true; }, stopPropagation() { stopped = true; } });
    assert(prevented && stopped); assert.equal(node('#ask-source-picker').open, false);
    assert.equal(state.askScope.mode, 'all'); assert.equal(state.askHistory.length, 1);
    assert.equal(context.document.activeElement, node('#ask-scope-mode'));
  });
  await check('Tab and Shift+Tab stay inside the open source picker', async () => {
    await call('openAskSourcePicker');
    node('#ask-source-cancel').focus(); let prevented = false;
    node('#ask-source-picker').listeners.keydown({ key: 'Tab', shiftKey: false, preventDefault() { prevented = true; } });
    assert(prevented); assert.equal(context.document.activeElement, node('#ask-source-search'));
    node('#ask-source-picker').listeners.keydown({ key: 'Tab', shiftKey: true, preventDefault() {} });
    assert.equal(context.document.activeElement, node('#ask-source-cancel'));
  });
  await check('Empty and unmatched catalogs explain the empty state and cannot apply', async () => {
    metadata = { notes: [], documents: [] }; await call('openAskSourcePicker');
    assert.match(node('#ask-source-options').innerHTML, /还没有可选资料/);
    assert.equal(node('#ask-source-apply').disabled, true); call('applyAskSourcePicker');
    assert.equal(state.askScope.mode, 'all');
    node('#ask-source-search').value = '不存在'; call('renderAskSourceChoices');
    assert.match(node('#ask-source-options').innerHTML, /没有匹配的资料/);
  });
  await check('History follows only the same effective scope; switching removes old evidence', async () => {
    await call('askWith', '/api/ask', '正在生成');
    await call('askWith', '/api/ask', '正在生成');
    assert.equal(requests[1].body.history.length, 2);
    call('rememberAskCurrentNote', { id: 27, title: '项目笔记' });
    call('setAskScope', { mode: 'current', note_ids: [], document_ids: [] });
    assert.equal(state.askHistory.length, 0);
    assert(!node('#answer-panel').innerHTML.includes('已核实的回答'));
    await call('askWith', '/api/ask', '正在生成');
    assert.equal(requests[2].body.history.length, 0);
    call('rememberAskCurrentNote', { id: 32, title: '另一笔记' });
    assert.equal(state.askHistory.length, 0);
    await call('askWith', '/api/ask', '正在生成');
    assert.equal(requests[3].body.history.length, 0);
  });
  await check('A scope change cancels the active request and discards a late answer', async () => {
    let deliver;
    context.fetch = async (url, options) => {
      requests.push({ url, body: JSON.parse(options.body), signal: options.signal });
      if (url.endsWith('/cancel')) return { ok: true };
      return new Promise(resolve => { deliver = resolve; });
    };
    const pending = call('askWith', '/api/ask', '正在生成');
    call('rememberAskCurrentNote', { id: 27, title: '项目笔记' });
    call('setAskScope', { mode: 'current', note_ids: [], document_ids: [] });
    assert.equal(requests[0].signal.aborted, true);
    assert.equal(requests[1].url, '/api/ask/cancel');
    deliver(sse('不可泄漏的旧回答'));
    await pending;
    assert(!node('#answer-panel').innerHTML.includes('不可泄漏'));
    assert.equal(state.askHistory.length, 0);
  });
  await check('Titles are escaped in both selector rows and final scope labels', async () => {
    await call('openAskSourcePicker');
    const options = node('#ask-source-options').innerHTML;
    assert(!options.includes('<img')); assert(options.includes('&lt;img'));
    state.askSourcePicker.selected.add('note:27'); call('applyAskSourcePicker');
    await call('askWith', '/api/ask', '正在生成');
    const answer = node('#answer-panel').innerHTML;
    assert(!answer.includes('<img src=x')); assert(answer.includes('&lt;img src=x'));
  });
  await check('Library reset cancels an open chooser and clears scope identities/history', async () => {
    call('rememberAskCurrentNote', { id: 27, title: '旧库' });
    call('setAskScope', { mode: 'current', note_ids: [], document_ids: [] });
    state.askHistory = [{ role: 'user', content: '旧库记录' }];
    await call('openAskSourcePicker'); call('resetAskScope');
    assert.equal(state.askScope.mode, 'all'); assert.equal(state.askCurrentNote, null);
    assert.equal(state.askSources, null); assert.equal(state.askHistory.length, 0);
    assert.equal(node('#ask-source-picker').open, false);
  });
  await check('A closed picker ignores a delayed metadata response', async () => {
    let deliver; context.request = () => new Promise(resolve => { deliver = resolve; });
    const pending = call('openAskSourcePicker'); call('closeAskSourcePicker');
    deliver(metadata); await pending;
    assert.equal(state.askSources, null); assert.equal(state.askSourcePicker, null);
    assert.equal(node('#ask-source-picker').open, false);
  });
  await check('The toolbar has no manual scope row and automatic restored tabs keep existing semantics', () => {
    assert.doesNotMatch(html, /class="ask-scope-controls"|id="ask-scope-mode"/);
    assert.match(app, /activeTab, \{ tabsBar, updateAskScope: false \}/);
  });
  await check('Removed scope controls initialize safely and clear old scoped history', () => {
    context.$ = selector => selector === '#ask-scope-mode' ? null : node(selector);
    state.askScope = { mode: 'selected', note_ids: [27], document_ids: [32] };
    state.askSources = metadata;
    state.askSourcePicker = { selected: new Set(['note:27']) };
    state.askHistory = [{ role: 'user', content: '旧的限定范围问题' }];
    state.askHistoryScopeKey = 'old';
    call('initializeAskScope');
    assert.equal(state.askScope.mode, 'all');
    assert.equal(state.askSources, null);
    assert.equal(state.askSourcePicker, null);
    assert.equal(state.askHistory.length, 0);
    assert.equal(state.askHistoryScopeKey, null);
  });
  await check('The real ask request ignores stale selected IDs when the scope row is absent', async () => {
    context.$ = selector => selector === '#ask-scope-mode' ? null : node(selector);
    state.askScope = { mode: 'selected', note_ids: [27], document_ids: [32] };
    state.askHistory = [{ role: 'user', content: '旧的限定范围问题' }];
    state.askHistoryScopeKey = call('askScopeKey', state.askScope);
    await call('askWith', '/api/ask', '正在生成');
    assert.deepEqual(plain(requests[0].body), { question: '项目有哪些风险', stream: true, history: [], scope: { mode: 'all', note_ids: [], document_ids: [] } });
  });
  await check('Opening another note leaves global question scope and same-scope history intact', async () => {
    context.$ = selector => selector === '#ask-scope-mode' ? null : node(selector);
    call('initializeAskScope');
    await call('askWith', '/api/ask', '正在生成');
    call('rememberAskCurrentNote', { id: 27, title: '另一篇笔记' });
    await call('askWith', '/api/ask', '正在生成');
    assert.deepEqual(plain(requests[1].body.scope), { mode: 'all', note_ids: [], document_ids: [] });
    assert.equal(requests[1].body.history.length, 2);
    assert.equal(alerts.length, 0);
  });
  const result = { passed: cases.filter(item => item.passed).length, total: cases.length, cases };
  console.log(JSON.stringify(result, null, 2));
  if (process.argv[2]) fs.writeFileSync(process.argv[2], JSON.stringify(result, null, 2) + '\n');
  process.exitCode = result.passed === result.total ? 0 : 1;
})().catch(error => { console.error(error); process.exitCode = 1; });
