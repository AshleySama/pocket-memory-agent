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
  assert.match(html, /<details class="source-context"><summary>查看上下文<\/summary>/);
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

const result = { passed: cases.filter((item) => item.passed).length, total: cases.length, cases };
console.log(JSON.stringify(result, null, 2));
if (process.argv[2]) fs.writeFileSync(process.argv[2], JSON.stringify(result, null, 2) + '\n');
process.exitCode = result.passed === result.total ? 0 : 1;
