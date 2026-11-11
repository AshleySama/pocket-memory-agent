/* Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0
 */

/* Local office tools. Each request and draft belongs to one library. */
(() => {
  'use strict';
  const root = document.getElementById('office-tools');
  if (!root) return;
  const $ = selector => root.querySelector(selector);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const clone = value => JSON.parse(JSON.stringify(value));
  const uid = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const names = {todos:'待办提取', rewrite:'文本加工'};
  const defaults = () => ({text:'', source:null, mode:'polish', result:null, resultSource:null, resultInput:'', requestId:uid(), saved:false, noteSaved:false});
  let library = '', limits = {max_chars:1000};
  let kind = 'todos', work = {todos:defaults(),rewrite:defaults()};
  let savedTodos = [], todoEdits = {}, revision = 0, busy = false, started = 0, ticker = null, selected = null, checking = false;
  const current = () => work[kind];
  const host = () => window.PocketOfficeHost;
  const maxChars = () => Number(limits.max_chars) || 1000;
  const status = (message, tone='') => { $('#office-status').textContent = message; $('#office-status').dataset.tone = tone; };
  async function api(path, body) {
    const response = await fetch(path, {method:body === undefined ? 'GET':'POST', headers:{'Content-Type':'application/json', ...(library ? {'X-PocketMemory-Library':library} : {})}, ...(body === undefined ? {} : {body:JSON.stringify(body)})});
    const data = await response.json();
    if (!response.ok) { const error = new Error(data.error || '操作失败，请重试'); error.status = response.status; throw error; }
    return data;
  }
  function persist() {
    if (!library) return;
    try {
      const key = `pocket-office-v1:${library}`;
      let retained = {};
      try { retained = JSON.parse(localStorage.getItem(key) || '{}').work || {}; } catch (_) {}
      // Keep older tool drafts in storage without reopening them in this UI.
      const value = JSON.stringify({version:1,kind,work:{...retained,...work},todoEdits});
      if (value.length > 100000) throw new Error('草稿过大');
      localStorage.setItem(key, value);
      $('#office-draft-warning').hidden = true;
    } catch (_) {
      $('#office-draft-warning').hidden = false;
      $('#office-draft-warning').textContent = '浏览器未能保留工具草稿，请先复制结果再关闭页面。';
    }
  }
  function restore() {
    work = {todos:defaults(),rewrite:defaults()}; kind = 'todos'; todoEdits = {};
    try {
      const raw = localStorage.getItem(`pocket-office-v1:${library}`);
      if (!raw || raw.length > 100000) return;
      const data = JSON.parse(raw);
      if (data.version !== 1) return;
      if (data.todoEdits && typeof data.todoEdits === 'object' && !Array.isArray(data.todoEdits)) todoEdits = data.todoEdits;
      if (names[data.kind]) kind = data.kind;
      for (const name of Object.keys(names)) {
        const saved = data.work?.[name];
        if (!saved || typeof saved.text !== 'string' || saved.text.length > 12000) continue;
        work[name] = {...defaults(), ...saved};
        if (!['polish','concise','polite','correct'].includes(work[name].mode)) work[name].mode = 'polish';
        const result = work[name].result;
        if (result && ((name === 'todos' && !Array.isArray(result.items))
            || (name === 'rewrite' && typeof result.text !== 'string'))) work[name].result = null;
      }
    } catch (_) { /* Invalid old drafts never prevent the tools from opening. */ }
  }
  function stopWaiting(message='已停止等待，输入已保留。后台任务可能仍在完成，稍后可重新处理。') {
    revision += 1; busy = false; clearInterval(ticker); ticker = null;
    renderControls(); status(message);
  }
  async function refreshLibrary({quiet=false}={}) {
    if (checking) return;
    checking = true;
    try {
      const data = await api('/api/office-tools');
      if (data.library_id !== library) {
        const changed = Boolean(library);
        persist(); stopWaiting(''); selected = null;
        library = data.library_id; limits = {...limits,...data.limits}; restore();
        savedTodos = data.todos || []; renderAll();
        if (changed) status('已切换资料库，显示本库的工具草稿和待办。');
      } else { limits = {...limits,...data.limits}; savedTodos = data.todos || []; renderSaved(); renderControls(); }
    } catch (error) { if (!quiet || !library) status(`工具暂时无法连接：${error.message}`, 'error'); }
    finally { checking = false; }
  }
  function renderControls() {
    for (const button of root.querySelectorAll('[data-office-kind]')) {
      button.setAttribute('aria-pressed', String(button.dataset.officeKind === kind)); button.disabled = busy;
    }
    $('#office-rewrite-options').hidden = kind !== 'rewrite';
    $('#office-text').disabled = busy; $('#office-mode').disabled = busy;
    for (const id of ['office-current','office-selection','office-clear']) $(`#${id}`).disabled = busy || !library;
    $('#office-selection').disabled = busy || !selected || selected.library !== library;
    const count = Array.from(current().text).length;
    $('#office-count').textContent = `${count} / ${maxChars()} 字`;
    $('#office-count').classList.toggle('is-over', count > maxChars());
    $('#office-run').disabled = busy || !library || !current().text.trim() || count > maxChars();
    $('#office-run').textContent = busy ? '正在处理…' : `开始${names[kind]}`;
    $('#office-stop').hidden = !busy;
    for (const element of $('#office-result').querySelectorAll('input,textarea,button')) {
      element.disabled = busy || (element.hasAttribute('data-office-save-todos') && current().saved)
        || (element.hasAttribute('data-office-save-note') && current().noteSaved);
    }
    $('#office-source').textContent = current().source?.title ? `来源：${current().source.title}` : '可粘贴文字，或使用当前笔记。';
    $('#office-hint').textContent = kind === 'todos' ? '提取明确交代的行动；负责人和期限没有写明时留空。' : '调整表达后请核对原意、姓名、数字和日期。结果不会覆盖原文。';
  }
  function renderAll() {
    $('#office-text').value = current().text; $('#office-mode').value = current().mode;
    renderControls(); renderResult(); renderSaved();
  }
  function warningsHtml(result) {
    const warnings = Array.isArray(result.warnings) ? result.warnings : [];
    return warnings.length ? `<div class="office-warnings">${warnings.map(text => `<div>${escape(text)}</div>`).join('')}</div>` : '';
  }
  function resultSourceHtml() {
    const source = current().resultSource;
    return source?.note_id ? `<button type="button" class="office-source-button" data-office-result-source>打开来源笔记：${escape(source.title || '原笔记')}</button>` : '';
  }
  const normalizedText = value => String(value ?? '').replace(/\r\n?/g, '\n').trim();
  function rewriteComparison(input, result) {
    const before = normalizedText(input), after = normalizedText(result);
    if (before === after) return '结果与本次输入一致，未作改写。';
    if (before.replace(/\s/g, '') === after.replace(/\s/g, '')) return '仅调整了空白或分段，文字内容未改动。';
    return '当前结果有文字调整，请核对原意。';
  }
  function renderResultNotice() {
    const notice = $('#office-result-notice');
    if (!notice || !current().result) return;
    notice.textContent = normalizedText(current().text) !== normalizedText(current().resultInput)
      ? '上方输入已修改；以下仍为上次处理结果，请重新处理。'
      : kind === 'rewrite' ? rewriteComparison(current().resultInput, current().result.text)
      : '可编辑后保存，请对照上方输入核对。';
  }
  function renderResult() {
    const box = $('#office-result'), result = current().result;
    box.hidden = !result; if (!result) { box.innerHTML = ''; return; }
    let body = '';
    if (kind === 'todos') {
      const items = Array.isArray(result.items) ? result.items : [];
      body = items.length ? items.map((item,i) => `<div class="office-todo-draft" data-draft-row="${i}">
        <label><input type="checkbox" data-draft-selected="${i}" ${item.selected !== false ? 'checked':''}>保存这条待办</label>
        <label>事项<input aria-label="待办事项 ${i+1}" maxlength="240" data-draft-field="task" data-row="${i}" value="${escape(item.task)}"></label>
        <div class="office-todo-fields"><label>负责人<input aria-label="负责人 ${i+1}" maxlength="80" data-draft-field="owner" data-row="${i}" placeholder="未写明" value="${escape(item.owner)}"></label><label>期限<input aria-label="期限 ${i+1}" maxlength="120" data-draft-field="due_text" data-row="${i}" placeholder="未写明" value="${escape(item.due_text)}"></label></div>
        ${item.evidence ? `<details><summary>核对原句</summary><blockquote>${escape(item.evidence)}</blockquote></details>`:''}</div>`).join('') : '<p class="office-caption">这段内容中没有提取到明确待办。可补充行动要求后重试。</p>';
      if (items.length) body += `<div class="office-result-actions"><button type="button" data-office-save-todos ${current().saved?'disabled':''}>${current().saved?'已保存到待办':'保存勾选的待办'}</button><button type="button" class="ghost" data-office-copy>复制清单</button></div>`;
    } else if (kind === 'rewrite') {
      body = `<label>加工结果<textarea id="office-rewrite-result" rows="8" maxlength="1800">${escape(result.text)}</textarea></label><div class="office-result-actions"><button type="button" data-office-copy>复制结果</button><button type="button" class="ghost" data-office-save-note ${current().noteSaved?'disabled':''}>${current().noteSaved?'已另存为笔记':'另存为笔记'}</button></div>`;
    }
    box.innerHTML = `<div class="office-heading-row"><h3>${names[kind]}结果</h3><span class="office-caption">可编辑后再保存</span></div><p id="office-result-notice" class="office-caption" aria-live="polite"></p>${warningsHtml(result)}${resultSourceHtml()}${body}`;
    renderResultNotice();
  }
  function renderSaved() {
    $('#office-todo-count').textContent = savedTodos.filter(item=>!item.completed).length;
    $('#office-saved-todos').innerHTML = savedTodos.length ? savedTodos.map(item=>{ const edited = {...item,...(todoEdits[item.id] || {})}; return `<article class="office-todo-saved ${item.completed?'is-completed':''}" data-saved-id="${escape(item.id)}">
      <div class="office-heading-row"><span class="office-todo-title">${escape(item.task)}</span><button type="button" class="ghost" data-toggle-todo="${escape(item.id)}">${item.completed?'重新打开':'标记完成'}</button></div>
      <p class="office-caption">${escape(item.owner || '负责人未指定')} · ${escape(item.due_text || '期限未指定')}</p>
      ${item.source?.note_id ? `<button type="button" class="office-source-button" data-open-todo-source="${escape(item.id)}">打开来源笔记：${escape(item.source.title || '原笔记')}</button>`:''}
      <details ${todoEdits[item.id]?'open':''}><summary>编辑 / 提醒 / 原句</summary><label>事项<input data-saved-field="task" maxlength="240" value="${escape(edited.task)}"></label><div class="office-todo-fields"><label>负责人<input data-saved-field="owner" maxlength="80" value="${escape(edited.owner)}"></label><label>期限<input data-saved-field="due_text" maxlength="120" value="${escape(edited.due_text)}"></label></div>
      <div class="office-result-actions"><button type="button" class="ghost" data-update-todo="${escape(item.id)}">保存修改</button></div>
      ${!item.completed?`<div class="office-reminder-fields"><label>具体提醒时间<input type="datetime-local" data-reminder-time aria-label="为待办选择提醒时间" value="${escape(edited.reminder_time || '')}"></label><button type="button" class="ghost" data-remind-todo="${escape(item.id)}">设置提醒</button></div>`:''}
      ${item.evidence?`<blockquote>${escape(item.evidence)}</blockquote>`:''}</details></article>`; }).join('') : '<p class="office-caption">还没有保存的待办。提取后勾选需要保留的事项。</p>';
  }
  async function run() {
    if (busy || !library) return;
    const startLibrary = library, submitted = clone(current()), startKind = kind;
    if (!submitted.text.trim() || Array.from(submitted.text).length > maxChars()) { status(`请提供 1—${maxChars()} 字内容，长笔记可先选取一段。`, 'error'); return; }
    current().result = null; current().resultSource = null; current().resultInput = '';
    current().saved = false; current().noteSaved = false; current().requestId = uid();
    const token = ++revision; busy = true; started = Date.now(); renderResult(); renderControls(); persist();
    status('正在提交本地任务…');
    const showProgress = () => status(`正在等待本地处理 · 已等待 ${Math.floor((Date.now()-started)/1000)} 秒`);
    ticker = setInterval(()=>{ if (busy) showProgress(); },1000);
    try {
      const job = await api('/api/office-tools/jobs', {library_id:startLibrary,kind:startKind,text:submitted.text,mode:submitted.mode,source:submitted.source || {}});
      while (token === revision && library === startLibrary) {
        const data = await api(`/api/office-tools/jobs/${encodeURIComponent(job.job_id)}?library_id=${encodeURIComponent(startLibrary)}`);
        if (token !== revision || library !== startLibrary) return;
        if (data.status === 'failed') throw new Error(data.error || '处理失败，请缩短文字后重试');
        if (data.status === 'completed') {
          current().result = data.result; current().resultSource = data.source || submitted.source;
          current().resultInput = data.input_text || submitted.text; current().saved = false; current().noteSaved = false; current().requestId = uid();
          renderResult(); persist();
          const outcome = startKind === 'rewrite' ? rewriteComparison(current().resultInput, data.result.text) : '请核对结果后使用。';
          status(`处理完成，用时 ${Number(data.elapsed_seconds ?? ((Date.now()-started)/1000)).toFixed(1)} 秒。${outcome}`, 'success'); return;
        }
        await new Promise(resolve=>setTimeout(resolve,1000));
      }
    } catch (error) {
      if (token === revision) {
        const message = String(error.message || '处理失败，请重试').replace(/[。；;]+$/, '');
        const retained = /(?:输入|原文)已保留/.test(message) ? '。' : '。输入已保留。';
        status(`未能完成：${message}${retained}`, 'error');
      }
    }
    finally { if (token === revision) { busy = false; clearInterval(ticker); ticker = null; renderControls(); } }
  }
  function markResultEdited() {
    revision += 1; current().saved = false; current().noteSaved = false; current().requestId = uid();
    const saveTodos = $('[data-office-save-todos]'), saveNote = $('[data-office-save-note]');
    if (saveTodos) { saveTodos.disabled = false; saveTodos.textContent = '保存勾选的待办'; }
    if (saveNote) { saveNote.disabled = false; saveNote.textContent = '另存为笔记'; }
    renderResultNotice(); persist();
  }
  function plainResult() {
    const result = current().result; if (!result) return '';
    if (kind === 'rewrite') return result.text || '';
    if (kind === 'todos') return (result.items || []).filter(item=>item.selected !== false).map(item=>`- [ ] ${item.task}${item.owner?`（${item.owner}）`:''}${item.due_text?` · ${item.due_text}`:''}`).join('\n');
    return '';
  }
  async function copyText(text) {
    if (!text) throw new Error('没有可复制的内容');
    try { await navigator.clipboard.writeText(text); }
    catch (_) {
      const field = document.createElement('textarea'); field.value = text; field.style.cssText = 'position:fixed;left:-10000px;top:0';
      document.body.append(field); field.select(); const copied = document.execCommand('copy'); field.remove();
      if (!copied) throw new Error('复制失败，请选中结果后按 Ctrl+C');
    }
  }
  async function action(button, fn) {
    if (button.disabled) return; const startLibrary = library, token = revision;
    button.disabled = true;
    try { await fn(startLibrary,token); }
    catch (error) { if (library === startLibrary) status(error.message,'error'); }
    finally { if (button.isConnected) button.disabled = false; }
  }
  root.addEventListener('input', event=>{
    const element = event.target;
    if (element.id === 'office-text') { current().text = element.value; renderControls(); renderResultNotice(); persist(); }
    else if (element.id === 'office-mode') { current().mode = element.value; persist(); }
    else if (element.id === 'office-rewrite-result') { current().result.text = element.value; markResultEdited(); }
    else if (element.dataset.draftField) { current().result.items[Number(element.dataset.row)][element.dataset.draftField] = element.value; markResultEdited(); }
    else if (element.dataset.draftSelected !== undefined) { current().result.items[Number(element.dataset.draftSelected)].selected = element.checked; markResultEdited(); }
    else if (element.dataset.savedField || element.hasAttribute('data-reminder-time')) {
      const id = element.closest('[data-saved-id]').dataset.savedId;
      todoEdits[id] = {...todoEdits[id], [element.dataset.savedField || 'reminder_time']:element.value}; persist();
    }
  });
  root.addEventListener('click', event=>{
    const button = event.target.closest('button'); if (!button || button.disabled) return;
    if (button.dataset.officeKind) {
      revision += 1; kind = button.dataset.officeKind; renderAll(); status(''); persist(); return;
    }
    if (button.id === 'office-run') { void run(); return; }
    if (button.id === 'office-stop') { stopWaiting(); return; }
    if (button.id === 'office-clear') {
      if ((current().text || current().result) && !confirm('清空当前工具的输入和结果？已保存的笔记和待办不受影响。')) return;
      revision += 1; work[kind] = defaults(); renderAll(); status(''); persist(); return;
    }
    if (button.id === 'office-current' || button.id === 'office-selection') {
      const value = button.id === 'office-selection' ? selected : host()?.getCurrent?.();
      if (!value?.text?.trim()) { status('请先打开有正文的笔记，或在此粘贴文字。'); return; }
      current().text = value.text; current().source = value.source || null; renderAll(); persist();
      status(Array.from(value.text).length > maxChars() ? `内容超过 ${maxChars()} 字，请选取一段或在输入框中精简后处理。` : '内容已填入，可修改后再处理。'); return;
    }
    if (button.hasAttribute('data-office-result-source')) { void action(button, async()=>host()?.openNote(current().resultSource)); return; }
    if (button.dataset.openTodoSource) { const item = savedTodos.find(item=>String(item.id)===button.dataset.openTodoSource); void action(button,async()=>host()?.openNote(item?.source)); return; }
    if (button.hasAttribute('data-office-copy')) { void action(button,async()=>{ await copyText(plainResult()); status('已复制结果。','success'); }); return; }
    if (button.hasAttribute('data-office-save-todos')) {
      void action(button,async(lib,token)=>{
        const items = current().result.items.filter(item=>item.selected !== false).map(({selected,...item})=>item);
        if (!items.length) throw new Error('请至少勾选一条待办');
        if (items.some(item=>!item.task.trim())) throw new Error('勾选的待办事项不能为空');
        const data = await api('/api/office-tools/todos',{library_id:lib,items,source:current().resultSource || {},request_id:current().requestId});
        if (lib !== library || token !== revision) return;
        savedTodos = data.todos; current().saved = true; persist(); renderSaved(); renderResult(); status(`已保存 ${items.length} 条待办。`,'success');
      }); return;
    }
    if (button.hasAttribute('data-office-save-note')) {
      void action(button,async(lib,token)=>{
        const title = `${current().resultSource?.title || names[kind]} · 加工结果`;
        const data = await api('/api/office-tools/save-note',{library_id:lib,title,content:plainResult(),request_id:`note-${current().requestId}`});
        if (lib !== library || token !== revision) return;
        current().noteSaved = true; persist(); renderResult(); status(`已另存为笔记「${data.note?.title || title}」。`,'success');
        await host()?.refresh?.();
      }); return;
    }
    if (button.dataset.toggleTodo || button.dataset.updateTodo) {
      void action(button,async(lib)=>{
        const id = button.dataset.toggleTodo || button.dataset.updateTodo, item = savedTodos.find(item=>String(item.id)===id);
        const body = {library_id:lib};
        if (button.dataset.toggleTodo) body.completed = !item.completed;
        else for (const input of button.closest('[data-saved-id]').querySelectorAll('[data-saved-field]')) body[input.dataset.savedField] = input.value;
        const data = await api(`/api/office-tools/todos/${encodeURIComponent(id)}`,body);
        if (lib !== library) return;
        if (button.dataset.updateTodo) delete todoEdits[id];
        savedTodos = savedTodos.map(item=>String(item.id)===id?data.todo:item); persist(); renderSaved(); status('待办已更新。','success');
      }); return;
    }
    if (button.dataset.remindTodo) {
      void action(button,async(lib)=>{
        const time = button.closest('[data-saved-id]').querySelector('[data-reminder-time]').value;
        if (!time || new Date(time).getTime() <= Date.now()) throw new Error('请选择一个未来的具体提醒时间');
        const data = await api(`/api/office-tools/todos/${encodeURIComponent(button.dataset.remindTodo)}/remind`,{library_id:lib,datetime_str:time.replace('T',' ')});
        if (lib !== library) return;
        if (data.result?.ok === false) throw new Error(data.result.error || '提醒未能保存');
        status(`已设置提醒：${time.replace('T',' ')}。`,'success');
      });
    }
  });
  document.addEventListener('selectionchange',()=>{
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed) return;
    const anchor = selection.anchorNode?.nodeType === 1 ? selection.anchorNode : selection.anchorNode?.parentElement;
    const focus = selection.focusNode?.nodeType === 1 ? selection.focusNode : selection.focusNode?.parentElement;
    const anchorEditor = anchor?.closest('#editor-dock-content .note-content');
    const focusEditor = focus?.closest('#editor-dock-content .note-content');
    const text = selection.toString();
    // Hiding the editor can keep a non-collapsed DOM range but yield empty text.
    // Preserve the last actual selection when focus moves to the tools panel.
    if (!anchorEditor || anchorEditor !== focusEditor || !anchorEditor.getClientRects().length || !text.trim()) return;
    const value = host()?.getCurrent?.();
    if (!value) return;
    selected = {text,source:value.source,library}; renderControls();
  });
  document.querySelector('[data-pane="tool-pane"]')?.addEventListener('click',()=>void refreshLibrary());
  document.addEventListener('visibilitychange',()=>{ if (!document.hidden) void refreshLibrary({quiet:true}); });
  window.addEventListener('beforeunload',persist);
  setInterval(()=>{
    const actual = host()?.library?.();
    if (library && actual && actual !== library) { stopWaiting('资料库已改变，正在载入本库工具…'); void refreshLibrary(); }
  },750);
  renderAll(); void refreshLibrary();
})();
