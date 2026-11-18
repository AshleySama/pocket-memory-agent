/* Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0
 */
'use strict';

// Run the real workbench script and its export button handler. No DOM package,
// browser, model, or filesystem writes are needed for these UI state checks.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const script = fs.readFileSync(path.join(__dirname, '..', 'frontend', 'reports-v2.js'), 'utf8');

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

function element(selector) {
  const classes = new Set();
  const attributes = new Map();
  const listeners = new Map();
  return {
    selector, textContent: '', innerHTML: '', value: '', href: '', download: '',
    hidden: selector === '#export-result' || selector === '#export-details' || selector === '#toast', disabled: false,
    dataset: {}, scrollCalls: 0,
    classList: {
      add: value => classes.add(value), remove: value => classes.delete(value),
      contains: value => classes.has(value),
      toggle(value, force) {
        const active = force === undefined ? !classes.has(value) : force;
        if (active) classes.add(value); else classes.delete(value);
        return active;
      },
    },
    append() {}, focus() {}, select() {},
    showModal() { this.open = true; },
    close() { this.open = false; return this.dispatch('close'); },
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(listener);
    },
    async dispatch(type, event = {}) {
      const dispatched = {target: this, preventDefault() {}, ...event};
      for (const listener of listeners.get(type) || []) await listener(dispatched);
    },
    querySelector() { return null; }, querySelectorAll() { return []; },
    insertAdjacentHTML(_position, html) { this.innerHTML += html; },
    setAttribute(name, value) { attributes.set(name, String(value)); },
    getAttribute(name) { return attributes.get(name) ?? null; },
    scrollIntoView() { this.scrollCalls++; },
  };
}

async function harness() {
  const nodes = new Map();
  const get = selector => {
    if (!nodes.has(selector)) nodes.set(selector, element(selector));
    return nodes.get(selector);
  };
  const exportResponse = deferred();
  const exportRequests = [];
  const sourceRequests = [];
  const jobRequests = [];
  const createRequests = [], opened = [];
  const context = vm.createContext({
    document: {
      ...element('document'), querySelector: get, querySelectorAll: () => [],
      createElement: name => element(name), body: element('body'),
    },
    location: {search: ''}, history: {replaceState() {}},
    localStorage: {getItem() { return null; }, setItem() {}, removeItem() {}},
    window: {scrollTo() {},open(...args){opened.push(args);return {};}}, navigator: {clipboard: {writeText: async () => {}}},
    URLSearchParams, console,
    setTimeout: () => 1, clearTimeout() {},
    async fetch(url, options) {
      const readRequest = !options?.method || options.method === 'GET';
      if (url === '/api/report-workspace/jobs' && !readRequest) {
        createRequests.push(JSON.parse(options.body));return {ok:true,json:async()=>({id:'fixture-job'})};
      }
      if (url === '/api/report-workspace' && readRequest) {
        return {ok: true, json: async () => ({templates: [], reports: []})};
      }
      if (url === '/api/report-exports') {
        exportRequests.push({url, options});
        return exportResponse.promise;
      }
      if (url.startsWith('/api/report-workspace/sources?') && readRequest) {
        const response = deferred();
        sourceRequests.push({url, response});
        return response.promise;
      }
      if (url.startsWith('/api/report-workspace/jobs/') && readRequest) {
        const response=deferred();jobRequests.push({url,response});return response.promise;
      }
      throw Error('Unexpected request: ' + url);
    },
  });
  vm.runInContext(script, context, {filename: 'reports-v2.js', timeout: 2000});
  // Let the script's ordinary initial catalogue request complete before actions.
  await new Promise(resolve => setImmediate(resolve));
  const run = code => vm.runInContext(code, context, {timeout: 2000});
  function select(report) {
    run(`display(${JSON.stringify({report, html: '<h1>' + report.title + '</h1>'})})`);
  }
  return {context, get, run, select, exportResponse, exportRequests, sourceRequests, jobRequests, createRequests, opened};
}

test('new report goes directly to complete phase, not an outline stop',async()=>{
  const h=await harness();h.run("poll=()=>{};state.selected.add('document:26')");
  h.get('#template-input').value='auto';h.get('#composition-mode').value='source_compiled';
  h.get('#focus-input').value='';h.get('#title-input').value='菜谱';
  await h.get('#create-form').onsubmit({preventDefault(){}});
  assert.equal(h.createRequests.length,1);assert.equal(h.createRequests[0].phase,'complete');
  assert.equal(h.createRequests[0].composition_mode,'source_compiled');
});

