/* Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0
 */

(() => {
  'use strict';
  const API = '/api/table-analysis';
  const PAGE_SIZE = 100;
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const text = value => value === null || value === undefined ? '' : typeof value === 'object' ? JSON.stringify(value) : String(value);
  const shown = value => value === null || value === undefined || value === '' ? '—' : text(value);
  const path = value => encodeURIComponent(String(value));
  const state = {datasets: [], reports: [], dataset: null, preview: null, report: null, filter: null, page: 1, busy: false, sourceRequest: 0, sourceText: ''};
  let toastTimer;

  async function api(endpoint, body) {
    const response = await fetch(API + endpoint, body === undefined ? {cache: 'no-store'} : {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    });
    let data;
    try { data = await response.json(); } catch (_) { throw Error('服务返回内容无法读取，请确认本机服务正在运行。'); }
    if (!response.ok) throw Error(text(data.error || data.message || data.detail) || `请求失败（${response.status}）`);
    return data;
  }

  function toast(message) {
    clearTimeout(toastTimer);
    $('#analysis-toast').textContent = message;
    $('#analysis-toast').hidden = false;
    toastTimer = setTimeout(() => { $('#analysis-toast').hidden = true; }, 5000);
  }

  function pageMessage(message, error = false) {
    $('#page-message').textContent = message;
    $('#page-message').classList.toggle('is-error', error);
    $('#page-message').hidden = !message;
  }

  function setBusy(busy, message = '') {
    state.busy = busy;
    for (const selector of ['#analysis-workflow input', '#analysis-workflow select', '#analysis-workflow textarea', '#analysis-workflow button', '#new-analysis', '#refresh-history', '.history-item', '.toolbar-actions button', '#analysis-edit-dialog input', '#analysis-edit-dialog textarea', '#save-edit']) {
      $$(selector).forEach(element => { element.disabled = busy; });
    }
    $('#analysis-workflow').setAttribute('aria-busy', String(busy));
    if (message) pageMessage(message);
  }

  function localDate() {
    const now = new Date();
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
  }

  function renderHistory() {
    $('#analysis-history').innerHTML = state.reports.length ? state.reports.map(report =>
      `<button type="button" class="history-item" data-report-id="${esc(report.id)}" aria-current="${String(report.id) === String(state.report?.id)}" ${state.busy ? 'disabled' : ''}><span>${esc(report.title || '未命名报告')}</span><small>${esc(text(report.created).slice(0, 10))} · v${esc(report.version)}</small></button>`
    ).join('') : '<p class="muted">还没有分析报告</p>';
  }

  async function refresh() {
    const data = await api('');
    state.datasets = Array.isArray(data.datasets) ? data.datasets : [];
    state.reports = Array.isArray(data.reports) ? data.reports : [];
    $('#dataset-select').innerHTML = '<option value="">选择已导入文件</option>' + state.datasets.map(dataset =>
      `<option value="${esc(dataset.id)}">${esc(dataset.filename)} · ${esc(text(dataset.created).slice(0, 10))}</option>`
    ).join('');
    if (state.dataset) $('#dataset-select').value = String(state.dataset.id);
    renderHistory();
  }

  async function refreshAfterSave() {
    try { await refresh(); }
    catch (error) { pageMessage('报告已保存，但目录暂时无法刷新：' + error.message, true); }
  }

  function invalidatePreview() {
    state.preview = null;
    $('#preview-result').hidden = true;
    $('#mapping-section').hidden = true;
    $('#preview-error').textContent = '';
    $('#analysis-error').textContent = '';
  }

  function selectDataset(dataset) {
    state.dataset = dataset;
    invalidatePreview();
    $('#sheet-section').hidden = !dataset;
    if (!dataset) { $('#dataset-summary').textContent = ''; return; }
    $('#dataset-select').value = String(dataset.id);
    $('#dataset-summary').textContent = `${dataset.filename} · ${(dataset.sheets || []).length} 个工作表 · 原始文件保留于本机`;
    $('#sheet-select').innerHTML = (dataset.sheets || []).map(sheet =>
      `<option value="${esc(sheet.name)}">${esc(sheet.name)}（${sheet.max_row && sheet.max_column ? `${esc(sheet.max_row)} 行 × ${esc(sheet.max_column)} 列` : '行列数待读取'}）</option>`
    ).join('');
    $('#header-row').value = '1';
    $('#report-title').value = `${text(dataset.filename).replace(/\.xlsx$/i, '')} · 任务与风险分析`;
    $('#report-notes').value = '';
    $('#as-of').value = localDate();
  }

  function issueLabel(issue) {
    if (typeof issue === 'string') return issue;
    if (!issue) return '';
    const rows = Array.isArray(issue.rows) && issue.rows.length ? `（原始行 ${issue.rows.slice(0, 12).join('、')}${issue.rows.length > 12 ? '…' : ''}）` : '';
    return `${issue.label || issue.message || issue.code || text(issue)}${rows}`;
  }

  function renderPreview(preview) {
    const headers = Array.isArray(preview.headers) ? preview.headers : [];
    const rows = Array.isArray(preview.rows) ? preview.rows : [];
    $('#preview-table').innerHTML = '<thead><tr><th scope="col">原始行</th>' + headers.map(header =>
      `<th scope="col"><span class="column-letter">${esc(header.key)}</span>${esc(header.label || '（空白表头）')}</th>`
    ).join('') + '</tr></thead><tbody>' + rows.map(row => '<tr><td>' + esc(row.row) + '</td>' + headers.map(header => {
      const formula = (row.formula_columns || []).includes(header.key);
      return `<td>${esc(shown(row.values?.[header.key]))}${formula ? '<span class="formula-tag">公式 · 不参与统计</span>' : ''}</td>`;
    }).join('') + '</tr>').join('') + '</tbody>';
    $('#preview-summary').textContent = `当前表头：第 ${$('#header-row').value} 行；数据行数：${shown(preview.row_count)}。以下显示前 ${rows.length} 行，生成时按下方指定起止行读取。`;
    $('#preview-issues').innerHTML = (preview.issues || []).map(issue => `<li>${esc(issueLabel(issue))}</li>`).join('');
    $$('[data-column]').forEach(select => {
      select.innerHTML = `<option value="">${select.dataset.column === 'title' ? '请选择事项名称列' : '不分析此字段'}</option>` + headers.map(header =>
        `<option value="${esc(header.key)}">${esc(header.key)} · ${esc(header.label || '（空白表头）')}</option>`
      ).join('');
      select.value = '';
    });
    $('#completed-options').replaceChildren();
    $('#additional-completed').value = '';
    $('#no-completed').checked = false;
    $('#completion-section').hidden = true;
    $('#number-settings').hidden = true;
    $('#numeric-unit').value = '';
    $('#numeric-unit').required = false;
    const first = Number($('#header-row').value) + 1;
    const last = Number(preview.data_end_row) || first;
    $('#start-row').min = String(first);
    $('#end-row').min = String(first);
    $('#start-row').value = String(first);
    $('#end-row').value = String(last);
    $('#start-row').max = String(last);
    $('#end-row').max = String(last);
    $('#preview-result').hidden = false;
    $('#mapping-section').hidden = false;
  }

  async function loadPreview() {
    if (!state.dataset || state.busy) return;
    const header = Number($('#header-row').value);
    if (!Number.isInteger(header) || header < 1) { $('#preview-error').textContent = '表头行号必须是大于 0 的整数。'; return; }
    invalidatePreview();
    setBusy(true, '正在读取工作表预览…');
    try {
      const preview = await api('/preview', {dataset_id: state.dataset.id, sheet: $('#sheet-select').value, header_row: header});
      state.preview = preview;
      renderPreview(preview);
      pageMessage('预览已读取。请核对表头，然后明确指定分析列与完成状态。');
    } catch (error) { $('#preview-error').textContent = error.message; pageMessage('', false); }
    finally { setBusy(false); }
  }

  function statusMappingChanged() {
    const key = $('#column-status').value;
    $('#completion-section').hidden = !key;
    $('#additional-completed').value = '';
    $('#no-completed').checked = false;
    const values = Array.isArray(state.preview?.values?.[key]) ? state.preview.values[key] : [];
    const distinct = [...new Set(values.map(value => text(value).trim()).filter(Boolean))];
    $('#completed-options').innerHTML = distinct.length ? distinct.map(value =>
      `<label class="check-label"><input type="checkbox" name="completed-value" value="${esc(value)}"><span>${esc(value || '（空白）')}</span></label>`
    ).join('') : '<p class="muted">预览中没有状态候选。可填写原表中的完成状态，或明确选择本次不定义。</p>';
  }

  function completedValues() {
    if (!$('#column-status').value || $('#no-completed').checked) return [];
    const checked = $$('#completed-options input:checked').map(input => input.value);
    const additional = $('#additional-completed').value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
    return [...new Set([...checked, ...additional])];
  }

  function safeFragment(html) {
    // The local renderer owns markup; limit its capabilities before attaching it to the app.
    const parsed = new DOMParser().parseFromString(String(html || ''), 'text/html');
    const allowed = new Set(['ARTICLE','SECTION','HEADER','FOOTER','DIV','SPAN','P','H1','H2','H3','H4','H5','H6','UL','OL','LI','DL','DT','DD','TABLE','CAPTION','COLGROUP','COL','THEAD','TBODY','TFOOT','TR','TH','TD','STRONG','B','EM','I','SMALL','SUP','SUB','BR','HR','PRE','CODE','BLOCKQUOTE','BUTTON','A','DETAILS','SUMMARY','FIGURE','FIGCAPTION','TIME','MARK','SVG','G','RECT','LINE','POLYLINE','POLYGON','PATH','CIRCLE','ELLIPSE','TEXT','TSPAN','TITLE','DESC','DEFS','CLIPPATH']);
    const svgAttrs = new Set(['viewbox','width','height','x','y','x1','x2','y1','y2','cx','cy','r','rx','ry','d','points','transform','fill','stroke','stroke-width','stroke-linecap','stroke-linejoin','text-anchor','font-size','font-weight','dominant-baseline','preserveaspectratio','colspan','rowspan','scope','datetime','tabindex','role','type']);
    for (const element of [...parsed.body.querySelectorAll('*')]) {
      if (!allowed.has(element.tagName.toUpperCase())) { element.remove(); continue; }
      for (const attribute of [...element.attributes]) {
        const name = attribute.name.toLowerCase();
        const value = attribute.value;
        const common = name === 'class' || name === 'id' || name === 'title' || name.startsWith('aria-') || ['data-drill-kind','data-drill-index','data-all-rows','data-source-row'].includes(name);
        const anchor = name === 'href' && element.tagName.toUpperCase() === 'A' && /^#[A-Za-z0-9_-]+$/.test(value);
        if ((!common && !svgAttrs.has(name) && !anchor) || /url\s*\(/i.test(value)) element.removeAttribute(attribute.name);
      }
      if (element.tagName.toUpperCase() === 'BUTTON') element.setAttribute('type', 'button');
    }
    const fragment = document.createDocumentFragment();
    fragment.append(...parsed.body.childNodes);
    return fragment;
  }

  function showReport(data, resetFilter = true) {
    state.report = data.report;
    if (resetFilter) { state.filter = null; state.page = 1; }
    $('#analysis-output').replaceChildren(safeFragment(data.html));
    const details = $('#analysis-output #analysis-details');
    if (details?.previousElementSibling?.classList.contains('analysis-actions')) details.previousElementSibling.remove();
    if (details?.previousElementSibling?.tagName === 'H2' && details.previousElementSibling.textContent.trim() === '记录明细') details.previousElementSibling.remove();
    $('#analysis-welcome').hidden = true;
    $('#saved-report').hidden = false;
    $('#analysis-workflow').open = false;
    $('#save-status').textContent = `已保存到本机 · v${shown(data.report.version)} · ${text(data.report.updated || data.report.created).replace('T', ' ').slice(0, 19)}`;
    renderDetails();
    renderHistory();
    history.replaceState(null, '', `/analysis.html?id=${path(data.report.id)}`);
  }

  function filterRows() {
    const rows = Array.isArray(state.report?.rows) ? state.report.rows : [];
    if (!state.filter) return rows;
    const group = state.report.groups?.[state.filter.kind]?.[state.filter.index];
    const included = new Set((group?.rows || []).map(String));
    return rows.filter(row => included.has(String(row.row)));
  }

  function dueText(due) {
    if (!due || typeof due !== 'object') return shown(due);
    if (due.parsed) return text(due.parsed);
    return due.raw === null || due.raw === undefined || due.raw === '' ? '未知' : `${text(due.raw)}（日期未知）`;
  }

  function rowIssueText(row) {
    return (Array.isArray(row.issues) ? row.issues : []).map(issueLabel).filter(Boolean).join('；');
  }

  function renderDetails() {
    let details = $('#analysis-output #analysis-details');
    if (!details) { details = document.createElement('section'); details.id = 'analysis-details'; $('#analysis-output').append(details); }
    const rows = filterRows();
    const pageCount = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
    state.page = Math.min(Math.max(1, state.page), pageCount);
    const start = (state.page - 1) * PAGE_SIZE;
    const pageRows = rows.slice(start, start + PAGE_SIZE);
    const config = state.report.config || {};
    const columns = config.columns || state.report.lineage?.columns || {};
    const group = state.filter ? state.report.groups?.[state.filter.kind]?.[state.filter.index] : null;
    const groupNames = {status: '状态', owner: '责任人', risk: '风险', deadline: '期限'};
    const selection = state.filter ? `${groupNames[state.filter.kind] || '分组'}：${group?.label || '（空白）'}` : '全部记录';
    const fields = ['action','status','owner','due','risk','number'].filter(key => columns[key]);
    const names = {action: '处理动作 / 下一步', status: '原始状态', owner: '责任人', due: '截止日期', risk: '原始风险', number: `数值${config.numeric_unit ? `（${config.numeric_unit}）` : ''}`};
    details.innerHTML = `<div class="detail-heading"><h2>逐行明细</h2><button type="button" data-all-rows>查看全部记录</button></div><p class="detail-note" role="status">${esc(selection)} · 共 ${rows.length} 行${rows.length ? ` · 显示 ${start + 1}–${start + pageRows.length} 行` : ''} · 每页 100 行。点击原始行号核对工作表单元格。</p>` +
      (rows.length ? `<div class="table-scroll" tabindex="0" aria-label="分析明细"><table class="detail-table"><thead><tr><th scope="col">原始行</th><th scope="col">事项</th>${fields.map(key => `<th scope="col">${esc(names[key])}</th>`).join('')}<th scope="col">核对提示</th></tr></thead><tbody>${pageRows.map(row =>
        `<tr><td><button type="button" data-source-row="${esc(row.row)}" aria-label="查看原始第 ${esc(row.row)} 行">${esc(row.row)}</button></td><td>${esc(shown(row.label))}</td>${fields.map(key => `<td${key === 'number' ? ' class="numeric"' : ''}>${esc(key === 'due' ? dueText(row.due) : shown(row[key]))}</td>`).join('')}<td><span class="row-issue">${esc(rowIssueText(row) || '—')}</span></td></tr>`
      ).join('')}</tbody></table></div>` : '<p class="empty-detail">此分组没有可显示的行。</p>') +
      `<div class="pagination"><button type="button" data-detail-page="${state.page - 1}" ${state.page === 1 ? 'disabled' : ''}>上一页</button><span>第 ${state.page} / ${pageCount} 页</span><button type="button" data-detail-page="${state.page + 1}" ${state.page === pageCount ? 'disabled' : ''}>下一页</button></div>`;
    $$('#analysis-output [data-drill-kind]').forEach(button => {
      button.setAttribute('aria-pressed', String(Boolean(state.filter && button.dataset.drillKind === state.filter.kind && Number(button.dataset.drillIndex) === state.filter.index)));
    });
  }

  async function openReport(id) {
    if (state.busy) return;
    setBusy(true, '正在打开已保存的报告…');
    try { showReport(await api(`/reports/${path(id)}`)); pageMessage(''); $('#saved-report').scrollIntoView({block: 'start'}); }
    catch (error) { pageMessage(error.message, true); }
    finally { setBusy(false); }
  }

  function datasetId() {
    return state.report?.dataset?.id || state.report?.config?.dataset_id;
  }

  async function download(endpoint, filename) {
    const response = await fetch(API + endpoint);
    if (!response.ok) {
      let message = '下载失败，请重试。';
      try { const data = await response.json(); message = text(data.error || data.message) || message; } catch (_) {}
      throw Error(message);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename.replace(/[\\/:*?"<>|\x00-\x1f]/g, '-');
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }

  async function downloadWorkbook() {
    const id = datasetId();
    if (!id) { toast('报告缺少原始工作簿标识。'); return; }
    try { await download(`/datasets/${path(id)}/file`, state.report.dataset?.filename || '原始工作簿.xlsx'); toast('已下载报告所用的原始工作簿。'); }
    catch (error) { toast(error.message); }
  }

  async function openSource(row) {
    if (!state.report || !Number.isInteger(Number(row)) || Number(row) < 1) return;
    const request = ++state.sourceRequest;
    state.sourceText = '';
    $('#copy-source').disabled = true;
    $('#source-title').textContent = `原始工作表 · 第 ${row} 行`;
    $('#source-body').textContent = '正在读取原始单元格…';
    if (!$('#analysis-source-dialog').open) $('#analysis-source-dialog').showModal();
    try {
      const data = await api(`/reports/${path(state.report.id)}/rows/${path(row)}`);
      if (request !== state.sourceRequest) return;
      state.sourceText = `工作簿：${data.filename}\n定位：${data.sheet}!${data.range}\nSHA-256：${data.sha256}\n\n` + (data.cells || []).map(cell =>
        `${cell.column}${data.row} [${cell.header || '空白表头'}]：${text(cell.value)}${cell.formula ? `\n  公式缓存（未参与统计）：${shown(cell.cached)}` : ''}`
      ).join('\n');
      $('#copy-source').disabled = false;
      $('#source-title').textContent = `${data.sheet} · 第 ${data.row} 行`;
      $('#source-body').innerHTML = `<dl class="source-meta"><dt>工作簿</dt><dd>${esc(data.filename)}</dd><dt>定位</dt><dd>${esc(data.sheet)}!${esc(data.range)}</dd><dt>文件指纹</dt><dd>${esc(data.sha256)}</dd></dl><div class="table-scroll" tabindex="0" aria-label="原始行单元格"><table class="source-table"><thead><tr><th scope="col">列</th><th scope="col">原始表头</th><th scope="col">单元格值</th><th scope="col">类型 / 公式</th></tr></thead><tbody>${(data.cells || []).map(cell => {
        const formula = cell.formula !== null && cell.formula !== undefined && cell.formula !== '' && cell.formula !== false;
        return `<tr><td>${esc(cell.column)}</td><td>${esc(shown(cell.header))}</td><td>${esc(shown(cell.value))}</td><td>${formula ? `<code>${esc(text(cell.formula))}</code><span class="formula-tag">公式及缓存不参与统计</span><span class="muted">缓存值：${esc(shown(cell.cached))}</span>` : esc(shown(cell.type))}</td></tr>`;
      }).join('')}</tbody></table></div><p class="section-note">此处显示报告所用原始文件的单元格。可下载同一工作簿，在所列工作表与行号核对。</p>`;
    } catch (error) { if (request === state.sourceRequest) $('#source-body').textContent = error.message; }
  }

  $('#import-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (state.busy) return;
    const file = $('#workbook-file').files[0];
    $('#import-error').textContent = '';
    if (!file || !/\.xlsx$/i.test(file.name)) { $('#import-error').textContent = '请选择 .xlsx 格式的工作簿。'; return; }
    setBusy(true, '正在导入本机工作簿…');
    let imported = false;
    try {
      const dataUrl = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(Error('无法读取所选文件，请重新选择。'));
        reader.readAsDataURL(file);
      });
      const dataset = await api('/import', {filename: file.name, data_url: dataUrl});
      try { await refresh(); } catch (error) { toast('工作簿已导入，目录暂时无法刷新：' + error.message); }
      selectDataset(dataset);
      imported = true;
    } catch (error) { $('#import-error').textContent = error.message; pageMessage(''); }
    finally { setBusy(false); }
    if (imported) await loadPreview();
  });

  $('#dataset-select').addEventListener('change', async () => {
    if (state.busy) return;
    selectDataset(state.datasets.find(dataset => String(dataset.id) === $('#dataset-select').value) || null);
    if (state.dataset) await loadPreview();
  });
  $('#sheet-select').addEventListener('change', invalidatePreview);
  $('#header-row').addEventListener('input', invalidatePreview);
  $('#preview-form').addEventListener('submit', event => { event.preventDefault(); loadPreview(); });
  $('#column-status').addEventListener('change', statusMappingChanged);
  $('#column-number').addEventListener('change', () => {
    const selected = Boolean($('#column-number').value);
    $('#number-settings').hidden = !selected;
    $('#numeric-unit').required = selected;
  });
  $('#completed-options').addEventListener('change', () => { $('#no-completed').checked = false; });
  $('#additional-completed').addEventListener('input', () => { if ($('#additional-completed').value.trim()) $('#no-completed').checked = false; });
  $('#no-completed').addEventListener('change', () => {
    if ($('#no-completed').checked) {
      $$('#completed-options input').forEach(input => { input.checked = false; });
      $('#additional-completed').value = '';
    }
  });

  $('#analysis-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (state.busy || !state.preview || !state.dataset) return;
    $('#analysis-error').textContent = '';
    const columns = Object.fromEntries($$('[data-column]').map(select => [select.dataset.column, select.value]));
    const completed = completedValues();
    if (!columns.title) { $('#analysis-error').textContent = '请指定任务 / 事项名称列。'; return; }
    if (columns.status && !completed.length && !$('#no-completed').checked) {
      $('#analysis-error').textContent = '请明确选择完成状态，或选择本次不定义完成状态。';
      $('#completion-section').scrollIntoView({block: 'center'});
      return;
    }
    const title = $('#report-title').value.trim();
    if (!title) { $('#analysis-error').textContent = '请填写报告标题。'; return; }
    if (columns.number && !$('#numeric-unit').value.trim()) { $('#analysis-error').textContent = '请填写数值单位；没有单位请填“无单位”。'; return; }
    const first = Number($('#start-row').value);
    const last = Number($('#end-row').value);
    if (!Number.isInteger(first) || !Number.isInteger(last) || first <= Number($('#header-row').value) || last < first || last > Number(state.preview.data_end_row)) {
      $('#analysis-error').textContent = '请核对数据范围：起始行须在表头之后，结束行不得早于起始行或超出工作表末行。'; return;
    }
    const payload = {dataset_id: state.dataset.id, sheet: $('#sheet-select').value, header_row: Number($('#header-row').value), title,
      as_of: $('#as-of').value, start_row: first, end_row: last, columns, completed_values: completed, numeric_unit: columns.number ? $('#numeric-unit').value.trim() : '', notes: $('#report-notes').value};
    setBusy(true, '正在计算并保存分析报告…');
    try {
      showReport(await api('/analyze', payload));
      pageMessage('报告已生成并保存。点击图表查看对应明细，点击原始行号核对来源。');
      await refreshAfterSave();
      $('#saved-report').scrollIntoView({block: 'start'});
    } catch (error) { $('#analysis-error').textContent = error.message; $('#analysis-workflow').open = true; pageMessage('生成未完成：' + error.message, true); }
    finally { setBusy(false); }
  });

  document.addEventListener('click', event => {
    const close = event.target.closest('[data-close]');
    if (close) { $('#' + close.dataset.close).close(); return; }
    const historyButton = event.target.closest('[data-report-id]');
    if (historyButton) { openReport(historyButton.dataset.reportId); return; }
    const output = event.target.closest('#analysis-output');
    if (!output || !state.report) return;
    const source = event.target.closest('[data-source-row]');
    if (source) { event.preventDefault(); openSource(source.dataset.sourceRow); return; }
    const drill = event.target.closest('[data-drill-kind]');
    if (drill) {
      const kind = drill.dataset.drillKind;
      const index = Number(drill.dataset.drillIndex);
      if (!Object.hasOwn(state.report.groups || {}, kind) || !Array.isArray(state.report.groups[kind]) || !Number.isInteger(index) || !state.report.groups[kind][index]) return;
      event.preventDefault(); state.filter = {kind, index}; state.page = 1; renderDetails(); $('#analysis-details').scrollIntoView({block: 'start'}); return;
    }
    if (event.target.closest('[data-all-rows]')) {
      event.preventDefault(); state.filter = null; state.page = 1; renderDetails(); $('#analysis-details').scrollIntoView({block: 'start'}); return;
    }
    const pageButton = event.target.closest('[data-detail-page]');
    if (pageButton && !pageButton.disabled) { state.page = Number(pageButton.dataset.detailPage); renderDetails(); $('#analysis-details').scrollIntoView({block: 'start'}); }
  });

  $('#new-analysis').addEventListener('click', () => {
    if (state.busy) return;
    state.report = null; state.filter = null; state.page = 1;
    $('#saved-report').hidden = true;
    $('#analysis-welcome').hidden = false;
    $('#analysis-workflow').open = true;
    $('#analysis-form').reset();
    $('#import-form').reset();
    $('#dataset-select').value = '';
    selectDataset(null);
    $('#import-error').textContent = '';
    pageMessage(''); renderHistory();
    history.replaceState(null, '', '/analysis.html');
    $('#workbook-file').focus();
    window.scrollTo({top: 0});
  });
  $('#refresh-history').addEventListener('click', async () => {
    if (state.busy) return;
    try { await refresh(); toast('报告目录已刷新。'); } catch (error) { pageMessage(error.message, true); }
  });
  $('#edit-report').addEventListener('click', () => {
    if (!state.report || state.busy) return;
    $('#edit-title').value = state.report.title || '';
    $('#edit-notes').value = text(state.report.notes);
    $('#edit-error').textContent = '';
    $('#analysis-edit-dialog').showModal();
  });
  $('#edit-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!state.report || state.busy) return;
    const title = $('#edit-title').value.trim();
    if (!title) { $('#edit-error').textContent = '请填写报告标题。'; return; }
    const reportId = state.report.id;
    setBusy(true);
    $('#edit-error').textContent = '';
    try {
      showReport(await api(`/reports/${path(reportId)}/save`, {version: state.report.version, title, notes: $('#edit-notes').value}), false);
      $('#analysis-edit-dialog').close();
      await refreshAfterSave();
      toast('标题与说明已保存；统计数字和原始来源保留。');
    } catch (error) { $('#edit-error').textContent = `${error.message} 如报告已在其他窗口修改，请关闭编辑并重新打开最新报告后再保存。`; }
    finally { setBusy(false); }
  });
  $('#download-workbook').addEventListener('click', downloadWorkbook);
  $('#source-download').addEventListener('click', downloadWorkbook);
  $('#copy-source').addEventListener('click', async () => {
    if (!state.sourceText) return;
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(state.sourceText);
      else {
        const area = document.createElement('textarea');
        area.value = state.sourceText;
        area.setAttribute('aria-label', '待复制的原始行');
        $('#source-body').append(area);
        area.select();
        const copied = document.execCommand('copy');
        area.remove();
        if (!copied) throw Error('当前环境无法自动复制，请从来源窗口选择文字复制。');
      }
      toast('已复制原始行、定位与文件指纹。');
    } catch (error) { toast(error.message || '复制失败，请从来源窗口选择文字复制。'); }
  });
  $('#export-report').addEventListener('click', async () => {
    if (!state.report) return;
    try { await download(`/reports/${path(state.report.id)}/export`, `${state.report.title || '任务与风险分析'}.html`); toast('完整 HTML 已导出，包含全部明细和来源附录。'); }
    catch (error) { toast(error.message); }
  });
  $('#analysis-source-dialog').addEventListener('close', () => { state.sourceRequest += 1; });

  (async () => {
    $('#as-of').value = localDate();
    try {
      await refresh();
      const id = new URLSearchParams(location.search).get('id');
      if (id) await openReport(id);
    } catch (error) { pageMessage('无法读取本机分析工作台：' + error.message, true); $('#analysis-history').textContent = '目录暂时不可用'; }
  })();
})();
