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
    append() {}, focus() {}, select() {}, showModal() {}, close() {},
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
  const context = vm.createContext({
    document: {
      querySelector: get, querySelectorAll: () => [], addEventListener() {},
      createElement: name => element(name), body: element('body'),
    },
    location: {search: ''}, history: {replaceState() {}},
    localStorage: {getItem() { return null; }, setItem() {}, removeItem() {}},
    window: {scrollTo() {}}, navigator: {clipboard: {writeText: async () => {}}},
    URLSearchParams, console,
    setTimeout: () => 1, clearTimeout() {},
    async fetch(url, options) {
      if (url === '/api/report-workspace' && !options) {
        return {ok: true, json: async () => ({templates: [], reports: []})};
      }
      if (url === '/api/report-exports') {
        exportRequests.push({url, options});
        return exportResponse.promise;
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
  return {context, get, run, select, exportResponse, exportRequests};
}

function report(letter, version = 1) {
  return {id: letter.repeat(32), title: '报告 ' + letter, version, template: 'project', edited: false};
}

function receipt(record, letter = 'c') {
  return {
    id: letter.repeat(32), report_id: record.id, version: record.version,
    path: `C:\\资料库\\report-exports\\${letter.repeat(32)}\\报告-${record.title}-v${record.version}.html`,
    filename: `报告-${record.title}-v${record.version}.html`,
    download_url: `/api/report-exports/${letter.repeat(32)}/download`,
  };
}

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