test('opening exported HTML uses its browser URL without OS file association',async()=>{
  const h=await harness(),r=report('a');h.select(r);
  const saved={...receipt(r),view_url:'/api/report-exports/'+ 'c'.repeat(32)+'/view'};
  h.run(`state.exports.set(exportKey(state.report),${JSON.stringify(saved)})`);
  await h.get('#export-open').onclick();
  assert.deepEqual(h.opened,[[saved.view_url,'_blank','noopener']]);
  assert.match(h.get('#export-action-status').textContent,/新标签页/);
});

function report(letter, version = 1) {
  return {id: letter.repeat(32), title: '报告 ' + letter, version, template: 'project', edited: false};
}

test('empty generation failure keeps its record but cannot export an empty document', async () => {
  const h=await harness();
  h.select({...report('f'),template:'auto',adaptive:{status:'partial',chapters:[]}});
  assert.equal(h.get('#export').disabled,true);
  assert.match(h.get('#save-status').textContent,/生成未完成/);
  await h.get('#export').onclick();
  assert.equal(h.get('#export').disabled,true);
});

function receipt(record, letter = 'c') {
  return {
    id: letter.repeat(32), report_id: record.id, version: record.version,
    path: `C:\\资料库\\report-exports\\${letter.repeat(32)}\\报告-${record.title}-v${record.version}.html`,
    filename: `报告-${record.title}-v${record.version}.html`,
    download_url: `/api/report-exports/${letter.repeat(32)}/download`,
  };
}

test('paused reading offers continuation and cannot export or rename unfinished work', async () => {
  const h=await harness();
  const r={...report('d'),template:'auto',adaptive:{status:'reading_paused'}};
  h.select(r);
  assert.equal(h.get('#export').disabled,true);
  assert.equal(h.get('#rename').disabled,true);
  assert.equal(h.get('#reading').disabled,true);
  assert.match(h.get('#save-status').textContent,/阅读进度已保存/);
  assert.equal(typeof h.get('#resume-reading').onclick,'function');
  await h.run("document.querySelector('#export').onclick()");
  assert.equal(h.exportRequests.length,0);
});

test('reopening a running snapshot follows the existing job only once', async () => {
  const h=await harness();
  const data={report:{...report('e'),template:'auto',adaptive:{status:'reading_paused'}},html:'<p>阅读进度</p>',active_job:{id:'existing'}};
  h.run(`display(${JSON.stringify(data)})`);
  h.run(`display(${JSON.stringify(data)})`);
  assert.equal(h.jobRequests.length,1);
  assert.equal(h.jobRequests[0].url,'/api/report-workspace/jobs/existing');
  assert.equal(h.get('#resume-reading').disabled,true);
  h.jobRequests[0].response.resolve({ok:true,json:async()=>({status:'error',stage:'fixture',started:0,error:'fixture_stop'})});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(h.get('#resume-reading').disabled,false);
});

test('successful export shows the saved path and download URL only after server confirmation', async () => {
  const h = await harness();
  const record = report('a', 3), saved = receipt(record);
  h.select(record);
  const completed = h.run("document.querySelector('#export').onclick()");
  assert.equal(h.get('#export').disabled, true);
  assert.equal(h.get('#export').textContent, '正在保存…');
  assert.equal(h.get('#export-result').hidden, true);
  assert.equal(h.exportRequests.length, 1);
  const request = h.exportRequests[0].options;
  assert.equal(request.method, 'POST');
  assert.equal(request.headers['X-PocketMemory-Report-Export'], '1');
  assert.deepEqual(JSON.parse(request.body), {report_id: record.id, version: 3});

  h.exportResponse.resolve({ok: true, json: async () => saved});
  await completed;
  assert.equal(h.get('#export-result').hidden, false);
  assert.equal(h.get('#export-path').value, saved.path);
  assert.equal(h.get('#export-download').href, saved.download_url);
  assert.equal(h.get('#export-download').download, saved.filename);
  assert.equal(h.get('#export-result-title').textContent, '已导出');
  assert.equal(h.get('#export-filename').textContent, saved.filename);
  assert.equal(h.get('#export-filename').title, saved.filename);
  assert.equal(h.get('#export-details').hidden, true);
  assert.equal(h.get('#export-details-toggle').getAttribute('aria-expanded'), 'false');
  assert.equal(h.get('#toast').textContent, '完整 HTML 已保存到本机');
  assert.equal(h.get('#export').disabled, false);
  assert.equal(h.get('#export').textContent, '导出完整 HTML');
  assert.equal(h.get('#export-result').scrollCalls, 1);
});

