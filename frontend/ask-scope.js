/* Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0
 */

// Scope is explicit user intent. Search ranking must never choose it.
function askScopeIds(values) {
  return [...new Set((values || []).map(Number).filter(id => Number.isSafeInteger(id) && id > 0))].sort((a, b) => a - b);
}

function askScopeKey(scope) {
  return JSON.stringify([scope.mode, askScopeIds(scope.note_ids), askScopeIds(scope.document_ids)]);
}

function askScopeSnapshot() {
  // No manual scope UI: never let a stale selection silently narrow global search.
  if (!$('#ask-scope-mode')) return { mode: 'all', note_ids: [], document_ids: [], label: '全部资料' };
  const scope = state.askScope;
  if (scope.mode === 'current') {
    const note = state.askCurrentNote;
    if (!note) return null;
    return { mode: 'current', note_ids: [note.id], document_ids: [], label: `当前笔记：${note.title}（全部页签）` };
  }
  if (scope.mode === 'selected') {
    if (!scope.note_ids.length && !scope.document_ids.length) return null;
    return { ...scope, note_ids: [...scope.note_ids], document_ids: [...scope.document_ids] };
  }
  return { mode: 'all', note_ids: [], document_ids: [], label: '全部资料' };
}

function askScopeRequest(scope) {
  return { mode: scope.mode, note_ids: [...scope.note_ids], document_ids: [...scope.document_ids] };
}

function renderAnswerScope(scope) {
  return `<p class="answer-scope">问答范围：${escapeHtml(scope?.label || '全部资料')}</p>`;
}

function clearAskScopeEvidence() {
  stopAskWaiting('问答范围已改变，请重新提问。');
  state.stoppedAsk = null;
  state.askHistory = [];
  state.askHistoryScopeKey = null;
  $('#answer-panel').innerHTML = '<div class="empty-guide"><p class="muted">问答范围已改变，请重新提问。</p></div>';
  $('#result-count').textContent = '';
}

function setAskScope(scope) {
  const before = askScopeSnapshot();
  state.askScope = { ...scope, note_ids: askScopeIds(scope.note_ids), document_ids: askScopeIds(scope.document_ids) };
  const after = askScopeSnapshot();
  if (askScopeKey(before || { mode: 'current' }) !== askScopeKey(after || { mode: 'current' })) clearAskScopeEvidence();
  renderAskScopeControls();
}

function rememberAskCurrentNote(note, origin = 'editor') {
  const before = askScopeSnapshot();
  const familyId = Number(note?.parent_note_id || note?.id);
  const parent = [...state.allNotes, ...state.notes].find(item => Number(item.id) === familyId);
  const current = Number.isSafeInteger(familyId) && familyId > 0 && note?.source_type !== 'welcome'
    ? { id: familyId, title: String(parent?.title || note.title || '未命名笔记'), origin } : null;
  if (origin === 'editor') state.askEditorNote = current;
  state.askCurrentNote = current;
  const after = askScopeSnapshot();
  if (state.askScope.mode === 'current' && askScopeKey(before || { mode: 'current' }) !== askScopeKey(after || { mode: 'current' })) clearAskScopeEvidence();
  renderAskScopeControls();
}

function rememberAskEditorNote(noteId) {
  const note = [...state.allNotes, ...state.notes].find(item => Number(item.id) === Number(noteId));
  if (note) rememberAskCurrentNote(note);
}

function resetAskScope() {
  closeAskSourcePicker(false);
  state.askCurrentNote = null;
  state.askEditorNote = null;
  state.askSources = null;
  state.askScope = { mode: 'all', note_ids: [], document_ids: [], label: '全部资料' };
  clearAskScopeEvidence();
  renderAskScopeControls();
}

function renderAskScopeControls() {
  const select = $('#ask-scope-mode');
  if (!select) return;
  select.value = state.askScope.mode;
  $('#ask-scope-current').disabled = !state.askCurrentNote;
  const scope = askScopeSnapshot();
  const summary = $('#ask-scope-summary');
  summary.textContent = state.askScope.mode === 'all'
    ? (state.askCurrentNote ? `当前笔记可选：${state.askCurrentNote.title}` : '先打开一条笔记，才能选「当前笔记」')
    : (scope?.label || '请先打开一条已保存的笔记');
  summary.title = summary.textContent;
  $('#ask-scope-edit').hidden = state.askScope.mode !== 'selected';
  $('#ask-button').disabled = !scope;
}

function askSourceRows(sources) {
  return [
    ...(sources?.notes || []).map(note => ({ kind: 'note', id: Number(note.id), title: String(note.title || '未命名笔记'), detail: `笔记 · ${Number(note.tab_count) || 1} 个页签（全部纳入）` })),
    ...(sources?.documents || []).map(doc => ({ kind: 'document', id: Number(doc.id), title: String(doc.title || doc.original_name || '未命名文档'), detail: '导入文档' })),
  ].filter(row => Number.isSafeInteger(row.id) && row.id > 0);
}

