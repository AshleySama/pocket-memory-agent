/*
 * Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0
 */

// Focused rendering checks; no browser, service, or model is started.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(process.env.POCKET_MEMORY_APP_JS || path.join(root, 'frontend/app.js'), 'utf8');
const context = vm.createContext({});
vm.runInContext(source.slice(source.indexOf('function escapeHtml('), source.indexOf('function queryString(')), context);
vm.runInContext(source.slice(source.indexOf('function answerSourceLink('), source.indexOf('function renderAnswer(payload')), context);
const call = (name, ...args) => {
  context.args = args;
  return vm.runInContext(`${name}(...args)`, context);
};
const plainText = (html) => html.replace(/<[^>]*>/g, '').replace(/&(amp|lt|gt|quot|#039);/g, (_, entity) => ({
  amp: '&', lt: '<', gt: '>', quot: '"', '#039': "'",
}[entity]));
const paragraphTexts = (html) => [...html.matchAll(/<div class="(?:answer-group-text|answer-prose)">([\s\S]*?)<\/div>/g)]
  .map((match) => plainText(match[1]));
const first = { index: 3, title: '项目计划', url: '/api/notes/41?tab_id=42', evidence: '甲证据。' };
const second = { index: 7, title: '复盘记录', url: '/api/notes/81', evidence: '乙证据。' };
const cases = [];
const check = (name, body) => {
  try { body(); cases.push({ name, passed: true }); }
  catch (error) { cases.push({ name, passed: false, error: error.message }); }
};

