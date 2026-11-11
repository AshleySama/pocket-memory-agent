/* Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0
 */

(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const hooks = window.PocketMemoryUpdateHooks;
  if (!hooks || !$('#app-update-panel')) return;
  const ACTIVE = new Set(['downloading', 'verifying', 'ready', 'restarting']);
  const LABELS = {preview: '待检查', idle: '待检查', checking: '检查中', unpublished: '尚未发布', available: '可更新', current: '无需更新', unavailable: '暂无更新', downloading: '下载中', verifying: '校验中', ready: '准备就绪', restarting: '正在重启', error: '未完成'};
  const RESUME_KEY = 'pocket-memory-update-resume-v1';
  const DISMISS_KEY = 'pocket-memory-update-dismissed-version';
  const UPDATE_CONTROLS = '#app-update-panel, #settings-dialog .dialog-header, #settings-button, #app-update-notice, [data-tab-target="app-update"]';
  let info = null;
  let timer = null;
  let fetching = false;
  let submitting = false;
  let preparing = false;
  let starting = false;
  let checking = false;
  let locked = false;
  let disconnected = false;
  let connectionSince = 0;
  let connectionPaused = false;
  let localError = '';
  let suppressActiveNotice = false;
  let resume = readStored(RESUME_KEY, true);
  const inertBefore = new Map();
  const disabledBefore = new Map();

  function readStored(key, json = false) {
    try { const value = (key === RESUME_KEY ? localStorage : sessionStorage).getItem(key); return json && value ? JSON.parse(value) : value; }
    catch (_) { return null; }
  }
  function store(key, value) {
    try { const storage = key === RESUME_KEY ? localStorage : sessionStorage; if (value === null) storage.removeItem(key); else storage.setItem(key, typeof value === 'string' ? value : JSON.stringify(value)); }
    catch (_) { /* The in-memory flow still works when storage is unavailable. */ }
  }
  function version(value) { return value ? String(value).replace(/^v/i, '') : ''; }
  function size(value) {
    if (!Number.isFinite(Number(value)) || Number(value) <= 0) return '—';
    const bytes = Number(value);
    return bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GB` : bytes >= 1024 ** 2 ? `${(bytes / 1024 ** 2).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB`;
  }
  function currentAddress() { return location.pathname + location.search + location.hash; }
  function safeAddress(address) {
    try { const url = new URL(String(address || '/'), location.origin); return url.origin === location.origin ? url.pathname + url.search + url.hash : '/'; }
    catch (_) { return '/'; }
  }
  function panelOpen() { return $('#settings-dialog').open && $('#app-update-panel').classList.contains('active'); }

  async function api(suffix = '', post = false) {
    const url = new URL(`/api/app-update${suffix}`, location.origin);
    if (url.origin !== location.origin) throw Error('更新请求必须由当前应用发起。');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url.href, {
        method: post ? 'POST' : 'GET', mode: 'same-origin', credentials: 'same-origin', redirect: 'error', cache: 'no-store',
        headers: {'Content-Type': 'application/json', 'X-PocketMemory-Update': '1'},
        ...(post ? {body: '{}'} : {}), signal: controller.signal
      });
      let data;
      try { data = await response.json(); }
      catch (_) { throw Error('暂时无法读取更新状态，请稍后重试。'); }
      if (!response.ok) {
        const error = Error(String(data.error || data.message || '更新请求未完成，请重试。'));
        error.status = response.status;
        throw error;
      }
      if (!data || typeof data.state !== 'string') throw Error('更新状态不完整，请重试。');
      return data;
    } finally { clearTimeout(timeout); }
  }

  function setLocked(value) {
    if (locked === value) return;
    locked = value;
    document.body.classList.toggle('app-update-in-progress', value);
    if (value) {
      // Only this application window is paused; other windows are handled by
      // the backend's final task drain before shutdown.
      const elements = document.querySelectorAll('.workspace, .global-search, .header-actions > :not(#settings-button):not(#app-update-notice), dialog:not(#settings-dialog), #settings-dialog .settings-panel-tab:not([data-tab="app-update"])');
      elements.forEach(element => { inertBefore.set(element, element.inert); element.inert = true; });
      document.querySelectorAll('#settings-dialog .settings-tab:not([data-tab-target="app-update"])').forEach(button => {
        disabledBefore.set(button, button.disabled); button.disabled = true;
      });
    } else {
      for (const [element, previous] of inertBefore) element.inert = previous;
      for (const [button, previous] of disabledBefore) button.disabled = previous;
      inertBefore.clear(); disabledBefore.clear();
    }
  }

  function render() {
    const state = info?.state || 'idle';
    const active = ACTIVE.has(state) || starting || preparing;
    const failed = Boolean(localError) || state === 'error';
    $('#app-update-panel').dataset.state = failed ? 'error' : state;
    $('#app-update-state').textContent = preparing ? '保存中' : starting && !ACTIVE.has(state) ? '准备中' : checking ? '检查中' : (LABELS[state] || '读取中');
    $('#app-update-current').textContent = info?.current_version ? `v${version(info.current_version)}` : '—';
    $('#app-update-latest').textContent = info?.latest_version ? `v${version(info.latest_version)}` : '—';
    $('#app-update-size').textContent = info?.latest_version ? size(info.package_bytes) : '—';
    let message = info?.message || '可以检查是否有新的正式版本。';
    if (info?.preview && state === 'preview') message = '';
    if (info?.preview && state === 'available') message = '发现新版本。';
    if (state === 'error') message = '本次更新未完成，可以重试检查。';
    if (preparing) message = '正在保存当前编辑和本机草稿…';
    else if (starting && !ACTIVE.has(state)) message = '正在准备更新，请保持应用打开…';
    else if (checking) message = '正在检查发布信息…';
    else if (disconnected && locked) message = '正在等待应用重新连接，将自动回到当前地址。';
    $('#app-update-message').textContent = message;
    $('#app-update-message').hidden = !message;
    const notes = info?.latest_version ? String(info.notes || '') : '';
    $('#app-update-changes').hidden = !notes.trim();
    $('#app-update-notes').textContent = notes;
    $('#app-update-error').hidden = !failed;
    $('#app-update-error').textContent = String(localError || (state === 'error' ? info.message : '') || '').slice(0, 700);
    const lastResult = info?.last_result;
    const lastMessage = lastResult && typeof lastResult.message === 'string' ? lastResult.message.trim() : '';
    const lastElement = $('#app-update-last-result');
    if (lastElement.textContent !== (lastMessage ? '上次更新：' + lastMessage : '')) lastElement.textContent = lastMessage ? '上次更新：' + lastMessage : '';
    lastElement.hidden = !lastMessage;
    lastElement.dataset.failed = String(Boolean(lastMessage && lastResult.state !== 'complete'));
    $('#app-update-progress-wrap').hidden = !active && !locked;
    const progress = $('#app-update-progress');
    if (state === 'downloading' && Number.isFinite(Number(info?.progress))) {
      const value = Math.max(0, Math.min(100, Number(info.progress)));
      progress.value = value;
      $('#app-update-progress-text').textContent = `${value}%${info.downloaded_bytes ? ` · ${size(info.downloaded_bytes)} / ${size(info.package_bytes)}` : ''}`;
    } else {
      progress.removeAttribute('value');
      $('#app-update-progress-text').textContent = preparing ? '保存完成后开始更新' : state === 'restarting' || disconnected ? '更新后自动重新打开' : state === 'verifying' ? '正在核对更新包' : '正在准备，请稍候';
    }
    const installVisible = info?.has_update === true && info?.preview !== true;
    $('#app-update-start').hidden = !installVisible;
    $('#app-update-start').disabled = info?.can_update !== true || info?.preview === true || active || locked || checking || submitting;
    $('#app-update-check').disabled = preparing || submitting || checking || (locked && !connectionPaused);
    $('#app-update-check').textContent = connectionPaused ? '重新连接' : failed ? '重试检查' : checking ? '正在检查…' : '检查更新';
    $('#app-update-later').textContent = active || locked ? '收起' : '稍后';
    $('#app-update-protection').textContent = locked
      ? '当前窗口已暂缓编辑。更新完成后会自动重启并返回这里，未提交的内容可从草稿箱恢复。'
      : '';
    $('#app-update-protection').hidden = !locked;
    const noticeKey = version(info?.latest_version);
    const offerNotice = info?.has_update === true && Boolean(noticeKey) && readStored(DISMISS_KEY) !== noticeKey;
    $('#app-update-notice').hidden = !(locked ? !suppressActiveNotice : offerNotice);
    $('#update-badge').textContent = locked ? '更新进行中' : `新版本${noticeKey ? ' v' + noticeKey : ''}`;
    $('#update-badge').title = '打开设置中的软件更新';
  }

  function schedule(delay = 1200) {
    clearTimeout(timer);
    if (!connectionPaused) timer = setTimeout(refresh, delay);
  }

  function rememberRestart() {
    if (resume?.phase === 'waiting') return;
    resume = {phase: 'waiting', previous: version(info?.current_version), target: version(info?.latest_version), address: currentAddress(), at: Date.now()};
    store(RESUME_KEY, resume);
  }

  function finishConnection(data) {
    if (resume?.phase !== 'waiting') return false;
    const now = version(data.current_version);
    const failedResult = ['rolled_back','not_updated','error','recovery_required'].includes(data.last_result?.state);
    if (now && resume.previous && now !== resume.previous && (!resume.target || now === resume.target) && !ACTIVE.has(data.state) && !failedResult) {
      resume = {...resume, phase: 'complete', installed: now};
      store(RESUME_KEY, resume);
      location.replace(safeAddress(resume.address));
      return true;
    }
    if (!submitting && !preparing && now && !ACTIVE.has(data.state) && data.state !== 'checking') {
      // /start publishes its active state before responding. A fresh, idle
      // status after disconnection therefore confirms no install is running.
      localError = failedResult ? '' : `应用已重新连接，当前${now === resume.previous ? '仍' : ''}为 v${now}。可重新检查更新。`;
      starting = false; disconnected = false; connectionSince = 0; connectionPaused = false; setLocked(false);
      resume = null; store(RESUME_KEY, null);
    }
    return false;
  }

  function accept(data) {
    if (finishConnection(data)) return;
    info = data;
    if (ACTIVE.has(data.state)) {
      starting = false;
      setLocked(true);
      rememberRestart();
    } else if (data.state === 'error') {
      starting = false; disconnected = false; connectionSince = 0;
      setLocked(false); resume = null; store(RESUME_KEY, null);
    }
    if (checking && ['available','current','unpublished','unavailable','error'].includes(data.state)) checking = false;
    if (!locked) disconnected = false;
    render();
    if (ACTIVE.has(data.state) || starting || checking || locked) schedule(data.state === 'restarting' ? 1800 : 1000);
    else if (panelOpen()) schedule(10000);
  }

  async function refresh() {
    if (fetching) { schedule(); return; }
    fetching = true;
    try {
      const data = await api();
      if (!locked) localError = '';
      accept(data);
      if (locked && connectionSince && !disconnected) connectionSince = 0;
    } catch (error) {
      if (locked) {
        disconnected = true;
        connectionSince ||= Date.now();
        if (Date.now() - connectionSince > 180000) {
          connectionPaused = true;
          localError = '应用尚未重新连接。可以稍后点击“重新连接”，当前地址已保留。';
        } else schedule(1800);
      } else {
        checking = false;
        if (panelOpen()) localError = '暂时无法读取更新状态，请重试。';
      }
      render();
    } finally { fetching = false; }
  }

  async function check() {
    if (submitting || preparing || checking) return;
    if (connectionPaused) {
      connectionPaused = false; connectionSince = Date.now(); localError = ''; render(); await refresh(); return;
    }
    if (locked) return;
    clearTimeout(timer); submitting = true; checking = true; localError = ''; render();
    try { accept(await api('/check', true)); schedule(800); }
    catch (error) { checking = false; localError = error.status ? error.message : '未能连接更新服务，请重试。'; }
    finally { submitting = false; render(); }
  }

  async function start() {
    if (submitting || preparing || locked || info?.preview === true || info?.can_update !== true) return;
    localError = hooks.busyReason();
    if (localError) { render(); return; }
    connectionSince = 0; connectionPaused = false; disconnected = false; suppressActiveNotice = false;
    clearTimeout(timer); preparing = true; setLocked(true); render();
    let sent = false;
    try {
      await hooks.prepare();
      preparing = false; starting = true; submitting = true;
      rememberRestart(); render(); sent = true;
      accept(await api('/start', true));
      schedule(700);
    } catch (error) {
      if (!sent || error.status) {
        starting = false; setLocked(false); resume = null; store(RESUME_KEY, null);
        localError = error.message || '更新尚未开始，请稍后重试。';
      } else {
        // A lost start response is not proof that installation did not begin.
        disconnected = true; connectionSince = Date.now();
        localError = ''; schedule(1000);
      }
    } finally { preparing = false; submitting = false; render(); }
  }

  function openPanel() {
    if (!$('#settings-dialog').open) $('#settings-button').click();
    $('[data-tab-target="app-update"]').click();
  }
  $('#update-badge').addEventListener('click', openPanel);
  $('#app-update-dismiss').addEventListener('click', () => {
    if (locked) suppressActiveNotice = true;
    if (info?.latest_version) store(DISMISS_KEY, version(info.latest_version));
    render();
  });
  $('[data-tab-target="app-update"]').addEventListener('click', () => { render(); void refresh(); });
  $('#app-update-check').addEventListener('click', check);
  $('#app-update-start').addEventListener('click', start);
  $('#app-update-later').addEventListener('click', () => {
    if (!locked && info?.latest_version) store(DISMISS_KEY, version(info.latest_version));
    $('#settings-dialog').close(); render();
  });
  $('#settings-dialog').addEventListener('close', () => {
    if (!locked && !checking && !ACTIVE.has(info?.state)) clearTimeout(timer);
  });
  $('#settings-button').addEventListener('click', () => {
    if (locked) $('[data-tab-target="app-update"]').click();
  });

  // Document-level shortcuts must not create edits behind an inert workspace.
  window.addEventListener('keydown', event => {
    if (!locked) return;
    if ((event.key === 'Escape' && $('#settings-dialog').open) || event.key === 'Tab' || ['ArrowUp','ArrowDown','ArrowLeft','ArrowRight','PageUp','PageDown','Home','End'].includes(event.key)) return;
    if ((event.ctrlKey || event.metaKey) && ['a','c','f'].includes(event.key.toLowerCase())) return;
    if (event.target.closest(UPDATE_CONTROLS) && !event.ctrlKey && !event.metaKey) return;
    event.preventDefault(); event.stopImmediatePropagation();
  }, true);
  window.addEventListener('click', event => {
    if (locked && !event.target.closest(UPDATE_CONTROLS)) {
      event.preventDefault(); event.stopImmediatePropagation();
    }
  }, true);
  for (const name of ['paste', 'drop', 'beforeinput']) {
    window.addEventListener(name, event => {
      if (locked) { event.preventDefault(); event.stopImmediatePropagation(); }
    }, true);
  }

  if (resume && (typeof resume.at !== 'number' || Date.now() - resume.at > 30 * 60 * 1000)) { resume = null; store(RESUME_KEY, null); }
  if (resume?.phase === 'waiting') { starting = true; disconnected = true; connectionSince = Date.now(); setLocked(true); }
  else if (resume?.phase === 'complete') {
    const installed = resume.installed;
    const address = safeAddress(resume.address);
    resume = null; store(RESUME_KEY, null);
    if (address !== currentAddress()) location.replace(address);
    else if (typeof window.showToast === 'function') window.showToast('软件已更新', `当前版本 v${installed}，可以继续使用。`);
  }
  render();
  void refresh(); // Status only. Network release checks are an explicit action.
})();
