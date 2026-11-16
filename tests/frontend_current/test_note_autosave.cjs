// Copyright 2026 AshleySama
// SPDX-License-Identifier: Apache-2.0

const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const assert = require('node:assert/strict');
const app = fs.readFileSync(path.join(__dirname, '../../frontend/app.js'), 'utf8');
const ui = fs.readFileSync(path.join(__dirname, '../../frontend/draft-ui.js'), 'utf8');
const take = (s, a, b) => s.slice(s.indexOf(a), s.indexOf(b, s.indexOf(a)));
const functions = take(app, 'function syncCachedNote(', 'async function prepareLibraryChange(') +
  take(ui, 'function draftKey(', 'function capturePersistentNew(') +
  take(app, '// Focus changes never end explicit editing', 'document.addEventListener("focus",');
const settle = () => new Promise(setImmediate);
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; };

function setup() {
  const timers = new Map(), listeners = new Map(), calls = [], drafts = new Map(), closed = [], notices = [];
  const remote = new Map([[1, {id:1, content:'原文', updated_at:'initial'}], [2, {id:2, content:'页签原文', updated_at:'initial'}]]);
  let now = 0, nextTimer = 0, current = null, hook = null, newSaves = 0;
  const c = {
    setTimeout(fn, delay) { const id = ++nextTimer; timers.set(id, {fn, at:now + delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
    document: {addEventListener(name, fn) { if (!listeners.has(name)) listeners.set(name, []); listeners.get(name).push(fn); }},
    $: selector => selector.includes('.note-content[data-note-id]') ? current : null,
    tablerIcon: name => `<svg data-icon="${name}"></svg>`,
    editableContentToMarkdown: node => node.text,
    showToast: (...args) => notices.push(args),
    draftVault: {library:'library-a', close(key) { closed.push(key); drafts.delete(key); return Promise.resolve(true); },
      capture(key, payload) { drafts.set(key, payload); }},
    syncNewNoteDraftFromDom() { c.s.newNoteDraft.content = current.text; },
    saveNewNote() { newSaves++; return Promise.resolve(true); },
    async request(url, options = {}) {
      const id = Number(url.split('/').at(-1)), body = options.body ? JSON.parse(options.body) : null;
      calls.push({id, method:options.method || 'GET', body});
      if (hook) await hook({id, options, body});
      const note = remote.get(id);
      if (!body) return {...note};
      assert.equal(body.autosave, true);
      if (body.expected_content !== note.content) { const error = Error('原笔记已更新'); error.status = 409; throw error; }
      const updated = {...note, content:body.content, updated_at:`saved-${calls.length}`};
      remote.set(id, updated); return {...updated};
    },
  };
  vm.createContext(c);
  vm.runInContext(app.slice(0, app.indexOf('/* ---- 风格定义')) + '\nglobalThis.s=state;\n' + functions, c);
  c.s.notes = [...remote.values()].map(n => ({...n})); c.s.allNotes = c.s.notes;
  for (const note of c.s.notes) c.s.draftNoteSources.set(note.id, note);
  function editor(id = 1) {
    const status = {dataset:{}}, toggle = {dataset:{}, setAttribute(name, value) { this[name] = value; }, focus() {}}, retry = {};
    const note = {classList:{toggle() {}}, querySelectorAll:() => [], querySelector(selector) {
      return selector === '.note-save-status' ? status : selector.includes('retry-note-save') ? retry : selector === '.editor-toolbar' ? {} : toggle;
    }};
    const node = {dataset:id ? {noteId:String(id)} : {draft:'1'}, text:id ? remote.get(id).content : '新建内容', isConnected:true,
      attributes:{contenteditable:'true'}, classList:{toggle() {}}, status, toggle, retry,
      getAttribute(name) { return this.attributes[name]; }, setAttribute(name,value) { this.attributes[name] = value; },
      querySelector:() => null, focus() {}, closest(selector) { return selector === '.note' ? note : !id && selector.includes('[data-note-id]') ? null : this; }};
    current = node;
    if (id) c.noteEditorSession(node).editing = true;
    else c.s.newNoteDraft = {title:'新笔记', content:node.text};
    return node;
  }
  function event(name, node = current, extra = {}) { for (const listener of listeners.get(name) || []) listener({target:node, ...extra}); }
  function input(text, node = current, extra = {}) { node.text = text; event('input', node, extra); }
  async function tick(ms) {
    now += ms;
    for (const [id, timer] of [...timers]) if (timer.at <= now && timers.has(id)) { timers.delete(id); timer.fn(); }
    await settle();
  }
  return {c, calls, remote, drafts, closed, notices, editor, event, input, tick, timers,
    session:id => c.s.noteEditorSessions.get(id || 1), hook:fn => { hook = fn; }, puts:() => calls.filter(x => x.method === 'PUT'),
    get newSaves() { return newSaves; }};
}

const results = [];
async function test(name, run) {
  try { await run(setup()); results.push({name, pass:true}); }
  catch (error) { results.push({name, pass:false, error:error.stack}); }
}
(async () => {
  await test('连续输入重置1.5秒停笔计时，成功落盘清理恢复草稿', async s => {
    const node = s.editor(); s.input('第一段'); await s.tick(1000); s.input('第一段和第二段');
    await s.tick(1499); assert.equal(s.calls.length, 0); assert.equal(s.drafts.get('note:1:content').content, node.text);
    await s.tick(1); assert.equal(s.puts().length, 1); assert.equal(s.remote.get(1).content, node.text);
    assert.equal(s.puts()[0].body.expected_content, '原文'); assert.equal(node.status.textContent, '已保存'); assert.equal(s.drafts.size, 0);
  });
  await test('中文组词期间停笔、失焦和Ctrl+S均不提交，组词结束后保存完整词', async s => {
    const node = s.editor(); s.input('准备'); s.event('compositionstart'); s.input('中', node, {isComposing:true});
    await s.tick(5000); s.event('blur'); assert.equal(await s.c.saveCurrentEditor(), false); assert.equal(s.calls.length, 0);
    node.text = '中文完成'; s.event('compositionend'); s.event('input');
    await s.tick(1499); assert.equal(s.calls.length, 0); await s.tick(1); assert.equal(s.remote.get(1).content, '中文完成');
  });
  await test('自动保存旧响应不改变新输入，新输入等待自己的停笔时间', async s => {
    const node = s.editor(), gate = deferred(); let first = true;
    s.hook(async ({body}) => { if (body && first) { first = false; await gate.promise; } });
    s.input('甲'); await s.tick(1500); s.input('甲乙'); gate.resolve(); await settle();
    assert.equal(node.text, '甲乙'); assert.equal(s.session().draft, '甲乙'); assert.equal(s.session().saved, '甲');
    assert.equal(s.puts().length, 1); assert.equal(node.status.textContent, '等待自动保存'); assert(s.drafts.has('note:1:content'));
    await s.tick(1500); assert.equal(s.puts().length, 2); assert.equal(s.puts()[1].body.expected_content, '甲'); assert.equal(s.remote.get(1).content, '甲乙');
  });
  await test('同页签请求串行，导航补存队列期间的新输入', async s => {
    s.editor(); const gates = [deferred(), deferred()]; let count = 0;
    s.hook(async ({body}) => { if (body) await gates[count++].promise; });
    s.input('第一版'); await s.tick(1500); s.input('第二版');
    const navigation = s.c.prepareEditorNavigation(); assert.equal(s.puts().length, 1);
    gates[0].resolve(); await settle(); assert.equal(s.puts().length, 2); assert.equal(s.puts()[1].body.expected_content, '第一版');
    gates[1].resolve(); assert.equal(await navigation, true); assert.equal(s.remote.get(1).content, '第二版'); assert.equal(s.timers.size, 0);
  });
  await test('快速切换笔记和页签时使用各自正文ID与基线', async s => {
    s.editor(); s.input('笔记修改'); assert.equal(await s.c.prepareEditorNavigation(), true);
    s.editor(2); s.input('页签修改'); assert.equal(await s.c.prepareEditorNavigation(), true); await s.tick(1500);
    assert.deepEqual(s.puts().map(p => [p.id, p.body.expected_content, p.body.content]), [[1,'原文','笔记修改'],[2,'页签原文','页签修改']]);
  });
  await test('新笔记只保留草稿，首次保存继续由显式操作触发', async s => {
    s.editor(null); s.input('新建草稿'); s.event('blur'); await s.tick(5000);
    assert.equal(s.calls.length, 0); assert.equal(s.c.s.newNoteDraft.content, '新建草稿'); await s.c.saveCurrentEditor(); assert.equal(s.newSaves, 1);
  });
  await test('失败保留草稿和错误状态，重试先核对远端再保存', async s => {
    const node = s.editor(); let fail = true; s.hook(({body}) => { if (body && fail) throw Error('disk unavailable'); });
    s.input('失败草稿'); await s.tick(1500); assert.equal(node.status.dataset.state, 'error'); assert.equal(node.retry.hidden, false);
    assert.equal(s.drafts.get('note:1:content').content, '失败草稿'); assert.equal(s.session().baseContent, '原文');
    fail = false; assert.equal(await s.c.saveCurrentEditor(), true); assert.equal(s.calls[1].method, 'GET'); assert.equal(s.remote.get(1).content, '失败草稿'); assert.equal(s.drafts.size, 0);
  });
  await test('已提交但响应丢失后回到旧正文，重试从已提交正文继续而不丢最后输入', async s => {
    s.editor(); let first = true; s.hook(({id, body}) => { if (body && first) { first = false; s.remote.set(id, {...s.remote.get(id), content:body.content}); throw Error('response lost'); } });
    s.input('提交正文'); await s.tick(1500); s.input('原文'); assert(s.drafts.has('note:1:content'));
    assert.equal(await s.c.saveCurrentEditor(), true); assert.equal(s.remote.get(1).content, '原文'); assert.equal(s.puts()[1].body.expected_content, '提交正文');
  });
  await test('旧窗口409不覆盖远端、不声称已保存，后续输入继续保留恢复副本', async s => {
    const node = s.editor(); s.remote.set(1, {...s.remote.get(1), content:'另一窗口的新正文'});
    s.input('我的正文'); await s.tick(1500); assert.equal(s.session().conflict, true); assert.match(node.status.textContent, /原笔记已更新/);
    s.input('我的后续正文'); await s.tick(5000); assert.equal(s.puts().length, 1); assert.equal(s.drafts.get('note:1:content').content, '我的后续正文');
    assert.equal(await s.c.prepareEditorNavigation(), false); assert.equal(s.remote.get(1).content, '另一窗口的新正文'); assert.equal(s.session().baseContent, '原文');
  });
  await test('不确定请求之后远端又被其他窗口修改，重试不会把它认作自己的基线', async s => {
    s.editor(); let first = true; s.hook(({body}) => { if (body && first) { first = false; throw Error('lost'); } });
    s.input('我的正文'); await s.tick(1500); s.remote.set(1, {...s.remote.get(1), content:'别人的正文'});
    assert.equal(await s.c.saveCurrentEditor(), false); assert.equal(s.puts().length, 1); assert.equal(s.session().conflict, true); assert(s.drafts.has('note:1:content'));
  });
  await test('写入期间进入中文组词，不把半成词追加提交', async s => {
    s.editor(); const gate = deferred(); s.hook(async ({body}) => { if (body) await gate.promise; });
    s.input('开始'); await s.tick(1500); s.event('compositionstart'); s.input('开始zhong', undefined, {isComposing:true}); gate.resolve(); await settle();
    assert.equal(s.puts().length, 1); assert.equal(s.session().saved, '开始'); assert.equal(s.session().draft, '开始zhong');
    s.input('开始中文', undefined, {isComposing:true}); s.event('compositionend'); await s.tick(1500); assert.equal(s.remote.get(1).content, '开始中文');
  });
  await test('已替换会话的旧响应不修改新会话、缓存或关闭草稿', async s => {
    s.editor(); const gate = deferred(); s.hook(async ({body}) => { if (body) await gate.promise; });
    s.input('旧会话写入'); await s.tick(1500); const old = s.session(); const replacement = {...old, draft:'新会话正文', saved:'新基线'};
    s.c.s.noteEditorSessions.set(1, replacement); const closedBefore = s.closed.length; gate.resolve(); await settle();
    assert.equal(replacement.saved, '新基线'); assert.equal(replacement.draft, '新会话正文'); assert.equal(s.closed.length, closedBefore); assert.equal(s.c.s.notes[0].content, '原文');
  });
  await test('阅读按钮结束编辑；自动保存后无需额外写入即可隐藏工具栏', async s => {
    const node = s.editor(); s.c.paintEditorState(node); assert.match(node.toggle.innerHTML, /阅读/);
    s.input('已自动保存'); await s.tick(1500); await s.c.toggleNoteEditing(node);
    assert.equal(node.getAttribute('contenteditable'), 'false'); assert.equal(s.puts().length, 1); assert.match(node.toggle.innerHTML, /编辑/);
  });
  console.log(JSON.stringify({checks:results.length, pass:results.every(r => r.pass), cases:results}, null, 2));
  if (results.some(r => !r.pass)) process.exitCode = 1;
})();