test('failed export does not display a saved path or success notification', async () => {
  const h = await harness();
  h.select(report('a'));
  const completed = h.run("document.querySelector('#export').onclick()");
  h.exportResponse.resolve({ok: false, json: async () => ({error: '目录无写入权限'})});
  await completed;
  assert.equal(h.get('#export-result').hidden, true);
  assert.equal(h.get('#export-path').value, '');
  assert.equal(h.get('#export-download').href, '');
  assert.equal(h.run('state.exports.size'), 0);
  assert.equal(h.get('#toast').textContent, '导出未完成：目录无写入权限');
  assert.equal(h.get('#export').disabled, false);
});

test('switching reports while exporting does not attach the old result to the new report', async () => {
  const h = await harness();
  const oldReport = report('a'), nextReport = report('b', 2), saved = receipt(oldReport);
  h.select(oldReport);
  const completed = h.run("document.querySelector('#export').onclick()");
  h.select(nextReport);
  h.exportResponse.resolve({ok: true, json: async () => saved});
  await completed;
  assert.equal(h.run('state.report.id'), nextReport.id);
  assert.equal(h.get('#report').innerHTML, '<h1>报告 b</h1>');
  assert.match(h.get('#save-status').textContent, /v2/);
  assert.equal(h.get('#export-result').hidden, true);
  assert.equal(h.get('#export-result').scrollCalls, 0);
  assert.equal(h.get('#export').disabled, false);
  // Returning to the exported report reveals only that report's saved result.
  h.select(oldReport);
  assert.equal(h.get('#export-result').hidden, false);
  assert.equal(h.get('#export-path').value, saved.path);
});

test('an old export finishing cannot replace another report\'s existing export result', async () => {
  const h = await harness();
  const oldReport = report('a'), nextReport = report('b', 2);
  const oldSaved = receipt(oldReport, 'c'), nextSaved = receipt(nextReport, 'd');
  h.select(oldReport);
  const completed = h.run("document.querySelector('#export').onclick()");
  h.run(`state.exports.set(${JSON.stringify(nextReport.id + ':v2')}, ${JSON.stringify(nextSaved)})`);
  h.select(nextReport);
  assert.equal(h.get('#export-path').value, nextSaved.path);
  h.exportResponse.resolve({ok: true, json: async () => oldSaved});
  await completed;
  assert.equal(h.run('state.report.id'), nextReport.id);
  assert.equal(h.get('#export-result').hidden, false);
  assert.equal(h.get('#export-path').value, nextSaved.path);
  assert.equal(h.get('#export-download').href, nextSaved.download_url);
  assert.equal(h.get('#export-result-title').textContent, '已导出');
  assert.equal(h.get('#export-filename').textContent, nextSaved.filename);
  assert.equal(h.get('#export-result').scrollCalls, 0);
});

test('export details are collapsed by default and reveal the intact saved path on demand', async () => {
  const h = await harness();
  const record = report('a'), saved = receipt(record);
  h.select(record);
  const completed = h.run("document.querySelector('#export').onclick()");
  h.exportResponse.resolve({ok: true, json: async () => saved});
  await completed;
  assert.equal(h.get('#export-details').hidden, true);
  h.run("document.querySelector('#export-details-toggle').onclick()");
  assert.equal(h.get('#export-details').hidden, false);
  assert.equal(h.get('#export-details-toggle').getAttribute('aria-expanded'), 'true');
  assert.equal(h.get('#export-path').value, saved.path);
  assert.equal(h.get('#export-download').href, saved.download_url);
  h.run("document.querySelector('#export-details-toggle').onclick()");
  assert.equal(h.get('#export-details').hidden, true);
  assert.equal(h.get('#export-details-toggle').getAttribute('aria-expanded'), 'false');
});

test('dismiss only hides the receipt; selecting reports preserves dismissal and exporting again restores it', async () => {
  const h = await harness();
  const record = report('a'), saved = receipt(record);
  h.select(record);
  const completed = h.run("document.querySelector('#export').onclick()");
  h.exportResponse.resolve({ok: true, json: async () => saved});
  await completed;
  h.run("document.querySelector('#export-details-toggle').onclick()");
  h.run("document.querySelector('#export-dismiss').onclick()");
  assert.equal(h.get('#export-result').hidden, true);
  assert.equal(h.get('#export-details').hidden, true);
  assert.equal(h.run('state.exports.size'), 1);
  h.select(report('b'));
  h.select(record);
  assert.equal(h.get('#export-result').hidden, true);
  // Even if a server returns the same valid receipt, a new successful export
  // explicitly restores the confirmation instead of remaining dismissed.
  await h.run("document.querySelector('#export').onclick()");
  assert.equal(h.exportRequests.length, 2);
  assert.equal(h.get('#export-result').hidden, false);
  assert.equal(h.get('#export-filename').textContent, saved.filename);
  assert.equal(h.get('#export-path').value, saved.path);
  assert.equal(h.get('#export-details').hidden, true);
});