check('Multi-source items keep original numbering, order, text, and citation targets', () => {
  const answer = '风险说明：\n\n4. 甲风险[3]。\n5. 乙风险[3]。\n6. 丙风险[7]。\n\n支持资料编号：[7][3]';
  const html = call('renderAnswerText', answer, [second, first]);
  assert.equal((html.match(/class="answer-source-group"/g) || []).length, 2);
  assert.equal(paragraphTexts(html).join('\n'), answer);
  assert.match(html, /class="answer-source-label"[^>]*data-doc-url="\/api\/notes\/41\?tab_id=42"[^>]*>\[3\] 项目计划<\/a>/);
  assert.match(html, /class="answer-source-label"[^>]*data-doc-url="\/api\/notes\/81"[^>]*>\[7\] 复盘记录<\/a>/);
  assert(html.indexOf('[3] 项目计划') < html.indexOf('[7] 复盘记录'));
});
check('A paragraph with two citations shows both without assigning claims to either', () => {
  const answer = '两项资料共同提供背景[7][3]。';
  const html = call('renderAnswerText', answer, [first, second]);
  assert.equal((html.match(/class="answer-source-group"/g) || []).length, 1);
  assert.equal(paragraphTexts(html).join('\n'), answer);
  assert(html.indexOf('[7] 复盘记录') < html.indexOf('[3] 项目计划'));
});
check('Uncited lines and unknown references do not inherit neighboring sources', () => {
  const answer = '甲[3]。\n待核实说明[99]。\n乙[7]。';
  const html = call('renderAnswerText', answer, [first, second]);
  assert.match(html, /<div class="answer-prose">待核实说明\[99\]。<\/div>/);
  assert.equal(paragraphTexts(html).join('\n'), answer);
});
check('Single-source and unsupported answers retain plain wording', () => {
  assert.equal(plainText(call('renderAnswerText', '原答\n第二行[3]。', [first])), '原答\n第二行[3]。');
  assert.equal(call('renderAnswerText', '资料不足，无法回答。', []), '资料不足，无法回答。');
});
check('Original-source members restore grouped evidence counts without counting table spans', () => {
  const base = { evidence: '表头\n记录', evidence_count: 1, evidence_locations: ['原文证据组 · 3处已选片段'] };
  assert.equal(call('answerSourceEvidenceCount', { ...base, citation_members: [{ id: 'a' }, { id: 'b' }, { id: 'c' }, { id: 'a' }] }), 3);
  assert.equal(call('answerSourceEvidenceCount', { ...base, citation_members: [{ id: 'a', spans: [{ quote: '表头' }, { quote: '记录' }] }], raw_evidence: [{}, {}] }), 1);
  assert.equal(call('answerSourceEvidenceCount', { evidence: '甲\n乙', evidence_locations: ['第 1 页', '第 2 页', '第 2 页'] }), 2);
});
check('Technical source metadata is hidden while friendly count and tab labels remain', () => {
  const html = call('renderAnswerSource', {
    ...first, reason: '本地原文证据', rank_score: 3.21, location: '保存正文 · 0–130',
    evidence_locations: ['保存正文 · 0–130'], tab_name: '实施计划',
    citation_members: [{ id: 'a' }, { id: 'b' }], excerpt: first.evidence,
  });
  assert(!html.includes('本地原文证据'));
  assert(!html.includes('保存正文'));
  assert(!html.includes('0–130'));
  assert(!html.includes('综合'));
  assert.match(html, /含 2 处证据/);
  assert.match(html, /页签：实施计划/);
});
check('PDF page and spreadsheet row locations survive character-offset cleanup', () => {
  const html = call('answerSourceMeta', {
    location: '第 4 页 · 字符 105—210；工作表 预算 · 第 3–8 行',
    evidence_locations: ['第 4 页 · 字符 105—210', '工作表 预算 · 第 3–8 行'],
  });
  assert.match(html, /第 4 页/);
  assert.match(html, /工作表 预算 · 第 3–8 行/);
  assert(!html.includes('105—210'));
  assert.equal((html.match(/第 4 页/g) || []).length, 1);
  assert.match(call('answerSourceMeta', { location: '工作表 字符统计 · 第 2 行' }), /工作表 字符统计 · 第 2 行/);
});
check('Member tab labels are distinct and a title alone never invents a tab relation', () => {
  const html = call('answerSourceMeta', { title: '页签2', citation_members: [{ tab_name: '计划' }, { tab_name: '计划' }, { tab_name: '复盘' }] });
  assert.equal((html.match(/页签：计划/g) || []).length, 1);
  assert.match(html, /页签：复盘/);
  assert(!html.includes('页签：页签2'));
});
check('Duplicate or contained context is absent from the rendered card', () => {
  for (const excerpt of ['甲证据。\n  乙证据。', '乙证据。']) {
    const html = call('renderAnswerSource', { ...first, evidence: '甲证据。 乙证据。', excerpt });
    assert(!html.includes('source-context'));
    assert(!html.includes('同一切片上下文'));
  }
});
check('Additional context remains complete and collapsed by default', () => {
  const html = call('renderAnswerSource', { ...first, excerpt: '前文。甲证据。后文。' });
  assert.match(html, /<details class="source-context"><summary>查看完整上下文<\/summary>/);
  assert(!html.includes('<details class="source-context" open'));
  assert.match(plainText(html), /前文。甲证据。后文。/);
});
check('Rendering escapes untrusted text and preserves source metadata without mutation', () => {
  const record = { ...first, title: '<img src=x onerror=alert(1)>', tab_name: '<script>bad()</script>',
    evidence: '<b>原文</b>', excerpt: '<b>原文</b>后文', location: '保存正文 · 0–130',
    raw_evidence: [{ char_start: 0, char_end_exclusive: 130 }] };
  const before = JSON.stringify(record);
  const card = call('renderAnswerSource', record);
  const answer = call('renderAnswerText', '<img src=x>[3]\n第二项[7]', [record, second]);
  assert(!/<(?:img|script|b)>/.test(answer));
  assert(!/<(?:img|script)[\s>]/.test(card));
  assert(card.includes('&lt;b&gt;原文&lt;/b&gt;'));
  assert(answer.includes('&lt;img src=x&gt;'));
  assert.equal(JSON.stringify(record), before);
});
check('Background remains complete and collapsed with its exact source link', () => {
  const quote = '本周完成：<img src=x onerror=alert(1)>\n**原文标记**与完整背景。';
  const record = { ...first, source_type: 'note', display_evidence: quote,
    display_spans: [{ source_id: 42, parent_note_id: 41, tab_name: '复盘', field: 'content',
      char_start: 10, char_end_exclusive: 80, source_revision: 'saved-revision', quote }] };
  const passage = { kind: 'background', source_index: 3, quote };
  const before = JSON.stringify({ record, passage });
  const html = call('renderAnswerContextPassages', [passage], [record]);
  assert.match(html, /<details class="source-context answer-context-passages"><summary>原文背景与限制<\/summary>/);
  assert(!html.includes(' open'));
  assert(!html.includes('<img'));
  assert(!html.includes('<strong>原文标记</strong>'));
  assert(plainText(html).includes(quote));
  assert.match(html, /data-doc-url="\/api\/notes\/41\?tab_id=42"/);
  assert.match(html, /data-doc-exact="true"/);
  assert.match(html, /data-doc-note-ranges=/);
  assert.match(html, /saved-revision/);
  assert.equal(JSON.stringify({ record, passage }), before);
});
check('Missing background or source identity never invents a reference', () => {
  for (const passages of [null, {}, [], [null], [{ kind: 'background', quote: '' }]]) {
    assert.equal(call('renderAnswerContextPassages', passages, [first]), '');
  }
  const html = call('renderAnswerContextPassages', [{ kind: 'background', source_index: 99, quote: '保留背景。' }], [first]);
  assert.match(html, /保留背景。/);
  assert.match(html, /来源未定位/);
  assert(!html.includes('data-doc-url'));
});

