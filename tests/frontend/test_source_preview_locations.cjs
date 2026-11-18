// Copyright 2026 AshleySama
// SPDX-License-Identifier: Apache-2.0

// Exercise the real preview-link, opening, validation, and tab-rendering paths.
// DOM/network substitutes keep this focused check free of services and models.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const app = fs.readFileSync(path.resolve(__dirname, '../../frontend/app.js'), 'utf8');
const state = { allNotes: [], notes: [], activeAsk: null, askHistory: [], askScope: { mode: 'all', note_ids: [], document_ids: [] } };
const context = vm.createContext({ state, crypto: crypto.webcrypto, TextEncoder, URLSearchParams });
vm.runInContext(fs.readFileSync(path.resolve(__dirname, '../../frontend/ask-scope.js'), 'utf8'), context);
for (const [start, end] of [
  ['function escapeHtml(', 'function queryString('],
  ['function answerSourceLink(', 'function renderAnswer(payload'],
  ['function renderAnswer(payload', 'const INTENT_LABELS ='],
  ['function renderReferenceNoteHtml(', 'function canManuallyReorderNotes('],
  ['function renderSourcePreview(', 'function watchAskRevision('],
  ['async function validatedNoteCitationSpans(', 'function renderDocumentCitationText('],
  ['async function switchTab(', 'async function addTab('],
]) vm.runInContext(app.slice(app.indexOf(start), app.indexOf(end)), context);

const call = (name, ...args) => {
  context.args = args;
  return vm.runInContext(`${name}(...args)`, context);
};
const unescape = value => value.replace(/&(amp|lt|gt|quot|#039);/g, (_, entity) => ({
  amp: '&', lt: '<', gt: '>', quot: '"', '#039': "'",
}[entity]));
const linkFromHtml = (html) => {
  const anchor = html.match(/<a\b[^>]*>/)[0];
  const dataset = Object.fromEntries([...anchor.matchAll(/data-([\w-]+)="([^"]*)"/g)]
    .map(([, key, value]) => [key.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()), unescape(value)]));
  return { dataset, isConnected: true };
};
const previewLink = (item) => {
  const html = call('renderSourcePreview', { revision: 'library-r1', items: [item] });
  assert.match(html, /尚未整理为回答/);
  assert.match(html, /以最终回答及引用为准/);
  return linkFromHtml(html);
};
const body = '😀复盘。\n风险一：手续未完成；负责人：演示人。\n风险二：确认延迟；负责人：演示人。\n风险三：交接遗漏；负责人：演示人。';
const quote = '风险二：确认延迟；负责人：演示人。';
const note = { id: 8, parent_note_id: 2, tab_name: '复盘', content: body };
const spanFor = (record, text, index = record.content.indexOf(text)) => {
  const start = Array.from(record.content.slice(0, index)).length;
  return { source_id: record.id, parent_note_id: record.parent_note_id ?? null,
    tab_name: record.tab_name || '', field: 'content',
    char_start: start, char_end_exclusive: start + Array.from(text).length,
    quote: text, source_revision: crypto.createHash('sha256').update(record.content).digest('hex') };
};
const span = spanFor(note, quote);
const item = { id: 'note:8:content:fragment', source_type: 'note', source_id: 8,
  parent_note_id: 2, title: '项目复盘', tab_name: '复盘', url: '/api/notes/2?tab_id=8',
  location: '正文', quotes: [quote], display_spans: [span], display_evidence: quote };
const marks = html => (html.match(/<mark>/g) || []).length;
const nodes = new Map();
const node = (selector) => {
  if (!nodes.has(selector)) nodes.set(selector, { innerHTML: '', textContent: '', dataset: {},
    classList: { add() {}, remove() {} }, addEventListener() {} });
  return nodes.get(selector);
};
let notes, requests, stopped, tabsBar, contentEl, afterRead;
const reset = () => {
  nodes.clear(); requests = []; stopped = []; afterRead = null;
  notes = new Map([[2, { ...note, id: 2, parent_note_id: null, tab_name: '首页' }],
    [8, { ...note }], [9, { ...note, id: 9, tab_name: '另一页' }]]);
  contentEl = { dataset: {}, innerHTML: '' };
  const noteEl = { classList: { contains: name => name === 'reference-note' },
    querySelector: selector => selector === '.note-tab-content' ? contentEl : null };
  tabsBar = { dataset: { noteId: '2', activeTab: '2' }, isConnected: true, closest: () => noteEl };
  state.activeAsk = { previewRevision: 'library-r1', completed: false, controller: new AbortController() };
  Object.assign(context, {
    $: selector => selector === '#doc-preview-body .note-tabs-bar' ? tabsBar : node(selector),
    document: { querySelector: () => tabsBar }, window: { scrollTo() {} },
    request: async (url) => {
      requests.push(url);
      const result = url.endsWith('/tabs') ? [...notes.values()] : notes.get(Number(url.match(/\d+$/)[0]));
      if (afterRead) await afterRead(url);
      return result;
    },
    fetch: async () => ({ ok: true, json: async () => ({ revision: 'library-r1' }) }),
    askIsCurrent: session => session === state.activeAsk && !session.controller.signal.aborted,
    stopAskWaiting: message => { stopped.push(message); state.activeAsk = null; },
    loadTabsBar: async () => {}, renderTabsBar: () => {}, scrollToCitation: () => {},
    flushPendingContentSaves: async () => {},
    showToast: (_, error) => { throw new Error(error); },
    searchHighlightTerms: () => ['负责人', '演示人'],
    INTENT_LABELS: { qa: '智能问答' }, renderIntentData: () => '',
  });
};
const cases = [];
const check = async (name, test) => {
  reset();
  try { await test(); cases.push({ name, passed: true }); }
  catch (error) { cases.push({ name, passed: false, error: error.stack }); }
};