function sourcePage(items, total = items.length, page = 1) {
  return {ok: true, json: async () => ({items, total, page, page_size: 30})};
}

test('source choices survive changed search and pagination and can be removed outside the current results', async () => {
  const h = await harness();
  h.get('#source-kind').value = 'all';
  h.get('#source-search').value = '预算';
  const firstSearch = h.run('searchSources(1)');
  h.sourceRequests[0].response.resolve(sourcePage([
    {kind: 'note', id: 7, title: '采购记录', tabs: 2, category: '项目', tags: ['预算'], matched_tab: '预算页', snippet: '二季度预算为十二万元。'},
  ]));
  await firstSearch;
  assert.equal(new URL('http://local' + h.sourceRequests[0].url).searchParams.get('q'), '预算');
  assert.match(h.get('#source-picker').innerHTML, /二季度预算为十二万元/);
  assert.match(h.get('#source-picker').innerHTML, /命中页签：预算页/);
  assert.equal(h.run('state.selected.size'), 0, 'discovery must not automatically select a result');
  h.get('#source-picker').onchange({target: {type: 'checkbox', value: 'note:7', checked: true}});
  assert.equal(h.run("state.selected.has('note:7')"), true);
  assert.match(h.get('#selected-sources').innerHTML, /采购记录/);

  h.get('#source-search').value = '验收';
  h.get('#source-kind').value = 'document';
  const nextPage = h.run('searchSources(2)');
  h.sourceRequests[1].response.resolve(sourcePage([
    {kind: 'document', id: 9, title: '交付办法', snippet: '验收材料应包括签字记录。'},
  ], 31, 2));
  await nextPage;
  const query = new URL('http://local' + h.sourceRequests[1].url).searchParams;
  assert.equal(query.get('kind'), 'document');
  assert.equal(query.get('page'), '2');
  assert.equal(h.run("state.selected.has('note:7')"), true);
  assert.match(h.get('#selected-sources').innerHTML, /采购记录/);
  assert.doesNotMatch(h.get('#source-picker').innerHTML, /采购记录/);
  assert.equal(h.get('#source-prev').disabled, false);
  assert.equal(h.get('#source-next').disabled, true);

  await h.run("document.dispatch('click', {target: {closest(selector) {return selector === '[data-remove-source]' ? {dataset: {removeSource: 'note:7'}} : null;}}})");
  assert.equal(h.run('state.selected.size'), 0);
  assert.equal(h.get('#selected-sources').hidden, true);
  assert.equal(h.get('#selected-count').textContent, '已选 0 项');
});

test('an older source response cannot replace a newer query or clear its selection', async () => {
  const h = await harness();
  h.get('#source-kind').value = 'all';
  h.get('#source-search').value = '旧查询';
  const oldSearch = h.run('searchSources(1)');
  h.get('#source-search').value = '新查询';
  const newSearch = h.run('searchSources(1)');
  h.sourceRequests[1].response.resolve(sourcePage([{kind: 'note', id: 2, title: '新查询结果'}]));
  await newSearch;
  h.get('#source-picker').onchange({target: {type: 'checkbox', value: 'note:2', checked: true}});
  h.sourceRequests[0].response.resolve(sourcePage([{kind: 'note', id: 1, title: '过期查询结果'}], 100));
  await oldSearch;
  assert.match(h.get('#source-picker').innerHTML, /新查询结果/);
  assert.doesNotMatch(h.get('#source-picker').innerHTML, /过期查询结果/);
  assert.equal(h.run('state.sourceTotal'), 1);
  assert.equal(h.run("state.selected.has('note:2')"), true);
  assert.match(h.get('#selected-sources').innerHTML, /新查询结果/);
  assert.equal(h.get('#source-picker').getAttribute('aria-busy'), 'false');
});

test('typing invalidates a pending source response before the debounce starts the next request', async () => {
  const h = await harness();
  h.get('#source-kind').value = 'all';
  const previous = h.run('searchSources(1)');
  h.get('#source-search').value = '刚输入的要求';
  h.get('#source-search').oninput();
  assert.equal(h.sourceRequests.length, 1, 'the debounce has not issued another request');
  h.sourceRequests[0].response.resolve(sourcePage([{kind: 'note', id: 1, title: '过期结果'}]));
  await previous;
  assert.doesNotMatch(h.get('#source-picker').innerHTML, /过期结果/);
  assert.equal(h.get('#source-picker').getAttribute('aria-busy'), 'true');
});
