/*
 * Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0
 */

// Runs the real app.js rendering helpers without starting a browser or model.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(process.env.POCKET_MEMORY_APP_JS || path.join(root, 'frontend/app.js'), 'utf8');
const helpers = source.slice(source.indexOf('function escapeHtml('), source.indexOf('function queryString('));
const context = vm.createContext({ state: { allNotes: [] } });
vm.runInContext(helpers, context);
vm.runInContext(source.slice(source.indexOf('function renderReferenceNoteHtml('), source.indexOf('function canManuallyReorderNotes(')), context);
const render = (content, evidence, terms = null) => {
  context.input = { content, evidence, terms };
  return vm.runInContext('renderNoteContent(input.content, input.terms || citationHighlightTerms(input.evidence), { citationEvidence: input.evidence })', context);
};
const markedRows = (html) => [...html.matchAll(/<tr>(.*?)<\/tr>/gs)]
  .filter((match) => match[1].includes('<mark>'))
  .map((match) => match[1].replace(/<[^>]*>/g, ''));
const table = [
  '| 编号 | Agent 名称 | 核心功能 | 输入资料 | 负责人 |',
  '| --- | --- | --- | --- | --- |',
  '| 1 | 会议行动项提取 Agent | 提取行动项 | 会议纪要 | 值班负责人 |',
  '| 9 | 文件更新检查 Agent | 提醒导入文件重建索引 | 文件版本 | 值班负责人 |',
  '| 10 | 术语对照 Agent | 汇总术语 | 术语表 | 值班负责人 |',
].join('\n');
const evidence = '| 9 | 文件更新检查 Agent | 提醒导入文件重建索引 | 文件版本 | 值班负责人 |';
const cases = [];
const check = (name, body) => {
  try { body(); cases.push({ name, passed: true }); }
  catch (error) { cases.push({ name, passed: false, error: error.message }); }
};
check('Markdown citation stays in its own row despite repeated owner', () => {
  const rows = markedRows(render(table, evidence));
  assert.equal(rows.length, 1);
  assert.match(rows[0], /文件更新检查 Agent/);
});
check('Reconstructed fields resolve against headers as one record', () => {
  const rows = markedRows(render(table, 'Agent名称：文件更新检查Agent；核心功能：提醒导入文件重建索引；负责人：值班负责人。'));
  assert.equal(rows.length, 1);
  assert.match(rows[0], /文件更新检查 Agent/);
});
check('Tab-separated tables keep citation cells in one row', () => {
  const tabTable = table.split('\n').filter((line) => !line.includes('---')).map((line) => line.replace(/^\|\s*|\s*\|$/g, '').split('|').map((cell) => cell.trim()).join('\t')).join('\n');
  const rows = markedRows(render(tabTable, evidence));
  assert.equal(rows.length, 1);
  assert.match(rows[0], /文件更新检查 Agent/);
});
check('Two cited records highlight both selected rows, no third row', () => {
  const second = '| 10 | 术语对照 Agent | 汇总术语 | 术语表 | 值班负责人 |';
  const rows = markedRows(render(table, evidence + '\n' + second));
  assert.equal(rows.length, 2);
  assert(rows.every((row) => !row.includes('会议行动项')));
});
check('Shared cells in another table do not become citation evidence', () => {
  const other = '\n\n| 小组 | 负责人 |\n| --- | --- |\n| 后勤组 | 值班负责人 |';
  const rows = markedRows(render(table + other, evidence));
  assert.equal(rows.length, 1);
});
check('Table cell fragments do not leak into unrelated prose', () => {
  const html = render('值班负责人负责轮班。\n\n' + table, evidence);
  assert(!html.includes('<p><mark>值班负责人</mark>'));
});
check('Normal search still highlights shared matches in every matching row', () => {
  assert.equal(markedRows(render(table, '', ['值班负责人'])).length, 3);
});
check('Ordinary prose citation retains exact evidence highlighting', () => {
  assert.match(render('规则说明。\n会议纪要当天归档。', '会议纪要当天归档。'), /<mark>会议纪要当天归档。<\/mark>/);
});
check('Unrelated evidence never selects a row from just its common owner', () => {
  assert.equal(markedRows(render(table, '| 30 | 外部评审 Agent | 另一项功能 | 文档包 | 值班负责人 |')).length, 0);
});
check('Citation rendering continues escaping source HTML', () => {
  const unsafe = '| 项目 | 内容 |\n| --- | --- |\n| 样例 | <img src=x onerror=alert(1)> |';
  const html = render(unsafe, '| 样例 | <img src=x onerror=alert(1)> |');
  assert(!html.includes('<img'));
  assert(html.includes('&lt;img'));
});
check('Reference note rendering passes the evidence scope to its table', () => {
  context.note = { id: 1, content: table, category: '工作' };
  context.evidence = evidence;
  const html = vm.runInContext('renderReferenceNoteHtml(note, citationHighlightTerms(evidence), { citationEvidence: evidence })', context);
  assert.equal(markedRows(html).length, 1);
});

async function checkChildTab() {
  const contentEl = { dataset: {}, innerHTML: '' };
  const noteEl = {
    classList: { contains: (name) => name === 'reference-note' },
    querySelector: (selector) => selector === '.note-tab-content' ? contentEl : null,
  };
  const tabsBar = { dataset: {}, closest: () => noteEl };
  Object.assign(context, {
    flushPendingContentSaves: async () => {},
    request: async (url) => url.endsWith('/tabs') ? [{ id: 1 }, { id: 2 }] : { id: 2, content: table },
    renderTabsBar: () => {},
    scrollToCitation: () => {},
    showToast: (_, error) => { throw new Error(error); },
    tabsBar,
  });
  vm.runInContext(source.slice(source.indexOf('async function switchTab('), source.indexOf('async function addTab(')), context);
  await vm.runInContext('switchTab(1, 2, { tabsBar, highlightTerms: citationHighlightTerms(evidence), citationEvidence: evidence })', context);
  check('Opening a cited child tab keeps row scope through switchTab', () => {
    assert.equal(tabsBar.dataset.activeTab, 2);
    const rows = markedRows(contentEl.innerHTML);
    assert.equal(rows.length, 1);
    assert.match(rows[0], /文件更新检查 Agent/);
  });
}

checkChildTab().then(() => {
  const result = { passed: cases.filter((item) => item.passed).length, total: cases.length, cases };
  console.log(JSON.stringify(result, null, 2));
  if (process.argv[2]) fs.writeFileSync(process.argv[2], JSON.stringify(result, null, 2) + '\n');
  process.exitCode = result.passed === result.total ? 0 : 1;
}).catch((error) => { console.error(error); process.exitCode = 1; });
