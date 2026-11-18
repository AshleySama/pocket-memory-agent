/*
 * Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0
 */

// Bounded editor state/transport tests. No browser, server, or model is started.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'frontend/app.js'), 'utf8');
const controls = fs.readFileSync(path.join(root, 'frontend/ui-controls.css'), 'utf8');
const draftUi = fs.readFileSync(path.join(root, 'frontend/draft-ui.js'), 'utf8');
const draftStore = fs.readFileSync(path.join(root, 'frontend/drafts.js'), 'utf8');
const timers = new Map();
let nextTimer = 0;
const context = vm.createContext({
  window: {}, crypto: crypto.webcrypto,
  setTimeout(fn, delay) { const id = ++nextTimer; timers.set(id, { fn, delay }); return id; },
  clearTimeout(id) { timers.delete(id); },
  document: { querySelector: () => null },
  importQueue: { busy: false, rows: [] },
});
vm.runInContext(draftStore, context);
vm.runInContext(source.slice(0, source.indexOf('/* ---- 风格定义')), context);
vm.runInContext(source.slice(source.indexOf('function syncCachedNote('), source.indexOf('const WORKBENCH_LAYOUT_KEY')), context);
vm.runInContext(source.slice(source.indexOf('function renderNoteBody('), source.indexOf('function renderNoteHtml(')), context);
vm.runInContext(draftUi.slice(draftUi.indexOf('function draftKey('), draftUi.indexOf('function capturePersistentNew(')), context);
const call = (name, ...args) => {
  context.args = args;
  return vm.runInContext(`${name}(...args)`, context);
};
const read = expression => vm.runInContext(expression, context);
function element(noteId = 42, text = '原始正文') {
  const toolbar = { hidden: true };
  const toggle = { dataset: {}, htmlWrites: 0, html: '',
    get innerHTML() { return this.html; },
    set innerHTML(value) { this.html = value; this.htmlWrites += 1; },
    setAttribute() {}, focus() {} };
  const status = { dataset: {}, textContent: '', title: '' };
  const retry = { hidden: true };
  const note = { dataset: { noteId: '7' }, classList: { toggle() {} }, querySelectorAll: () => [],
    querySelector: selector => selector === '.editor-toolbar' ? toolbar
      : selector === '.note-save-status' ? status
      : selector.includes('retry-note-save') ? retry : toggle };
  return { dataset: { noteId: String(noteId) }, text, attributes: { contenteditable: 'false' },
    classList: { toggle() {} }, toolbar, toggle, status, retry, note, focused: false,
    getAttribute(name) { return this.attributes[name]; },
    setAttribute(name, value) { this.attributes[name] = value; },
    querySelector() { return null; }, closest() { return note; },
    focus() { this.focused = true; } };
}
function reset(noteId = 42) {
  context.draftVault?.detach();
  timers.clear();
  context.editor = element(noteId);
  context.calls = [];
  context.toasts = [];
  context.failSave = false;
  context.failDraftSave = false;
  context.newSaves = 0;
  context.askScopeResets = 0;
  // Ask scope is outside this editor suite; observe the integration call.
  context.resetAskScope = () => { context.askScopeResets += 1; };
  context.$ = selector => selector.includes('.note-content[data-note-id]') ? context.editor : null;
  context.tablerIcon = name => `<svg data-icon="${name}"></svg>`;
  context.editableContentToMarkdown = node => node.text;
  context.renderNoteContent = value => value;
  context.showToast = (...args) => context.toasts.push(args);
  context.syncNewNoteDraftFromDom = () => {};
  context.saveNewNote = () => { context.newSaves += 1; return Promise.resolve(true); };
  const remote = { id: noteId, content: '原始正文', updated_at: 'initial' };
  context.request = async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null;
    context.calls.push({ url, method: options.method || 'GET', body });
    assert.equal(url, `/api/notes/${noteId}`);
    if (!body) return { ...remote };
    assert.equal(options.method, 'PUT');
    assert.equal(body.autosave, true);
    assert.equal(body.expected_content, remote.content);
    if (context.failSave) throw new Error('controlled 503');
    Object.assign(remote, { content: body.content, updated_at: '2026-10-29T01:00:00' });
    return { ...remote };
  };
  // Use the production journal implementation, with separate browser and
  // server stores so browser-only capture cannot masquerade as disk recovery.
  context.journal = new Map();
  const local = new Map();
  context.draftVault = new context.window.PocketDraftVault({
    storage: { get length() { return local.size; }, key: i => [...local.keys()][i],
      getItem: key => local.get(key), setItem: (key, value) => local.set(key, value), removeItem: key => local.delete(key) },
    api: async (url, options = {}) => {
      if (!options.method) return { library_id: 'library-a', drafts: [...context.journal.values()], closed_ids: [] };
      const body = JSON.parse(options.body);
      if (options.method === 'POST') {
        assert.equal(url, '/api/drafts');
        assert.equal(body.library_id, 'library-a');
        if (context.failDraftSave) throw new Error('controlled journal failure');
        context.journal.set(body.draft_id, { ...body });
        return { draft: { ...body } };
      }
      assert.equal(options.method, 'DELETE');
      context.journal.delete(url.split('/').at(-1));
      return {};
    },
  });
  context.draftVault.library = 'library-a';
  context.initialNote = { ...remote };
  vm.runInContext('state.noteEditorSessions.clear(); state.editorActiveTabs.clear(); state.pendingContentSaves.clear(); state.draftNoteSources.clear(); state.newNoteDraft = null; state.notes = [initialNote]; state.allNotes = state.notes; state.selectedNoteId = 7;', context);
  call('noteEditorSession', context.editor);
  return context.editor;
}
const cases = [];
async function check(name, body) {
  try { await body(); cases.push({ name, passed: true }); }
  catch (error) { cases.push({ name, passed: false, error: error.stack }); }
}
async function main() {
  await check('Existing notes start read-only; explicit edit and completion control the toolbar', async () => {
    const editor = reset();
    assert.match(call('renderNoteBody', { id: 42, content: '原文' }), /contenteditable="false"/);
    await call('toggleNoteEditing');
    assert.equal(editor.getAttribute('contenteditable'), 'true');
    assert.equal(editor.toolbar.hidden, false);
    editor.text = '编辑后的正文';
    call('captureEditorDraft');
    await call('toggleNoteEditing');
    assert.equal(editor.getAttribute('contenteditable'), 'false');
    assert.equal(editor.toolbar.hidden, true);
    assert.equal(editor.status.textContent, '已保存');
    assert.equal(editor.dataset.saved, '编辑后的正文');
    assert.equal(context.calls[0].url, '/api/notes/42');
    assert.equal(context.calls[0].body.content, '编辑后的正文');
  });
  await check('A failed save keeps its baseline; only a disk-persisted recovery draft permits same-library navigation', async () => {
    const editor = reset();
    await call('toggleNoteEditing');
    const modeWrites = editor.toggle.htmlWrites;
    editor.text = '失败后保留的草稿';
    context.failSave = true;
    context.failDraftSave = true;
    assert.equal(await call('saveEditorContent'), false);
    assert.equal(editor.dataset.saved, '原始正文');
    assert.equal(read('state.noteEditorSessions.get(42).draft'), '失败后保留的草稿');
    call('noteEditorSession', editor); // Focusing again cannot accept an unsaved draft.
    assert.equal(editor.dataset.saved, '原始正文');
    assert.equal(await call('prepareEditorNavigation'), false);
    assert.equal(context.journal.size, 0);
    assert.equal(context.calls.length, 1, 'Navigation must not silently retry the failed note write');
    assert.equal(editor.getAttribute('contenteditable'), 'true');
    assert.equal(editor.retry.hidden, false);
    context.failDraftSave = false;
    assert.equal(await call('prepareEditorNavigation'), true);
    assert.equal([...context.journal.values()][0].content, '失败后保留的草稿');
    assert.equal(editor.dataset.saved, '原始正文');
    assert.equal(read('state.noteEditorSessions.get(42).status'), 'error');
    assert.equal(await call('prepareLibraryChange'), false, 'A recovery draft must not permit switching libraries');
    assert.equal(context.calls.length, 1);
    context.failSave = false;
    assert.equal(await call('saveCurrentEditor'), true);
    assert.deepEqual(context.calls.map(item => item.method), ['PUT', 'GET', 'PUT']);
    assert.equal(editor.toggle.htmlWrites, modeWrites, 'Saving must keep the Edit/Done child nodes stable through a pending click');
    assert.equal(editor.dataset.saved, '失败后保留的草稿');
    assert.equal(editor.retry.hidden, true);
    assert.equal(context.draftVault.active.size, 0);
    assert.equal(context.journal.size, 0);
    assert.equal(await call('prepareEditorNavigation'), true);
  });
  await check('Typing during an in-flight save is serialized and the newest draft is saved last', async () => {
    const editor = reset();
    await call('toggleNoteEditing');
    const pending = [];
    context.request = (url, options) => {
      context.calls.push({ url, body: JSON.parse(options.body) });
      return new Promise(resolve => pending.push(() => resolve({ id: 42, content: JSON.parse(options.body).content })));
    };
    editor.text = '第一段';
    const first = call('saveEditorContent');
    editor.text = '第一段和新输入';
    call('captureEditorDraft');
    const sameQueue = call('saveEditorContent');
    assert.equal(first, sameQueue);
    assert.equal(context.calls.length, 1);
    pending[0]();
    // request() returns a host-realm promise to VM code. Drain that promise's
    // cross-realm assimilation and continuation before inspecting the queue.
    await new Promise(setImmediate);
    assert.equal(context.calls.length, 2);
    assert.equal(context.calls[1].body.content, '第一段和新输入');
    assert.equal(context.calls[1].body.expected_content, '第一段');
    pending[1]();
    assert.equal(await first, true);
    assert.equal(editor.dataset.saved, '第一段和新输入');
    assert.equal(read('state.notes[0].content'), '第一段和新输入');
  });
  await check('Current-tab save uses the body ID and new-note save uses the draft path', async () => {
    const editor = reset(91);
    await call('toggleNoteEditing');
    editor.text = '子页签独有内容';
    await call('saveCurrentEditor');
    assert.equal(context.calls[0].url, '/api/notes/91');
    vm.runInContext('state.newNoteDraft = {title: "新稿", content: "新内容"};', context);
    await call('saveCurrentEditor');
    assert.equal(context.newSaves, 1);
    assert.equal(context.calls.length, 1);
  });
  await check('Entering and leaving an unchanged editor does not rewrite content', async () => {
    reset();
    await call('toggleNoteEditing');
    await call('toggleNoteEditing');
    assert.equal(context.calls.length, 0);
  });
  await check('Library changes require explicit retry after failure and clear editor IDs only after success', async () => {
    const editor = reset();
    await call('toggleNoteEditing');
    editor.text = '当前资料库的草稿';
    vm.runInContext('state.editorActiveTabs.set(7, 42)', context);
    context.failSave = true;
    assert.equal(await call('prepareLibraryChange'), false);
    assert.equal(read('state.noteEditorSessions.get(42).draft'), '当前资料库的草稿');
    assert.equal(read('state.editorActiveTabs.get(7)'), 42);
    context.failSave = false;
    assert.equal(await call('prepareLibraryChange'), false, 'A failed version requires an explicit save retry');
    assert.equal(context.calls.length, 1);
    assert.equal(await call('saveCurrentEditor'), true);
    assert.equal(await call('prepareLibraryChange'), true);
    assert.equal(editor.dataset.saved, '当前资料库的草稿');
    call('resetEditorForLibraryChange');
    assert.equal(read('state.noteEditorSessions.size'), 0);
    assert.equal(read('state.editorActiveTabs.size'), 0);
    assert.equal(read('state.selectedNoteId'), null);
    assert.equal(context.draftVault.library, null);
    assert.equal(context.askScopeResets, 1);
    for (const endpoint of ['switch-data-dir', 'migrate']) {
      const start = source.indexOf(endpoint === 'migrate' ? '$("#migrate-directory").addEventListener("click"' : '$("#switch-directory").addEventListener("click"');
      const handler = source.slice(start, source.indexOf('\n});', start));
      assert(handler.indexOf('prepareLibraryChange()') < handler.indexOf(`request("/api/settings/${endpoint}"`));
      assert(handler.indexOf('resetEditorForLibraryChange()') > handler.indexOf(`request("/api/settings/${endpoint}"`));
    }
    const restore = source.slice(source.indexOf('restoreBtn.addEventListener("click"'), source.indexOf('dlg.querySelector', source.indexOf('restoreBtn.addEventListener("click"')));
    assert(restore.indexOf('state.noteEditorSessions.delete(Number(noteId))') < restore.indexOf('await refresh()'));
  });
  await check('Reading toolbar stays hidden despite old display:flex; menu actions are not duplicated', () => {
    assert.match(controls, /\.editor-toolbar\[hidden\][\s\S]*?display:\s*none\s*!important/);
    const render = source.slice(source.indexOf('function renderNoteHtml('), source.indexOf('function renderReferenceNoteHtml('));
    assert.equal((render.match(/data-action="delete"/g) || []).length, 1);
    assert.equal((render.match(/data-action="versions"/g) || []).length, 1);
    assert.equal((render.match(/data-action="intelligence"/g) || []).length, 1);
    assert.match(render, /note-mark-actions/);
    assert.match(render, /note-more-menu/);
    assert.match(render, /note-menu-divider/);
    const switcher = source.slice(source.indexOf('async function switchTab('), source.indexOf('async function addTab('));
    const navigationGuard = switcher.indexOf('if (!isReference && !await prepareEditorNavigation()) return false;');
    assert(navigationGuard >= 0, 'Editor tab switches must run the current navigation guard');
    assert(navigationGuard < switcher.indexOf('tabsBar.dataset.activeTab = tabId'));
    assert.match(source, /if \(!tabChip \|\| tabChip\.closest\("\.reference-note"\)\) return/);
    assert.match(source, /e\.key\.toLowerCase\(\) === "s"[\s\S]*?saveCurrentEditor\(\)/);
  });
  const result = { passed: cases.filter(item => item.passed).length, total: cases.length, cases };
  console.log(JSON.stringify(result, null, 2));
  if (process.argv[2]) fs.writeFileSync(process.argv[2], JSON.stringify(result, null, 2) + '\n');
  process.exitCode = result.passed === result.total ? 0 : 1;
}
main().catch(error => { console.error(error); process.exitCode = 1; });
