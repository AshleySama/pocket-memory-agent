// Copyright 2026 AshleySama
// SPDX-License-Identifier: Apache-2.0

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../frontend/imports.js'), 'utf8');
const view = vm.runInNewContext(`${source}\nPocketImportView;`);

// No DOM package is installed. This models only attachment, focus and the DOM
// methods used by the view. Template input is a deterministic receipt fixture;
// native HTML parsing, CSS motion and actual browser focus need browser checks.
const attrName = key => `data-${String(key).replace(/[A-Z]/g, letter => `-${letter.toLowerCase()}`)}`;
class Element {
  constructor(document, tag) {
    this.ownerDocument = document;
    this.tagName = tag.toUpperCase();
    this.parentElement = null;
    this.nodes = [];
    this.attributes = new Map();
    this.listeners = new Map();
    this.open = false;
    this.dataset = new Proxy({}, {
      get: (_, key) => this.attributes.get(attrName(key)),
      set: (_, key, value) => { this.setAttribute(attrName(key), value); return true; },
    });
  }
  get children() { return this.nodes; }
  get childNodes() { return this.nodes; }
  get firstElementChild() { return this.nodes[0] || null; }
  get isConnected() { return this === this.ownerDocument.body || Boolean(this.parentElement?.isConnected); }
  get disabled() { return this.hasAttribute('disabled'); }
  hasAttribute(name) { return this.attributes.has(name); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); this.ownerDocument.mutations++; }
  removeAttribute(name) { if (this.attributes.delete(name)) this.ownerDocument.mutations++; }
  contains(node) { return this === node || this.nodes.some(child => child.contains(node)); }
  closest(tag) { return this.tagName === tag.toUpperCase() ? this : this.parentElement?.closest(tag) || null; }
  remove() {
    if (!this.parentElement) return;
    if (this.contains(this.ownerDocument.activeElement)) this.ownerDocument.activeElement = this.ownerDocument.body;
    const siblings = this.parentElement.nodes;
    siblings.splice(siblings.indexOf(this), 1);
    this.parentElement = null;
    this.ownerDocument.mutations++;
  }
  insertBefore(node, reference) {
    if (node === reference) return node;
    node.remove();
    const index = reference ? this.nodes.indexOf(reference) : this.nodes.length;
    assert(index >= 0, 'reference node belongs to this parent');
    this.nodes.splice(index, 0, node);
    node.parentElement = this;
    this.ownerDocument.mutations++;
    return node;
  }
  replaceChildren(...nodes) {
    for (const child of [...this.nodes]) child.remove();
    for (const child of nodes) this.insertBefore(child, null);
  }
  querySelectorAll(selector) {
    const matches = element => {
      if (selector === 'details > summary') return element.tagName === 'SUMMARY' && element.parentElement?.tagName === 'DETAILS';
      if (selector.startsWith('[')) return element.hasAttribute(selector.slice(1, -1));
      return element.tagName === selector.toUpperCase();
    };
    return this.nodes.flatMap(child => [...(matches(child) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() {
    if (this.isConnected && !this.disabled) {
      this.ownerDocument.activeElement = this;
      this.ownerDocument.focusCalls++;
    }
  }
  addEventListener(type, callback) { this.listeners.set(type, callback); }
  finishAnimation() { this.listeners.get('animationend')?.({target:this}); }
}

function setup({reduced=false, open=true} = {}) {
  const document = {mutations:0, focusCalls:0};
  document.defaultView = {matchMedia:() => ({matches:reduced})};
  document.body = new Element(document, 'body');
  document.activeElement = document.body;
  const make = (tag, attributes={}) => {
    const element = new Element(document, tag);
    for (const [name, value] of Object.entries(attributes)) element.setAttribute(name, value);
    return element;
  };
  document.createElement = tag => {
    if (tag !== 'template') return make(tag);
    const content = make('fragment');
    return {content, set innerHTML(value) {
      const row = JSON.parse(value);
      const item = make('li', {'data-failed':String(['failed', 'upload_failed', 'rejected'].includes(row.status))});
      const heading = make('div');
      const label = make('span');
      label.textContent = row.status;
      heading.insertBefore(label, null);
      item.insertBefore(heading, null);
      if (row.details) {
        const details = make('details');
        const summary = make('summary');
        summary.textContent = row.details;
        details.insertBefore(summary, null);
        item.insertBefore(details, null);
      }
      const action = make('button', {[row.action]:row.key, ...(row.disabled ? {disabled:''} : {})});
      item.insertBefore(action, null);
      content.replaceChildren(item);
    }};
  };
  const dialog = make('dialog');
  dialog.open = open;
  const list = make('ul');
  document.body.insertBefore(dialog, null);
  dialog.insertBefore(list, null);
  const renderRow = row => JSON.stringify({key:row.key, status:row.status, details:row.details || '', action:row.action || 'data-import-retry', disabled:!!row.disabled});
  return {document, dialog, list, render:(rows, scope='library-A') => view.render(list, rows, renderRow, scope)};
}

const results = [];
function test(name, run) {
  try { run(); results.push({name, pass:true}); }
  catch (error) { results.push({name, pass:false, error:error.stack}); }
}

test('unchanged polling preserves row, action, focus and expanded details without DOM writes', () => {
  const s = setup();
  const row = {key:'one', status:'failed', details:'First failure'};
  s.render([row]);
  const item = s.list.firstElementChild;
  item.finishAnimation();
  const button = item.querySelector('button');
  item.querySelector('details').open = true;
  button.focus();
  const mutations = s.document.mutations;
  s.render([{...row}]);
  assert.equal(s.list.firstElementChild, item);
  assert.equal(item.querySelector('button'), button);
  assert.equal(s.document.activeElement, button);
  assert.equal(item.querySelector('details').open, true);
  assert.equal(item.dataset.importAnimate, undefined);
  assert.equal(s.document.mutations, mutations);
});

test('changed status keeps row identity and restores the same action and open details', () => {
  const s = setup();
  s.render([{key:'one', status:'failed', details:'First failure'}]);
  const item = s.list.firstElementChild;
  item.finishAnimation();
  item.querySelector('details').open = true;
  item.querySelector('button').focus();
  s.render([{key:'one', status:'upload_failed', details:'Submission unconfirmed'}]);
  assert.equal(s.list.firstElementChild, item);
  assert.equal(item.querySelector('span').textContent, 'upload_failed');
  assert.equal(item.dataset.importState, 'upload_failed');
  assert.equal(item.dataset.importAnimate, 'change');
  assert.equal(s.document.activeElement, item.querySelector('button'));
  assert.equal(item.querySelector('details').open, true);
  item.querySelector('summary').focus();
  s.render([{key:'one', status:'upload_failed', details:'Updated reason'}]);
  assert.equal(s.document.activeElement, item.querySelector('summary'));
  assert.equal(item.querySelector('details').open, true);
});

test('removing an earlier row leaves surviving keyboard focus and action identity untouched', () => {
  const s = setup();
  const first = {key:'first', status:'ready'};
  const second = {key:'second', status:'failed'};
  s.render([first, second]);
  const removed = s.list.children[0];
  const survivor = s.list.children[1];
  const button = survivor.querySelector('button');
  button.focus();
  const calls = s.document.focusCalls;
  s.render([second]);
  assert.equal(s.list.children.length, 1);
  assert.equal(s.list.firstElementChild, survivor);
  assert.equal(s.document.activeElement, button);
  assert.equal(s.document.focusCalls, calls);
  assert.equal(removed.isConnected, false);
  s.render([]);
  assert.equal(s.list.children.length, 0);
  s.render([second]);
  assert.notEqual(s.list.firstElementChild, survivor);
});

test('switching libraries never reuses receipt nodes or focused controls across scopes', () => {
  const s = setup();
  const row = {key:'same-key', status:'failed'};
  s.render([row]);
  const old = s.list.firstElementChild;
  old.querySelector('button').focus();
  s.render([row], 'library-B');
  assert.equal(s.list.children.length, 1);
  assert.notEqual(s.list.firstElementChild, old);
  assert.equal(old.isConnected, false);
  assert.equal(s.document.activeElement, s.document.body);
  const other = s.list.firstElementChild;
  s.render([row], 'library-A');
  assert.notEqual(s.list.firstElementChild, old);
  assert.notEqual(s.list.firstElementChild, other);
});

test('reduced motion updates real processing states without requesting entrance or change animations', () => {
  const s = setup({reduced:true});
  s.render([{key:'one', status:'pending'}]);
  const item = s.list.firstElementChild;
  assert.equal(item.dataset.importActivity, 'waiting');
  assert.equal(item.dataset.importAnimate, undefined);
  for (const status of ['extracting', 'indexing']) {
    s.render([{key:'one', status}]);
    assert.equal(item.dataset.importActivity, 'working');
    assert.equal(item.dataset.importState, status);
    assert.equal(item.dataset.importAnimate, undefined);
  }
  s.render([{key:'one', status:'ready'}]);
  assert.equal(item.dataset.importActivity, 'ready');
  assert.equal(item.dataset.importAnimate, undefined);
});

test('closed-dialog polling never queues row animations for the next opening', () => {
  const s = setup({open:false});
  s.render([{key:'one', status:'extracting'}]);
  const item = s.list.firstElementChild;
  assert.equal(item.dataset.importAnimate, undefined);
  s.render([{key:'one', status:'ready'}]);
  assert.equal(item.dataset.importAnimate, undefined);
  s.dialog.open = true;
  s.render([{key:'one', status:'ready'}]);
  assert.equal(item.dataset.importAnimate, undefined);
  s.render([{key:'one', status:'ready'}, {key:'two', status:'pending'}]);
  assert.equal(s.list.children[1].dataset.importAnimate, 'enter');
});

console.log(JSON.stringify(results, null, 2));
if (results.some(result => !result.pass)) process.exitCode = 1;