(async () => {
  await check('Preview serializes original identity/ranges and highlights only risk two', async () => {
    const link = previewLink(item);
    const options = call('documentLinkOptions', link);
    assert.equal(options.exactEvidence, true);
    assert.deepEqual(JSON.parse(JSON.stringify(options.noteSpans)), [span]);
    await call('openSourcePreviewLink', link);
    assert.equal(marks(node('#doc-preview-body').innerHTML), 0, 'parent with equal text stays unmarked');
    assert.equal(marks(contentEl.innerHTML), 1);
    assert(contentEl.innerHTML.includes(`<mark>${quote}</mark>`));
    assert(contentEl.innerHTML.includes('风险一：手续未完成；负责人：演示人。'));
    assert(contentEl.innerHTML.includes('风险三：交接遗漏；负责人：演示人。'));
    assert(!contentEl.innerHTML.includes('未定位'));
  });
  await check('Repeated identical quote uses its supplied occurrence, including Unicode offsets', async () => {
    const repeated = '负责人：演示人。';
    const exact = spanFor(note, repeated, body.lastIndexOf(repeated));
    await call('openSourcePreviewLink', previewLink({ ...item, quotes: [repeated], display_spans: [exact] }));
    assert.equal(marks(contentEl.innerHTML), 1);
    assert(contentEl.innerHTML.includes(`风险三：交接遗漏；<mark>${repeated}</mark>`));
  });
  await check('Legacy preview without ranges never falls back to repeated owner or search text', async () => {
    const legacy = { ...item, quotes: ['负责人：演示人。'] };
    delete legacy.display_spans;
    await call('openSourcePreviewLink', previewLink(legacy));
    assert.equal(marks(contentEl.innerHTML), 0);
    assert.match(contentEl.innerHTML, /未定位/);
  });
  await check('Stale content opens unmarked with a clear location status', async () => {
    notes.set(8, { ...note, content: body + '\n新增说明。' });
    await call('openSourcePreviewLink', previewLink(item));
    assert.equal(marks(contentEl.innerHTML), 0);
    assert.match(contentEl.innerHTML, /未定位/);
    assert.match(contentEl.innerHTML, /新增说明/);
  });
  await check('Wrong identity, tab, revision, quote, and malformed ranges stay unmarked', async () => {
    for (const bad of [null, { ...span, source_id: 9 }, { ...span, tab_name: '另一页' },
      { ...span, parent_note_id: 3 }, { ...span, source_revision: 'old' }, { ...span, quote: '错误' }]) {
      await call('openSourcePreviewLink', previewLink({ ...item, display_spans: [bad] }));
      assert.equal(marks(contentEl.innerHTML), 0);
      assert.match(contentEl.innerHTML, /未定位/);
    }
  });
  await check('Manual tab switches retain preview identity, including returning to the original tab', async () => {
    await call('openSourcePreviewLink', previewLink(item));
    await call('switchTab', 2, 9, { tabsBar });
    assert.equal(marks(contentEl.innerHTML), 0);
    assert.match(contentEl.innerHTML, /未定位/);
    await call('switchTab', 2, 8, { tabsBar });
    assert.equal(marks(contentEl.innerHTML), 1);
    assert(!contentEl.innerHTML.includes('未定位'));
  });
  await check('Table/OCR preview never uses text matching when exact rendering is unsupported', async () => {
    const table = '| 项目 | 负责人 |\n| --- | --- |\n| 风险二 | 演示人 |\n| 风险二 | 演示人 |';
    const tableNote = { ...note, content: table };
    const row = '| 风险二 | 演示人 |';
    notes.set(8, tableNote);
    await call('openSourcePreviewLink', previewLink({ ...item, quotes: [row], display_spans: [spanFor(tableNote, row)] }));
    assert.equal(marks(contentEl.innerHTML), 0);
    assert.match(contentEl.innerHTML, /未定位/);
    notes.set(8, note);
    await call('openSourcePreviewLink', previewLink({ ...item, display_spans: [{ ...span, field: 'ocr_text' }] }));
    assert.equal(marks(contentEl.innerHTML), 0);
    assert.match(contentEl.innerHTML, /未定位/);
  });
  await check('Library revision mismatch stops the old preview before source loading', async () => {
    context.fetch = async () => ({ ok: true, json: async () => ({ revision: 'library-r2' }) });
    await call('openSourcePreviewLink', previewLink(item));
    assert.equal(requests.length, 0);
    assert.equal(stopped.length, 1);
    assert.match(stopped[0], /资料在回答期间发生了更新/);
  });
  await check('Cancellation during source load cannot open a superseded preview', async () => {
    afterRead = async () => { state.activeAsk.controller.abort(); };
    await call('openSourcePreviewLink', previewLink(item));
    assert.equal(node('#doc-preview-body').innerHTML, '');
    assert.equal(contentEl.innerHTML, '');
  });
  await check('Final completion during child load cannot render old preview marks', async () => {
    afterRead = async url => { if (url === '/api/notes/8') state.activeAsk.completed = true; };
    await call('openSourcePreviewLink', previewLink(item));
    assert.equal(contentEl.innerHTML, '');
    assert.equal(marks(node('#doc-preview-body').innerHTML), 0);
  });
  await check('Visible total stays truthful while queue details remain diagnostics only', async () => {
    call('renderAnswer', { answer: '测试。', sources: [], timing: { total_seconds: 3.25, queue_seconds: 2 } }, '问题');
    assert.match(node('#answer-panel').innerHTML, /3\.25s/);
    assert(!node('#answer-panel').innerHTML.includes('含排队'));
    call('renderAnswer', { answer: '测试。', sources: [], timing: { total_seconds: 1, queue_seconds: 0.001 } }, '问题');
    assert(!node('#answer-panel').innerHTML.includes('含排队'));
  });
  await check('Background-only sources stay collapsed and are not counted as answer evidence', async () => {
    const mainSource = { index: 1, title: '风险', source_type: 'note', url: '/api/notes/8', evidence: quote };
    const backgroundSource = { index: 4, title: '进展背景', source_type: 'note', url: '/api/notes/8',
      display_evidence: '本周完成联调。', display_spans: [span] };
    const payload = { answer: '风险说明[1]。', sources: [mainSource, backgroundSource],
      context_passages: [{ kind: 'background', source_index: 4, quote: '本周完成联调。' }] };
    const before = JSON.stringify(payload);
    call('renderAnswer', payload, '有哪些风险');
    const html = node('#answer-panel').innerHTML;
    assert.match(html, /<summary>原文背景与限制<\/summary>/);
    assert.match(html, /本周完成联调。/);
    assert.equal(node('#result-count').textContent, '参考 1 条');
    assert.equal((html.match(/class="answer-source"/g) || []).length, 1);
    assert(!html.match(/<div class="answer-body">([\s\S]*?)<\/div>/)[1].includes('本周完成'));
    assert.equal(JSON.stringify(payload), before);
    call('renderAnswer', { ...payload, answer: '风险说明[1]。\n引用相关进展[4]。' }, '有哪些风险');
    assert.equal(node('#result-count').textContent, '参考 2 条');
    assert.equal((node('#answer-panel').innerHTML.match(/class="answer-source"/g) || []).length, 2);
  });
  await check('Grouped answer opens all three exact original passages from its single source link', async () => {
    const records = body.split('\n').slice(1);
    const sources = records.map((text, offset) => {
      const exact = spanFor(note, text);
      return { index: offset + 1, source_type: 'note', source_id: note.id,
        parent_note_id: note.parent_note_id, tab_name: note.tab_name, title: '项目复盘',
        url: '/api/notes/2?tab_id=8', display_evidence: text, display_context: text,
        display_spans: [exact], source_revision: exact.source_revision,
        citation_members: [{ source_type: 'note', source_id: note.id, parent_note_id: note.parent_note_id,
          tab_name: note.tab_name, verification: 'exact_saved_original', spans: [exact] }] };
    });
    const payload = { answer: records.map((text, offset) => `${text} [${offset + 1}]`).join('\n\n'), sources,
      timing: { total_seconds: 3.25, queue_seconds: 2 } };
    const before = JSON.stringify(payload);
    call('renderAnswer', payload, '有哪些风险');
    const html = node('#answer-panel').innerHTML;
    assert.equal((html.match(/class="answer-source"/g) || []).length, 1);
    assert.equal((html.match(/<ul class="answer-record-list">/g) || []).length, 1);
    assert.equal((html.match(/<li>/g) || []).length, 3);
    assert.match(html, /含 3 处证据/);
    assert.equal(node('#result-count').textContent, '参考 1 条');
    assert(!html.includes('含排队'));
    const link = linkFromHtml(html);
    const options = call('documentLinkOptions', link);
    assert.equal(options.noteSpans.length, 3);
    await call('openDoc', link.dataset.docUrl, link.dataset.docTitle, link.dataset.docEvidence, options);
    assert.equal(marks(contentEl.innerHTML), 3);
    for (const text of records) assert(contentEl.innerHTML.includes(`<mark>${text}</mark>`));
    assert.equal(JSON.stringify(payload), before);
  });
  const result = { passed: cases.filter(item => item.passed).length, total: cases.length, cases };
  console.log(JSON.stringify(result, null, 2));
  if (process.argv[2]) fs.writeFileSync(process.argv[2], JSON.stringify(result, null, 2) + '\n');
  process.exitCode = result.passed === result.total ? 0 : 1;
})().catch(error => { console.error(error); process.exitCode = 1; });