function renderAskSourceChoices() {
  const picker = state.askSourcePicker;
  if (!picker) return;
  const query = $('#ask-source-search').value.trim().toLocaleLowerCase();
  const rows = askSourceRows(state.askSources).filter(row => row.title.toLocaleLowerCase().includes(query));
  $('#ask-source-options').innerHTML = rows.length ? rows.map(row => {
    const key = `${row.kind}:${row.id}`;
    return `<label class="ask-source-choice"><input type="checkbox" data-ask-source="${key}" ${picker.selected.has(key) ? 'checked' : ''}><span><strong>${escapeHtml(row.title)}</strong><small>${escapeHtml(row.detail)}</small></span></label>`;
  }).join('') : `<p class="muted ask-source-empty">${query ? '没有匹配的资料，试试其他关键词。' : '还没有可选资料，请先保存笔记或导入文档。'}</p>`;
  renderAskSourceCount();
}

function renderAskSourceCount() {
  const size = state.askSourcePicker?.selected.size || 0;
  $('#ask-source-count').textContent = size ? `已选 ${size} 项 · 笔记包含全部页签` : '尚未选择资料';
  $('#ask-source-apply').disabled = !size;
}

async function openAskSourcePicker() {
  if (state.askSourcePicker) return;
  const picker = { selected: new Set([
    ...state.askScope.note_ids.map(id => `note:${id}`),
    ...state.askScope.document_ids.map(id => `document:${id}`),
  ]), returnFocus: document.activeElement };
  state.askSourcePicker = picker;
  state.askSources = null;
  $('#ask-source-picker').showModal();
  $('#ask-source-search').value = '';
  $('#ask-source-options').innerHTML = '<p class="muted ask-source-empty">正在读取资料列表…</p>';
  $('#ask-source-apply').disabled = true;
  $('#ask-source-count').textContent = '';
  $('#ask-source-search').focus();
  try {
    const sources = await request('/api/ask/sources');
    if (state.askSourcePicker !== picker) return;
    state.askSources = sources;
    const available = new Set(askSourceRows(sources).map(row => `${row.kind}:${row.id}`));
    picker.selected = new Set([...picker.selected].filter(key => available.has(key)));
    renderAskSourceChoices();
  } catch (error) {
    if (state.askSourcePicker !== picker) return;
    $('#ask-source-options').innerHTML = `<p class="ocr-error ask-source-empty">${escapeHtml(error.message || '资料列表读取失败，请关闭后重试。')}</p>`;
  }
}

function closeAskSourcePicker(restoreFocus = true) {
  const picker = state.askSourcePicker;
  state.askSourcePicker = null;
  const dialog = $('#ask-source-picker');
  if (dialog?.open) dialog.close();
  renderAskScopeControls();
  if (restoreFocus && picker) (picker.returnFocus?.isConnected ? picker.returnFocus : $('#ask-scope-mode')).focus();
}

function applyAskSourcePicker() {
  const picker = state.askSourcePicker;
  if (!picker?.selected.size) return;
  const chosen = askSourceRows(state.askSources).filter(row => picker.selected.has(`${row.kind}:${row.id}`));
  if (!chosen.length) return;
  setAskScope({ mode: 'selected', note_ids: chosen.filter(row => row.kind === 'note').map(row => row.id),
    document_ids: chosen.filter(row => row.kind === 'document').map(row => row.id),
    label: `指定资料：${chosen.map(row => `${row.title}${row.kind === 'note' ? '（全部页签）' : ''}`).join('、')}` });
  closeAskSourcePicker();
}

function initializeAskScope() {
  if (!$('#ask-scope-mode')) {
    state.askScope = { mode: 'all', note_ids: [], document_ids: [], label: '全部资料' };
    state.askSources = null;
    state.askSourcePicker = null;
    state.askHistory = [];
    state.askHistoryScopeKey = null;
    return;
  }
  $('#ask-scope-mode').addEventListener('change', event => {
    if (event.target.value === 'selected') {
      renderAskScopeControls();
      void openAskSourcePicker();
    } else {
      setAskScope({ mode: event.target.value, note_ids: [], document_ids: [] });
    }
  });
  $('#ask-scope-edit').addEventListener('click', () => void openAskSourcePicker());
  $('#ask-source-search').addEventListener('input', () => { if (state.askSources) renderAskSourceChoices(); });
  $('#ask-source-options').addEventListener('change', event => {
    const input = event.target.closest('[data-ask-source]');
    if (!input || !state.askSourcePicker) return;
    if (input.checked) state.askSourcePicker.selected.add(input.dataset.askSource);
    else state.askSourcePicker.selected.delete(input.dataset.askSource);
    renderAskSourceCount();
  });
  $('#ask-source-cancel').addEventListener('click', () => closeAskSourcePicker());
  $('#ask-source-apply').addEventListener('click', applyAskSourcePicker);
  $('#ask-source-picker').addEventListener('cancel', event => { event.preventDefault(); closeAskSourcePicker(); });
  $('#ask-source-picker').addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      event.preventDefault(); event.stopPropagation(); closeAskSourcePicker(); return;
    }
    if (event.key === 'Tab') {
      const focusable = [...$('#ask-source-picker').querySelectorAll('input:not(:disabled), button:not(:disabled)')];
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  });
  for (const eventName of ['pointerdown', 'focusin']) {
    $('#editor-dock-content').addEventListener(eventName, event => {
      const note = event.target.closest('.note[data-note-id]');
      if (note) rememberAskEditorNote(Number(note.dataset.noteId));
    });
  }
  renderAskScopeControls();
}