const exactSource = (index, text, start, identity = {}) => {
  const { id = 27, parent = null, tab = '复盘', revision = 'a'.repeat(64) } = identity;
  const span = { source_id: id, parent_note_id: parent, tab_name: tab, field: 'content',
    char_start: start, char_end_exclusive: start + Array.from(text).length, quote: text, source_revision: revision };
  return { index, title: '相同标题', source_type: 'note', source_id: id, parent_note_id: parent, tab_name: tab,
    source_key: `note:${id}`, source_revision: revision, source_field: 'content',
    url: parent === null ? `/api/notes/${id}` : `/api/notes/${parent}?tab_id=${id}`,
    evidence: text, display_evidence: text, display_context: `完整上下文：${text}`,
    display_spans: [span], raw_evidence: [span],
    citation_members: [{ id: `original:${id}:${start}`, source_type: 'note', source_id: id,
      parent_note_id: parent, tab_name: tab, verification: 'exact_saved_original', spans: [span] }] };
};
const groupedRecords = [
  '风险一：资料过杂。处理动作：按主题整理；负责人：演示人；完成时间：录制前一天。',
  '风险二：确认延迟。仅在验收通过后发布；负责人：演示人；完成时间：周五。',
  '风险三：交接遗漏。处理动作：复核清单；负责人：演示人；完成时间：演示之前。',
];
const groupedSources = groupedRecords.map((text, index) => exactSource(index + 1, text, index * 100));
const groupedAnswer = groupedRecords.map((text, index) => `${text} [${index + 1}]`).join('\n\n');
check('Same exact note becomes one list, one source, and one reference identity without shortening facts', () => {
  const before = JSON.stringify({ answer: groupedAnswer, sources: groupedSources });
  const view = call('deriveAnswerPresentation', groupedAnswer, groupedSources, []);
  assert.equal(view.grouped, true);
  assert.equal(view.sources.length, 1);
  assert.equal(view.sources[0].index, 1);
  assert.equal(view.sources[0].display_spans.length, 3);
  assert.equal(view.sources[0].display_evidence_count, 3);
  assert.match(call('renderAnswerSource', view.sources[0]), /含 3 处证据/);
  const html = call('renderAnswerText', view.answer, view.sources, { sharedSource: view.grouped });
  assert.equal((html.match(/<ul class="answer-record-list">/g) || []).length, 1);
  assert.equal((html.match(/<li>/g) || []).length, 3);
  assert.equal((html.match(/class="answer-source-label"/g) || []).length, 1);
  assert(!html.includes('class="answer-source-group"'));
  assert(!html.includes('class="answer-citation"'));
  for (const text of groupedRecords) {
    assert(plainText(html).includes(text));
    assert(view.sources[0].display_context.includes(`完整上下文：${text}`));
  }
  assert.equal(JSON.stringify({ answer: groupedAnswer, sources: groupedSources }), before);
});
check('Duplicate verified ranges count once and conflicting overlap disables grouping', () => {
  const source = exactSource(1, '共同条件：验收通过。', 0);
  const duplicate = { ...source, index: 2, display_spans: [{ extra: 'unchanged proof', ...source.display_spans[0] }] };
  const view = call('deriveAnswerPresentation', '甲[1]。\n\n乙[2]。', [source, duplicate], []);
  assert.equal(view.sources.length, 1);
  assert.equal(view.sources[0].display_evidence_count, 1);
  const overlap = exactSource(2, '条件：验收通过。', 2);
  assert.equal(call('deriveAnswerPresentation', '甲[1]。\n\n乙[2]。', [source, overlap], []).grouped, false);
});
check('Titles never merge different notes, tabs, revisions, fields, or unverifiable sources', () => {
  const base = exactSource(1, '第一处事实。', 0);
  const second = exactSource(2, '第二处事实。', 100);
  const variants = [
    exactSource(2, '第二处事实。', 100, { id: 28 }),
    exactSource(2, '第二处事实。', 100, { id: 28, parent: 27, tab: '另一页' }),
    exactSource(2, '第二处事实。', 100, { tab: '另一页' }),
    exactSource(2, '第二处事实。', 100, { revision: 'b'.repeat(64) }),
    { ...second, source_type: 'document' },
    { ...second, source_field: 'ocr_text' },
    { ...second, url: '/api/notes/99' },
    { ...second, display_spans: [] },
    { ...second, citation_members: [] },
    { ...second, citation_members: [{ ...second.citation_members[0], verification: 'unverified' }] },
  ];
  for (const other of variants) {
    const view = call('deriveAnswerPresentation', '事实甲[1]。\n\n事实乙[2]。', [base, other], []);
    assert.equal(view.grouped, false);
    assert.equal(view.sources.length, 2);
    assert.equal(view.answer, '事实甲[1]。\n\n事实乙[2]。');
  }
});
check('Background and other document types keep separate consistent view references', () => {
  const background = exactSource(4, '本周完成资料整理。', 400);
  const document = { index: 9, source_type: 'document', source_id: 5, title: '相同标题', url: '/api/documents/5/file', evidence: '外部事实。' };
  const passages = [{ kind: 'background', source_index: 4, quote: background.evidence }];
  const view = call('deriveAnswerPresentation', groupedAnswer + '\n\n外部事实[9]。', [...groupedSources, background, document], passages);
  assert.equal(view.sources.length, 2);
  assert.equal(view.sources[0].display_evidence_count, 3);
  assert.equal(view.sources[1].source_type, 'document');
  assert.equal(view.sources[1].index, 2);
  assert.match(view.answer, /外部事实\[2\]/);
  assert.equal(view.contextPassages[0].source_index, 3);
  assert.equal(view.contextSources.find(source => source.index === 3).evidence, background.evidence);
  assert(!view.sources[0].display_evidence.includes(background.evidence));
  assert.equal(passages[0].source_index, 4);
});
check('Unknown references and an unrelated list are not reassigned invented support', () => {
  const shifted = groupedSources.map((source, index) => ({ ...source, index: index + 3 }));
  const view = call('deriveAnswerPresentation', '未知[1]。\n\n甲[3]。\n\n乙[4]。\n\n丙[5]。', shifted, []);
  assert.equal(view.sources[0].index, 2);
  assert.match(view.answer, /未知\[1\]/);
  assert.match(view.answer, /甲\[2\]/);
  const plain = '4. 甲风险[3]。\n5. 乙风险[7]。';
  const unchanged = call('deriveAnswerPresentation', plain, [first, second], []);
  assert.equal(unchanged.grouped, false);
  assert.equal(unchanged.answer, plain);
});

const result = { passed: cases.filter((item) => item.passed).length, total: cases.length, cases };
console.log(JSON.stringify(result, null, 2));
if (process.argv[2]) fs.writeFileSync(process.argv[2], JSON.stringify(result, null, 2) + '\n');
process.exitCode = result.passed === result.total ? 0 : 1;
