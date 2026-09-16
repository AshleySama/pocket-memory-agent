const state = {
  notes: [],
  taxonomy: { total: 0, categories: [], tags: [] },
  folders: [],
  selectedFolderId: null,
  expandedFolders: new Set(),
  embeddingStatus: null,
  intelligenceStatus: null,
  editingId: null,
  imageDataUrl: null,
  imageName: null,
  selectedDirectory: null,
  filter: {},
  ocrPollTimer: null,
  activeTimers: [],
  pendingScrollId: null,
  selectedNoteId: null,
  resultScope: "note",
  pendingContentSaves: new Map(),
  editorSelection: null,
  allNotes: [],
  documents: [],
  searchDocuments: [],
  documentStatus: null,
  documentPollTimer: null,
  documentReplacementId: null,
  reportSelectionMode: false,
  reportSelection: new Map(),
  reportDraft: null,
  newNoteDraft: null,
  resultSortDirection: "desc",
};

/* ---- 风格定义 ----
   每个风格包含 id（对应 CSS 的 data-theme）和名称。
   新增风格时：1) 在此数组添加条目  2) 在 styles.css 添加 html[data-theme="id"] 变量覆盖
*/
const THEMES = [
  { id: "aqua-memory", name: "泠川" },
  { id: "paper-warm", name: "素笺" },
  { id: "ink-scholar", name: "墨客" },
  { id: "nordic-mint", name: "北野" },
  { id: "midnight-focus", name: "星阑" },
];

/* iOS 风格页签调色板 */
const TAB_COLORS = ["#007AFF", "#34C759", "#FF9500", "#AF52DE", "#FF2D55", "#5AC8FA", "#FFCC00", "#5856D6"];

function tabColor(name) {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) | 0;
  return TAB_COLORS[Math.abs(hash) % TAB_COLORS.length];
}

const TABLER_ICON_PATHS = {
  pinned: '<path d="M9 4v6l-2 4v2h10v-2l-2-4v-6"/><path d="M12 16v5"/><path d="M8 4h8"/>',
  "pinned-off": '<path d="M3 3l18 18"/><path d="M15 4.5l-3.249 3.249m-2.57 1.433l-2.181 .818l-1.5 1.5l7 7l1.5-1.5l.82-2.186m1.43-2.563l3.25-3.251"/><path d="M9 15l-4.5 4.5"/><path d="M14.5 4l5.5 5.5"/>',
  trash: '<path d="M4 7h16"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M5 7l1 12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2l1-12"/><path d="M9 7v-3a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3"/>',
};

function tablerIcon(name, className) {
  return `<svg class="${className}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.35" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${TABLER_ICON_PATHS[name]}</svg>`;
}

function applyTheme(themeId) {
  document.documentElement.dataset.theme = themeId;
  try { localStorage.setItem("pocket-memory-theme", themeId); } catch (e) { /* ignore */ }
}

function savedTheme() {
  try {
    const saved = localStorage.getItem("pocket-memory-theme") || "aqua-memory";
    // 已删除的主题回退到默认，避免样式失效
    const valid = THEMES.some((t) => t.id === saved);
    return valid ? saved : "aqua-memory";
  } catch (e) { return "aqua-memory"; }
}

const $ = (selector) => document.querySelector(selector);

function switchPane(paneId) {
  document.querySelectorAll(".detail-tab").forEach((tab) => {
    tab.classList.toggle("active", tab.dataset.pane === paneId);
  });
  document.querySelectorAll(".detail-pane").forEach((pane) => {
    pane.classList.toggle("active", pane.id === paneId);
  });
}

document.querySelectorAll(".detail-tab").forEach((tab) => {
  tab.addEventListener("click", () => switchPane(tab.dataset.pane));
});

async function request(url, options = {}) {
  const response = await fetch(url, { headers: { "Content-Type": "application/json" }, ...options });
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error(payload.error || "操作失败");
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function syncCachedNote(noteId, patch) {
  const seen = new Set();
  for (const collection of [state.notes, state.allNotes]) {
    for (const note of collection || []) {
      if (note.id !== noteId || seen.has(note)) continue;
      Object.assign(note, patch);
      seen.add(note);
    }
  }
}

async function flushPendingContentSaves() {
  const pending = [...state.pendingContentSaves.values()];
  if (pending.length) await Promise.allSettled(pending);
}

const WORKBENCH_LAYOUT_KEY = "pocket-memory-workbench-layout-v2";
const DEFAULT_WORKBENCH_LAYOUT = { sidebar: null, content: null };
let workbenchLayout = { ...DEFAULT_WORKBENCH_LAYOUT };

function defaultWorkbenchLayout() {
  const workspace = $(".workspace");
  const width = workspace?.clientWidth || window.innerWidth || 1600;
  return { sidebar: Math.round(width * 0.15), content: Math.round(width * 0.40) };
}

function clampWorkbenchLayout(layout = workbenchLayout) {
  const workspace = $(".workspace");
  const defaults = defaultWorkbenchLayout();
  const next = { ...defaults, ...layout };
  if (!workspace || window.innerWidth <= 1050) return next;
  const width = workspace.clientWidth;
  const sidebarMax = Math.max(190, width - 480 - 420);
  next.sidebar = Math.min(Math.max(Number(layout.sidebar) || defaults.sidebar, 190), sidebarMax);
  const contentMax = Math.max(480, width - next.sidebar - 420);
  next.content = Math.min(Math.max(Number(layout.content) || defaults.content, 480), contentMax);
  return Object.fromEntries(Object.entries(next).map(([key, value]) => [key, value == null ? value : Math.round(value)]));
}

function applyWorkbenchLayout(layout, { persist = false } = {}) {
  workbenchLayout = clampWorkbenchLayout(layout);
  const root = document.body;
  root.style.setProperty("--sidebar-width", `${workbenchLayout.sidebar}px`);
  root.style.setProperty("--content-width", `${workbenchLayout.content}px`);
  document.querySelector(".sidebar-resizer")?.setAttribute("aria-valuenow", String(workbenchLayout.sidebar));
  document.querySelector(".content-resizer")?.setAttribute("aria-valuenow", String(workbenchLayout.content));
  if (persist) {
    try { localStorage.setItem(WORKBENCH_LAYOUT_KEY, JSON.stringify(workbenchLayout)); } catch (e) { /* ignore */ }
  }
}

function initializeWorkbenchResizers() {
  try {
    const saved = JSON.parse(localStorage.getItem(WORKBENCH_LAYOUT_KEY) || "null");
    if (saved && typeof saved === "object") workbenchLayout = { ...workbenchLayout, ...saved };
  } catch (e) { /* ignore */ }
  applyWorkbenchLayout(workbenchLayout);

  let drag = null;
  document.querySelectorAll(".column-resizer").forEach((resizer) => {
    resizer.addEventListener("pointerdown", (event) => {
      if (window.innerWidth <= 1050) return;
      event.preventDefault();
      drag = { key: resizer.dataset.resizeColumn, pointerId: event.pointerId };
      resizer.setPointerCapture(event.pointerId);
      document.body.classList.add("resizing-columns");
    });
    resizer.addEventListener("dblclick", () => {
      const next = { ...workbenchLayout, [resizer.dataset.resizeColumn]: defaultWorkbenchLayout()[resizer.dataset.resizeColumn] };
      applyWorkbenchLayout(next, { persist: true });
    });
    resizer.addEventListener("keydown", (event) => {
      const validKeys = ['ArrowLeft', 'ArrowRight', 'Home'];
      if (!validKeys.includes(event.key)) return;
      event.preventDefault();
      const key = resizer.dataset.resizeColumn;
      const step = event.shiftKey ? 48 : 16;
      const next = { ...workbenchLayout };
      if (event.key === "Home") next[key] = defaultWorkbenchLayout()[key];
      else next[key] += event.key === "ArrowLeft" ? -step : step;
      applyWorkbenchLayout(next, { persist: true });
    });
  });
  document.addEventListener("pointermove", (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    const next = { ...workbenchLayout };
    const workspace = $(".workspace");
    if (!workspace) return;
    const x = event.clientX - workspace.getBoundingClientRect().left;
    next[drag.key] = drag.key === "sidebar" ? x : x - workbenchLayout.sidebar;
    applyWorkbenchLayout(next);
  });
  document.addEventListener("pointerup", (event) => {
    if (!drag || event.pointerId !== drag.pointerId) return;
    drag = null;
    document.body.classList.remove("resizing-columns");
    applyWorkbenchLayout(workbenchLayout, { persist: true });
  });
  window.addEventListener("resize", () => applyWorkbenchLayout(workbenchLayout));
}

initializeWorkbenchResizers();

async function downloadFile(url, fallbackFilename) {
  const response = await fetch(url);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.error || "下载失败");
  }
  const disposition = response.headers.get("Content-Disposition") || "";
  const encodedMatch = disposition.match(/filename\*\s*=\s*UTF-8''([^;]+)/i);
  const match = disposition.match(/filename="?([^";]+)"?/i);
  const filename = encodedMatch ? decodeURIComponent(encodedMatch[1]) : (match?.[1] || fallbackFilename);
  const objectUrl = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = filename;
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
  // 部分 WebView 会在点击下载后才异步读取 Blob。过早回收会生成 0KB 文件。
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
  return filename;
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>"']/g, (char) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[char]
  ));
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function searchHighlightTerms() {
  const query = $("#search").value.trim();
  if (!query) return [];
  const terms = new Set();
  const add = (value) => {
    const term = value.trim();
    if (term.length >= 2) terms.add(term);
  };
  add(query);
  for (const token of query.split(/[\s,，。；;:：、"'“”‘’()[\]{}<>《》]+/)) {
    add(token);
    const chineseRuns = token.match(/[\u3400-\u9fff]{2,}/g) || [];
    for (const run of chineseRuns) {
      for (let size = Math.min(4, run.length); size >= 2; size -= 1) {
        for (let index = 0; index <= run.length - size; index += 1) {
          add(run.slice(index, index + size));
        }
      }
    }
  }
  return [...terms].sort((a, b) => b.length - a.length || a.localeCompare(b));
}

function directDocumentSearchHighlightTerms(query = "") {
  // A document search result is not a citation. Highlight only the complete
  // user query here; splitting a Chinese query into two-character fragments
  // makes spreadsheet rows look as though nearly every word matched.
  const term = String(query).trim();
  return term.length >= 3 && term.length <= 80 ? [term] : [];
}

function citationHighlightTerms(evidence = "") {
  // A citation is a precise passage selected by RAG. Keep it intact when
  // opening the source; splitting it into query keywords obscures the actual
  // basis of the answer, especially in long spreadsheets and policy notes.
  const terms = new Set();
  const add = (value) => {
    const term = String(value || "").replace(/\s+/g, " ").trim();
    if (term.length >= 3 && term.length <= 760) terms.add(term);
  };
  const source = String(evidence || "").trim();
  add(source);
  source.split(/\r?\n|[；;。！？!?]/).forEach((line) => {
    add(line);
    // RAG may reconstruct a table row as "核心功能：..." while the source
    // table stores only the cell value. Highlight that value as well, so an
    // opened reference visibly points to the exact evidence rather than an
    // unrelated keyword hit.
    const fieldValue = String(line).split(/[：:]/).slice(1).join("：").trim();
    add(fieldValue);
    const compactFieldValue = fieldValue.replace(/\s+/g, "");
    if (/Agent$/i.test(compactFieldValue)) {
      add(compactFieldValue.replace(/Agent$/i, " Agent"));
    }
    const tableLine = String(line).trim();
    const withoutTableEdges = tableLine.replace(/^\|\s*/, "").replace(/\s*\|$/, "").trim();
    add(withoutTableEdges);
    add(withoutTableEdges.replace(/^[-*+]\s+/, ""));
    withoutTableEdges.split("|").forEach((cell) => {
      add(cell);
      add(String(cell).replace(/^[-*+]\s+/, ""));
    });
  });
  return [...terms].sort((a, b) => b.length - a.length || a.localeCompare(b));
}

function scrollToCitation(root) {
  requestAnimationFrame(() => {
    root?.querySelector("mark")?.scrollIntoView({ behavior: "smooth", block: "center" });
  });
}

function highlightText(value = "", terms = []) {
  const text = String(value);
  if (!terms.length || !text) return escapeHtml(text);
  const pattern = new RegExp(terms.map(escapeRegExp).join("|"), "giu");
  let cursor = 0;
  let html = "";
  text.replace(pattern, (match, offset) => {
    html += escapeHtml(text.slice(cursor, offset));
    html += `<mark>${escapeHtml(match)}</mark>`;
    cursor = offset + match.length;
    return match;
  });
  html += escapeHtml(text.slice(cursor));
  return html;
}

// 笔记标题集合缓存：renderWikiLinks 据此判断 [[标题]] 是否失效（目标笔记不存在）。
// 在 loadNotes 赋值 state.allNotes 后通过 invalidateNoteTitleSet() 失效重建。
let _noteTitleSet = null;
function getNoteTitleSet() {
  if (!_noteTitleSet) {
    // escapeHtml 与 renderWikiLinks 收到的转义后 title 保持一致，确保匹配正确
    _noteTitleSet = new Set((state.allNotes || []).map((n) => escapeHtml(n.title || "")));
  }
  return _noteTitleSet;
}
function invalidateNoteTitleSet() { _noteTitleSet = null; }

function renderWikiLinks(html) {
  // 将 [[标题]] 转为可点击的只读参考笔记链接。目标笔记不存在时标记为失效链接（红色虚线）。
  const titles = getNoteTitleSet();
  return html.replace(/\[\[([^\]]+)\]\]/g, (match, title) => {
    const trimmed = title.trim();
    const broken = titles.size > 0 && !titles.has(trimmed);
    const cls = broken ? "wiki-link wiki-link-broken" : "wiki-link";
    const dataAttrs = broken ? ' data-broken="1"' : "";
    const hint = broken ? "参考笔记不存在" : "点击阅读参考笔记";
    return `<a class="${cls}" href="javascript:void(0)" data-wiki="${escapeHtml(trimmed)}" title="${hint}"${dataAttrs}>${escapeHtml(trimmed)}</a>`;
  });
}

function renderInlineNoteContent(text, terms = []) {
  // 正文源格式允许极小的受控 HTML 集合，用于保留编辑器中的行内格式。
  // 先逐段转义普通文本，只有这里白名单内的标签才会恢复为 HTML，避免把笔记内容直接注入页面。
  const source = String(text || "");
  const tagPattern = /<\/(?:strong|em|u|span)>|<(strong|em|u)>|<span style="(color:\s*#[0-9a-f]{6}(?:;\s*background-color:\s*#[0-9a-f]{6})?|background-color:\s*#[0-9a-f]{6}(?:;\s*color:\s*#[0-9a-f]{6})?)">/gi;
  let html = "";
  let cursor = 0;
  let match;
  const renderText = (value) => renderWikiLinks(highlightText(value, terms));

  while ((match = tagPattern.exec(source))) {
    html += renderText(source.slice(cursor, match.index));
    const token = match[0].toLowerCase();
    if (token.startsWith("</")) {
      html += token;
    } else if (match[1]) {
      html += `<${match[1].toLowerCase()}>`;
    } else {
      const style = match[2]
        .split(";")
        .map((part) => part.trim())
        .filter(Boolean)
        .map((part) => part.replace(/\s*:\s*/, ":"))
        .join(";");
      html += `<span style="${style}">`;
    }
    cursor = tagPattern.lastIndex;
  }
  return html + renderText(source.slice(cursor));
}

function renderNoteContent(content, terms = []) {
  // 渲染笔记正文：将表格块渲染为 HTML <table>，其余文本走高亮+双链。
  // 支持两种表格格式：
  //   1. Markdown 表格：每行以 | 开头并以 | 结尾
  //   2. Tab 分隔表格：每行含 \t，且至少 2 行有相同列数（兼容旧笔记和 Excel 粘贴）
  const lines = String(content).split("\n");
  let html = "";
  let tableLines = [];
  let tableType = null; // "markdown" | "tab"

  const flushTable = () => {
    if (tableLines.length === 0) return;
    let rows;
    if (tableType === "markdown") {
      // Markdown 表格：去掉首尾 |，按 | 分割
      rows = tableLines.map((line) => {
        const trimmed = line.trim().replace(/^\|/, "").replace(/\|$/, "");
        return trimmed.split("|").map((cell) => cell.trim());
      });
      // 第二行是分隔行（---），跳过
      rows = rows.filter((row, i) =>
        !(i === 1 && row.every((cell) => /^:?-{2,}:?$/.test(cell)))
      );
    } else {
      // Tab 分隔表格：按 \t 分割
      rows = tableLines.map((line) => line.split("\t").map((cell) => cell.trim()));
    }
    html += '<table class="note-table"><tbody>';
    rows.forEach((row, i) => {
      const tag = i === 0 ? "th" : "td";
      const cells = row.map((cell) => `<${tag}>${highlightText(cell, terms)}</${tag}>`).join("");
      html += `<tr>${cells}</tr>`;
    });
    html += "</tbody></table>";
    tableLines = [];
    tableType = null;
  };

  let textBuffer = [];
  const flushText = () => {
    if (textBuffer.length === 0) return;
    html += `<p>${renderInlineNoteContent(textBuffer.join("\n"), terms)}</p>`;
    textBuffer = [];
  };

  const isMarkdownTableRow = (line) => {
    const t = line.trim();
    return t.startsWith("|") && t.endsWith("|") && t.length >= 3;
  };
  const isTabTableRow = (line) => line.includes("\t") && line.trim().length > 0;

  for (const line of lines) {
    const mdRow = isMarkdownTableRow(line);
    const tabRow = isTabTableRow(line);
    if (mdRow && (!tableType || tableType === "markdown")) {
      flushText();
      tableType = "markdown";
      tableLines.push(line);
    } else if (tabRow && (!tableType || tableType === "tab")) {
      flushText();
      tableType = "tab";
      tableLines.push(line);
    } else {
      flushTable();
      textBuffer.push(line);
    }
  }
  flushTable();
  flushText();
  return html;
}

function queryString(values) {
  return new URLSearchParams(Object.entries(values).filter(([, value]) => value)).toString();
}

function withCacheBuster(url) {
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}_=${Date.now()}`;
}

function excerpt(text = "", length = 180) {
  const clean = String(text).replace(/\s+/g, " ").trim();
  if (!clean) return "";
  return clean.length > length ? `${clean.slice(0, length)}...` : clean;
}

function active(filter) {
  return JSON.stringify(state.filter) === JSON.stringify(filter) ? "active" : "";
}

function reportSourceKey(kind, id) {
  return `${kind}:${Number(id)}`;
}

function selectedReportSources() {
  return [...state.reportSelection.values()];
}

function isReportSourceSelected(kind, id) {
  return state.reportSelection.has(reportSourceKey(kind, id));
}

function toggleReportSource(kind, id) {
  const key = reportSourceKey(kind, id);
  if (state.reportSelection.has(key)) {
    state.reportSelection.delete(key);
    return;
  }
  if (state.reportSelection.size >= 8) {
    showToast("最多选择 8 项资料", "建议将资料拆分为两个专题后分别生成报告");
    return;
  }
  state.reportSelection.set(key, { kind, id: Number(id) });
}

function renderReportSelectionBar() {
  const bar = $("#report-selection-bar");
  if (!bar) return;
  const count = state.reportSelection.size;
  bar.classList.toggle("hidden", !state.reportSelectionMode);
  $("#report-selection-count").textContent = `已选 ${count} 项资料`;
  $("#report-next").disabled = count < 1;
}

function renderFolderTree(folders, depth = 0) {
  return folders.map((folder) => {
    const isExpanded = state.expandedFolders.has(folder.id);
    const isActive = state.filter.folder_id === folder.id ? "active" : "";
    const hasChildren = folder.children && folder.children.length > 0;
    return `
      <div class="folder-node" data-folder-id="${folder.id}">
        <div class="folder-row ${isActive}" data-drop-folder-id="${folder.id}" style="padding-left:${12 + depth * 14}px">
          ${hasChildren ? `<button class="folder-toggle" data-toggle-folder="${folder.id}" type="button">${isExpanded ? "▾" : "▸"}</button>` : `<span class="folder-toggle-placeholder"></span>`}
          <button class="folder-name" data-folder-id="${folder.id}" type="button" title="点击筛选，右键菜单">
            <span>${escapeHtml(folder.name)}</span><b>${folder.note_count}</b>
          </button>
        </div>
        ${hasChildren && isExpanded ? `<div class="folder-children">${renderFolderTree(folder.children, depth + 1)}</div>` : ""}
      </div>`;
  }).join("");
}

function renderRecentNav() {
  // 取最近 5 条按 updated_at 倒序，排除子页签（parent_note_id 非空的）
  // 使用 allNotes 而非 notes，避免搜索/筛选影响最近编辑列表
  const recent = [...(state.allNotes.length ? state.allNotes : state.notes)]
    .filter((n) => !n.parent_note_id)
    .sort((a, b) => new Date(b.updated_at || b.created_at) - new Date(a.updated_at || a.created_at))
    .slice(0, 5);
  const nav = $("#recent-nav");
  if (!nav) return;
  if (!recent.length) {
    nav.innerHTML = `<p class="muted" style="padding:6px 8px">还没有笔记</p>`;
    return;
  }
  nav.innerHTML = recent.map((note) => {
    const ts = note.updated_at || note.created_at;
    const rel = relativeTime(ts);
    const title = escapeHtml(note.title || "（无标题）");
    return `<button class="nav-item recent-item" data-recent-id="${note.id}" title="${title}">
      <span class="recent-title">${title}</span>
      <b class="recent-time">${rel}</b>
    </button>`;
  }).join("");
}

function relativeTime(iso) {
  // 简易相对时间："刚刚 / 5分钟前 / 3小时前 / 2天前 / MM-DD"
  if (!iso) return "";
  const t = new Date(iso);
  const diff = (Date.now() - t.getTime()) / 1000;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)}分前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}时前`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)}天前`;
  return `${String(t.getMonth() + 1).padStart(2, "0")}-${String(t.getDate()).padStart(2, "0")}`;
}

async function jumpToNote(noteId) {
  // 若笔记不在当前筛选结果中，先清空筛选再 refresh，最后滚动定位
  const inView = state.notes.some((n) => n.id === noteId);
  if (!inView) {
    state.filter = {};
    $("#search").value = "";
    await refresh();
  }
  // 等待渲染后定位
  requestAnimationFrame(() => {
    setTimeout(() => scrollToNote(noteId), 80);
  });
}

function renderSidebar() {
  // 最近编辑列表：取最近 5 条按 updated_at 倒序，主笔记优先（排除子页签）
  renderRecentNav();

  $("#category-nav").innerHTML = `
    <button class="nav-item ${active({})}" data-filter="all">
      <span>全部笔记</span><b>${state.taxonomy.total}</b>
    </button>
    ${state.folders.length ? renderFolderTree(state.folders) : `<p class="muted" style="padding:8px">还没有分区，点击右上 + 创建</p>`}`;

  $("#tag-nav").innerHTML = state.taxonomy.tags.slice(0, 24).map((tag) => `
    <button class="tag-filter ${active({ tag: tag.name })}" data-tag="${escapeHtml(tag.name)}">
      #${escapeHtml(tag.name)} <b>${tag.count}</b>
    </button>`).join("") || `<p class="muted">还没有标签</p>`;

  const favorites = (state.allNotes || []).filter((note) => note.favorite && !note.parent_note_id);
  $("#favorite-count").textContent = String(favorites.length);
  $("#favorite-nav").innerHTML = favorites.length
    ? favorites.map((note) => `<button class="nav-item favorite-item" data-favorite-id="${note.id}" title="${escapeHtml(note.title)}"><span>♥ ${escapeHtml(note.title || "未命名笔记")}</span></button>`).join("")
    : `<p class="muted" style="padding:6px 8px">暂未收藏笔记</p>`;

  $("#clear-filter").classList.toggle("hidden", Object.keys(state.filter).length === 0);
  const embedding = state.embeddingStatus;
  const intelligence = state.intelligenceStatus;
  const indexed = Number(embedding?.indexed || 0);
  const total = Number(embedding?.total || 0);
  const embeddingText = !embedding
    ? "资料检索：正在检查"
    : !embedding.available
      ? "资料检索：暂不可用"
      : total === 0
        ? "资料检索：导入资料后自动建立"
        : indexed >= total
          ? "资料检索：已就绪"
          : `资料检索：正在建立（${indexed}/${total}）`;
  const pending = Number(intelligence?.pending || 0);
  const intelligenceText = intelligence?.available && pending > 0
    ? `自动整理：正在处理 ${pending} 篇`
    : "";
  $("#embedding-status").textContent = [embeddingText, intelligenceText].filter(Boolean).join("\n");

  renderStatsPanel();
  populateFolderSelect();
}

function renderTagGovernance(data) {
  const content = $("#tag-governance-content");
  if (!content) return;
  const core = data.core_tags || [];
  const rare = data.rare_tags || [];
  const suggestions = data.suggestions || [];
  const renderTag = (tag) => `<span class="managed-tag">#${escapeHtml(tag.name)} <b>${tag.count}</b></span>`;
  content.innerHTML = `
    <section class="tag-governance-section">
      <div class="tag-governance-title"><h3>标签词表</h3><span>${data.total || 0} 个标签</span></div>
      <p>使用次数不少于 2 次的标签会进入模型复用词表。</p>
      <div class="managed-tag-list">${core.map(renderTag).join("") || '<span class="muted">还没有形成常用标签</span>'}</div>
    </section>
    <section class="tag-governance-section">
      <div class="tag-governance-title"><h3>合并建议</h3><span>${suggestions.length} 项待确认</span></div>
      ${suggestions.length ? `<div class="tag-merge-list">${suggestions.map((item) => `
        <div class="tag-merge-row">
          <span>#${escapeHtml(item.from)}</span><span class="tag-merge-arrow">→</span><strong>#${escapeHtml(item.to)}</strong>
          <button class="ghost tag-merge-btn" type="button" data-tag-from="${escapeHtml(item.from)}" data-tag-to="${escapeHtml(item.to)}">合并</button>
        </div>`).join("")}</div>` : '<p class="muted">没有需要人工确认的相近标签。</p>'}
    </section>
    <section class="tag-governance-section">
      <div class="tag-governance-title"><h3>低频标签</h3><span>仅使用 1 次</span></div>
      <div class="managed-tag-list">${rare.map(renderTag).join("") || '<span class="muted">没有低频标签</span>'}</div>
    </section>`;
}

async function openTagManager() {
  const dialog = $("#tag-manager-dialog");
  const content = $("#tag-governance-content");
  if (!dialog || !content) return;
  if (!dialog.open) dialog.showModal();
  content.innerHTML = '<p class="muted">正在读取标签词表...</p>';
  try {
    renderTagGovernance(await request("/api/tags/governance"));
  } catch (error) {
    content.innerHTML = `<p class="ocr-error">${escapeHtml(error.message)}</p>`;
  }
}

function renderStatsPanel() {
  const statsEl = $("#stats-panel");
  if (!statsEl) return;
  const now = new Date();
  const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const weekStart = new Date(todayStart.getTime() - 6 * 24 * 60 * 60 * 1000);
  const fmt = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} 00:00:00`;
  const todaySince = fmt(todayStart);
  const weekSince = fmt(weekStart);
  // The welcome note is visible guidance, not user-created knowledge.
  const userNotes = state.notes.filter((note) => note.source_type !== "welcome");
  const weekCount = userNotes.filter((note) => new Date(note.created_at) >= weekStart).length;
  const todayCount = userNotes.filter((note) => new Date(note.created_at) >= todayStart).length;
  const categoryCount = state.taxonomy.categories.length;
  const tagCount = state.taxonomy.tags.length;
  const todayActive = state.filter.since === todaySince ? " active" : "";
  const weekActive = state.filter.since === weekSince ? " active" : "";
  statsEl.innerHTML = `
    <div class="stat-item">
      <span class="stat-value">${state.taxonomy.total}</span>
      <span class="stat-label">总笔记</span>
    </div>
    <div class="stat-item clickable${todayActive}" data-since="${todaySince}" title="点击筛选今日笔记">
      <span class="stat-value">${todayCount}</span>
      <span class="stat-label">今日</span>
    </div>
    <div class="stat-item clickable${weekActive}" data-since="${weekSince}" title="点击筛选本周笔记">
      <span class="stat-value">${weekCount}</span>
      <span class="stat-label">本周</span>
    </div>
    <div class="stat-item">
      <span class="stat-value">${categoryCount}</span>
      <span class="stat-label">分类</span>
    </div>
    <div class="stat-item">
      <span class="stat-value">${tagCount}</span>
      <span class="stat-label">标签</span>
    </div>`;
}

function renderAiStatus(note) {
  if (!note.ai_status || note.ai_status === "not_requested") return "";
  if (note.ai_status === "done") {
    const reason = note.ai_reason || "AI已整理";
    return `<span class="ai-chip done" title="${escapeHtml(reason)}">${escapeHtml(reason)}</span>`;
  }
  const labels = {
    pending: "待整理",
    processing: "整理中",
    failed: "整理失败",
    unavailable: "整理未启用",
  };
  return `<span class="ai-chip ${note.ai_status}">${escapeHtml(labels[note.ai_status] || note.ai_status)}</span>`;
}

function renderAiSummary(note) {
  if (note.ai_status !== "done") return "";
  const summary = String(note.ai_reason || "").trim();
  if (!summary || summary === "AI已整理") return "";
  return `<span class="result-note-ai-summary" title="AI 整理说明：${escapeHtml(summary)}">${escapeHtml(summary)}</span>`;
}

function resultPreviewText(value = "") {
  // The editor preserves a small formatting whitelist. A list card is only an
  // overview, so formatting markup must never leak into its plain-text excerpt.
  return String(value)
    .replace(/<\/?(?:strong|em|u|span)(?:\s+[^>]*)?>/gi, "")
    .replace(/\s+/g, " ")
    .trim();
}

function renderSearchReason(note) {
  if (note.search_reason !== "semantic" || note.search_score == null) return "";
  return `<span class="score-chip" title="向量相似度，当前低于 0.50 不返回">语义 ${Number(note.search_score).toFixed(2)}</span>`;
}

function renderAnswer(payload, question) {
  const kind = payload.kind || "qa";
  const data = payload.data || {};
  if (kind === "stats" || kind === "taxonomy") {
    $("#result-count").textContent = "即时结果";
  } else {
    $("#result-count").textContent = `参考 ${(payload.sources || []).length} 条`;
  }
  const engineLabel = payload.engine === "llama-index" ? "LlamaIndex 实验解答" : "智能解答";
  const intentLabel = INTENT_LABELS[kind] || "智能问答";
  const timing = payload.timing || {};
  const elapsed = timing.total_seconds != null ? ` · ${Number(timing.total_seconds).toFixed(2)}s` : "";
  const timingTitle = timing.first_token_seconds != null
    ? `首字 ${Number(timing.first_token_seconds).toFixed(2)}s；检索 ${Number(timing.retrieval_seconds || 0).toFixed(2)}s；生成 ${Number(timing.generation_seconds || 0).toFixed(2)}s`
    : (timing.retrieval_seconds != null ? `检索 ${Number(timing.retrieval_seconds).toFixed(2)}s` : "");
  const expandedTerms = (payload.expanded_terms || []).filter((t) => t && t !== question);
  const expandedHtml = expandedTerms.length
    ? `<div class="expanded-terms">查询扩展：<span>${expandedTerms.map(escapeHtml).join("</span> <span>")}</span></div>`
    : "";
  const dataHtml = renderIntentData(kind, data);
  $("#answer-panel").innerHTML = `
    <div class="answer-header">
      <div>
        <p class="eyebrow" title="${escapeHtml(timingTitle)}"><span class="intent-badge">${escapeHtml(intentLabel)}</span> ${escapeHtml(engineLabel)}${escapeHtml(elapsed)}</p>
        <h2>${escapeHtml(question)}</h2>
      </div>
      <div class="answer-header-actions"><button id="close-answer" class="ghost" type="button">关闭</button></div>
    </div>
    ${expandedHtml}
    ${dataHtml}
    <div class="answer-response">
      <img class="answer-cat-mark" src="/brand/pocket-memory-small-logo.png" alt="智能问答标识">
      <div class="answer-body">${escapeHtml(payload.answer || "没有生成回答。")}</div>
    </div>
    ${(payload.sources || []).length ? `
      <div class="answer-sources">
        ${(payload.sources || []).map((source) => {
          const reasonParts = [];
          if (source.reason) reasonParts.push(escapeHtml(source.reason));
          if (source.rank_score != null) reasonParts.push(`综合 ${Number(source.rank_score).toFixed(2)}`);
          const chunkOrders = Array.isArray(source.chunk_orders) && source.chunk_orders.length
            ? [...new Set(source.chunk_orders.map((order) => Number(order) + 1))]
            : (Number.isInteger(source.chunk_order) ? [Number(source.chunk_order) + 1] : []);
          const chunkLabel = source.type === "document" && chunkOrders.length
            ? `<span class="source-chunk">切片 ${chunkOrders.join("、")}</span>`
            : "";
          const evidenceCount = Array.isArray(source.evidence_locations) && source.evidence_locations.length > 1
            ? `<span class="source-chunk">含 ${source.evidence_locations.length} 处证据</span>`
            : "";
          const evidenceTerms = String(source.evidence || "").split(/\r?\n/).map((term) => term.trim()).filter(Boolean);
          const contextTerms = [...evidenceTerms, ...(source.matched_terms || [])].filter(Boolean);
          return `
           <div class="answer-source">
              <b>[${source.index}] ${escapeHtml(source.title)}</b>
              <em>${reasonParts.join("；")}</em>
              <div class="source-reference-meta">${chunkLabel}${evidenceCount}${source.location ? `<span class="source-location">${escapeHtml(source.location)}</span>` : ""}</div>
              ${source.url ? `<a class="source-link" href="javascript:void(0)" data-doc-url="${escapeHtml(source.url)}" data-doc-title="${escapeHtml(source.title)}" data-doc-evidence="${escapeHtml(source.evidence || "")}">打开参考文档</a>` : ""}
             ${source.evidence ? `<div class="evidence-box"><strong>证据句</strong>${highlightText(source.evidence, source.matched_terms || [])}</div>` : ""}
             <p class="source-excerpt"><span>${evidenceCount ? "同一来源上下文" : "同一切片上下文"}</span>${highlightText(source.excerpt, contextTerms)}</p>
           </div>`;
        }).join("")}
      </div>` : ""}`;
  const closeAnswer = () => {
    $("#answer-panel").innerHTML = `<div class="empty-guide"><p class="muted">在搜索框输入问题，点击「智能问答」<br>会基于你的本地笔记生成回答。</p></div>`;
    $("#result-count").textContent = "";
  };
  $("#close-answer").addEventListener("click", closeAnswer);
}

const INTENT_LABELS = {
  time: "时间查询", stats: "统计", taxonomy: "分类体系", metadata: "元数据筛选",
  list: "笔记列表", summary: "摘要", compare: "对比", entity: "实体定位",
  related: "关联推荐", qa: "智能问答",
};

function renderIntentData(kind, data) {
  if (!data || typeof data !== "object") return "";
  if (kind === "stats" || kind === "taxonomy") {
    const cats = data.categories;
    const tags = data.tags;
    let html = "";
    if (Array.isArray(cats) && cats.length) {
      const maxCount = Math.max(...cats.map((c) => c.count || 0), 1);
      html += `<div class="intent-data-card"><h4>分类分布</h4>${cats.slice(0, 8).map((c) => `
        <div class="intent-bar-row">
          <span class="intent-bar-label">${escapeHtml(c.name)}</span>
          <div class="intent-bar-track"><div class="intent-bar-fill" style="width:${Math.round((c.count / maxCount) * 100)}%"></div></div>
          <span class="intent-bar-count">${c.count || 0}</span>
        </div>`).join("")}</div>`;
    }
    if (Array.isArray(tags) && tags.length) {
      html += `<div class="intent-data-card"><h4>标签</h4><div class="intent-tag-cloud">${tags.slice(0, 20).map((t) =>
        `<span class="intent-tag">${escapeHtml(t.name)}<em>${t.count || 0}</em></span>`).join("")}</div></div>`;
    }
    return html ? `<div class="intent-data-wrap">${html}</div>` : "";
  }
  return "";
}

function renderTags(tags = [], terms = [], noteId = null) {
  const visible = tags.slice(0, 2);
  return visible.map((tag, i) => {
    const editable = noteId != null ? `data-action="edit-tag" data-id="${noteId}" data-tag-index="${i}" title="点击编辑标签"` : "";
    const remove = noteId != null
      ? `<button class="tag-remove" data-action="remove-tag" data-id="${noteId}" data-tag="${escapeHtml(tag)}" type="button" title="删除标签" aria-label="删除标签 ${escapeHtml(tag)}">×</button>`
      : "";
    return `<span class="tag-chip editable" ${editable}><span>#${highlightText(tag, terms)}</span>${remove}</span>`;
  }).join("");
}

function renderOcr(note, terms = []) {
  if (!note.attachment_path || note.ocr_status === "not_applicable") return "";
  const labels = {
    pending: "等待 OCR",
    processing: "正在识别图片文字",
    done: "OCR 文字",
    failed: "OCR 识别失败",
    unavailable: "OCR 未启用",
  };
  return `
    <div class="ocr-panel" data-note-id="${note.id}">
      <span class="ocr-status">${escapeHtml(labels[note.ocr_status] || note.ocr_status)}</span>
      <div class="ocr-content" hidden>
        ${note.ocr_text ? `<pre>${highlightText(note.ocr_text, terms)}</pre>` : ""}
        ${note.ocr_error ? `<p class="ocr-error">${escapeHtml(note.ocr_error)}</p>` : ""}
      </div>
    </div>`;
}

function renderRecallCard() {
  // 仅在无搜索、无筛选时显示每日回顾
  const query = $("#search").value.trim();
  if (query || Object.keys(state.filter).length > 0) return "";

  const now = new Date();
  const todayMonthDay = `${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  const thisYear = now.getFullYear();

  const recalls = [];
  for (const note of state.notes) {
    const d = new Date(note.created_at);
    if (isNaN(d)) continue;
    const md = `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    if (md === todayMonthDay && d.getFullYear() < thisYear) {
      const yearsAgo = thisYear - d.getFullYear();
      recalls.push({ note, yearsAgo });
    }
  }
  if (recalls.length === 0) return "";
  recalls.sort((a, b) => b.yearsAgo - a.yearsAgo);
  const top = recalls.slice(0, 3);

  return `
    <section class="recall-card card">
      <div class="recall-header">
        <span class="recall-icon">🗓️</span>
        <h2>那年今日</h2>
      </div>
      <div class="recall-list">
        ${top.map(({ note, yearsAgo }) => `
          <div class="recall-item">
            <span class="recall-years">${yearsAgo}年前</span>
            <div class="recall-content">
              <strong>${escapeHtml(note.title)}</strong>
              <p class="muted">${escapeHtml(excerpt(note.content || note.ocr_text || "", 60))}</p>
            </div>
          </div>
        `).join("")}
      </div>
    </section>`;
}

function renderNoteHtml(note, terms) {
  const preview = note.matched_excerpt || excerpt(note.content || note.ocr_text || (note.attachment_path ? "图片笔记，展开后查看 OCR 文字。" : ""));
  const subcategoryChip = note.subcategory
    ? `<span class="category-chip editable" data-action="edit-category" data-id="${note.id}" data-field="subcategory" title="点击编辑小分类">${highlightText(note.subcategory, terms)}</span>`
    : `<span class="category-chip editable muted-chip" data-action="edit-category" data-id="${note.id}" data-field="subcategory" title="点击添加小分类">+ 小分类</span>`;
  return `
    <article class="note${note.pinned ? " pinned" : ""}">
      <div class="note-header">
        <h3 class="editable-note-title" data-action="edit-title" data-id="${note.id}" title="点击编辑标题">${note.pinned ? `${tablerIcon("pinned", "pin-title-icon")} ` : ""}${highlightText(note.title, terms)}</h3>
        <div class="note-header-actions">
          <button class="ghost note-action-btn scroll-top-btn" data-action="scroll-editor-top" data-id="${note.id}" type="button" title="返回笔记顶部" aria-label="返回笔记顶部">↑</button>
          <button class="ghost note-action-btn danger delete-note-btn" data-action="delete" data-id="${note.id}" type="button" title="删除笔记" aria-label="删除笔记">${tablerIcon("trash", "trash-icon")}</button>
          <button class="ghost note-action-btn pin-btn ${note.pinned ? "active" : ""}" data-action="pin" data-id="${note.id}" type="button" title="${note.pinned ? "取消置顶" : "置顶"}" aria-label="${note.pinned ? "取消置顶" : "置顶"}">${tablerIcon("pinned", "pin-icon")}</button>
          <button class="ghost note-action-btn favorite-btn ${note.favorite ? "active" : ""}" data-action="favorite" data-id="${note.id}" type="button" title="${note.favorite ? "取消收藏" : "收藏"}" aria-label="${note.favorite ? "取消收藏" : "收藏"}">${note.favorite ? "♥" : "♡"}</button>
          <button class="ghost note-action-btn" data-action="intelligence" data-id="${note.id}" title="AI 重新整理">重新整理</button>
          <time title="最后编辑时间">${escapeHtml((note.updated_at || note.created_at).slice(11, 16))}</time>
        </div>
      </div>
      <div class="note-meta">
        <span class="category-chip editable" data-action="edit-category" data-id="${note.id}" data-field="category" title="点击编辑大分类">${highlightText(note.category, terms)}</span>
        ${subcategoryChip}
        ${renderTags(note.tags || [], terms, note.id)}
        ${note.backlink_count ? `<span class="backlink-chip" title="${note.backlink_count} 篇笔记引用了此笔记">↩ ${note.backlink_count}</span>` : ""}
        ${renderSearchReason(note)}
        ${renderAiStatus(note)}
        ${note.source_url ? `<a class="source-link" href="javascript:void(0)" data-doc-url="${escapeHtml(note.source_url)}" data-doc-title="${escapeHtml(note.title)}">${escapeHtml(note.source_location || "参考文档")}</a>` : ""}
      </div>
      ${note.attachment_path ? `<img class="note-image" src="/data/${escapeHtml(note.attachment_path)}" alt="笔记图片">` : ""}
      <div class="note-tabs-bar" data-note-id="${note.id}" data-active-tab="${note.id}"></div>
      ${preview ? `<div class="note-excerpt">${renderWikiLinks(highlightText(preview, terms))}</div>` : ""}
      <details class="note-detail">
        <summary>展开全文与操作</summary>
        <div class="editor-detail-body"><div class="editor-detail-inner">
          <div class="note-tab-content" data-note-id="${note.id}">
            <div class="note-content editable" contenteditable="true" data-note-id="${note.id}" data-field="content" data-placeholder="点击此处开始编辑..." title="点击直接编辑">${note.content ? renderNoteContent(note.content, terms) : ""}</div>
          </div>
          ${renderOcr(note, terms)}
          <div class="note-footer-actions">
            ${note.attachment_path && note.ocr_status !== "not_applicable" ? `<button class="ghost" data-action="toggle-ocr" data-id="${note.id}" type="button">展开OCR内容</button>` : ""}
            ${note.attachment_path && note.ocr_status !== "not_applicable" ? `<button class="ghost" data-action="ocr" data-id="${note.id}">重新识别</button>` : ""}
            <button class="ghost" data-action="versions" data-id="${note.id}" type="button" title="查看历史版本与差异对比">历史版本</button>
          </div>
        </div></div>
      </details>
    </article>`;
}

function renderReferenceNoteHtml(note, terms = []) {
  const tags = (note.tags || []).slice(0, 2).map((tag) => (
    `<span class="tag-chip"><span>#${highlightText(tag, terms)}</span></span>`
  )).join("");
  const ocr = note.attachment_path && note.ocr_status !== "not_applicable" && note.ocr_text
    ? `<section class="reference-ocr"><strong>图片识别文字</strong><pre>${highlightText(note.ocr_text, terms)}</pre></section>`
    : "";
  return `
    <article class="note reference-note" aria-label="参考笔记只读预览">
      <div class="note-meta reference-note-meta">
        <span class="category-chip">${highlightText(note.category || "未分类", terms)}</span>
        ${note.subcategory ? `<span class="category-chip">${highlightText(note.subcategory, terms)}</span>` : ""}
        ${tags}
      </div>
      ${note.attachment_path ? `<img class="note-image" src="/data/${escapeHtml(note.attachment_path)}" alt="笔记图片">` : ""}
      <div class="note-tabs-bar" data-note-id="${note.id}" data-active-tab="${note.id}"></div>
      <div class="note-tab-content" data-note-id="${note.id}">
        <div class="note-content reference-note-content">${note.content ? renderNoteContent(note.content, terms) : "<p class='muted'>此页签暂无正文。</p>"}</div>
      </div>
      ${ocr}
    </article>`;
}

function canManuallyReorderNotes() {
  return !state.reportSelectionMode
    && state.resultScope === "note"
    && !$("#search").value.trim();
}

function renderResultNote(note, terms, index) {
  const previewSource = note.matched_excerpt || note.content || note.ocr_text || (note.attachment_path ? "图片笔记，展开后查看 OCR 文字。" : "");
  const preview = excerpt(resultPreviewText(previewSource), 108);
  const isActive = Number(note.id) === Number(state.selectedNoteId);
  const matches = note.search_matches || [];
  const primaryMatch = matches[0] || note;
  const categoryLocation = [note.category, note.subcategory].filter(Boolean).join(" / ") || "未归类";
  const matchLocation = matches.length > 1
    ? `${matches.length} 个命中片段${primaryMatch.source_location ? ` · ${primaryMatch.source_location}` : ""}`
    : categoryLocation;
  const tabMatch = String(primaryMatch.source_url || "").match(/[?&]tab_id=(\d+)/);
  const resultTabId = tabMatch ? Number(tabMatch[1]) : null;
  const isWelcome = note.source_type === "welcome";
  const metaParts = [
    escapeHtml(matchLocation),
    renderAiSummary(note),
    note.updated_at ? `<i>${escapeHtml(note.updated_at.slice(0, 10))}</i>` : "",
  ].filter(Boolean);
  const selectable = state.reportSelectionMode && !isWelcome;
  const selectedForReport = selectable && isReportSourceSelected("note", note.id);
  // 搜索命中、欢迎笔记和报告选择模式不支持拖拽；在分区视图中仍可拖到左侧其他分区。
  const draggable = canManuallyReorderNotes() && !isWelcome;
  const dragAttributes = draggable ? ` data-drag-note-id="${note.id}" draggable="true"` : "";
  return `
    <article class="result-note${isWelcome ? " welcome-result-card" : ""}${isActive ? " active" : ""}${selectedForReport ? " report-source-selected" : ""}" data-note-id="${note.id}"${dragAttributes}>
      ${selectable ? `<button class="report-source-toggle ${selectedForReport ? "selected" : ""}" data-report-source-kind="note" data-report-source-id="${note.id}" type="button" aria-label="${selectedForReport ? "取消选择" : "选择资料"}" title="${selectedForReport ? "取消选择" : "选择为报告资料"}">${selectedForReport ? "✓" : ""}</button>` : ""}
      <button class="result-note-open" data-action="select-note" data-id="${note.id}" data-result-tab-id="${resultTabId || ""}" type="button">
        ${isWelcome ? "" : `<span class="result-index">${index + 1}</span>`}
        <span class="result-note-main">
          <span class="result-note-title">${highlightText(note.title || "未命名笔记", terms)}</span>
          <span class="result-note-excerpt">${renderWikiLinks(highlightText(preview, terms))}</span>
          ${metaParts.length ? `<span class="result-note-meta">${metaParts.join("")}</span>` : ""}
        </span>
        ${note.attachment_path ? '<span class="result-type">图文</span>' : ""}
      </button>
      <div class="result-note-actions" aria-label="笔记操作">
        <button class="result-card-action danger delete-note-btn" data-action="delete" data-id="${note.id}" type="button" title="删除笔记" aria-label="删除笔记">${tablerIcon("trash", "trash-icon")}</button>
        <button class="result-card-action pin-btn ${note.pinned ? "active" : ""}" data-action="pin" data-id="${note.id}" type="button" title="${note.pinned ? "取消置顶" : "置顶"}" aria-label="${note.pinned ? "取消置顶" : "置顶"}">${tablerIcon("pinned", "pin-icon")}</button>
        <button class="result-card-action favorite-btn ${note.favorite ? "active" : ""}" data-action="favorite" data-id="${note.id}" type="button" title="${note.favorite ? "取消收藏" : "收藏"}" aria-label="${note.favorite ? "取消收藏" : "收藏"}">${note.favorite ? "♥" : "♡"}</button>
      </div>
    </article>`;
}

function renderResultDocument(document, index) {
  const status = documentStatusLabel(document.status);
  const matches = document.search_matches || [];
  const primaryMatch = matches[0] || null;
  const location = primaryMatch
    ? `${matches.length} 个相关切片${primaryMatch.source_location ? ` · ${primaryMatch.source_location}` : ""}`
    : `${document.file_type.toUpperCase()} · ${document.chunk_count || 0} 个切片`;
  const preview = primaryMatch
    ? primaryMatch.matched_excerpt || "已命中相关内容，打开后可查看定位切片。"
    : document.status === "ready" ? "已建立本地切片索引，可用于智能问答与引用定位。" : status;
  const selectable = state.reportSelectionMode && document.status === "ready";
  const selectedForReport = selectable && isReportSourceSelected("document", document.id);
  return `
    <article class="result-note document-result-card${selectedForReport ? " report-source-selected" : ""}" data-document-id="${document.id}">
      ${selectable ? `<button class="report-source-toggle ${selectedForReport ? "selected" : ""}" data-report-source-kind="document" data-report-source-id="${document.id}" type="button" aria-label="${selectedForReport ? "取消选择" : "选择资料"}" title="${selectedForReport ? "取消选择" : "选择为报告资料"}">${selectedForReport ? "✓" : ""}</button>` : ""}
      <button class="result-note-open" data-document-card-action="preview" data-document-id="${document.id}" data-document-chunk-id="${primaryMatch?.chunk_id || ""}" data-document-evidence="${escapeHtml(primaryMatch?.matched_excerpt || "")}" data-document-search-query="${escapeHtml($("#search")?.value || "")}" type="button">
        <span class="result-index">${index + 1}</span>
        <span class="result-note-main">
          <span class="result-note-title">${escapeHtml(document.original_name)}</span>
          <span class="result-note-excerpt">${highlightText(preview, searchHighlightTerms())}</span>
          <span class="result-note-meta">${escapeHtml(location)}${document.updated_at ? ` <i>${escapeHtml(document.updated_at.slice(0, 10))}</i>` : ""}</span>
        </span>
        <span class="result-type">文档</span>
      </button>
      <div class="result-note-actions" aria-label="文档操作">
        <button class="result-card-action danger delete-note-btn" data-document-card-action="delete" data-document-id="${document.id}" type="button" title="删除文档" aria-label="删除文档">${tablerIcon("trash", "trash-icon")}</button>
      </div>
    </article>`;
}

function noteResultType(note) {
  if (note.attachment_path) return "image";
  return "note";
}

function visibleResultNotes() {
  if (state.resultScope !== "note") return [];
  const direction = state.resultSortDirection === "asc" ? 1 : -1;
  return [...state.notes].sort((left, right) => {
    const pinned = Number(right.pinned || 0) - Number(left.pinned || 0);
    if (pinned) return pinned;
    const leftTime = new Date(left.updated_at || left.created_at || 0).getTime();
    const rightTime = new Date(right.updated_at || right.created_at || 0).getTime();
    return direction * (leftTime - rightTime);
  });
}

function groupDocumentSearchResults(results, documents) {
  const knownDocuments = new Map((documents || []).map((document) => [Number(document.id), document]));
  const groups = new Map();
  for (const result of results) {
    const documentId = Number(result.document_id || result.id);
    if (!documentId) continue;
    const known = knownDocuments.get(documentId) || {};
    const group = groups.get(documentId) || {
      ...known,
      id: documentId,
      original_name: result.original_name || known.original_name || result.title || "导入文档",
      file_type: known.file_type || "document",
      status: known.status || "ready",
      chunk_count: known.chunk_count || 0,
      updated_at: known.updated_at || "",
      search_matches: [],
    };
    if (!group.search_matches.some((match) => Number(match.chunk_id) === Number(result.chunk_id))) {
      group.search_matches.push(result);
    }
    groups.set(documentId, group);
  }
  return [...groups.values()].sort((left, right) => {
    const leftScore = left.search_matches[0]?.search_score || 0;
    const rightScore = right.search_matches[0]?.search_score || 0;
    return rightScore - leftScore;
  });
}

function groupNoteSearchResults(results) {
  const groups = new Map();
  for (const result of results || []) {
    const noteId = Number(result.id);
    if (!noteId) continue;
    const group = groups.get(noteId);
    if (!group) {
      groups.set(noteId, { ...result, search_matches: [result] });
      continue;
    }
    group.search_matches.push(result);
    if (Number(result.search_score || 0) > Number(group.search_score || 0)) {
      Object.assign(group, result, { search_matches: group.search_matches });
    }
  }
  return [...groups.values()].sort((left, right) => (
    Number(right.search_score || 0) - Number(left.search_score || 0)
  ));
}

function renderEditorDock(terms = []) {
  const dock = $("#editor-dock-content");
  if (!dock) return;
  if (state.newNoteDraft) {
    syncNewNoteDraftFromDom();
    renderNewNoteDraft(dock);
    return;
  }
  const selected = state.notes.find((note) => Number(note.id) === Number(state.selectedNoteId))
    || state.allNotes.find((note) => Number(note.id) === Number(state.selectedNoteId))
    || state.notes[0];
  if (!selected) {
    dock.innerHTML = `<div class="editor-empty"><strong>选择一条笔记开始编辑</strong><span>从中栏检索结果选择一条笔记。</span></div>`;
    return;
  }
  state.selectedNoteId = selected.id;
  dock.innerHTML = renderNoteHtml(selected, terms);
  const noteEl = dock.querySelector(".note");
  noteEl?.querySelector("details.note-detail")?.setAttribute("open", "");
  const tabsBar = noteEl?.querySelector(".note-tabs-bar");
  if (noteEl && tabsBar) {
    const noteHeader = noteEl.querySelector(":scope > .note-header");
    const noteMeta = noteEl.querySelector(":scope > .note-meta");
    const organizeButton = noteHeader?.querySelector("[data-action='intelligence']");
    const chrome = document.createElement("div");
    chrome.className = "editor-chrome";
    chrome.innerHTML = `
      <div class="editor-metadata-row"><div class="editor-organize-slot"></div></div>
      <div class="editor-tabs-row"><div class="editor-tabs-slot"></div></div>
      ${renderEditorToolbar()}`;
    noteEl.prepend(chrome);
    if (noteHeader) chrome.prepend(noteHeader);
    if (noteMeta) chrome.querySelector(".editor-metadata-row").prepend(noteMeta);
    if (organizeButton) chrome.querySelector(".editor-organize-slot").append(organizeButton);
    chrome.querySelector(".editor-tabs-slot").append(tabsBar);
  }
  if (tabsBar) loadTabsBar(Number(tabsBar.dataset.noteId), tabsBar);
}

function folderOptionsHtml(selectedFolderId = null) {
  const folders = [];
  const collect = (items, depth = 0) => {
    for (const folder of items || []) {
      folders.push({ id: folder.id, name: `${"  ".repeat(depth)}${folder.name}` });
      collect(folder.children, depth + 1);
    }
  };
  collect(state.folders);
  const selected = Number(selectedFolderId);
  return `<option value="">未归类</option>${folders.map((folder) => (
    `<option value="${folder.id}" ${Number(folder.id) === selected ? "selected" : ""}>${escapeHtml(folder.name)}</option>`
  )).join("")}`;
}

function renderEditorToolbar() {
  return `
    <div class="editor-toolbar" role="toolbar" aria-label="编辑工具栏">
      <select class="editor-format-select" aria-label="段落样式" data-editor-command="formatBlock">
        <option value="p">正文</option>
        <option value="h2">标题</option>
        <option value="h3">小标题</option>
      </select>
      <button class="editor-tool" type="button" data-editor-command="bold" title="加粗"><b>B</b></button>
      <button class="editor-tool editor-tool-italic" type="button" data-editor-command="italic" title="斜体">I</button>
      <button class="editor-tool" type="button" data-editor-command="underline" title="下划线"><u>U</u></button>
      <label class="editor-color-swatch" title="文字颜色">
        <span aria-hidden="true">A</span><input type="color" class="editor-color-input" data-editor-color="foreColor" value="#1f2f2d" aria-label="文字颜色">
      </label>
      <label class="editor-color-swatch editor-highlight-swatch" title="文字背景颜色">
        <span aria-hidden="true"></span><input type="color" class="editor-color-input" data-editor-color="hiliteColor" value="#fff3a6" aria-label="文字背景颜色">
      </label>
      <span class="editor-tool-divider" aria-hidden="true"></span>
      <button class="editor-tool" type="button" data-editor-command="insertUnorderedList" title="无序列表">&#8226;&#8801;</button>
      <button class="editor-tool" type="button" data-editor-command="insertOrderedList" title="有序列表">1&#8801;</button>
      <button class="editor-tool" type="button" data-editor-command="formatBlock" data-editor-value="blockquote" title="引用">&#8220;</button>
    </div>`;
}

function syncNewNoteDraftFromDom() {
  if (!state.newNoteDraft) return;
  const title = $("#new-note-title");
  const content = $("#new-note-content");
  const folder = $("#new-note-folder");
  if (title) state.newNoteDraft.title = title.value;
  if (content) state.newNoteDraft.content = editableContentToMarkdown(content);
  if (folder) state.newNoteDraft.folder_id = folder.value ? Number(folder.value) : null;
}

function renderNewNoteDraft(dock) {
  const draft = state.newNoteDraft || {};
  dock.innerHTML = `
    <section class="new-note-editor" aria-label="新建笔记编辑器">
      <div class="new-note-chrome">
        <header class="new-note-header">
          <div>
            <p class="eyebrow">新建笔记</p>
            <input id="new-note-title" class="new-note-title" value="${escapeHtml(draft.title || "")}" placeholder="给这条笔记起个标题" aria-label="笔记标题">
          </div>
          <div class="new-note-actions">
            <button class="ghost" data-action="cancel-new-note" type="button">取消</button>
            <button data-action="save-new-note" type="button">保存笔记</button>
          </div>
        </header>
        <div class="new-note-metadata">
          <label>存入分区<select id="new-note-folder" aria-label="选择笔记分区">${folderOptionsHtml(draft.folder_id)}</select></label>
          <span class="new-note-hint">保存后可添加标签、页签和图片。</span>
        </div>
        <div class="new-note-tabs" aria-label="新笔记页签"><span class="tab-chip active"><span>页签 1</span></span></div>
        ${renderEditorToolbar()}
      </div>
      <div id="new-note-content" class="note-content editable new-note-content" contenteditable="true" data-draft="1" data-placeholder="从这里开始记录..." title="点击开始编辑">${draft.content ? renderNoteContent(draft.content, []) : ""}</div>
      ${state.imageDataUrl ? `<div class="new-note-attachment"><img src="${escapeHtml(state.imageDataUrl)}" alt="待保存的图片附件"><button class="ghost" data-action="remove-new-note-attachment" type="button">移除图片</button></div>` : ""}
    </section>`;
  requestAnimationFrame(() => $("#new-note-content")?.focus());
}

function renderTimeline() {
  // 如果用户正在编辑正文（contenteditable 获焦），跳过本次渲染避免打断编辑
  if (document.activeElement?.closest(".note-content.editable")) return;
  const terms = $("#search-mode").value === "semantic"
    ? [...new Set(state.notes.flatMap((note) => note.matched_terms || []))]
    : searchHighlightTerms();

  const hasActiveSearch = Boolean($("#search").value.trim()) || Object.keys(state.filter).length > 0;
  // 导入文档只在“文档”页签中显示：它们是可追溯的知识库原件，而不是可编辑笔记。
  const documents = hasActiveSearch ? state.searchDocuments : state.documents;
  const counts = { note: state.notes.length, document: documents.length };
  $("#result-count").textContent = `${counts.note} 条`;
  Object.entries(counts).forEach(([scope, count]) => {
    const el = $(`#${scope}-result-count`);
    if (el) el.textContent = count;
  });
  const notes = visibleResultNotes();
  const visibleDocuments = state.resultScope === "document" ? documents : [];
  const totalResults = notes.length + visibleDocuments.length;
  $("#result-summary").textContent = totalResults ? `找到 ${totalResults} 条相关结果` : "没有找到相关内容";
  const emptyState = !totalResults
    ? (() => {
        const q = $("#search").value.trim();
        return q
          ? `<p class="muted empty-state">没有找到匹配「${escapeHtml(q)}」的笔记。试试点击「智能问答」直接提问。</p>`
          : state.resultScope === "document"
            ? `<p class="muted empty-state">还没有导入文档。</p>`
            : `<p class="muted empty-state">还没有笔记，按 Ctrl+N 或点击左栏 + 开始记录第一条。</p>`;
      })()
    : "";
  if (notes.length && !notes.some((note) => Number(note.id) === Number(state.selectedNoteId))) {
    state.selectedNoteId = notes[0]?.id || null;
  }
  let userNoteIndex = 0;
  const notesHtml = notes.map((note) => {
    const index = note.source_type === "welcome" ? null : userNoteIndex++;
    return renderResultNote(note, terms, index);
  }).join("");
  $("#timeline").innerHTML = notesHtml
    + visibleDocuments.map((document, index) => renderResultDocument(document, userNoteIndex + index)).join("") + emptyState;
  renderReportSelectionBar();
  highlightSearchMatch();
  renderEditorDock(terms);
}

let _lastSearchQuery = "";
function highlightSearchMatch() {
  const query = $("#search").value.trim();
  if (!query) { _lastSearchQuery = ""; return; }
  // 找到第一个包含高亮标记的笔记
  const firstMatch = document.querySelector("#timeline mark");
  if (!firstMatch) return;
  const noteEl = firstMatch.closest(".result-note");
  if (!noteEl) return;
  // 仅在新搜索时滚动，避免每次轮询都跳
  if (_lastSearchQuery !== query) {
    _lastSearchQuery = query;
    setTimeout(() => {
      noteEl.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 100);
  }
}

async function refresh() {
  const query = $("#search").value.trim();
  const mode = $("#search-mode").value;
  const parameters = queryString({ q: query, mode, ...state.filter });
  const notesUrl = withCacheBuster(query ? `/api/search?${parameters}` : `/api/notes${parameters ? `?${parameters}` : ""}`);
  let searchResults = [];
  if (query || Object.keys(state.filter).length > 0) {
    // 有搜索/筛选时，并行请求全量列表用于最近编辑侧栏
    const [filtered, all] = await Promise.all([
      request(notesUrl),
      request(withCacheBuster("/api/notes")),
    ]);
    searchResults = filtered;
    state.notes = groupNoteSearchResults(filtered.filter((item) => item.source_type !== "document"));
    state.allNotes = all;
  } else {
    state.notes = await request(notesUrl);
    state.allNotes = state.notes;
    state.searchDocuments = [];
  }
  try {
    const [documents, status] = await Promise.all([request("/api/documents"), request("/api/documents/status")]);
    state.documents = documents;
    state.documentStatus = status;
    if (query || Object.keys(state.filter).length > 0) {
      state.searchDocuments = groupDocumentSearchResults(
        searchResults.filter((item) => item.source_type === "document"),
        documents,
      );
    }
  } catch (error) {
    console.warn("文档库状态刷新失败", error);
  }
  invalidateNoteTitleSet();
  renderTimeline();
  try {
    state.taxonomy = await request("/api/taxonomy");
    state.folders = await request("/api/folders");
    state.embeddingStatus = await request("/api/embedding/status");
    state.intelligenceStatus = await request("/api/intelligence/status");
  } catch (err) {
    console.warn("侧栏状态刷新失败", err);
  }
  renderSidebar();
  if (state.pendingScrollId != null) {
    const targetId = state.pendingScrollId;
    state.pendingScrollId = null;
    setTimeout(() => scrollToNote(targetId), 80);
  }
  scheduleOcrPoll();
}

async function resolveFolderId(category, subcategory = "") {
  // 根据 category/subcategory 查找或创建 folder，返回 folder_id（未分类返回 null）
  if (!category || category === "未分类") return null;
  let topFolder = state.folders.find((f) => f.parent_id === null && f.name === category);
  if (!topFolder) {
    topFolder = await request("/api/folders", {
      method: "POST",
      body: JSON.stringify({ parent_id: null, name: category }),
    });
  }
  if (!subcategory) return topFolder.id;
  let childFolder = (topFolder.children || []).find((f) => f.name === subcategory);
  if (!childFolder) {
    childFolder = await request("/api/folders", {
      method: "POST",
      body: JSON.stringify({ parent_id: topFolder.id, name: subcategory }),
    });
  }
  return childFolder.id;
}

function makeCategoryEditable(chip) {
  // 点击分类 chip 后转为 input 内联编辑，blur 自动保存，Esc 取消
  // 分类编辑与 folder 模型联动：编辑分类 = 移动到对应 folder
  const noteId = Number(chip.dataset.id);
  const field = chip.dataset.field;
  const note = state.notes.find((n) => n.id === noteId);
  if (!note) return;
  const currentValue = note[field] || "";
  const input = document.createElement("input");
  input.type = "text";
  input.value = currentValue;
  input.className = "category-edit-input";
  input.placeholder = field === "category" ? "大分类" : "小分类（可空）";
  input.dataset.field = field;
  chip.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    const newValue = input.value.trim();
    if (!commit || newValue === currentValue) {
      await refresh();
      return;
    }
    try {
      // 编辑大分类 → 移到顶级 folder；编辑小分类 → 在当前大分类下找/建子 folder
      const folderId = field === "category"
        ? await resolveFolderId(newValue || "未分类", "")
        : await resolveFolderId(note.category, newValue);
      await request(`/api/notes/${noteId}`, {
        method: "PUT",
        body: JSON.stringify({ folder_id: folderId }),
      });
      showToast("已保存", field === "category" ? "大分类已更新" : "小分类已更新");
      await refresh();
    } catch (err) {
      showToast("保存失败", err.message);
      await refresh();
    }
  };
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); input.blur(); }
    else if (event.key === "Escape") { event.preventDefault(); finish(false); }
  });
}

function makeTitleEditable(titleEl) {
  const noteId = Number(titleEl.dataset.id);
  const note = state.notes.find((item) => item.id === noteId)
    || (state.allNotes || []).find((item) => item.id === noteId);
  if (!note) return;
  const input = document.createElement("input");
  input.type = "text";
  input.value = note.title || "";
  input.className = "note-title-edit-input";
  input.maxLength = 120;
  input.setAttribute("aria-label", "笔记标题");
  titleEl.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    const title = input.value.trim();
    if (!commit || !title || title === note.title) {
      await refresh();
      return;
    }
    try {
      const updated = await request(`/api/notes/${noteId}`, {
        method: "PUT",
        body: JSON.stringify({ title }),
      });
      syncCachedNote(noteId, { title: updated.title, updated_at: updated.updated_at });
      invalidateNoteTitleSet();
      showToast("已保存", "笔记标题已更新");
      await refresh();
    } catch (err) {
      showToast("保存失败", err.message);
      await refresh();
    }
  };
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); input.blur(); }
    else if (event.key === "Escape") { event.preventDefault(); finish(false); }
  });
}

function makeTagEditable(chip) {
  // 点击标签 chip 后转为 input 内联编辑，blur 自动保存，Esc 取消
  const noteId = Number(chip.dataset.id);
  const tagIndex = Number(chip.dataset.tagIndex);
  const note = state.notes.find((n) => n.id === noteId);
  if (!note) return;
  const tags = note.tags || [];
  const currentValue = tags[tagIndex] || "";
  const input = document.createElement("input");
  input.type = "text";
  input.value = currentValue;
  input.className = "category-edit-input";
  input.placeholder = "标签名";
  chip.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    const newValue = input.value.trim();
    if (!commit || newValue === currentValue) {
      await refresh();
      return;
    }
    try {
      // 更新标签数组：替换指定位置的标签
      const newTags = [...tags];
      if (newValue) {
        newTags[tagIndex] = newValue;
      } else {
        // 清空则删除该标签
        newTags.splice(tagIndex, 1);
      }
      await request(`/api/notes/${noteId}`, {
        method: "PUT",
        body: JSON.stringify({ tags: newTags }),
      });
      showToast("已保存", "标签已更新");
      await refresh();
    } catch (err) {
      showToast("保存失败", err.message);
      await refresh();
    }
  };
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); input.blur(); }
    else if (event.key === "Escape") { event.preventDefault(); finish(false); }
  });
}

function findFolderById(folders, id) {
  for (const f of folders) {
    if (f.id === id) return f;
    if (f.children) {
      const found = findFolderById(f.children, id);
      if (found) return found;
    }
  }
  return null;
}

function showFolderContextMenu(x, y, folderId) {
  document.querySelectorAll(".context-menu").forEach((m) => m.remove());
  const menu = document.createElement("div");
  menu.className = "context-menu card";
  menu.style.left = `${x}px`;
  menu.style.top = `${y}px`;
  menu.innerHTML = `
    <button class="context-item" data-action="new-note" type="button">📝 在此分区下新建笔记</button>
    <button class="context-item" data-action="new-subfolder" type="button">📂 新建子分区</button>
    <button class="context-item" data-action="generate-moc" type="button">🗺️ 生成 MOC</button>
    <button class="context-item" data-action="rename" type="button">✏️ 重命名</button>
    <button class="context-item danger" data-action="delete" type="button">🗑️ 删除分区</button>`;
  document.body.appendChild(menu);
  const close = () => menu.remove();
  menu.addEventListener("click", async (e) => {
    const item = e.target.closest("[data-action]");
    if (!item) return;
    const action = item.dataset.action;
    close();
    await handleFolderAction(action, folderId);
  });
  setTimeout(() => document.addEventListener("click", close, { once: true }), 0);
}

async function handleFolderAction(action, folderId) {
  if (action === "new-note") {
    state.selectedFolderId = folderId;
    beginNewNote(folderId);
  } else if (action === "new-subfolder") {
    showInlineFolderInput(folderId);
  } else if (action === "rename") {
    const folder = findFolderById(state.folders, folderId);
    const name = window.prompt("重命名分区：", folder ? folder.name : "");
    if (!name || !name.trim()) return;
    try {
      await request(`/api/folders/${folderId}`, { method: "PUT", body: JSON.stringify({ name: name.trim() }) });
      await refresh();
    } catch (err) { showToast("重命名失败", err.message); }
  } else if (action === "delete") {
    if (!await showConfirm("删除此分区及其子分区？笔记会移到未归类。", "删除分区", { confirmText: "删除", isDanger: true })) return;
    try {
      await request(`/api/folders/${folderId}`, { method: "DELETE" });
      if (state.filter.folder_id === folderId) state.filter = {};
      await refresh();
    } catch (err) { showToast("删除失败", err.message); }
  } else if (action === "generate-moc") {
    await generateMoc(folderId);
  }
}

async function generateMoc(folderId) {
  const folder = findFolderById(state.folders, folderId);
  const folderName = folder ? folder.name : "未分类";
  showToast("正在生成", `正在为「${folderName}」生成 MOC，请稍候...`);
  try {
    const moc = await request("/api/moc/generate", {
      method: "POST",
      body: JSON.stringify({ folder_id: folderId }),
    });
    showToast("MOC 已生成", moc.updated ? `「🗺️ MOC: ${folderName}」已更新` : `「🗺️ MOC: ${folderName}」已创建`);
    state.pendingScrollId = moc.id;
    await refresh();
  } catch (err) {
    showToast("MOC 生成失败", err.message);
  }
}

function focusInlineComposer(folderId) {
  beginNewNote(folderId);
}

function scrollToNote(noteId) {
  // 在结果列表中定位指定笔记，并在右栏编辑器中打开它。
  state.selectedNoteId = Number(noteId);
  renderTimeline();
  switchPane("editor-pane");
  const btn = document.querySelector(`#timeline [data-id="${noteId}"]`);
  if (!btn) return;
  const noteEl = btn.closest(".result-note");
  if (!noteEl) return;
  noteEl.scrollIntoView({ behavior: "smooth", block: "center" });
  noteEl.classList.add("flash");
  setTimeout(() => noteEl.classList.remove("flash"), 1800);
}

function scheduleOcrPoll() {
  const hasPendingTask = state.notes.some((note) =>
    ["pending", "processing"].includes(note.ocr_status) || ["pending", "processing"].includes(note.ai_status)
  );
  if (!hasPendingTask) {
    clearTimeout(state.ocrPollTimer);
    state.ocrPollTimer = null;
    return;
  }
  if (state.ocrPollTimer) return;
  state.ocrPollTimer = setTimeout(async () => {
    state.ocrPollTimer = null;
    try {
      await refreshStatusOnly();
    } catch (error) {
      console.warn("状态刷新失败", error);
      scheduleOcrPoll();
    }
  }, 1500);
}

async function refreshStatusOnly() {
  // 轻量状态更新：只拉取笔记列表，patch DOM 中的状态 chip，不重建 timeline
  const query = $("#search").value.trim();
  const mode = $("#search-mode").value;
  const parameters = queryString({ q: query, mode, ...state.filter });
  const freshNotes = await request(withCacheBuster(query ? `/api/search?${parameters}` : `/api/notes${parameters ? `?${parameters}` : ""}`));
  // 更新 state.notes 的状态字段
  const statusMap = new Map(freshNotes.map((n) => [n.id, n]));
  let statusChanged = false;
  for (const note of state.notes) {
    const fresh = statusMap.get(note.id);
    if (!fresh) continue;
    if (note.ai_status !== fresh.ai_status || note.ai_reason !== fresh.ai_reason || note.ocr_status !== fresh.ocr_status) {
      note.ai_status = fresh.ai_status;
      note.ai_reason = fresh.ai_reason;
      note.ai_error = fresh.ai_error;
      note.ocr_status = fresh.ocr_status;
      note.ocr_text = fresh.ocr_text;
      note.category = fresh.category;
      note.subcategory = fresh.subcategory;
      note.tags = fresh.tags;
      statusChanged = true;
      // 只更新该笔记的 chip DOM，不重建整个 timeline
      patchNoteStatus(note);
    }
  }
  // 如果有笔记从 pending 变为 done，需要刷新侧栏分类统计
  if (statusChanged) {
    try {
      state.intelligenceStatus = await request("/api/intelligence/status");
      state.taxonomy = await request("/api/taxonomy");
      renderSidebar();
    } catch (err) { /* ignore */ }
  }
  // 检查是否还有 pending 任务
  const stillPending = state.notes.some((note) =>
    ["pending", "processing"].includes(note.ocr_status) || ["pending", "processing"].includes(note.ai_status)
  );
  if (!stillPending) {
    clearTimeout(state.ocrPollTimer);
    state.ocrPollTimer = null;
    // 所有 AI/OCR 任务完成时，做一次全量刷新以展示新增的标签和分类
    if (statusChanged) {
      try { await refresh(); } catch (err) { /* ignore */ }
    }
    return;
  }
  scheduleOcrPoll();
}

function patchNoteStatus(note) {
  // 只更新单条笔记的 AI/OCR 状态 chip，不重建整个 timeline
  const noteEl = document.querySelector(`#timeline .note [data-id="${note.id}"]`)?.closest(".note");
  if (!noteEl) return;
  const metaEl = noteEl.querySelector(".note-meta");
  if (!metaEl) return;
  // 找到 ai-chip 和 ocr 相关元素并替换
  const oldAiChip = metaEl.querySelector(".ai-chip");
  const newAiHtml = renderAiStatus(note);
  if (oldAiChip) {
    if (newAiHtml) {
      oldAiChip.outerHTML = newAiHtml;
    } else {
      oldAiChip.remove();
    }
  } else if (newAiHtml) {
    metaEl.insertAdjacentHTML("afterbegin", newAiHtml);
  }
}

function populateFolderSelect() {
  const select = $("#inline-folder-select");
  if (!select) return;
  select.innerHTML = folderOptionsHtml();
}

function beginNewNote(folderId = null, { keepAttachment = false } = {}) {
  if (!keepAttachment) {
    state.imageDataUrl = null;
    state.imageName = null;
  }
  const preferredFolder = folderId != null ? folderId : (state.filter.folder_id ?? state.selectedFolderId ?? null);
  state.editingId = null;
  state.editorSelection = null;
  state.newNoteDraft = { title: "", content: "", folder_id: preferredFolder };
  state.selectedNoteId = null;
  $("#inline-composer")?.classList.add("hidden");
  document.querySelectorAll("#timeline .result-note.active").forEach((item) => item.classList.remove("active"));
  switchPane("editor-pane");
  renderEditorDock();
}

async function cancelNewNote() {
  syncNewNoteDraftFromDom();
  const draft = state.newNoteDraft || {};
  const hasContent = Boolean(String(draft.title || "").trim() || String(draft.content || "").trim() || state.imageDataUrl);
  if (hasContent && !await showConfirm("放弃这条尚未保存的笔记？", "取消新建", { confirmText: "放弃", isDanger: true })) return;
  state.newNoteDraft = null;
  state.imageDataUrl = null;
  state.imageName = null;
  state.selectedNoteId = state.notes[0]?.id || state.allNotes[0]?.id || null;
  renderTimeline();
}

async function saveNewNote() {
  syncNewNoteDraftFromDom();
  const draft = state.newNoteDraft || {};
  const content = String(draft.content || "").trim();
  if (!content && !state.imageDataUrl) {
    await showAlert("写点内容，或者粘贴一张截图。");
    return;
  }
  const button = $("#editor-dock-content [data-action='save-new-note']");
  const originalText = button?.textContent || "保存笔记";
  if (button) {
    button.disabled = true;
    button.textContent = "保存中...";
  }
  try {
    const created = await request("/api/notes", {
      method: "POST",
      body: JSON.stringify({
        title: String(draft.title || "").trim() || "新笔记",
        content,
        folder_id: draft.folder_id || null,
        image_data_url: state.imageDataUrl,
        image_name: state.imageName,
      }),
    });
    state.newNoteDraft = null;
    state.imageDataUrl = null;
    state.imageName = null;
    state.pendingScrollId = created.id;
    state.selectedNoteId = created.id;
    state.filter = {};
    $("#search").value = "";
    $("#search-clear").classList.add("hidden");
    showToast("保存成功", "新笔记已创建");
    await refresh();
  } catch (error) {
    showToast("保存失败", error.message);
  } finally {
    if (button?.isConnected) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

function clearInlineComposer() {
  state.editingId = null;
  state.imageDataUrl = null;
  state.imageName = null;
  $("#inline-content").value = "";
  $("#inline-composer").classList.add("hidden");
  const preview = $("#inline-attachment-preview");
  if (preview) preview.classList.add("hidden");
}

function loadNoteIntoComposer(note) {
  // 将指定笔记载入中区录入条用于编辑
  state.editingId = note.id;
  state.imageDataUrl = null;
  state.imageName = null;
  $("#inline-content").value = note.content;
  $("#inline-composer").classList.remove("hidden");
  const preview = $("#inline-attachment-preview");
  if (preview) preview.classList.add("hidden");
  const folderSelect = $("#inline-folder-select");
  if (folderSelect) folderSelect.value = note.folder_id ? String(note.folder_id) : "";
  $("#inline-content").focus();
  $("#inline-content").scrollIntoView({ behavior: "smooth", block: "center" });
}

function insertAtCursor(textarea, value) {
  const start = textarea.selectionStart;
  textarea.value = textarea.value.slice(0, start) + value + textarea.value.slice(textarea.selectionEnd);
  textarea.selectionStart = textarea.selectionEnd = start + value.length;
}

function tableToMarkdown(text) {
  // 将 Tab 分隔的粘贴内容转为 Markdown 表格。
  // Excel 复制的 text/plain 用 \t 分隔单元格、\r\n 分隔行。
  // 部分应用（如 WPS、网页表格）可能用多个空格分隔，也兼容处理。
  const rawRows = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim().split("\n");
  if (rawRows.length < 2) return null;
  // 判断分隔符：含 \t 用 \t，否则用 2+ 连续空格
  const hasTab = rawRows.some((row) => row.includes("\t"));
  const splitRow = (row) => {
    if (hasTab) return row.split("\t");
    // 多空格分隔：只在连续 2+ 空格处分割，保留单元格内的单空格
    return row.split(/\s{2,}/);
  };
  const rows = rawRows.map((row) => splitRow(row).map((cell) => cell.trim()));
  // 至少 2 行、每行至少 2 列才视为表格
  if (Math.max(...rows.map((row) => row.length)) < 2) return null;
  const width = Math.max(...rows.map((row) => row.length));
  const clean = (cell) => String(cell || "").trim().replace(/\|/g, "\\|").replace(/\n/g, " ");
  const normalize = (row) => Array.from({ length: width }, (_, index) => clean(row[index]));
  const lines = rows.map((row) => `| ${normalize(row).join(" | ")} |`);
  lines.splice(1, 0, `| ${Array.from({ length: width }, () => "---").join(" | ")} |`);
  return lines.join("\n");
}

function editableTableToMarkdown(table) {
  const rows = [...table.querySelectorAll("tr")].map((row) => (
    [...row.children]
      .filter((cell) => cell.matches("th, td"))
      .map((cell) => String(cell.innerText || "").trim().replace(/\|/g, "\\|").replace(/\n+/g, " "))
  ));
  const width = Math.max(0, ...rows.map((row) => row.length));
  if (rows.length < 2 || width < 2) return table.innerText;
  const normalize = (row) => Array.from({ length: width }, (_, index) => row[index] || "");
  const lines = rows.map((row) => `| ${normalize(row).join(" | ")} |`);
  lines.splice(1, 0, `| ${Array.from({ length: width }, () => "---").join(" | ")} |`);
  return lines.join("\n");
}

function normalizeEditorColor(value) {
  const raw = String(value || "").trim();
  const hex = raw.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
  if (hex) {
    const compact = hex[1];
    return `#${compact.length === 3 ? compact.split("").map((part) => part + part).join("") : compact}`.toLowerCase();
  }
  const rgb = raw.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i);
  if (!rgb) return "";
  const toHex = (part) => Math.max(0, Math.min(255, Number(part))).toString(16).padStart(2, "0");
  return `#${toHex(rgb[1])}${toHex(rgb[2])}${toHex(rgb[3])}`;
}

function serializeEditableNode(node) {
  if (node.nodeType === Node.TEXT_NODE) return node.nodeValue || "";
  if (node.nodeType !== Node.ELEMENT_NODE) return "";

  const tag = node.tagName.toLowerCase();
  if (tag === "br") return "\n";
  if (tag === "table") return `\n${editableTableToMarkdown(node)}\n`;

  const children = () => [...node.childNodes].map(serializeEditableNode).join("");
  if (tag === "a" && node.classList.contains("wiki-link")) return `[[${node.dataset.wiki || node.textContent || ""}]]`;
  if (tag === "strong" || tag === "b") return `<strong>${children()}</strong>`;
  if (tag === "em" || tag === "i") return `<em>${children()}</em>`;
  if (tag === "u") return `<u>${children()}</u>`;
  if (tag === "ul") return [...node.children].filter((child) => child.tagName === "LI")
    .map((item) => `- ${serializeEditableNode(item).trim()}`).join("\n") + "\n";
  if (tag === "ol") return [...node.children].filter((child) => child.tagName === "LI")
    .map((item, index) => `${index + 1}. ${serializeEditableNode(item).trim()}`).join("\n") + "\n";
  if (tag === "blockquote") return `> ${children().trim().replace(/\n/g, "\n> ")}\n`;
  if (/^h[1-6]$/.test(tag)) return `${"#".repeat(Number(tag.slice(1)))} ${children().trim()}\n`;
  if (tag === "div" || tag === "p") return `${children()}\n`;

  const color = normalizeEditorColor(node.style.color || node.getAttribute("color"));
  const background = normalizeEditorColor(node.style.backgroundColor);
  const content = children();
  if (color || background) {
    const declarations = [color ? `color:${color}` : "", background ? `background-color:${background}` : ""].filter(Boolean).join("; ");
    return `<span style="${declarations}">${content}</span>`;
  }
  return content;
}

function editableContentToMarkdown(content) {
  // 不再使用 innerText：它会抹掉 <strong>/<span> 等编辑格式。
  return [...content.childNodes].map(serializeEditableNode).join("").replace(/\n{3,}/g, "\n\n").trim();
}

$("#inline-save").addEventListener("click", async () => {
  const content = $("#inline-content").value;
  if (!content.trim() && !state.imageDataUrl) { await showAlert("写点内容，或者粘贴一张截图。"); return; }
  const folderId = $("#inline-folder-select").value || null;
  const body = { title: "", content, folder_id: folderId ? Number(folderId) : null };
  const btn = $("#inline-save");
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = "保存中...";
  try {
    if (state.editingId) {
      const updated = await request(`/api/notes/${state.editingId}`, { method: "PUT", body: JSON.stringify(body) });
      showToast("保存成功", "笔记已更新");
      state.pendingScrollId = updated.id;
      state.selectedNoteId = updated.id;
    } else {
      body.image_data_url = state.imageDataUrl;
      body.image_name = state.imageName;
      const created = await request("/api/notes", { method: "POST", body: JSON.stringify(body) });
      showToast("保存成功", "新笔记已创建");
      state.pendingScrollId = created.id;
      state.selectedNoteId = created.id;
      // 渐进式引导：首次保存图片笔记时提示 OCR
      if (state.imageDataUrl) {
        progressiveHint("image-ocr", "已自动 OCR", "图片笔记会自动识别文字，稍后在笔记展开后可查看。");
      }
    }
    clearInlineComposer();
    state.filter = {};
    $("#search").value = "";
    $("#search-clear").classList.add("hidden");
    await refresh();
  } catch (err) {
    showToast("保存失败", err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
});

$("#inline-cancel").addEventListener("click", clearInlineComposer);
$("#inline-attachment-remove").addEventListener("click", () => {
  state.imageDataUrl = null;
  state.imageName = null;
  const preview = $("#inline-attachment-preview");
  if (preview) preview.classList.add("hidden");
  const img = $("#inline-attachment-img");
  if (img) img.src = "";
});
$("#inline-content").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("#inline-save").click(); }
});
$("#search").addEventListener("input", () => {
  $("#search-clear").classList.toggle("hidden", !$("#search").value);
  clearTimeout(window.searchTimer);
  window.searchTimer = setTimeout(refresh, 180);
});
$("#search").addEventListener("change", refresh);
$("#search").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    $("#ask-button").click();
  }
});
$("#search-clear").addEventListener("click", async () => {
  $("#search").value = "";
  $("#search-clear").classList.add("hidden");
  await refresh();
  $("#search").focus();
});
$("#search-mode").addEventListener("change", refresh);
async function askWith(endpoint, loadingText) {
  const question = $("#search").value.trim();
  if (!question) { await showAlert("先输入一个问题。"); return; }
  switchPane("answer-pane");
  $("#answer-panel").innerHTML = `
    <div class="rag-loading">
      <span class="rag-loading-dots"><span></span><span></span><span></span></span>
      <span class="rag-loading-text">${escapeHtml(loadingText)}</span>
    </div>`;

  // LlamaIndex 实验链路不支持流式，走原逻辑
  if (endpoint.includes("llama-index")) {
    try {
      const payload = await request(endpoint, { method: "POST", body: JSON.stringify({ question }) });
      renderAnswer(payload, question);
    } catch (error) {
      $("#answer-panel").innerHTML = `<p class="ocr-error">${escapeHtml(error.message)}</p>`;
    }
    return;
  }

  // 主链路走 SSE 流式
  try {
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, stream: true }),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const error = new Error(payload.error || `HTTP ${response.status}`);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    if (!response.body) throw new Error("浏览器不支持流式读取");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let sources = [];
    let expandedTerms = [];
    let answerText = "";
    let generationStarted = false;
    let resultKind = "qa";
    let resultData = {};

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const data = line.slice(6);
        if (data === "[DONE]") continue;
        let event;
        try { event = JSON.parse(data); } catch (e) { continue; }
        if (event.type === "sources") {
          sources = event.sources || [];
          expandedTerms = event.expanded_terms || [];
          resultKind = event.kind || "qa";
          resultData = event.data || {};
        } else if (event.type === "token") {
          answerText += event.text;
          // The local model's draft is not source-verified until the stream
          // completes. Keep it off-screen so a later evidence correction does
          // not look like the application contradicted itself.
          if (!generationStarted) {
            generationStarted = true;
            const loading = $("#answer-panel .rag-loading-text");
            if (loading) loading.textContent = "正在生成并核验来源…";
          }
        } else if (event.type === "done") {
          if (event.answer) answerText = event.answer;
          sources = event.sources || sources;
          resultKind = event.kind || resultKind;
          resultData = event.data || resultData;
          renderAnswer({ answer: answerText, sources, expanded_terms: expandedTerms, kind: resultKind, data: resultData, timing: event.timing || {} }, question);
        } else if (event.type === "error") {
          $("#answer-panel").innerHTML = `<p class="ocr-error">${escapeHtml(event.message)}</p>`;
        }
      }
    }
  } catch (error) {
    renderAskError(error);
  }
}

function renderAskError(error) {
  const code = error?.payload?.code;
  const modelMissing = error?.status === 503 && code === "model_missing";
  const modelRuntimeUnavailable = error?.status === 503 && code === "model_runtime_unavailable";
  if (!modelMissing && !modelRuntimeUnavailable) {
    $("#answer-panel").innerHTML = `<p class="ocr-error">${escapeHtml(error.message || "智能问答暂时不可用")}</p>`;
    return;
  }
  if (modelRuntimeUnavailable) {
    $("#answer-panel").innerHTML = `
      <div class="empty-guide model-required-guide">
        <strong>智能模型无法加载</strong>
        <p class="muted">模型文件可能已存在，但本机运行组件没有成功加载。</p>
        <p class="muted">原因：${escapeHtml(error.message || "未返回具体原因")}</p>
        <button id="open-model-settings" type="button">查看智能设置</button>
      </div>`;
    $("#open-model-settings")?.addEventListener("click", () => $("#settings-button").click());
    return;
  }
  $("#answer-panel").innerHTML = `
    <div class="empty-guide model-required-guide">
      <strong>智能模型尚未就绪</strong>
      <p class="muted">请在设置的“智能”页下载并校验 Qwen 4B 模型；完成后即可直接提问。</p>
      <button id="open-model-settings" type="button">打开智能设置</button>
    </div>`;
  $("#open-model-settings")?.addEventListener("click", () => $("#settings-button").click());
}

function renderAnswerStreaming(question, sources, currentText, expandedTerms) {
  $("#result-count").textContent = `参考 ${sources.length} 条`;
  const cleanText = currentText.replace(/^[\s\n]+/, "").replace(/\n{3,}/g, "\n\n");
  const terms = (expandedTerms || []).filter((t) => t && t !== question);
  const expandedHtml = terms.length
    ? `<div class="expanded-terms">查询扩展：<span>${terms.map(escapeHtml).join("</span> <span>")}</span></div>`
    : "";
  $("#answer-panel").innerHTML = `
    <div class="answer-header">
      <div>
        <p class="eyebrow">智能解答 · 生成中...</p>
        <h2>${escapeHtml(question)}</h2>
      </div>
      <button id="close-answer" class="ghost" type="button">关闭</button>
    </div>
    ${expandedHtml}
    <div class="answer-response">
      <img class="answer-cat-mark" src="/brand/pocket-memory-small-logo.png" alt="智能问答标识">
      <div class="answer-body typing-cursor">${escapeHtml(cleanText)}</div>
  </div>`;
  $("#close-answer").addEventListener("click", () => {
    $("#answer-panel").innerHTML = `<div class="empty-guide"><p class="muted">在搜索框输入问题，点击「智能问答」<br>会基于你的本地笔记生成回答。</p></div>`;
    $("#result-count").textContent = "";
  });
}

$("#ask-button").addEventListener("click", async () => {
  await askWith("/api/ask", "正在基于本地笔记生成回答...");
  // 渐进式引导：首次使用智能问答时提示
  progressiveHint("first-ask", "智能问答", "支持跨笔记、跨页签检索。点击来源链接可直接跳转到具体页签。");
});

$("#new-note-btn").addEventListener("click", () => {
  beginNewNote();
});

function documentStatusLabel(status) {
  return { pending: "等待导入", extracting: "正在提取", indexing: "正在建立索引", ready: "已就绪", failed: "导入失败" }[status] || status;
}

function formatDocumentSize(bytes) {
  if (!Number.isFinite(bytes)) return "";
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

async function refreshDocuments() {
  try {
    const [documents, status] = await Promise.all([request("/api/documents"), request("/api/documents/status")]);
    state.documents = documents;
    state.documentStatus = status;
    if (!document.activeElement?.closest(".note-content.editable")) renderTimeline();
    clearTimeout(state.documentPollTimer);
    if (status.processing) state.documentPollTimer = setTimeout(refreshDocuments, 1200);
  } catch (error) {
    const hint = $("#document-import-hint");
    if (hint) hint.textContent = `无法读取文档库状态：${error.message}`;
  }
}

function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("无法读取文件"));
    reader.onload = () => resolve(String(reader.result || ""));
    reader.readAsDataURL(file);
  });
}

$("#import-document-btn").addEventListener("click", async () => {
  $("#document-dialog").showModal();
  await refreshDocuments();
});

async function uploadDocumentFiles(files) {
  const selectedFiles = [...files];
  if (!selectedFiles.length) return;
  const allowed = ["txt", "docx", "xlsx", "pdf"];
  const hint = $("#document-import-hint");
  const accepted = [];
  const rejected = [];
  selectedFiles.forEach((file) => {
    const extension = file.name.split(".").pop()?.toLowerCase() || "";
    if (!allowed.includes(extension) || file.size > 10 * 1024 * 1024) rejected.push(file);
    else accepted.push(file);
  });
  if (rejected.length) showToast("已跳过部分文件", `${rejected.length} 个文件格式不支持或超过 10MB`);
  if (!accepted.length) return;
  let succeeded = 0;
  for (const [index, file] of accepted.entries()) {
    hint.textContent = `正在提交 ${index + 1}/${accepted.length}：${file.webkitRelativePath || file.name}`;
    try {
      await request("/api/documents", {
        method: "POST",
        body: JSON.stringify({
          name: file.webkitRelativePath || file.name,
          data_url: await readFileAsDataUrl(file),
          chunk_profile: "standard",
        }),
      });
      succeeded += 1;
    } catch (error) {
      console.warn("导入失败", file.name, error);
    }
  }
  hint.textContent = succeeded === accepted.length
    ? `已提交 ${succeeded} 个文件，正在本机提取、切片和建立索引。`
    : `已提交 ${succeeded}/${accepted.length} 个文件，请检查失败项。`;
  showToast("导入已提交", `成功提交 ${succeeded} 个文件`);
  await refreshDocuments();
}

function bindDocumentFileInput(selector) {
  $(selector).addEventListener("change", async (event) => {
    // FileList is live: resetting the input first would empty it before upload starts.
    const files = [...(event.target.files || [])];
    event.target.value = "";
    await uploadDocumentFiles(files);
  });
}

bindDocumentFileInput("#document-file-input");
bindDocumentFileInput("#document-folder-input");

function isSupportedDocumentFile(file) {
  const extension = file?.name?.split(".").pop()?.toLowerCase() || "";
  return ["txt", "docx", "xlsx", "pdf"].includes(extension) && Number(file?.size) <= 10 * 1024 * 1024;
}

async function replaceImportedDocument(documentId, file) {
  if (!isSupportedDocumentFile(file)) {
    showToast("无法替换", "请选择不超过 10MB 的 TXT、DOCX、XLSX 或 PDF 文件");
    return;
  }
  try {
    await request(`/api/documents/${documentId}/file`, {
      method: "PUT",
      body: JSON.stringify({ name: file.name, data_url: await readFileAsDataUrl(file) }),
    });
    showToast("原件已替换", "旧切片和向量已清理，正在重新提取并建立索引");
    await refreshDocuments();
    await openImportedDocument(documentId, file.name);
  } catch (error) {
    showToast("替换失败", error.message);
  }
}

$("#document-replace-input").addEventListener("change", async (event) => {
  const file = event.target.files?.[0];
  const documentId = state.documentReplacementId;
  event.target.value = "";
  state.documentReplacementId = null;
  if (file && documentId) await replaceImportedDocument(documentId, file);
});

async function handleDocumentAction(document, action) {
  if (action === "preview") {
    await openImportedDocument(document.id, document.original_name);
    return;
  }
  if (action === "download") {
    window.open(`/api/documents/${document.id}/file?download=1`, "_blank", "noopener");
    return;
  }
  if (action === "reindex") {
    try {
      await request(`/api/documents/${document.id}/reindex`, { method: "POST", body: "{}" });
      showToast("已提交", "正在重新提取并建立索引");
      await refreshDocuments();
    } catch (error) { showToast("操作失败", error.message); }
    return;
  }
  if (action === "delete" && await showConfirm(`删除“${document.original_name}”及其索引？`, "删除导入文件", { confirmText: "删除", isDanger: true })) {
    try {
      await request(`/api/documents/${document.id}`, { method: "DELETE" });
      showToast("已删除", document.original_name);
      await refreshDocuments();
    } catch (error) { showToast("删除失败", error.message); }
  }
}

// ---------- 小工具 ----------
$("#tool-input").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#tool-run").click(); });

$("#tool-run").addEventListener("click", async () => {
  const input = $("#tool-input").value.trim();
  if (!input) return;
  const resultEl = $("#tool-result");
  resultEl.className = "tool-result";
  resultEl.textContent = "正在解析意图...";
  try {
    const resp = await fetch("/api/tools", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ input }),
    });
    const data = await resp.json();
    if (data.tool && data.tool !== "none") {
      const r = data.result || {};
      if (r.ok) {
        resultEl.className = "tool-result ok";
        if (data.tool === "set_timer") {
          resultEl.textContent = `已设置倒计时：${formatDuration(r.seconds)} 后提醒${r.message ? "「" + r.message + "」" : ""}`;
        } else if (data.tool === "pomodoro_start") {
          resultEl.textContent = `番茄钟开始：专注 ${r.work_minutes} 分钟，休息 ${r.break_minutes} 分钟`;
        } else if (data.tool === "schedule") {
          const repeatLabels = { none: "", daily: "（每天）", weekdays: "（工作日）", weekly: "（每周）", monthly: "（每月）", quarterly: "（每季度）", yearly: "（每年）" };
          resultEl.textContent = `已添加日历提醒：${formatDateTime(r.due_at)} ${r.message}${repeatLabels[r.repeat] || ""}`;
        } else {
          resultEl.textContent = "工具执行成功";
        }
        $("#tool-input").value = "";
      } else {
        resultEl.className = "tool-result error";
        resultEl.textContent = "执行失败：" + (r.error || "未知错误");
      }
    } else {
      resultEl.className = "tool-result error";
      resultEl.textContent = "没有识别出工具意图" + (data.error ? "：" + data.error : "") + "。试试：5分钟后提醒我开会";
    }
  } catch (err) {
    resultEl.className = "tool-result error";
    resultEl.textContent = "请求失败：" + err.message;
  }
  refreshToolActive();
});

async function refreshToolActive() {
  try {
    const resp = await fetch("/api/tools/timers");
    const data = await resp.json();
    state.activeTimers = data.active_timers || [];
    renderToolActive();
  } catch (err) { /* silent */ }
}

function renderToolActive() {
  const activeEl = $("#tool-active");
  const active = state.activeTimers;
  if (!active.length) {
    activeEl.innerHTML = `<p class="muted tool-empty">暂无活跃计时器。<br>试试输入「5分钟后提醒我开会」</p>`;
    return;
  }
  activeEl.innerHTML = active.map((t) => {
    const total = Math.max(0, t.remaining_seconds);
    const mins = Math.floor(total / 60);
    const secs = Math.floor(total) % 60;
    const label = t.kind === "pomodoro_work" ? "番茄钟·工作" : t.kind === "pomodoro_break" ? "番茄钟·休息" : "倒计时";
    return `<div class="tool-active-item">
      <span>${label} · ${escapeHtml(t.message || "")}</span>
      <span class="remaining">${String(mins).padStart(2,"0")}:${String(secs).padStart(2,"0")}</span>
      <button class="ghost" data-cancel="${escapeHtml(String(t.id))}">取消</button>
    </div>`;
  }).join("");
  activeEl.querySelectorAll("[data-cancel]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await fetch("/api/tools/timers/cancel", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ timer_id: btn.dataset.cancel }),
      });
      refreshToolActive();
    });
  });
}

// 前端每秒本地递减倒计时，避免 3 秒轮询导致的卡顿
function tickTimers() {
  if (!state.activeTimers.length) return;
  state.activeTimers.forEach((t) => {
    if (t.remaining_seconds > 0) t.remaining_seconds -= 1;
  });
  document.querySelectorAll(".tool-active-item .remaining").forEach((el, i) => {
    const t = state.activeTimers[i];
    if (!t) return;
    const total = Math.max(0, t.remaining_seconds);
    const mins = Math.floor(total / 60);
    const secs = Math.floor(total) % 60;
    el.textContent = `${String(mins).padStart(2,"0")}:${String(secs).padStart(2,"0")}`;
  });
}
setInterval(tickTimers, 1000);

// ---------- Toast 提醒 + 蜂鸣 + 轮询 ----------
let _audioCtx = null;

function playBeep() {
  // 柔和的"叮咚"双音：三角波 + 低音量 + 缓入缓出，替代尖锐蜂鸣
  try {
    if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const ctx = _audioCtx;
    const notes = [523.25, 659.25]; // C5 → E5，上行小三度
    notes.forEach((freq, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.frequency.value = freq;
      osc.type = "triangle";
      const start = ctx.currentTime + i * 0.35;
      gain.gain.setValueAtTime(0, start);
      gain.gain.linearRampToValueAtTime(0.12, start + 0.08);
      gain.gain.linearRampToValueAtTime(0, start + 0.4);
      osc.start(start);
      osc.stop(start + 0.45);
    });
  } catch (e) { /* AudioContext 不可用 */ }
}

function showToast(title, message, isReminder = false, extraClass = "") {
  const container = isReminder ? $("#reminder-container") : $("#toast-container");
  const toast = document.createElement("div");
  toast.className = `toast${isReminder ? " reminder" : ""}${extraClass ? ` ${extraClass}` : ""}`;
  toast.innerHTML = `
    <div class="toast-title"><span class="toast-icon">${isReminder ? "⏰" : "✓"}</span>${escapeHtml(title)}</div>
    <div class="toast-message">${escapeHtml(message)}</div>`;
  const remove = () => {
    toast.classList.add("hide");
    setTimeout(() => toast.remove(), 300);
  };
  // The report-selection toast sits below header controls, so it stays open
  // long enough to read instead of collapsing under the pointer that opened it.
  if (extraClass !== "report-selection-toast") {
    toast.addEventListener("pointerenter", () => toast.classList.add("is-collapsed"), { once: true });
  }
  toast.addEventListener("click", remove);
  container.appendChild(toast);
  // 提醒类停留更久（12s），普通提示 8s
  setTimeout(remove, isReminder ? 12000 : 8000);
}

// ---------- 图片放大 Lightbox ----------
function openLightbox(src, alt) {
  const overlay = document.createElement("div");
  overlay.className = "lightbox-overlay";
  overlay.innerHTML = `<img src="${escapeHtml(src)}" alt="${escapeHtml(alt || "")}">`;
  overlay.addEventListener("click", () => {
    overlay.classList.add("closing");
    setTimeout(() => overlay.remove(), 200);
  });
  document.addEventListener("keydown", function escClose(e) {
    if (e.key === "Escape") { overlay.click(); document.removeEventListener("keydown", escClose); }
  });
  document.body.appendChild(overlay);
}

// ---------- 自定义确认/提示对话框（替代 confirm/alert） ----------
function showDialog({ title, message, confirmText, cancelText, isDanger }) {
  return new Promise((resolve) => {
    const dlg = document.createElement("dialog");
    dlg.className = "app-dialog";
    dlg.innerHTML = `
      <div class="app-dialog-body">
        <h3>${escapeHtml(title || "提示")}</h3>
        <p>${escapeHtml(message || "")}</p>
      </div>
      <div class="app-dialog-actions">
        ${cancelText !== null ? `<button class="ghost" data-act="cancel">${escapeHtml(cancelText || "取消")}</button>` : ""}
        <button class="${isDanger ? "danger" : ""}" data-act="ok">${escapeHtml(confirmText || "确定")}</button>
      </div>`;
    document.body.appendChild(dlg);
    dlg.showModal();
    const close = (val) => { dlg.close(); dlg.remove(); resolve(val); };
    dlg.querySelector('[data-act="ok"]').addEventListener("click", () => close(true));
    const cancelBtn = dlg.querySelector('[data-act="cancel"]');
    if (cancelBtn) cancelBtn.addEventListener("click", () => close(false));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(false); });
  });
}

function showConfirm(message, title, opts = {}) {
  return showDialog({ title: title || "确认操作", message, confirmText: opts.confirmText, cancelText: opts.cancelText, isDanger: opts.isDanger });
}

function showAlert(message, title) {
  return showDialog({ title: title || "提示", message, confirmText: "知道了", cancelText: null });
}

function showPrompt(message, title, opts = {}) {
  // 简易输入对话框：返回用户输入的字符串，取消返回 null
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "dialog-overlay";
    const defaultValue = opts.defaultValue || "";
    overlay.innerHTML = `
      <div class="dialog card">
        <h3>${escapeHtml(title || "输入")}</h3>
        <p class="dialog-message">${escapeHtml(message || "")}</p>
        <input type="text" class="dialog-input" value="${escapeHtml(defaultValue)}" placeholder="${escapeHtml(opts.placeholder || "")}">
        <div class="dialog-actions">
          <button class="ghost" data-dialog-cancel type="button">取消</button>
          <button data-dialog-confirm type="button">确定</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const input = overlay.querySelector(".dialog-input");
    input.focus();
    input.select();
    const close = (value) => { overlay.remove(); resolve(value); };
    overlay.querySelector("[data-dialog-cancel]").addEventListener("click", () => close(null));
    overlay.querySelector("[data-dialog-confirm]").addEventListener("click", () => close(input.value.trim()));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") close(input.value.trim());
      else if (e.key === "Escape") close(null);
    });
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(null); });
  });
}

// ---------- 历史版本与差异对比 ----------
// 行级 diff：返回 [{type:"same"|"add"|"del", text}] 数组，用于高亮渲染
function diffLines(oldText, newText) {
  const oldLines = String(oldText || "").split("\n");
  const newLines = String(newText || "").split("\n");
  // 最长公共子序列（LCS）DP，O(n*m)。笔记长度通常不大，可接受
  const n = oldLines.length, m = newLines.length;
  const dp = Array.from({ length: n + 1 }, () => new Int32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = oldLines[i] === newLines[j]
        ? dp[i + 1][j + 1] + 1
        : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const result = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (oldLines[i] === newLines[j]) {
      result.push({ type: "same", text: newLines[j] });
      i++; j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      result.push({ type: "del", text: oldLines[i] });
      i++;
    } else {
      result.push({ type: "add", text: newLines[j] });
      j++;
    }
  }
  while (i < n) { result.push({ type: "del", text: oldLines[i++] }); }
  while (j < m) { result.push({ type: "add", text: newLines[j++] }); }
  return result;
}

function renderDiffHtml(diffResult) {
  return diffResult.map((line) => {
    const cls = line.type === "add" ? "diff-add" : line.type === "del" ? "diff-del" : "diff-same";
    const prefix = line.type === "add" ? "+ " : line.type === "del" ? "- " : "  ";
    return `<div class="${cls}">${escapeHtml(prefix + line.text)}</div>`;
  }).join("");
}

async function showVersionsDialog(noteId) {
  let versions = [];
  let currentNote = null;
  try {
    [versions, currentNote] = await Promise.all([
      request(`/api/notes/${noteId}/versions`),
      request(`/api/notes/${noteId}`),
    ]);
  } catch (e) {
    showToast("加载失败", e.message);
    return;
  }
  if (!versions.length) {
    showAlert("这条笔记还没有历史版本。编辑保存后会自动记录版本。", "历史版本");
    return;
  }
  const dlg = document.createElement("dialog");
  dlg.className = "app-dialog versions-dialog";
  // 当前版本作为虚拟条目放在列表顶部，方便与历史版本对比
  const currentEntry = {
    id: "current",
    note_id: noteId,
    title: currentNote.title,
    content: currentNote.content,
    category: currentNote.category,
    subcategory: currentNote.subcategory,
    tags: (currentNote.tags || []).join(","),
    ocr_text: currentNote.ocr_text || "",
    tab_name: currentNote.tab_name || "",
    created_at: currentNote.updated_at,
    is_current: true,
  };
  const allVersions = [currentEntry, ...versions];
  dlg.innerHTML = `
    <div class="versions-dialog-body">
      <div class="versions-header">
        <h3>历史版本</h3>
        <button class="ghost" data-act="close" type="button">✕</button>
      </div>
      <div class="versions-layout">
        <div class="versions-list" data-role="list"></div>
        <div class="versions-diff" data-role="diff">
          <p class="muted">勾选 1 个版本查看内容或恢复，勾选 2 个版本进行差异对比。</p>
        </div>
      </div>
      <div class="versions-actions">
        <span class="muted versions-hint" data-role="hint"></span>
        <button class="ghost" data-act="restore" type="button" disabled>恢复到此版本</button>
      </div>
    </div>`;
  document.body.appendChild(dlg);
  dlg.showModal();

  const listEl = dlg.querySelector('[data-role="list"]');
  const diffEl = dlg.querySelector('[data-role="diff"]');
  const hintEl = dlg.querySelector('[data-role="hint"]');
  const restoreBtn = dlg.querySelector('[data-act="restore"]');
  let selectedIds = [];

  const formatTime = (ts) => ts ? ts.replace("T", " ").slice(0, 16) : "";
  const labelOf = (v) => v.is_current ? "当前版本" : formatTime(v.created_at);
  const tabLabel = (v) => v.tab_name ? ` · ${escapeHtml(v.tab_name)}` : "";

  const renderList = () => {
    listEl.innerHTML = allVersions.map((v) => {
      const selected = selectedIds.includes(v.id);
      return `<div class="version-item ${selected ? "selected" : ""} ${v.is_current ? "is-current" : ""}"
                data-id="${v.id}" data-role="version">
        <label class="version-check" title="${v.is_current ? '当前版本（可用于对比，不可恢复）' : '勾选后可查看、对比或恢复'}">
          <input type="checkbox" ${selected ? "checked" : ""}>
        </label>
        <span class="version-label">${escapeHtml(labelOf(v))}${tabLabel(v)}</span>
      </div>`;
    }).join("");
  };

  const updateRestoreBtn = () => {
    // 当恰好选中 1 个非当前版本时，启用恢复按钮
    const canRestore = selectedIds.length === 1 && selectedIds[0] !== "current";
    restoreBtn.disabled = !canRestore;
  };

  const renderDiff = () => {
    if (selectedIds.length === 0) {
      diffEl.innerHTML = `<p class="muted">勾选 1 个版本查看内容或恢复，勾选 2 个版本进行差异对比。</p>`;
      return;
    }
    if (selectedIds.length === 1) {
      const v = allVersions.find((x) => x.id === selectedIds[0]);
      diffEl.innerHTML = `<div class="version-detail">
        <h4>${escapeHtml(v.title)}${tabLabel(v)}</h4>
        <div class="version-meta">
          <span>分类: ${escapeHtml(v.category)} / ${escapeHtml(v.subcategory || "—")}</span>
          <span>标签: ${escapeHtml(v.tags || "—")}</span>
        </div>
        <pre class="version-content">${escapeHtml(v.content)}</pre>
      </div>`;
      return;
    }
    // 两个版本对比：按 selectedIds 顺序，前者为旧、后者为新
    const oldV = allVersions.find((x) => x.id === selectedIds[0]);
    const newV = allVersions.find((x) => x.id === selectedIds[1]);
    const contentDiff = diffLines(oldV.content, newV.content);
    diffEl.innerHTML = `<div class="version-detail">
      <h4>差异对比：${escapeHtml(labelOf(oldV))} → ${escapeHtml(labelOf(newV))}${tabLabel(newV)}</h4>
      <div class="diff-section-title">正文</div>
      <div class="diff-block">${renderDiffHtml(contentDiff)}</div>
    </div>`;
  };

  const updateHint = () => {
    if (selectedIds.length === 0) hintEl.textContent = "";
    else if (selectedIds.length === 1) hintEl.textContent = selectedIds[0] === "current" ? "已选中当前版本" : "已选择 1 个版本，可恢复或再选 1 个对比";
    else hintEl.textContent = `已选择 2 个版本进行对比`;
  };

  listEl.addEventListener("click", (e) => {
    const item = e.target.closest("[data-role='version']");
    if (!item) return;
    const id = item.dataset.id;
    const idNum = isNaN(Number(id)) ? id : Number(id);
    const input = item.querySelector('input[type="checkbox"]');
    const willSelect = e.target.matches('input[type="checkbox"]') ? input.checked : !selectedIds.includes(idNum);
    if (willSelect) {
      if (!selectedIds.includes(idNum)) {
        if (selectedIds.length >= 2) selectedIds.shift();
        selectedIds.push(idNum);
      }
    } else {
      selectedIds = selectedIds.filter((x) => x !== idNum);
    }
    renderList();
    renderDiff();
    updateHint();
    updateRestoreBtn();
  });

  restoreBtn.addEventListener("click", async () => {
    if (selectedIds.length !== 1 || selectedIds[0] === "current") return;
    const targetId = selectedIds[0];
    if (!await showConfirm("恢复到此版本？当前内容会被覆盖（已自动保存为新版本，可再次恢复）。", "恢复版本", { confirmText: "恢复" })) return;
    try {
      await request(`/api/versions/${targetId}/restore`, { method: "POST", body: "{}" });
      showToast("已恢复", "当前内容已覆盖为历史版本");
      dlg.close();
      await refresh();
    } catch (e) {
      showToast("恢复失败", e.message);
    }
  });

  dlg.querySelector('[data-act="close"]').addEventListener("click", () => dlg.close());
  dlg.addEventListener("close", () => dlg.remove());
  dlg.addEventListener("cancel", (e) => { e.preventDefault(); dlg.close(); });

  renderList();
  updateHint();
}

const NOTIFICATION_ICON = "data:image/svg+xml," + encodeURIComponent(
  "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'>" +
  "<rect width='64' height='64' rx='12' fill='#7c9a7d'/>" +
  "<text x='32' y='46' font-size='38' text-anchor='middle'>📓</text>" +
  "</svg>"
);

function notifyReminder(title, message) {
  // 后端已弹系统消息框（带系统信息音，最小化也可见），这里只做应用内视觉补充
  showToast(title, message, true);
  if ("Notification" in window && Notification.permission === "granted") {
    const iconLink = $("#notification-icon");
    const icon = (iconLink && iconLink.href) ? iconLink.href : NOTIFICATION_ICON;
    try { new Notification(title, { body: message, icon }); } catch (e) { /* ignore */ }
  }
}

if ("Notification" in window && Notification.permission === "default") {
  document.addEventListener("click", () => Notification.requestPermission(), { once: true });
}

async function pollTimers() {
  try {
    const resp = await fetch("/api/tools/timers");
    const data = await resp.json();
    const dueTimers = data.due_timers || [];
    const dueSchedules = data.due_schedules || [];
    dueTimers.forEach((t) => {
      const msg = t.message || "时间到了";
      if (t.kind === "pomodoro_work") {
        notifyReminder("🍅 番茄钟·工作结束", `${msg}，专注完成，起来活动一下吧～`);
      } else if (t.kind === "pomodoro_break") {
        notifyReminder("☕ 番茄钟·休息结束", `${msg}，休息好了，继续加油！`);
      } else {
        notifyReminder("⏰ 倒计时到啦！", `${msg}，快去看看吧～`);
      }
    });
    dueSchedules.forEach((s) => {
      const msg = s.message || "提醒时间到了";
      notifyReminder("📅 日历提醒", `${msg}，别忘了哦～`);
    });
    // 有计时器到期时清空结果区，避免残留「已设置倒计时」文本
    if (dueTimers.length || dueSchedules.length) {
      const resultEl = $("#tool-result");
      resultEl.className = "tool-result";
      resultEl.textContent = "";
    }
    refreshToolActive();
  } catch (err) { /* silent */ }
}
setInterval(pollTimers, 3000);
refreshToolActive();

// 最近编辑列表点击：跳转到对应笔记
$("#recent-nav").addEventListener("click", async (event) => {
  const item = event.target.closest("[data-recent-id]");
  if (!item) return;
  const noteId = Number(item.dataset.recentId);
  await jumpToNote(noteId);
});

$("#favorite-nav").addEventListener("click", async (event) => {
  const item = event.target.closest("[data-favorite-id]");
  if (!item) return;
  await jumpToNote(Number(item.dataset.favoriteId));
});

$("#category-nav").addEventListener("click", async (event) => {
  const toggleBtn = event.target.closest("[data-toggle-folder]");
  if (toggleBtn) {
    const fid = Number(toggleBtn.dataset.toggleFolder);
    if (state.expandedFolders.has(fid)) state.expandedFolders.delete(fid);
    else state.expandedFolders.add(fid);
    renderSidebar();
    return;
  }
  const folderBtn = event.target.closest("button[data-folder-id]");
  if (folderBtn) {
    const fid = Number(folderBtn.dataset.folderId);
    state.filter = { folder_id: fid };
    // 点击分类名称时也展开/收起子分类（若有子分类），与点击 ▾/▸ 按钮行为一致
    const row = folderBtn.closest(".folder-row");
    if (row && row.querySelector("[data-toggle-folder]")) {
      if (state.expandedFolders.has(fid)) state.expandedFolders.delete(fid);
      else state.expandedFolders.add(fid);
    }
    await refresh();
    return;
  }
  const button = event.target.closest("button[data-filter]");
  if (button && button.dataset.filter === "all") {
    state.filter = {};
    await refresh();
  }
});

// 右键 folder 弹出菜单
$("#category-nav").addEventListener("contextmenu", (event) => {
  const folderBtn = event.target.closest("button[data-folder-id]");
  if (!folderBtn) return;
  event.preventDefault();
  const folderId = Number(folderBtn.dataset.folderId);
  showFolderContextMenu(event.clientX, event.clientY, folderId);
});

let draggedNoteId = null;
let reorderInFlight = false;

function clearNoteDragState() {
  draggedNoteId = null;
  document.querySelectorAll(".folder-row.drop-target").forEach((row) => row.classList.remove("drop-target"));
  document.querySelectorAll(".result-note.drop-before, .result-note.drop-after").forEach((row) => row.classList.remove("drop-before", "drop-after"));
}

function transferNoteId(event) {
  const raw = event.dataTransfer?.getData("text/plain");
  const noteId = Number.parseInt(raw || "", 10);
  return Number.isInteger(noteId) && noteId > 0 ? noteId : null;
}

$("#timeline").addEventListener("dragstart", (event) => {
  if (event.target.closest(".result-note-actions")) {
    event.preventDefault();
    return;
  }
  const card = event.target.closest("[data-drag-note-id]");
  if (!card || reorderInFlight || !canManuallyReorderNotes()) return;
  draggedNoteId = Number(card.dataset.dragNoteId);
  if (!Number.isInteger(draggedNoteId) || draggedNoteId <= 0) {
    clearNoteDragState();
    return;
  }
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", String(draggedNoteId));
  card.classList.add("dragging");
});
$("#timeline").addEventListener("dragend", (event) => {
  event.target.closest("[data-drag-note-id]")?.classList.remove("dragging");
  clearNoteDragState();
});
$("#timeline").addEventListener("dragover", (event) => {
  const card = event.target.closest("[data-drag-note-id]");
  const sourceId = draggedNoteId || transferNoteId(event);
  if (!card || !sourceId || reorderInFlight || Number(card.dataset.dragNoteId) === sourceId) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "move";
  const after = event.clientY > card.getBoundingClientRect().top + card.clientHeight / 2;
  document.querySelectorAll(".result-note.drop-before, .result-note.drop-after").forEach((item) => item.classList.remove("drop-before", "drop-after"));
  card.classList.add(after ? "drop-after" : "drop-before");
});
$("#timeline").addEventListener("drop", async (event) => {
  const card = event.target.closest("[data-drag-note-id]");
  const sourceId = draggedNoteId || transferNoteId(event);
  if (!card || !sourceId || reorderInFlight || Number(card.dataset.dragNoteId) === sourceId) return;
  event.preventDefault();
  event.stopPropagation();
  if (!canManuallyReorderNotes() || !document.querySelector(`#timeline [data-drag-note-id="${sourceId}"]`)) {
    clearNoteDragState();
    return;
  }
  const after = card.classList.contains("drop-after");
  card.classList.remove("drop-before", "drop-after");
  reorderInFlight = true;
  try {
    await request("/api/notes/reorder", {
      method: "POST",
      body: JSON.stringify({ note_id: sourceId, target_note_id: Number(card.dataset.dragNoteId), after }),
    });
    showToast("顺序已调整", "笔记位置已保存");
  } catch (err) {
    showToast("调整失败", err.message);
  } finally {
    reorderInFlight = false;
    clearNoteDragState();
    await refresh();
  }
});
$("#category-nav").addEventListener("dragover", (event) => {
  const row = event.target.closest("[data-drop-folder-id]");
  if (!row) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "move";
  document.querySelectorAll(".folder-row.drop-target").forEach((item) => {
    if (item !== row) item.classList.remove("drop-target");
  });
  row.classList.add("drop-target");
});
$("#category-nav").addEventListener("dragleave", (event) => {
  const row = event.target.closest("[data-drop-folder-id]");
  if (row && !row.contains(event.relatedTarget)) row.classList.remove("drop-target");
});
$("#category-nav").addEventListener("drop", async (event) => {
  const row = event.target.closest("[data-drop-folder-id]");
  if (!row) return;
  event.preventDefault();
  row.classList.remove("drop-target");
  const noteId = draggedNoteId || Number(event.dataTransfer.getData("text/plain"));
  const folderId = Number(row.dataset.dropFolderId);
  if (!noteId || !folderId) return;
  const folder = findFolderById(state.folders, folderId);
  try {
    await request(`/api/notes/${noteId}`, {
      method: "PUT",
      body: JSON.stringify({ folder_id: folderId }),
    });
    showToast("已归类", `已移动到“${folder?.name || "目标文件夹"}”`);
    await refresh();
  } catch (err) {
    showToast("移动失败", err.message);
  }
});

function showInlineFolderInput(parentId = null) {
  document.querySelectorAll(".folder-inline-create").forEach((item) => item.remove());
  const slot = parentId == null
    ? $("#new-folder-inline")
    : document.querySelector(`.folder-node[data-folder-id="${parentId}"]`);
  if (!slot) return;

  const form = document.createElement("form");
  form.className = "folder-inline-create";
  form.innerHTML = `
    <input type="text" maxlength="60" placeholder="输入分区名称" aria-label="分区名称" autocomplete="off">
    <button type="submit" class="ghost" title="创建分区">确定</button>
    <button type="button" class="ghost" data-cancel-folder title="取消">取消</button>`;
  if (parentId == null) slot.replaceChildren(form);
  else slot.insertBefore(form, slot.querySelector(".folder-children"));

  const input = form.querySelector("input");
  const close = () => form.remove();
  form.querySelector("[data-cancel-folder]").addEventListener("click", close);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = input.value.trim();
    if (!name) {
      input.focus();
      return;
    }
    const submit = form.querySelector("button[type=submit]");
    submit.disabled = true;
    try {
      await request("/api/folders", { method: "POST", body: JSON.stringify({ parent_id: parentId, name }) });
      if (parentId != null) state.expandedFolders.add(parentId);
      await refresh();
    } catch (error) {
      submit.disabled = false;
      showToast("创建失败", error.message);
    }
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
    }
  });
  input.focus();
}

$("#new-folder-btn").addEventListener("click", async () => {
  showInlineFolderInput(null);
});

$("#tag-nav").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-tag]");
  if (!button) return;
  state.filter = { tag: button.dataset.tag };
  await refresh();
});

$("#tag-manager-button")?.addEventListener("click", (event) => {
  event.preventDefault();
  event.stopPropagation();
  openTagManager();
});

$("#tag-manager-close")?.addEventListener("click", () => $("#tag-manager-dialog")?.close());

$("#tag-governance-content")?.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-tag-from][data-tag-to]");
  if (!button) return;
  const source = button.dataset.tagFrom;
  const target = button.dataset.tagTo;
  if (!await showConfirm(`将标签 #${source} 合并到 #${target}？`, "合并标签", { confirmText: "合并" })) return;
  try {
    const result = await request("/api/tags/merge", {
      method: "POST",
      body: JSON.stringify({ from: source, to: target }),
    });
    showToast("已合并标签", `更新了 ${result.affected_notes} 篇笔记`);
    await Promise.all([refresh(), openTagManager()]);
  } catch (error) {
    showToast("合并失败", error.message);
  }
});

$("#clear-filter").addEventListener("click", async () => {
  state.filter = {};
  await refresh();
});

document.querySelectorAll(".result-scope-button").forEach((button) => {
  button.addEventListener("click", () => {
    state.resultScope = button.dataset.resultScope || "note";
    document.querySelectorAll(".result-scope-button").forEach((item) => {
      item.classList.toggle("active", item === button);
    });
    renderTimeline();
  });
});

$("#result-sort").addEventListener("click", () => {
  state.resultSortDirection = state.resultSortDirection === "desc" ? "asc" : "desc";
  const button = $("#result-sort");
  const isAscending = state.resultSortDirection === "asc";
  button.textContent = `按更新时间 ${isAscending ? "↑" : "↓"}`;
  button.title = `当前按更新时间${isAscending ? "升序" : "降序"}，点击切换`;
  renderTimeline();
});

function rememberEditorSelection() {
  const selection = window.getSelection();
  if (!selection?.rangeCount) return;
  const range = selection.getRangeAt(0);
  const container = range.commonAncestorContainer.nodeType === Node.ELEMENT_NODE
    ? range.commonAncestorContainer
    : range.commonAncestorContainer.parentElement;
  const editor = container?.closest?.(".note-content.editable");
  if (editor) state.editorSelection = range.cloneRange();
}

function restoreEditorSelection(editor) {
  const range = state.editorSelection;
  const container = range && (range.commonAncestorContainer.nodeType === Node.ELEMENT_NODE
    ? range.commonAncestorContainer
    : range.commonAncestorContainer.parentElement);
  editor.focus({ preventScroll: true });
  if (!range || !container || !editor.contains(container)) return;
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
}

function applyEditorCommand(editor, command, value = null) {
  restoreEditorSelection(editor);
  document.execCommand(command, false, value);
  rememberEditorSelection();
  editor.dispatchEvent(new Event("input", { bubbles: true }));
}

document.addEventListener("selectionchange", rememberEditorSelection);
document.addEventListener("pointerdown", (event) => {
  if (event.target.closest(".editor-color-swatch")) rememberEditorSelection();
}, true);

document.addEventListener("mousedown", (event) => {
  if (event.target.closest(".editor-tool")) event.preventDefault();
});

document.addEventListener("click", (event) => {
  const tool = event.target.closest(".editor-tool");
  if (!tool) return;
  const editor = $("#editor-dock-content .note-content.editable");
  if (!editor) return;
  applyEditorCommand(editor, tool.dataset.editorCommand, tool.dataset.editorValue || null);
});

document.addEventListener("change", (event) => {
  const select = event.target.closest(".editor-format-select");
  const colorInput = event.target.closest(".editor-color-input");
  if (!select && !colorInput) return;
  const editor = $("#editor-dock-content .note-content.editable");
  if (!editor) return;
  if (select) {
    applyEditorCommand(editor, "formatBlock", `<${select.value}>`);
  } else {
    colorInput.closest(".editor-color-swatch")?.style.setProperty("--editor-picked-color", colorInput.value);
    applyEditorCommand(editor, colorInput.dataset.editorColor, colorInput.value);
  }
});

$("#stats-panel").addEventListener("click", async (event) => {
  const item = event.target.closest(".stat-item[data-since]");
  if (!item) return;
  const since = item.dataset.since;
  // 再次点击同一筛选则取消
  if (state.filter.since === since) {
    delete state.filter.since;
  } else {
    state.filter = { since };
    $("#search").value = "";
    $("#search-clear").classList.add("hidden");
  }
  await refresh();
});

async function handleNoteInteraction(event) {
  const draftAction = event.target.closest("[data-action='save-new-note'], [data-action='cancel-new-note'], [data-action='remove-new-note-attachment']");
  if (draftAction) {
    if (draftAction.dataset.action === "save-new-note") await saveNewNote();
    else if (draftAction.dataset.action === "cancel-new-note") await cancelNewNote();
    else {
      state.imageDataUrl = null;
      state.imageName = null;
      renderEditorDock();
    }
    return;
  }
  const reportSource = event.target.closest("[data-report-source-id]");
  if (reportSource) {
    toggleReportSource(reportSource.dataset.reportSourceKind, Number(reportSource.dataset.reportSourceId));
    renderTimeline();
    return;
  }
  const documentAction = event.target.closest("[data-document-card-action]");
  if (documentAction) {
    const documentId = Number(documentAction.dataset.documentId);
    const document = state.documents.find((item) => item.id === documentId)
      || state.searchDocuments.find((item) => item.id === documentId);
    if (documentAction.dataset.documentCardAction === "preview") {
      await openImportedDocument(documentId, document?.original_name, {
        chunkId: Number(documentAction.dataset.documentChunkId) || null,
        evidence: documentAction.dataset.documentEvidence || "",
        searchQuery: documentAction.dataset.documentSearchQuery || "",
        openedFromSearch: true,
      });
    } else if (document) {
      await handleDocumentAction(document, documentAction.dataset.documentCardAction);
    }
    return;
  }
  const resultSelect = event.target.closest("[data-action='select-note']");
  if (resultSelect) {
    await flushPendingContentSaves();
    state.selectedNoteId = Number(resultSelect.dataset.id);
    renderTimeline();
    switchPane("editor-pane");
    const tabId = Number(resultSelect.dataset.resultTabId) || null;
    if (tabId && tabId !== state.selectedNoteId) {
      const tabsBar = document.querySelector(`.note-tabs-bar[data-note-id="${state.selectedNoteId}"]`);
      if (tabsBar) {
        await loadTabsBar(state.selectedNoteId, tabsBar);
        await switchTab(state.selectedNoteId, tabId, { tabsBar });
      }
    }
    return;
  }
  const title = event.target.closest("[data-action='edit-title']");
  if (title) { makeTitleEditable(title); return; }
  const removeTag = event.target.closest("[data-action='remove-tag']");
  if (removeTag) {
    try {
      await request(`/api/notes/${Number(removeTag.dataset.id)}/tags/remove`, {
        method: "POST",
        body: JSON.stringify({ tag: removeTag.dataset.tag }),
      });
      showToast("已删除", `标签“${removeTag.dataset.tag}”不会被自动加回`);
      await refresh();
    } catch (err) {
      showToast("删除失败", err.message);
    }
    return;
  }
  // 分类 chip 内联编辑（点击大分类/小分类直接编辑）
  const catChip = event.target.closest("[data-action='edit-category']");
  if (catChip) { makeCategoryEditable(catChip); return; }
  // 标签 chip 内联编辑
  const tagChip = event.target.closest("[data-action='edit-tag']");
  if (tagChip) { makeTagEditable(tagChip); return; }
  // wiki-link 跳转已由 document 级监听统一处理，这里跳过
  if (event.target.closest(".wiki-link")) return;
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  if (button.dataset.action === "scroll-editor-top") {
    const editor = $("#editor-dock-content");
    editor?.scrollTo({ top: 0, behavior: "smooth" });
    return;
  }
  const noteId = Number(button.dataset.id);
  let note = state.notes.find((item) => item.id === noteId)
    || (state.allNotes || []).find((item) => item.id === noteId);
  if (!note) {
    // 笔记可能通过 openDoc 打开，不在 state 列表中，按需获取
    try { note = await request(`/api/notes/${noteId}`); } catch (e) { return; }
  }
  if (!note) return;
  if (button.dataset.action === "delete") {
    if (!await showConfirm(`删除"${note.title}"？`, "删除笔记", { confirmText: "删除", isDanger: true })) return;
    try {
      await request(`/api/notes/${note.id}`, { method: "DELETE" });
      showToast("已删除", note.title);
      await refresh();
    } catch (err) {
      showToast("删除失败", err.message);
    }
  } else if (button.dataset.action === "ocr") {
    try {
      await request(`/api/notes/${note.id}/ocr`, { method: "POST", body: "{}" });
      showToast("已提交", "正在重新识别图片文字");
      await refresh();
    } catch (err) {
      showToast("操作失败", err.message);
    }
  } else if (button.dataset.action === "intelligence") {
    try {
      await request(`/api/notes/${note.id}/intelligence`, { method: "POST", body: "{}" });
      showToast("已提交", "正在重新整理笔记");
      await refresh();
    } catch (err) {
      showToast("操作失败", err.message);
    }
  } else if (button.dataset.action === "pin") {
    try {
      const result = await request(`/api/notes/${note.id}/pin`, { method: "POST", body: "{}" });
      showToast(result.pinned ? "已置顶" : "已取消置顶", note.title);
      await refresh();
    } catch (err) {
      showToast("操作失败", err.message);
    }
  } else if (button.dataset.action === "favorite") {
    try {
      const result = await request(`/api/notes/${note.id}/favorite`, { method: "POST", body: "{}" });
      showToast(result.favorite ? "已收藏" : "已取消收藏", note.title);
      await refresh();
    } catch (err) {
      showToast("操作失败", err.message);
    }
  } else if (button.dataset.action === "toggle-ocr") {
    const noteEl = button.closest(".note");
    const ocrContent = noteEl?.querySelector(".ocr-content");
    if (!ocrContent) return;
    if (ocrContent.hasAttribute("hidden")) {
      ocrContent.removeAttribute("hidden");
      button.textContent = "折叠OCR内容";
    } else {
      ocrContent.setAttribute("hidden", "");
      button.textContent = "展开OCR内容";
    }
  } else if (button.dataset.action === "versions") {
    showVersionsDialog(Number(button.dataset.id));
  }
}

$("#timeline").addEventListener("click", handleNoteInteraction);
$("#editor-dock-content").addEventListener("click", handleNoteInteraction);

// 点击笔记正文时折叠 OCR 内容（避免正文与 OCR 同时占据视觉焦点）
document.addEventListener("click", (event) => {
  const content = event.target.closest(".note-content.editable");
  if (!content) return;
  const noteEl = content.closest(".note");
  if (!noteEl) return;
  const ocrContent = noteEl.querySelector(".ocr-content");
  if (ocrContent && !ocrContent.hasAttribute("hidden")) {
    ocrContent.setAttribute("hidden", "");
    const toggleBtn = noteEl.querySelector('[data-action="toggle-ocr"]');
    if (toggleBtn) toggleBtn.textContent = "展开OCR内容";
  }
});

// 编辑器内的全文默认展开；保留悬停展开，便于小屏幕的紧凑查看。
$("#editor-dock-content").addEventListener("mouseover", (event) => {
  const noteEl = event.target.closest(".note");
  if (!noteEl) return;
  const detail = noteEl.querySelector("details.note-detail");
  if (detail && !detail.open) detail.setAttribute("open", "");
  // 收起其他未在编辑中的笔记详情，避免同时展开多篇造成视觉拥挤
  document.querySelectorAll("#editor-dock-content .note").forEach((el) => {
    if (el === noteEl) return;
    const d = el.querySelector("details.note-detail");
    if (d && d.open && !el.querySelector(".note-content.editable[data-editing]")) {
      d.removeAttribute("open");
    }
  });
});

/* ---- 页签功能 ---- */
async function loadTabsBar(noteId, scopedTabsBar = null) {
  const tabsBar = scopedTabsBar || document.querySelector(`.note-tabs-bar[data-note-id="${noteId}"]`);
  if (!tabsBar || tabsBar.dataset.loaded === "1") return;
  tabsBar.dataset.loaded = "1";
  try {
    const tabs = await request(`/api/notes/${noteId}/tabs`);
    renderTabsBar(tabsBar, tabs, Number(tabsBar.dataset.activeTab));
  } catch (e) { /* ignore */ }
}

function renderTabsBar(tabsBar, tabs, activeId) {
  if (!tabsBar) return;
  // 编辑器始终提供新增入口；参考文档只读预览只保留切换入口。
  const noteId = Number(tabsBar.dataset.noteId);
  const isReference = Boolean(tabsBar.closest(".reference-note"));
  const chips = tabs.map((tab, index) => {
    const isActive = tab.id === activeId;
    const displayName = tab.tab_name || `页签${index + 1}`;
    const visibleName = `${isActive ? ">" : ""}${displayName}`;
    const color = tabColor(displayName);
    return `<button class="tab-chip ${isActive ? "active" : ""}" data-tab-id="${tab.id}" style="--tab-color:${color}" title="${escapeHtml(displayName)}">
      <span class="tab-dot" style="background:${color}"></span>
      <span class="tab-label" data-tab-id="${tab.id}" data-tab-name="${escapeHtml(displayName)}">${escapeHtml(visibleName)}</span>
    </button>`;
  }).join("");
  const addButton = isReference ? "" : `<button class="tab-add-btn" data-action="add-tab-inline" data-note-id="${noteId}" type="button" aria-label="新增页签" title="新增页签：将同一主题拆分为背景、方案、行动项等独立内容"><span class="tab-add-icon" aria-hidden="true">+</span><span class="tab-add-label">新增页签</span></button>`;
  tabsBar.innerHTML = `${chips}${addButton}`;
}

async function switchTab(noteId, tabId, { focusEditor = false, highlightTerms = [], tabsBar: scopedTabsBar = null } = {}) {
  await flushPendingContentSaves();
  const tabsBar = scopedTabsBar || document.querySelector(`.note-tabs-bar[data-note-id="${noteId}"]`);
  const noteEl = tabsBar?.closest(".note");
  if (!noteEl) return;
  const isReference = noteEl.classList.contains("reference-note");
  tabsBar.dataset.activeTab = tabId;
  try {
    const tabNote = await request(`/api/notes/${tabId}`);
    const contentEl = noteEl.querySelector(".note-tab-content");
    contentEl.dataset.activeTab = tabId;
    contentEl.innerHTML = isReference
      ? `<div class="note-content reference-note-content">${tabNote.content ? renderNoteContent(tabNote.content, highlightTerms) : "<p class='muted'>此页签暂无正文。</p>"}</div>`
      : `<div class="note-content editable" contenteditable="true" data-note-id="${tabId}" data-field="content" data-placeholder="点击此处开始编辑..." title="点击直接编辑">${tabNote.content ? renderNoteContent(tabNote.content, highlightTerms) : ""}</div>`;
    // 折叠状态下，更新 excerpt 预览为当前页签内容
    const detail = noteEl.querySelector("details.note-detail");
    if (detail && !detail.hasAttribute("open")) {
      const excerptEl = noteEl.querySelector(".note-excerpt");
      if (excerptEl) {
        const preview = excerpt(tabNote.content || tabNote.ocr_text || "", 200);
        if (preview) {
          excerptEl.innerHTML = renderWikiLinks(preview);
        }
      }
    }
    // 更新页签高亮
    const tabs = await request(`/api/notes/${noteId}/tabs`);
    renderTabsBar(tabsBar, tabs, tabId);
    if (focusEditor && !isReference) {
      requestAnimationFrame(() => contentEl.querySelector(".note-content.editable")?.focus());
    }
    if (highlightTerms.length) scrollToCitation(contentEl);
  } catch (e) {
    showToast("切换失败", e.message);
  }
}

async function addTab(parentNoteId, scopedTabsBar = null) {
    // 统一以页签编号命名，主笔记是页签1。
  try {
    // 查询现有页签数决定编号
    const tabs = await request(`/api/notes/${parentNoteId}/tabs`);
    const n = tabs.length;
    const name = `页签${n + 1}`;
    const tab = await request(`/api/notes/${parentNoteId}/tabs`, {
      method: "POST",
      body: JSON.stringify({ tab_name: name, content: "" }),
    });
    showToast("已创建页签", name);
    // 重新加载页签栏并切换到新页签（新页签正文可编辑）
    const tabsBar = scopedTabsBar || document.querySelector(`.note-tabs-bar[data-note-id="${parentNoteId}"]`);
    if (tabsBar) {
      tabsBar.dataset.loaded = "0";
      tabsBar.dataset.activeTab = tab.id;
      await loadTabsBar(parentNoteId, tabsBar);
      await switchTab(parentNoteId, tab.id, { focusEditor: true, tabsBar });
    }
  } catch (e) {
    showToast("创建失败", e.message);
  }
}

async function deleteTab(tabId) {
  if (!await showConfirm("删除该页签？", "删除页签", { isDanger: true })) return;
  try {
    await request(`/api/notes/${tabId}`, { method: "DELETE" });
    showToast("已删除页签", "");
    await refresh();
  } catch (e) {
    showToast("删除失败", e.message);
  }
}

// 页签栏事件委托：点击切换、+ 号新建、右键菜单、双击重命名
let tabClickTimer = null;
document.addEventListener("click", (event) => {
  // + 按钮：直接新建页签
  const addBtn = event.target.closest("[data-action='add-tab-inline']");
  if (addBtn) {
    event.stopPropagation();
    addTab(Number(addBtn.dataset.noteId), addBtn.closest(".note-tabs-bar"));
    return;
  }
  const tabChip = event.target.closest(".tab-chip");
  if (!tabChip) return;
  const tabsBar = tabChip.closest(".note-tabs-bar");
  if (!tabsBar) return;
  const noteId = Number(tabsBar.dataset.noteId);
  const tabId = Number(tabChip.dataset.tabId);
  // 点击 tab-label 时延迟切换，给 dblclick 重命名留出取消时间
  if (event.target.closest(".tab-label")) {
    clearTimeout(tabClickTimer);
    tabClickTimer = setTimeout(() => switchTab(noteId, tabId, { tabsBar }), 220);
    return;
  }
  switchTab(noteId, tabId, { tabsBar });
});

// 双击页签名重命名（内联编辑）
document.addEventListener("dblclick", (event) => {
  const label = event.target.closest(".tab-label");
  if (!label) return;
  if (label.closest(".reference-note")) return;
  event.preventDefault();
  clearTimeout(tabClickTimer); // 取消延迟的 switchTab，避免输入框被刷新掉
  const tabId = Number(label.dataset.tabId);
  const oldName = label.dataset.tabName || label.textContent.replace(/^>/, "");
  makeTabLabelEditable(label, tabId, oldName);
});

function makeTabLabelEditable(label, tabId, oldName) {
  const input = document.createElement("input");
  input.type = "text";
  input.value = oldName;
  input.className = "tab-rename-input";
  input.style.width = Math.max(60, oldName.length * 12) + "px";
  label.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    const newName = input.value.trim();
    if (!commit || !newName || newName === oldName) {
      // 取消：还原 label
      const newLabel = document.createElement("span");
      newLabel.className = "tab-label";
      newLabel.dataset.tabId = tabId;
      newLabel.dataset.tabName = oldName;
      newLabel.textContent = oldName;
      input.replaceWith(newLabel);
      return;
    }
    try {
      const current = await request(`/api/notes/${tabId}`);
      await request(`/api/notes/${tabId}`, {
        method: "PUT",
        body: JSON.stringify({ tab_name: newName, title: newName }),
      });
      showToast("已重命名", newName);
      await refresh();
    } catch (e) {
      showToast("重命名失败", e.message);
      await refresh();
    }
  };
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); input.blur(); }
    else if (e.key === "Escape") { e.preventDefault(); finish(false); }
  });
}

// contenteditable 正文 blur 自动保存
document.addEventListener("blur", async (event) => {
  const content = event.target.closest(".note-content.editable");
  if (!content) return;
  if (content.dataset.draft === "1") {
    syncNewNoteDraftFromDom();
    return;
  }
  const noteId = Number(content.dataset.noteId);
  const text = editableContentToMarkdown(content);
  // 避免重复保存：对比上次内容
  if (content.dataset.saved === text) return;
  content.dataset.saved = text;
  const save = request(`/api/notes/${noteId}`, {
    method: "PUT",
    body: JSON.stringify({ content: text }),
  });
  state.pendingContentSaves.set(noteId, save);
  try {
    // 回写列表和全量缓存，避免切换笔记时重新渲染出旧正文。
    const updated = await save;
    syncCachedNote(noteId, { content: updated.content ?? text, updated_at: updated.updated_at });
    const savedTime = updated.updated_at?.slice(11, 16);
    if (savedTime) content.closest(".note")?.querySelector(".note-header time")?.replaceChildren(savedTime);
  } catch (e) {
    showToast("保存失败", e.message);
  } finally {
    if (state.pendingContentSaves.get(noteId) === save) state.pendingContentSaves.delete(noteId);
  }
}, true);

// contenteditable 聚焦时清除占位符样式（CSS :empty + data-placeholder 处理）
document.addEventListener("focus", (event) => {
  const content = event.target.closest(".note-content.editable");
  if (!content) return;
  // 聚焦时如果有占位符提示（通过 CSS :empty::before 显示），自动不显示
  // 实际清理由 CSS 处理，JS 仅标记 editing 状态
  content.dataset.editing = "1";
  // 仅仅进入再退出编辑区不应触发保存，更不能把展示中的表格降级为纯文本。
  content.dataset.saved = editableContentToMarkdown(content);
  // 激活编辑时调整笔记位置：让笔记标题停在搜索栏下方，保证标题始终可见。
  // 不用 scrollIntoView（它会冲出滚动容器、被搜索栏遮挡），改用相对 timeline 的精确滚动。
  const noteEl = content.closest(".note");
  const timeline = document.getElementById("timeline");
  if (noteEl && timeline) {
    const searchPanel = document.querySelector(".search-panel");
    // 顶部留白：搜索栏高度 + 少量间距，避免标题被搜索栏遮挡
    const topOffset = (searchPanel ? searchPanel.offsetHeight : 0) + 12;
    // offsetTop 是相对最近的 positioned 祖先；timeline 是滚动容器，需用相对它的距离
    let relativeTop = 0;
    let node = noteEl;
    while (node && node !== timeline) {
      relativeTop += node.offsetTop;
      node = node.offsetParent;
    }
    if (node === timeline) {
      timeline.scrollTo({ top: Math.max(0, relativeTop - topOffset), behavior: "smooth" });
    } else {
      // 兜底：无法精确定位时退回 scrollIntoView
      noteEl.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }
}, true);
document.addEventListener("focusout", (event) => {
  const content = event.target.closest(".note-content.editable");
  if (!content) return;
  delete content.dataset.editing;
}, true);

// details 展开时懒加载页签内容（页签栏已在 details 外部，始终可见）
document.addEventListener("toggle", (event) => {
  const detail = event.target.closest("details.note-detail");
  if (!detail || !detail.open) return;
  // 如果 details 内有未加载的页签内容，可在此补充加载逻辑
}, true);

document.addEventListener("contextmenu", (event) => {
  const tabChip = event.target.closest(".tab-chip");
  if (!tabChip) return;
  event.preventDefault();
  const tabId = Number(tabChip.dataset.tabId);
  const label = tabChip.querySelector(".tab-label")?.textContent || "";
  showTabContextMenu(event.clientX, event.clientY, tabId, label);
});

function showTabContextMenu(x, y, tabId, currentName) {
  document.querySelectorAll(".context-menu").forEach((m) => m.remove());
  const menu = document.createElement("div");
  menu.className = "context-menu card";
  menu.style.left = x + "px";
  menu.style.top = y + "px";
  menu.innerHTML = `
    <button class="context-item danger" data-tab-action="delete" type="button">🗑️ 删除页签</button>`;
  document.body.appendChild(menu);
  menu.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-tab-action]");
    if (!btn) return;
    const action = btn.dataset.tabAction;
    menu.remove();
    if (action === "delete") await deleteTab(tabId);
  });
  setTimeout(() => {
    document.addEventListener("click", function close() {
      menu.remove();
      document.removeEventListener("click", close);
    }, { once: true });
  }, 0);
}

document.addEventListener("paste", (event) => {
  const image = [...event.clipboardData.items].find((item) => item.type.startsWith("image/"));
  if (image) {
    const file = image.getAsFile();
    const reader = new FileReader();
    reader.onload = () => {
      state.imageDataUrl = reader.result;
      state.imageName = file.name || "clipboard.png";
      // 粘贴截图同样进入右侧新建草稿，避免再回到中栏的小录入条。
      if (!state.newNoteDraft) beginNewNote(null, { keepAttachment: true });
      else renderEditorDock();
      showToast("截图已附加", "保存时写入图片目录");
    };
    reader.readAsDataURL(file);
    return;
  }
  const textarea = $("#inline-content");
  if (event.target !== textarea) return;
  const markdown = tableToMarkdown(event.clipboardData.getData("text/plain"));
  if (!markdown) return;
  event.preventDefault();
  insertAtCursor(textarea, markdown);
});

// 设置面板 TAB 切换
document.querySelectorAll(".settings-tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    const target = tab.dataset.tabTarget;
    document.querySelectorAll(".settings-tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".settings-panel-tab").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    document.querySelector(`.settings-panel-tab[data-tab="${target}"]`).classList.add("active");
  });
});

$("#settings-button").addEventListener("click", async () => {
  const settings = await request("/api/settings");
  $("#data-dir").value = settings.data_dir;
  state.selectedDirectory = null;
  $("#switch-directory").disabled = true;
  $("#migrate-directory").disabled = true;
  // 填充风格下拉，以 localStorage 保存的值为准
  const current = savedTheme();
  $("#theme-select").innerHTML = THEMES.map((t) =>
    `<option value="${t.id}" ${t.id === current ? "selected" : ""}>${t.name}</option>`
  ).join("");
  applyTheme(current);
  // 背景图状态
  toggleClearBtn("#clear-background", !!settings.background);
  // AI 整理模式
  $("#ai-auto-apply").checked = settings.ai_auto_apply !== false;
  updateAiModeLabel();
  $("#settings-dialog").showModal();
  refreshModelStatus();
  loadModelList();
});

async function refreshModelStatus() {
  const el = $("#model-status-text");
  if (!el) return;
  try {
    const status = await request("/api/intelligence/status");
    if (!status.available) {
      el.textContent = `模型不可用：${status.reason || "未返回具体原因"}`;
    } else {
      el.textContent = status.model_loaded
        ? "模型已加载（占用约 3-4GB 内存）"
        : "模型未加载（内存已释放）";
    }
    const unloadButton = $("#unload-model-btn");
    unloadButton.disabled = !status.model_loaded;
    unloadButton.textContent = status.model_loaded ? "释放模型内存" : "当前未加载";
  } catch (err) {
    el.textContent = "状态查询失败";
    $("#unload-model-btn").disabled = true;
    $("#unload-model-btn").textContent = "当前未加载";
  }
}

$("#unload-model-btn").addEventListener("click", async () => {
  const btn = $("#unload-model-btn");
  btn.disabled = true;
  $("#model-status-text").textContent = "正在卸载模型...";
  try {
    await request("/api/intelligence/unload", { method: "POST", body: "{}" });
    await refreshModelStatus();
  } catch (err) {
    $("#model-status-text").textContent = `卸载失败: ${err.message}`;
    btn.disabled = false;
  }
});

// ---------- 模型选择（可插拔替换） ----------
async function loadModelList() {
  const select = $("#llm-model-select");
  if (!select) return;
  try {
    const data = await request("/api/intelligence/models");
    const models = data.models || [];
    const current = data.current || "";
    select.innerHTML = models.length
      ? models.map((m) => {
          const selected = m.path === current || (!current && m.is_default);
          return `<option value="${escapeHtml(m.path)}" ${selected ? "selected" : ""}>
                    ${escapeHtml(m.name)} (${m.size_mb} MB)${m.is_default ? " · 默认" : ""}
                  </option>`;
        }).join("")
      : `<option value="" disabled>未找到模型文件</option>`;
  } catch (err) {
    select.innerHTML = `<option value="" disabled>加载失败</option>`;
  }
}

$("#apply-model-btn").addEventListener("click", async () => {
  const btn = $("#apply-model-btn");
  const select = $("#llm-model-select");
  const modelPath = select.value;
  if (!modelPath) return;
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = "切换中...";
  try {
    await request("/api/intelligence/models", {
      method: "POST",
      body: JSON.stringify({ model_path: modelPath }),
    });
    showToast("模型已切换", "下次调用时自动加载新模型");
  } catch (err) {
    showToast("切换失败", err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
});

// 重新查看启动引导（设置面板按钮，方便演示）
$("#show-onboarding-btn").addEventListener("click", () => {
  $("#settings-dialog").close();
  showOnboardingDialog(true);
});

function updateAiModeLabel() {
  const enabled = $("#ai-auto-apply").checked;
  $("#ai-mode-label").textContent = enabled ? "新笔记将自动整理分类与标签" : "已关闭自动整理，可按需点击“重新整理”";
}

$("#ai-auto-apply").addEventListener("change", async (e) => {
  const enabled = e.target.checked;
  updateAiModeLabel();
  try {
    await request("/api/settings/ai-mode", {
      method: "POST",
      body: JSON.stringify({ auto_apply: enabled }),
    });
    showToast(enabled ? "已开启自动整理" : "已关闭自动整理", enabled ? "新笔记将自动整理分类与标签" : "新笔记不会自动调用 AI，可按需点击“重新整理”");
  } catch (err) {
    e.target.checked = !enabled;
    updateAiModeLabel();
    showToast("设置失败", err.message);
  }
});

function toggleClearBtn(selector, show) {
  const btn = $(selector);
  if (btn) btn.classList.toggle("hidden", !show);
}

$("#theme-select").addEventListener("change", async (e) => {
  const themeId = e.target.value;
  applyTheme(themeId);
  try {
    await request("/api/settings/theme", { method: "POST", body: JSON.stringify({ theme: themeId }) });
  } catch (err) { /* localStorage 已保存，后端失败不影响使用 */ }
});

// ---------- 自定义背景图 ----------
function applyBackground(url) {
  document.body.classList.toggle("has-custom-bg", !!url);
  if (url) {
    document.body.style.backgroundImage = `url("${url}")`;
  } else {
    document.body.style.backgroundImage = "";
  }
}

$("#upload-background").addEventListener("click", () => $("#background-input").click());

$("#background-input").addEventListener("change", async (e) => {
  const file = e.target.files?.[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = async () => {
    try {
      const result = await request("/api/settings/background", {
        method: "POST",
        body: JSON.stringify({ image_data_url: reader.result, image_name: file.name }),
      });
      applyBackground(result.url + "?t=" + Date.now());
      toggleClearBtn("#clear-background", true);
      showToast("背景已更新", "自定义背景图已应用");
    } catch (err) {
      showToast("上传失败", err.message);
    }
    e.target.value = "";
  };
  reader.readAsDataURL(file);
});

$("#clear-background").addEventListener("click", async () => {
  try {
    await request("/api/settings/background", { method: "DELETE" });
    applyBackground(null);
    toggleClearBtn("#clear-background", false);
    showToast("已清除", "已恢复主题默认背景");
  } catch (err) {
    showToast("操作失败", err.message);
  }
});

$("#choose-directory").addEventListener("click", async () => {
  const result = await request("/api/settings/choose-data-dir", { method: "POST", body: "{}" });
  if (!result.data_dir) return;
  state.selectedDirectory = result.data_dir;
  $("#data-dir").value = result.data_dir;
  $("#switch-directory").disabled = false;
  $("#migrate-directory").disabled = false;
});

$("#data-dir").addEventListener("input", (event) => {
  const selected = event.target.value.trim();
  state.selectedDirectory = selected || null;
  $("#switch-directory").disabled = !state.selectedDirectory;
  $("#migrate-directory").disabled = !state.selectedDirectory;
});

$("#switch-directory").addEventListener("click", async () => {
  if (!state.selectedDirectory) return;
  if (!await showConfirm("仅切换到所选数据目录，不复制当前笔记、文档或附件。", "切换数据目录", { confirmText: "切换" })) return;
  const result = await request("/api/settings/switch-data-dir", { method: "POST", body: JSON.stringify({ data_dir: state.selectedDirectory }) });
  $("#settings-dialog").close();
  await refresh();
  showToast("已切换数据目录", result.data_dir);
});

$("#migrate-directory").addEventListener("click", async () => {
  if (!state.selectedDirectory) return;
  if (!await showConfirm("复制全部数据并切换到新目录？原目录会保留。", "迁移数据", { confirmText: "迁移" })) return;
  await request("/api/settings/migrate", { method: "POST", body: JSON.stringify({ data_dir: state.selectedDirectory }) });
  $("#settings-dialog").close();
  await refresh();
});

$("#export-data").addEventListener("click", async () => {
  const btn = $("#export-data");
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = "导出中...";
  try {
    const filename = await downloadFile("/api/export/backup", "pocket-memory-backup.zip");
    showToast("备份已开始下载", `已生成完整备份：${filename}`);
  } catch (err) {
    showToast("导出失败", err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
});

// ---------- 参考文档主区域预览 ----------
function formatDateTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function formatDuration(totalSeconds) {
  const s = Number(totalSeconds) || 0;
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.round(s / 60 * 10) / 10} 分钟`;
  if (s < 86400) return `${Math.round(s / 3600 * 10) / 10} 小时`;
  return `${Math.round(s / 86400 * 10) / 10} 天`;
}

function parseFrontMatter(text) {
  const match = text.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
  if (!match) return { meta: null, body: text };
  const meta = {};
  for (const line of match[1].split(/\r?\n/)) {
    const m = line.match(/^(\w+):\s*(.*)$/);
    if (!m) continue;
    let val = m[2].trim();
    if (val.startsWith('"') && val.endsWith('"')) val = val.slice(1, -1);
    if (val.startsWith("[") && val.endsWith("]")) {
      val = val.slice(1, -1).split(",").map((s) => s.trim()).filter(Boolean);
    }
    meta[m[1]] = val;
  }
  return { meta, body: match[2] };
}

function renderMarkdown(text) {
  const { meta, body } = parseFrontMatter(text);
  let html = "";
  if (meta) {
    const tags = Array.isArray(meta.tags) ? meta.tags : [];
    const cat = meta.category ? escapeHtml(meta.category) + (meta.subcategory ? ` / ${escapeHtml(meta.subcategory)}` : "") : "";
    const created = formatDateTime(meta.created_at);
    const updated = formatDateTime(meta.updated_at);
    html += `<div class="doc-meta">`;
    if (cat) html += `<span class="category-chip">${cat}</span>`;
    if (created) html += `<span class="doc-meta-time">创建 ${escapeHtml(created)}</span>`;
    if (updated && updated !== created) html += `<span class="doc-meta-time">更新 ${escapeHtml(updated)}</span>`;
    if (tags.length) {
      html += `<div class="doc-meta-tags">${tags.map((t) => `<span class="tag-chip">#${escapeHtml(t)}</span>`).join("")}</div>`;
    }
    html += `</div>`;
  }
  const lines = body.split("\n");
  let inCode = false;
  let firstH1Skipped = false;
  for (const line of lines) {
    if (line.trim().startsWith("```")) {
      inCode = !inCode;
      html += inCode ? "<pre>" : "</pre>";
      continue;
    }
    if (inCode) { html += escapeHtml(line) + "\n"; continue; }
    const heading = line.match(/^(#{1,6})\s+(.*)/);
    if (heading) {
      if (heading[1].length === 1 && !firstH1Skipped) {
        firstH1Skipped = true;
        continue;
      }
      html += `<h${heading[1].length}>${escapeHtml(heading[2])}</h${heading[1].length}>`;
      continue;
    }
    if (!line.trim()) continue;
    let processed = escapeHtml(line);
    processed = processed.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (_, alt, src) => {
      let path = src;
      if (!path.startsWith("http") && !path.startsWith("/")) {
        path = "/data/" + path.replace(/^(\.\.\/)+/, "");
      }
      return `<img alt="${alt}" src="${escapeHtml(path)}">`;
    });
    processed = processed.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    processed = processed.replace(/`([^`]+)`/g, "<code>$1</code>");
    html += `<p>${processed}</p>`;
  }
  return html || "<p class='muted'>文档为空</p>";
}

async function openDoc(url, title, evidence = "") {
  const documentMatch = url.match(/^\/api\/documents\/(\d+)\/file(?:\?.*)?$/);
  if (documentMatch) {
    const documentParams = new URLSearchParams((url.split("?")[1] || ""));
    await openImportedDocument(Number(documentMatch[1]), title, {
      chunkId: Number(documentParams.get("chunk_id")) || null,
      evidence,
    });
    return;
  }
  // 优先用 note ID 加载完整笔记（含页签栏+编辑能力），回退到 markdown 只读
  const noteIdMatch = url.match(/[?&]note_id=(\d+)/) || url.match(/^\/api\/notes\/(\d+)/);
  if (noteIdMatch) {
    const noteId = Number(noteIdMatch[1]);
    // 解析 tab_id 参数：用于从智能解答来源直接定位到具体页签
    const tabIdMatch = url.match(/[?&]tab_id=(\d+)/);
    const tabId = tabIdMatch ? Number(tabIdMatch[1]) : null;
    $("#doc-preview-eyebrow").textContent = "参考笔记 · 只读定位";
    $("#doc-preview-title").textContent = title || "参考文档";
    $("#doc-preview-body").innerHTML = `<p class="muted">加载中...</p>`;
    $("#doc-preview").classList.remove("hidden");
    $("#timeline").classList.add("hidden");
    $("#doc-fab").classList.remove("hidden");
    window.scrollTo({ top: 0 });
    try {
      const note = await request(`/api/notes/${noteId}`);
      // 引用视图只展示可核对原文，不能携带编辑、删除或整理操作。
      const citationTerms = citationHighlightTerms(evidence);
      const terms = citationTerms.length ? citationTerms : searchHighlightTerms();
      $("#doc-preview-body").innerHTML = `<div class="timeline reference-timeline">${renderReferenceNoteHtml(note, terms)}</div>`;
      const tabsBar = $("#doc-preview-body .note-tabs-bar");
      if (tabsBar) {
        if (tabId && tabId !== noteId) {
          tabsBar.dataset.activeTab = tabId;
        }
        await loadTabsBar(Number(tabsBar.dataset.noteId), tabsBar);
        if (tabId && tabId !== noteId) {
          await switchTab(noteId, tabId, { highlightTerms: terms, tabsBar });
        }
      }
      if (!tabId || tabId === noteId) scrollToCitation($("#doc-preview-body"));
    } catch (err) {
      $("#doc-preview-body").innerHTML = `<p class="ocr-error">${escapeHtml(err.message)}</p>`;
    }
    return;
  }
  // 回退：直接渲染 markdown
  $("#doc-preview-eyebrow").textContent = "参考文档 · 原文预览";
  $("#doc-preview-title").textContent = title || "参考文档";
  $("#doc-preview-body").innerHTML = `<p class="muted">加载中...</p>`;
  $("#doc-preview").classList.remove("hidden");
  $("#timeline").classList.add("hidden");
  $("#doc-fab").classList.remove("hidden");
  window.scrollTo({ top: 0 });
  try {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error("文档加载失败");
    $("#doc-preview-body").innerHTML = renderMarkdown(await resp.text());
  } catch (err) {
    $("#doc-preview-body").innerHTML = `<p class="ocr-error">${escapeHtml(err.message)}</p>`;
  }
}

async function openImportedDocument(documentId, fallbackTitle = "导入文档", options = {}) {
  $("#doc-preview-eyebrow").textContent = "参考文档 · 原文预览";
  $("#doc-preview-title").textContent = fallbackTitle;
  $("#doc-preview-body").innerHTML = `<p class="muted">正在加载文档内容与切片...</p>`;
  $("#doc-preview").classList.remove("hidden");
  $("#timeline").classList.add("hidden");
  $("#doc-fab").classList.remove("hidden");
  window.scrollTo({ top: 0 });
  try {
    const payload = await request(`/api/documents/${documentId}/chunks`);
    const document = payload.document;
    const chunks = payload.chunks || [];
    $("#doc-preview-title").textContent = document.original_name || fallbackTitle;
    const statusText = documentStatusLabel(document.status);
    const previewAction = `
      <button class="ghost" type="button" data-document-preview-replace="${document.id}">替换原件</button>
      <button class="ghost" type="button" data-document-preview-reindex="${document.id}">重新索引</button>
      <button class="ghost" type="button" data-document-preview-download="${document.id}">下载原件</button>
      <button class="ghost danger" type="button" data-document-preview-delete="${document.id}">删除文档</button>`;
    const evidence = String(options.evidence || "").trim();
    // A reference view must highlight the cited evidence, not every two-character
    // fragment of the search question. The latter paints a long spreadsheet row
    // almost entirely yellow and hides the actual supporting rule.
    const highlightTerms = options.openedFromSearch
      ? directDocumentSearchHighlightTerms(options.searchQuery)
      : evidence
        ? citationHighlightTerms(evidence)
        : searchHighlightTerms();
    const chunkHtml = chunks.length
      ? chunks.map((chunk, index) => `<article class="document-chunk${Number(chunk.id) === Number(options.chunkId) ? " is-reference" : ""}" data-document-chunk-id="${chunk.id}">
          <header><strong>切片 ${index + 1}</strong><span>${escapeHtml(chunk.location || "正文")}</span></header>
          <pre>${highlightText(chunk.text, highlightTerms)}</pre>
        </article>`).join("")
      : `<p class="muted">${document.status === "failed" ? `提取失败：${escapeHtml(document.error || "请重新建立索引")}` : "尚未生成切片，正在处理或等待重新建立索引。"}</p>`;
    $("#doc-preview-body").innerHTML = `
      <div class="document-preview-meta">
        <span>${escapeHtml(document.file_type.toUpperCase())}</span>
        <span>${formatDocumentSize(document.size_bytes)}</span>
        <span>${document.chunk_count || 0} 个切片</span>
        <span>标准策略</span>
        <span>${escapeHtml(statusText)}</span>
      </div>
      <div class="document-preview-actions">${previewAction}</div>
      <section aria-label="已提取切片">${chunkHtml}</section>`;
    if (options.chunkId) {
      requestAnimationFrame(() => {
        const target = $(`#doc-preview-body [data-document-chunk-id="${options.chunkId}"]`);
        target?.scrollIntoView({ behavior: "smooth", block: "center" });
      });
    }
  } catch (error) {
    $("#doc-preview-body").innerHTML = `<p class="ocr-error">${escapeHtml(error.message)}</p>`;
  }
}

async function closeDoc() {
  $("#doc-preview").classList.add("hidden");
  $("#timeline").classList.remove("hidden");
  $("#doc-fab").classList.add("hidden");
  // 重新拉取列表，确保从引用视图返回时能看到最新页签、标题和状态。
  await refresh();
}

$("#doc-preview-close").addEventListener("click", closeDoc);
$("#doc-fab").addEventListener("click", closeDoc);

$("#doc-preview-body").addEventListener("click", async (event) => {
  const download = event.target.closest("[data-document-preview-download]");
  if (download) window.open(`/api/documents/${download.dataset.documentPreviewDownload}/file?download=1`, "_blank", "noopener");
  const replace = event.target.closest("[data-document-preview-replace]");
  if (replace) {
    state.documentReplacementId = Number(replace.dataset.documentPreviewReplace);
    $("#document-replace-input").click();
  }
  const reindex = event.target.closest("[data-document-preview-reindex]");
  if (reindex) {
    const documentId = Number(reindex.dataset.documentPreviewReindex);
    request(`/api/documents/${documentId}/reindex`, { method: "POST", body: "{}" })
      .then(async () => {
        showToast("已提交", "正在重新提取并建立索引");
        await refreshDocuments();
        await openImportedDocument(documentId);
      })
      .catch((error) => showToast("操作失败", error.message));
  }
  const remove = event.target.closest("[data-document-preview-delete]");
  if (remove) {
    const documentId = Number(remove.dataset.documentPreviewDelete);
    const current = state.documents.find((item) => item.id === documentId);
    showConfirm(`删除“${current?.original_name || "该文档"}”及其索引？`, "删除导入文件", { confirmText: "删除", isDanger: true })
      .then(async (confirmed) => {
        if (!confirmed) return;
        await request(`/api/documents/${documentId}`, { method: "DELETE" });
        showToast("已删除", current?.original_name || "文档");
        await refreshDocuments();
        closeDoc();
      })
      .catch((error) => showToast("删除失败", error.message));
  }
});

// 全局 wiki-link 跳转（覆盖 doc-preview 容器）
document.addEventListener("click", async (event) => {
  const wikiLink = event.target.closest(".wiki-link");
  if (!wikiLink) return;
  event.preventDefault();
  const title = wikiLink.dataset.wiki;
  let target = (state.allNotes || []).find((n) => n.title === title);
  if (!target) {
    try {
      const results = await request(`/api/search?q=${encodeURIComponent(title)}&mode=keyword&limit=1`);
      target = (results.notes || [])[0];
    } catch (e) { /* ignore */ }
  }
  if (target) {
    openDoc(`/api/notes/${target.id}?note_id=${target.id}`, target.title);
  } else {
    toast(`未找到标题为「${title}」的笔记`);
  }
});

document.addEventListener("click", (event) => {
  const img = event.target.closest("img.note-image, .doc-preview-body img");
  if (img) { openLightbox(img.src, img.alt); return; }
  const link = event.target.closest("[data-doc-url]");
  if (link) {
    event.preventDefault();
    openDoc(link.dataset.docUrl, link.dataset.docTitle, link.dataset.docEvidence || "");
  }
});

// 页面加载时应用保存的风格（优先后端 config.json，localStorage 作为回退）
// PyWebView 环境中 localStorage 可能因端口变化丢失，后端持久化更可靠
applyTheme(savedTheme());
request("/api/settings").then((settings) => {
  if (settings.theme && settings.theme !== "ocean-blue" && THEMES.some((t) => t.id === settings.theme)) {
    applyTheme(settings.theme);
  }
  if (settings.background) applyBackground(settings.background);
}).catch(() => { /* 忽略，不影响主功能 */ });

// 问答面板初始引导
$("#answer-panel").innerHTML = `<div class="empty-guide"><p class="muted">在搜索框输入问题，点击「智能问答」<br>会基于你的本地笔记生成回答。</p></div>`;

refresh().catch((err) => {
  $("#timeline").innerHTML = `<p class="muted empty-state">加载失败：${escapeHtml(err.message)}。请确认服务已启动。</p>`;
  console.error("初始加载失败", err);
});

// ---------- 版本更新检查（非阻塞，失败静默） ----------
(async function checkUpdate() {
  try {
    const info = await request("/api/update/check");
    if (info.has_update) {
      const badge = $("#update-badge");
      badge.textContent = `有新版本 v${info.latest_version}`;
      badge.href = info.download_url || "#";
      badge.title = info.release_notes
        ? `新版本 v${info.latest_version}\n\n${info.release_notes}`
        : `新版本 v${info.latest_version}，点击下载`;
      badge.classList.remove("hidden");
    }
  } catch (err) {
    // 版本检查失败不影响正常使用
    console.debug("版本检查失败", err);
  }
})();

// ---------- 键盘快捷键 ----------
// 快速跳转面板（Ctrl+P）和命令面板（Ctrl+Shift+P）共享键盘选择逻辑
const quickJumpState = { items: [], index: 0, kind: "jump" };

function fuzzyMatch(query, text) {
  // 简易模糊匹配：所有字符按顺序出现即命中，不区分大小写
  if (!query) return true;
  const q = query.toLowerCase();
  const t = (text || "").toLowerCase();
  let qi = 0;
  for (let i = 0; i < t.length && qi < q.length; i++) {
    if (t[i] === q[qi]) qi++;
  }
  return qi === q.length;
}

function renderQuickJumpList(items, listEl, getLabel) {
  listEl.innerHTML = items.length
    ? items.map((item, i) => `
      <li class="quick-jump-item ${i === quickJumpState.index ? "selected" : ""}" data-index="${i}">
        ${escapeHtml(getLabel(item))}
      </li>`).join("")
    : `<li class="quick-jump-empty muted">无匹配结果</li>`;
}

function openQuickJump() {
  const dlg = $("#quick-jump-dialog");
  const input = $("#quick-jump-input");
  const list = $("#quick-jump-list");
  input.value = "";
  quickJumpState.kind = "jump";
  quickJumpState.index = 0;
  // 候选 = 全部主笔记按 updated_at 倒序
  quickJumpState.items = [...state.notes]
    .filter((n) => !n.parent_note_id)
    .sort((a, b) => new Date(b.updated_at || b.created_at) - new Date(a.updated_at || a.created_at));
  renderQuickJumpList(quickJumpState.items, list, (n) => n.title || "（无标题）");
  dlg.showModal();
  input.focus();
}

function openCommandPalette() {
  const dlg = $("#command-palette-dialog");
  const input = $("#command-palette-input");
  const list = $("#command-palette-list");
  input.value = "";
  quickJumpState.kind = "cmd";
  quickJumpState.index = 0;
  quickJumpState.items = getCommandList();
  renderQuickJumpList(quickJumpState.items, list, (c) => `${c.label}  ·  ${c.hint || ""}`);
  dlg.showModal();
  input.focus();
}

function getCommandList() {
  // 命令清单：所有可执行的快捷动作
  return [
    { label: "新建笔记", hint: "Ctrl+N", action: () => beginNewNote() },
    { label: "保存笔记", hint: "Ctrl+S", action: () => { if (state.newNoteDraft) saveNewNote(); else { const b = $("#inline-save"); if (b && !b.disabled) b.click(); } } },
    { label: "聚焦搜索", hint: "Ctrl+K", action: () => { $("#search").focus(); $("#search").select(); } },
    { label: "智能问答", hint: "", action: () => $("#ask-button").click() },
    { label: "工具面板", hint: "Ctrl+/", action: () => { switchPane("tool-pane"); $("#tool-input").focus(); } },
    { label: "设置", hint: "", action: () => $("#settings-button").click() },
    { label: "新建顶级分区", hint: "", action: () => $("#new-folder-btn").click() },
    { label: "清除筛选", hint: "", action: () => { state.filter = {}; $("#search").value = ""; refresh(); } },
  ];
}

function handlePaletteInput(value) {
  if (quickJumpState.kind === "jump") {
    const filtered = [...state.notes]
      .filter((n) => !n.parent_note_id && fuzzyMatch(value, n.title))
      .sort((a, b) => new Date(b.updated_at || b.created_at) - new Date(a.updated_at || a.created_at))
      .slice(0, 30);
    quickJumpState.items = filtered;
  } else {
    quickJumpState.items = getCommandList().filter((c) => fuzzyMatch(value, c.label));
  }
  quickJumpState.index = 0;
  const list = quickJumpState.kind === "jump" ? $("#quick-jump-list") : $("#command-palette-list");
  renderQuickJumpList(quickJumpState.items, list, (item) =>
    quickJumpState.kind === "jump" ? (item.title || "（无标题）") : `${item.label}  ·  ${item.hint || ""}`
  );
}

function handlePaletteKey(e) {
  const isJump = quickJumpState.kind === "jump";
  const list = isJump ? $("#quick-jump-list") : $("#command-palette-list");
  // ↑↓ 选择
  if (e.key === "ArrowDown") {
    e.preventDefault();
    if (quickJumpState.items.length === 0) return;
    quickJumpState.index = (quickJumpState.index + 1) % quickJumpState.items.length;
    renderQuickJumpList(quickJumpState.items, list, (item) =>
      isJump ? (item.title || "（无标题）") : `${item.label}  ·  ${item.hint || ""}`
    );
    return;
  }
  if (e.key === "ArrowUp") {
    e.preventDefault();
    if (quickJumpState.items.length === 0) return;
    quickJumpState.index = (quickJumpState.index - 1 + quickJumpState.items.length) % quickJumpState.items.length;
    renderQuickJumpList(quickJumpState.items, list, (item) =>
      isJump ? (item.title || "（无标题）") : `${item.label}  ·  ${item.hint || ""}`
    );
    return;
  }
  // Enter：执行
  if (e.key === "Enter") {
    e.preventDefault();
    const item = quickJumpState.items[quickJumpState.index];
    if (!item) return;
    (isJump ? $("#quick-jump-dialog") : $("#command-palette-dialog")).close();
    if (isJump) {
      jumpToNote(item.id);
    } else if (typeof item.action === "function") {
      item.action();
    }
    return;
  }
}

// 快速跳转面板事件绑定
$("#quick-jump-input").addEventListener("input", (e) => handlePaletteInput(e.target.value));
$("#quick-jump-input").addEventListener("keydown", handlePaletteKey);
$("#quick-jump-list").addEventListener("click", (e) => {
  const li = e.target.closest("[data-index]");
  if (!li) return;
  const item = quickJumpState.items[Number(li.dataset.index)];
  if (!item) return;
  $("#quick-jump-dialog").close();
  jumpToNote(item.id);
});

// 命令面板事件绑定
$("#command-palette-input").addEventListener("input", (e) => handlePaletteInput(e.target.value));
$("#command-palette-input").addEventListener("keydown", handlePaletteKey);
$("#command-palette-list").addEventListener("click", (e) => {
  const li = e.target.closest("[data-index]");
  if (!li) return;
  const item = quickJumpState.items[Number(li.dataset.index)];
  if (!item) return;
  $("#command-palette-dialog").close();
  if (typeof item.action === "function") item.action();
});

// 点击遮罩关闭
$("#quick-jump-dialog").addEventListener("click", (e) => {
  if (e.target === $("#quick-jump-dialog")) $("#quick-jump-dialog").close();
});
$("#command-palette-dialog").addEventListener("click", (e) => {
  if (e.target === $("#command-palette-dialog")) $("#command-palette-dialog").close();
});

document.addEventListener("keydown", (e) => {
  // 在输入框/文本域中只处理 Ctrl+S 和 Ctrl+K
  const inEditor = ["INPUT", "TEXTAREA"].includes(e.target.tagName) || e.target.isContentEditable;

  // Ctrl+S: 保存笔记
  if ((e.ctrlKey || e.metaKey) && e.key === "s") {
    e.preventDefault();
    if (state.newNoteDraft) saveNewNote();
    else {
      const saveBtn = $("#inline-save");
      if (saveBtn && !saveBtn.disabled) saveBtn.click();
    }
    return;
  }

  // Ctrl+K: 聚焦搜索框
  if ((e.ctrlKey || e.metaKey) && e.key === "k") {
    e.preventDefault();
    $("#search").focus();
    $("#search").select();
    return;
  }

  // Ctrl+N: 在右侧编辑器建立新笔记草稿。
  if ((e.ctrlKey || e.metaKey) && e.key === "n") {
    e.preventDefault();
    beginNewNote();
    return;
  }

  // Ctrl+P: 快速跳转笔记（弹窗内仍允许使用）
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "p" && !e.shiftKey) {
    e.preventDefault();
    if ($("#quick-jump-dialog").open) $("#quick-jump-dialog").close();
    else openQuickJump();
    return;
  }

  // Ctrl+Shift+P: 命令面板
  if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "p") {
    e.preventDefault();
    if ($("#command-palette-dialog").open) $("#command-palette-dialog").close();
    else openCommandPalette();
    return;
  }

  // Esc: 多级回退（弹窗 > 文档预览 > 设置 > 录入条 > 清筛选）
  if (e.key === "Escape") {
    // 优先关闭模态弹窗（quick-jump / command-palette / settings / graph）
    if ($("#quick-jump-dialog").open) { $("#quick-jump-dialog").close(); return; }
    if ($("#command-palette-dialog").open) { $("#command-palette-dialog").close(); return; }
    if ($("#settings-dialog").open) { $("#settings-dialog").close(); return; }
    const graphDlg = $("#graph-dialog");
    if (graphDlg && graphDlg.open) { graphDlg.close(); return; }
    if (inEditor) return; // 输入框内的 Esc 由浏览器处理
    if (!$("#doc-preview").classList.contains("hidden")) {
      $("#doc-preview-close").click();
      return;
    }
    if (state.newNoteDraft) {
      cancelNewNote();
      return;
    }
    if (!$("#inline-composer").classList.contains("hidden")) {
      $("#inline-composer").classList.add("hidden");
      return;
    }
    // 最后回退：清除筛选
    if (Object.keys(state.filter).length > 0 || $("#search").value) {
      state.filter = {};
      $("#search").value = "";
      $("#search-clear").classList.add("hidden");
      refresh();
      return;
    }
  }

  // Ctrl+1/2/3: 切换编辑/问答/工具 Tab
  if ((e.ctrlKey || e.metaKey) && ["1", "2", "3"].includes(e.key)) {
    e.preventDefault();
    const panes = ["editor-pane", "answer-pane", "tool-pane"];
    switchPane(panes[Number(e.key) - 1]);
    return;
  }

  // Ctrl+/: 聚焦工具输入框
  if ((e.ctrlKey || e.metaKey) && e.key === "/") {
    e.preventDefault();
    switchPane("tool-pane");
    $("#tool-input").focus();
    return;
  }
});

// ---------- 首次启动引导 + 渐进式引导 ----------
// onboarding 状态持久化到后端文件（.onboarded），不依赖 localStorage（WebView2 可能不持久）

async function isOnboarded() {
  try {
    const res = await request("/api/onboarding/status");
    return !!res.onboarded;
  } catch (e) { return true; } // 查询失败时不弹引导，避免反复打扰
}

async function markOnboarded() {
  try { await request("/api/onboarding/complete", { method: "POST", body: "{}" }); } catch (e) { /* ignore */ }
}

async function showOnboardingIfNeeded() {
  if (await isOnboarded()) return;
  showOnboardingDialog(false);
}

function showOnboardingDialog(force = true) {
  // force=true 用于设置面板手动唤起（跳过 isOnboarded 检查）
  const dlg = $("#onboarding-dialog");
  if (!dlg) return;
  let step = 1;
  const totalSteps = 4;
  const steps = dlg.querySelectorAll(".onboarding-step");
  const dotsEl = $("#onboarding-dots");

  // 克隆按钮移除旧监听器（避免多次调用累积），后续统一使用新节点
  let prevBtn = $("#onboarding-prev");
  const clonedPrev = prevBtn.cloneNode(true);
  prevBtn.parentNode.replaceChild(clonedPrev, prevBtn);
  prevBtn = clonedPrev;

  let nextBtn = $("#onboarding-next");
  const clonedNext = nextBtn.cloneNode(true);
  nextBtn.parentNode.replaceChild(clonedNext, nextBtn);
  nextBtn = clonedNext;

  // 渲染进度点
  const renderDots = () => {
    dotsEl.innerHTML = Array.from({ length: totalSteps }, (_, i) =>
      `<span class="onboarding-dot ${i + 1 === step ? "active" : ""}"></span>`
    ).join("");
  };

  const renderStep = () => {
    steps.forEach((el) => el.classList.toggle("hidden", Number(el.dataset.step) !== step));
    prevBtn.disabled = step === 1;
    nextBtn.textContent = step === totalSteps ? "开始使用" : "下一步";
    renderDots();
  };

  prevBtn.addEventListener("click", () => {
    if (step > 1) { step--; renderStep(); }
  });
  nextBtn.addEventListener("click", () => {
    if (step < totalSteps) { step++; renderStep(); }
    else { dlg.close(); }
  });

  // 无论以何种方式关闭（Esc、backdrop、按钮）都标记为已引导
  const onClose = () => {
    markOnboarded();
    dlg.removeEventListener("close", onClose);
  };
  dlg.addEventListener("close", onClose);

  renderStep();
  dlg.showModal();
}

// 渐进式引导：首次触发特定行为时 toast 提示，每个引导点仅显示一次
function progressiveHint(key, title, message) {
  const storageKey = `pocket-memory-hint-${key}`;
  try {
    if (localStorage.getItem(storageKey) === "1") return;
    localStorage.setItem(storageKey, "1");
  } catch (e) { /* ignore */ }
  // 延迟 1.5s 让用户先看到操作结果
  setTimeout(() => showToast(title, message), 1500);
}

// Ctrl+P 首次使用提示
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "p" && !e.shiftKey) {
    progressiveHint("ctrl-p", "快速跳转", "按 Ctrl+P 可随时跳转到任意笔记，支持模糊搜索。");
  }
}, { once: true });

// ==================== 双链补全 [[笔记标题]] ====================
const wikiCompleteState = {
  active: false,      // 是否正在显示补全
  editor: null,       // 当前编辑器元素（contenteditable 或 textarea）
  items: [],          // 候选笔记列表
  index: 0,           // 当前选中索引
  query: "",          // [[ 后输入的查询文本
  range: null,        // contenteditable 的光标位置（用于定位弹窗）
};

function openWikiComplete(editor, query, rect) {
  wikiCompleteState.active = true;
  wikiCompleteState.editor = editor;
  wikiCompleteState.query = query;
  // 从全量笔记列表筛选
  const allNotes = state.allNotes || state.notes || [];
  const q = query.toLowerCase();
  let candidates;
  if (!q) {
    candidates = allNotes.slice(0, 10);
  } else {
    candidates = allNotes
      .filter((n) => (n.title || "").toLowerCase().includes(q))
      .slice(0, 10);
  }
  wikiCompleteState.items = candidates;
  wikiCompleteState.index = 0;
  renderWikiCompleteList(rect);
}

function renderWikiCompleteList(rect) {
  const panel = $("#wiki-complete");
  const listEl = $("#wiki-complete-list");
  listEl.innerHTML = "";
  if (!wikiCompleteState.items.length) {
    listEl.innerHTML = `<li class="wiki-complete-empty">未找到匹配笔记，按 Esc 取消</li>`;
  } else {
    wikiCompleteState.items.forEach((note, i) => {
      const li = document.createElement("li");
      li.className = i === wikiCompleteState.index ? "selected" : "";
      li.textContent = note.title || "未命名笔记";
      li.addEventListener("mousedown", (e) => {
        e.preventDefault(); // 防止编辑器失焦
        wikiCompleteState.index = i;
        insertWikiLink();
      });
      listEl.appendChild(li);
    });
  }
  // 仅在有 rect 时定位（键盘导航时不重定位）
  if (rect) {
    panel.style.left = `${rect.left}px`;
    panel.style.top = `${rect.bottom + 4}px`;
  }
  panel.classList.remove("hidden");
}

function closeWikiComplete() {
  wikiCompleteState.active = false;
  wikiCompleteState.editor = null;
  wikiCompleteState.items = [];
  wikiCompleteState.index = 0;
  $("#wiki-complete").classList.add("hidden");
}

function insertWikiLink() {
  const note = wikiCompleteState.items[wikiCompleteState.index];
  if (!note) return;
  const editor = wikiCompleteState.editor;
  const title = note.title || "未命名笔记";
  const linkText = `[[${title}]]`;

  if (editor.tagName === "TEXTAREA") {
    // textarea：在光标位置插入
    const start = editor.selectionStart;
    const end = editor.selectionEnd;
    // 找到 [[ 的位置，替换到光标处
    const before = editor.value.substring(0, start);
    const after = editor.value.substring(end);
    const bracketPos = before.lastIndexOf("[[");
    if (bracketPos >= 0) {
      const newBefore = before.substring(0, bracketPos);
      editor.value = newBefore + linkText + after;
      const newCursor = newBefore.length + linkText.length;
      editor.setSelectionRange(newCursor, newCursor);
    }
  } else {
    // contenteditable：用 Selection API 插入文本节点
    const sel = window.getSelection();
    if (!sel.rangeCount) { closeWikiComplete(); return; }
    const range = sel.getRangeAt(0);
    // 找到 [[ 开始的位置，删除到当前光标
    const node = range.startContainer;
    if (node.nodeType === Node.TEXT_NODE) {
      const text = node.textContent;
      const offset = range.startOffset;
      const before = text.substring(0, offset);
      const after = text.substring(offset);
      const bracketPos = before.lastIndexOf("[[");
      if (bracketPos >= 0) {
        const newText = before.substring(0, bracketPos) + linkText + after;
        node.textContent = newText;
        const newOffset = bracketPos + linkText.length;
        const newRange = document.createRange();
        newRange.setStart(node, newOffset);
        newRange.setEnd(node, newOffset);
        sel.removeAllRanges();
        sel.addRange(newRange);
      }
    }
  }
  // 触发 input 事件让编辑器感知变化
  editor.dispatchEvent(new Event("input", { bubbles: true }));
  closeWikiComplete();
}

// 监听编辑器输入，检测 [[ 触发补全
document.addEventListener("input", (event) => {
  const editor = event.target;
  if (!editor) return;
  // 只处理 contenteditable 笔记内容和 textarea 录入条
  const isEditable = editor.classList?.contains("note-content") && editor.classList?.contains("editable");
  const isTextarea = editor.id === "inline-content";
  if (!isEditable && !isTextarea) return;

  // 获取光标前的文本
  let textBeforeCursor = "";
  let rect = null;
  if (editor.tagName === "TEXTAREA") {
    const start = editor.selectionStart;
    textBeforeCursor = editor.value.substring(0, start);
    // textarea 光标坐标估算（用 mirror div 更准，但简化处理用编辑器位置）
    rect = editor.getBoundingClientRect();
    rect = { left: rect.left + 20, bottom: rect.top + 30 };
  } else {
    const sel = window.getSelection();
    if (!sel.rangeCount) { closeWikiComplete(); return; }
    const range = sel.getRangeAt(0);
    textBeforeCursor = range.startContainer.textContent?.substring(0, range.startOffset) || "";
    // 获取光标坐标用于定位弹窗
    const tempRange = range.cloneRange();
    tempRange.collapse(true);
    let caretRect = tempRange.getBoundingClientRect();
    if (caretRect.left === 0 && caretRect.top === 0) {
      // contenteditable 有时拿不到坐标，用编辑器位置
      const editorRect = editor.getBoundingClientRect();
      caretRect = { left: editorRect.left + 20, bottom: editorRect.top + 30 };
    }
    rect = caretRect;
  }

  // 检测 [[ 模式：光标前有 [[，且 [[ 后没有 ]]
  const lastOpen = textBeforeCursor.lastIndexOf("[[");
  if (lastOpen < 0) { closeWikiComplete(); return; }
  const afterBracket = textBeforeCursor.substring(lastOpen + 2);
  if (afterBracket.includes("]]")) { closeWikiComplete(); return; }
  // 查询文本不超过 30 字（避免误触发）
  if (afterBracket.length > 30) { closeWikiComplete(); return; }

  openWikiComplete(editor, afterBracket, rect);
});

// 补全弹窗键盘导航
document.addEventListener("keydown", (e) => {
  if (!wikiCompleteState.active) return;
  const panel = $("#wiki-complete");
  if (panel.classList.contains("hidden")) return;

  if (e.key === "ArrowDown") {
    e.preventDefault();
    wikiCompleteState.index = Math.min(wikiCompleteState.index + 1, wikiCompleteState.items.length - 1);
    renderWikiCompleteList(null);
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    wikiCompleteState.index = Math.max(wikiCompleteState.index - 1, 0);
    renderWikiCompleteList(null);
  } else if (e.key === "Enter") {
    e.preventDefault();
    e.stopPropagation();
    insertWikiLink();
  } else if (e.key === "Escape") {
    e.preventDefault();
    closeWikiComplete();
  }
}, true); // 捕获阶段，优先于编辑器默认行为

// 点击外部关闭补全
document.addEventListener("mousedown", (event) => {
  if (!wikiCompleteState.active) return;
  if (!event.target.closest("#wiki-complete")) {
    closeWikiComplete();
  }
});

// 初始化时检查是否需要 onboarding（延迟到 DOM 就绪）
setTimeout(showOnboardingIfNeeded, 800);

// ---------- 知识图谱 ----------
// 基于 Canvas 的轻量力导向图：节点=笔记+标签，边=标签归属+[[双链]]。
// 优化：收敛机制(alpha decay)、孤立节点过滤、搜索高亮、标签开关、节点大小按 degree 缩放。
const graph = {
  canvas: null,
  ctx: null,
  rawNodes: [],
  rawEdges: [],
  nodes: [],
  edges: [],
  nodeMap: {},
  rafId: null,
  dragNode: null,
  dragMoved: false,
  hoverNode: null,
  scale: 1,
  offsetX: 0,
  offsetY: 0,
  width: 0,
  height: 0,
  running: false,
  bound: false,
  alpha: 1,          // 模拟温度，从1衰减到0，0时停止
  searchTerm: "",
  showIsolated: true,
  showTags: true,
  showBroken: false,
};

const CATEGORY_COLORS = {
  "工作": "#C89B6A",
  "技术": "#3399CC",
  "学习": "#7C3AED",
  "生活": "#22C55E",
  "灵感": "#EC4899",
  "其他": "#64748B",
};

function nodeColor(node) {
  if (node.type === "tag") return "#22C55E";
  if (node.type === "broken") return "#EF4444";
  return CATEGORY_COLORS[node.category] || "#3399CC";
}

function resizeGraphCanvas() {
  const c = graph.canvas;
  if (!c) return;
  const dpr = window.devicePixelRatio || 1;
  const rect = c.getBoundingClientRect();
  c.width = Math.max(1, rect.width) * dpr;
  c.height = Math.max(1, rect.height) * dpr;
  graph.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  graph.width = rect.width;
  graph.height = rect.height;
}

function filterGraphNodes() {
  // 根据开关和搜索词过滤节点
  const showIsolated = graph.showIsolated;
  const showTags = graph.showTags;
  const showBroken = graph.showBroken;
  graph.nodes = graph.rawNodes.filter((n) => {
    if (n.type === "tag" && !showTags) return false;
    if (n.type === "broken" && !showBroken) return false;
    if (!showIsolated && (n.degree || 0) === 0) return false;
    return true;
  });
  const nodeIds = new Set(graph.nodes.map((n) => n.id));
  graph.edges = graph.rawEdges.filter((e) => nodeIds.has(e.source) && nodeIds.has(e.target));
  graph.nodeMap = {};
  graph.nodes.forEach((n) => { graph.nodeMap[n.id] = n; });
}

function initGraph(data) {
  graph.rawNodes = data.nodes.map((n) => ({
    ...n,
    vx: 0,
    vy: 0,
    // 节点半径按 degree 缩放：有连接的更大，孤立的更小
    radius: n.type === "tag"
      ? Math.min(6 + (n.count || 1) * 1.2, 16)
      : n.type === "broken"
      ? 6
      : Math.min(5 + (n.degree || 0) * 1.5, 12),
  }));
  graph.rawEdges = data.edges;
  filterGraphNodes();
  // 初始位置：环形分布，避免重叠
  layoutCircular();
  graph.scale = 1;
  graph.offsetX = 0;
  graph.offsetY = 0;
  graph.dragNode = null;
  graph.hoverNode = null;
  graph.alpha = 1;
}

function layoutCircular() {
  const cx = graph.width / 2, cy = graph.height / 2;
  const r = Math.min(graph.width, graph.height) * 0.35;
  const n = graph.nodes.length;
  graph.nodes.forEach((node, i) => {
    const angle = (i / Math.max(n, 1)) * Math.PI * 2;
    node.x = cx + Math.cos(angle) * r + (Math.random() - 0.5) * 20;
    node.y = cy + Math.sin(angle) * r + (Math.random() - 0.5) * 20;
    node.vx = 0;
    node.vy = 0;
  });
}

function tickGraph() {
  const nodes = graph.nodes;
  if (!nodes.length) return;
  const cx = graph.width / 2, cy = graph.height / 2;
  // 参数随 alpha 衰减，收敛后停止
  const alpha = graph.alpha;
  const repulsion = 3500 * alpha;
  const linkDistance = 140;
  const linkStrength = 0.06 * alpha;
  const centerStrength = 0.01 * alpha;
  const damping = 0.82;

  for (let i = 0; i < nodes.length; i += 1) {
    for (let j = i + 1; j < nodes.length; j += 1) {
      const a = nodes[i], b = nodes[j];
      let dx = b.x - a.x, dy = b.y - a.y;
      let dist2 = dx * dx + dy * dy;
      if (dist2 < 0.01) { dist2 = 0.01; dx = Math.random() - 0.5; dy = Math.random() - 0.5; }
      const dist = Math.sqrt(dist2);
      // 节点半径之和的排斥（防止重叠）
      const minDist = a.radius + b.radius + 4;
      const force = dist < minDist ? repulsion / Math.max(dist2, minDist * minDist) : repulsion / dist2;
      const fx = (dx / dist) * force, fy = (dy / dist) * force;
      a.vx -= fx; a.vy -= fy;
      b.vx += fx; b.vy += fy;
    }
  }
  for (const edge of graph.edges) {
    const a = graph.nodeMap[edge.source], b = graph.nodeMap[edge.target];
    if (!a || !b) continue;
    const dx = b.x - a.x, dy = b.y - a.y;
    const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
    const diff = dist - linkDistance;
    const fx = (dx / dist) * diff * linkStrength;
    const fy = (dy / dist) * diff * linkStrength;
    a.vx += fx; a.vy += fy;
    b.vx -= fx; b.vy -= fy;
  }
  for (const n of nodes) {
    if (n === graph.dragNode) { n.vx = 0; n.vy = 0; continue; }
    n.vx += (cx - n.x) * centerStrength;
    n.vy += (cy - n.y) * centerStrength;
    n.vx *= damping; n.vy *= damping;
    n.x += n.vx; n.y += n.vy;
  }
  // alpha 衰减
  graph.alpha = Math.max(0, graph.alpha - 0.005);
  if (graph.alpha <= 0.01) {
    graph.running = false;
  }
}

function isNodeMatched(node) {
  if (!graph.searchTerm) return false;
  return (node.label || "").toLowerCase().includes(graph.searchTerm.toLowerCase());
}

function drawGraph() {
  const ctx = graph.ctx;
  if (!ctx) return;
  ctx.clearRect(0, 0, graph.width, graph.height);
  ctx.save();
  ctx.translate(graph.offsetX, graph.offsetY);
  ctx.scale(graph.scale, graph.scale);

  // 搜索时未匹配节点变暗
  const hasSearch = !!graph.searchTerm;

  // 画边
  for (const edge of graph.edges) {
    const a = graph.nodeMap[edge.source], b = graph.nodeMap[edge.target];
    if (!a || !b) continue;
    const matched = hasSearch && (isNodeMatched(a) || isNodeMatched(b));
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    if (edge.type === "link") {
      ctx.strokeStyle = matched ? "rgba(51, 153, 204, 0.8)" : "rgba(51, 153, 204, 0.3)";
      ctx.lineWidth = matched ? 2 : 1.2;
      ctx.setLineDash([]);
    } else if (edge.type === "broken") {
      ctx.strokeStyle = matched ? "rgba(239, 68, 68, 0.8)" : "rgba(239, 68, 68, 0.35)";
      ctx.lineWidth = matched ? 2 : 1.2;
      ctx.setLineDash([5, 3]);
    } else {
      ctx.strokeStyle = matched ? "rgba(100, 116, 139, 0.5)" : "rgba(100, 116, 139, 0.18)";
      ctx.lineWidth = 1;
      ctx.setLineDash([]);
    }
    ctx.stroke();
  }
  ctx.setLineDash([]);

  // 画节点
  for (const n of graph.nodes) {
    const color = nodeColor(n);
    const matched = isNodeMatched(n);
    const isHover = n === graph.hoverNode;
    let alpha = 0.85;
    if (hasSearch) alpha = matched ? 1 : 0.2;
    if (isHover) alpha = 1;

    if (n.type === "broken") {
      // 失效目标：红色空心虚线圆，表示不存在的笔记
      ctx.beginPath();
      ctx.arc(n.x, n.y, n.radius, 0, Math.PI * 2);
      ctx.globalAlpha = alpha;
      ctx.fillStyle = "rgba(239, 68, 68, 0.08)";
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.strokeStyle = "#EF4444";
      ctx.lineWidth = isHover ? 2.5 : 1.5;
      ctx.setLineDash([3, 2]);
      ctx.stroke();
      ctx.setLineDash([]);
    } else {
      ctx.beginPath();
      ctx.arc(n.x, n.y, n.radius, 0, Math.PI * 2);
      ctx.globalAlpha = alpha;
      ctx.fillStyle = color;
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = isHover ? 2.5 : 1.5;
      ctx.stroke();
    }

    // 标签：搜索匹配或悬停时显示完整，否则截断
    const showFull = isHover || matched || !hasSearch;
    const label = showFull
      ? (n.label.length > 14 ? n.label.slice(0, 13) + "…" : n.label)
      : "";
    if (label) {
      ctx.fillStyle = "#1E293B";
      ctx.font = n.type === "tag" ? "600 11px Inter, sans-serif" : "500 11px Inter, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      // 白色描边提高可读性
      ctx.strokeStyle = "rgba(255,255,255,0.9)";
      ctx.lineWidth = 3;
      ctx.strokeText(label, n.x, n.y + n.radius + 3);
      ctx.fillText(label, n.x, n.y + n.radius + 3);
    }

    if (n.type === "tag") {
      ctx.fillStyle = "#ffffff";
      ctx.font = "700 9px Inter, sans-serif";
      ctx.textBaseline = "middle";
      ctx.textAlign = "center";
      ctx.fillText(String(n.count), n.x, n.y);
    }
  }
  ctx.restore();
}

function startGraphSimulation() {
  stopGraphSimulation();
  graph.alpha = 1;
  graph.running = true;
  const step = () => {
    if (!graph.running) { drawGraph(); return; }
    tickGraph();
    drawGraph();
    if (graph.running) {
      graph.rafId = requestAnimationFrame(step);
    } else {
      graph.rafId = requestAnimationFrame(() => drawGraph());
    }
  };
  graph.rafId = requestAnimationFrame(step);
}

function stopGraphSimulation() {
  graph.running = false;
  if (graph.rafId) cancelAnimationFrame(graph.rafId);
  graph.rafId = null;
}

function restartGraphSimulation() {
  // 拖拽或过滤后重启模拟
  graph.alpha = Math.max(graph.alpha, 0.3);
  if (!graph.running) startGraphSimulation();
}

function screenToGraph(sx, sy) {
  return { x: (sx - graph.offsetX) / graph.scale, y: (sy - graph.offsetY) / graph.scale };
}

function nodeAtScreen(sx, sy) {
  const p = screenToGraph(sx, sy);
  for (let i = graph.nodes.length - 1; i >= 0; i -= 1) {
    const n = graph.nodes[i];
    const dx = p.x - n.x, dy = p.y - n.y;
    if (dx * dx + dy * dy <= (n.radius + 3) * (n.radius + 3)) return n;
  }
  return null;
}

function bindGraphEvents() {
  if (graph.bound) return;
  graph.bound = true;
  const c = graph.canvas;

  c.addEventListener("mousedown", (e) => {
    const rect = c.getBoundingClientRect();
    graph.dragNode = nodeAtScreen(e.clientX - rect.left, e.clientY - rect.top);
    graph.dragMoved = false;
  });

  c.addEventListener("mousemove", (e) => {
    const rect = c.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    if (graph.dragNode) {
      const p = screenToGraph(x, y);
      graph.dragNode.x = p.x;
      graph.dragNode.y = p.y;
      graph.dragMoved = true;
      restartGraphSimulation();
    } else {
      const prev = graph.hoverNode;
      graph.hoverNode = nodeAtScreen(x, y);
      if (prev !== graph.hoverNode) drawGraph();
      c.style.cursor = graph.hoverNode ? "pointer" : "grab";
    }
  });

  c.addEventListener("mouseup", () => {
    if (graph.dragNode && !graph.dragMoved) {
      if (graph.dragNode.type === "note") {
        const noteId = graph.dragNode.noteId;
        closeGraph();
        $("#search").value = "";
        $("#search-clear").classList.add("hidden");
        state.filter = {};
        state.pendingScrollId = noteId;
        refresh();
      } else if (graph.dragNode.type === "broken") {
        toast(`失效链接：目标笔记「${graph.dragNode.label}」不存在或已删除`);
      }
    }
    graph.dragNode = null;
  });

  c.addEventListener("mouseleave", () => {
    graph.dragNode = null;
    graph.hoverNode = null;
    drawGraph();
  });

  c.addEventListener("wheel", (e) => {
    e.preventDefault();
    const rect = c.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    const delta = e.deltaY < 0 ? 1.1 : 0.9;
    const newScale = Math.max(0.3, Math.min(3, graph.scale * delta));
    graph.offsetX = mx - (mx - graph.offsetX) * (newScale / graph.scale);
    graph.offsetY = my - (my - graph.offsetY) * (newScale / graph.scale);
    graph.scale = newScale;
    drawGraph();
  }, { passive: false });

  // 工具栏事件
  $("#graph-search").addEventListener("input", (e) => {
    graph.searchTerm = e.target.value.trim();
    drawGraph();
  });
  $("#graph-show-isolated").addEventListener("change", (e) => {
    graph.showIsolated = e.target.checked;
    filterGraphNodes();
    layoutCircular();
    restartGraphSimulation();
  });
  $("#graph-show-tags").addEventListener("change", (e) => {
    graph.showTags = e.target.checked;
    filterGraphNodes();
    layoutCircular();
    restartGraphSimulation();
  });
  $("#graph-show-broken").addEventListener("change", (e) => {
    graph.showBroken = e.target.checked;
    filterGraphNodes();
    layoutCircular();
    restartGraphSimulation();
  });
  $("#graph-relayout").addEventListener("click", () => {
    layoutCircular();
    restartGraphSimulation();
  });
}

async function openGraph() {
  const dialog = $("#graph-dialog");
  dialog.showModal();
  graph.canvas = $("#graph-canvas");
  graph.ctx = graph.canvas.getContext("2d");
  // 重置工具栏状态
  $("#graph-search").value = "";
  graph.searchTerm = "";
  graph.showIsolated = true;
  graph.showTags = true;
  graph.showBroken = false;
  $("#graph-show-isolated").checked = true;
  $("#graph-show-tags").checked = true;
  $("#graph-show-broken").checked = false;
  resizeGraphCanvas();
  bindGraphEvents();
  $("#graph-empty").classList.add("hidden");
  $("#graph-empty").textContent = "暂无足够数据生成图谱（至少需要 2 篇笔记或 1 条双链）。";
  try {
    const data = await request("/api/graph");
    initGraph(data);
    if (!graph.nodes.length) {
      $("#graph-empty").classList.remove("hidden");
      stopGraphSimulation();
      drawGraph();
      return;
    }
    startGraphSimulation();
  } catch (err) {
    $("#graph-empty").textContent = "加载图谱失败：" + err.message;
    $("#graph-empty").classList.remove("hidden");
    stopGraphSimulation();
  }
}

function closeGraph() {
  stopGraphSimulation();
  const dialog = $("#graph-dialog");
  if (dialog.open) dialog.close();
}

$("#graph-button")?.addEventListener("click", openGraph);
$("#graph-close")?.addEventListener("click", closeGraph);

/* ---------- 专题报告：选择资料 -> 模板编排 -> 编辑/导出 ---------- */
let reportTemplates = [];

function cleanReportSourceLabel(value = "") {
  return String(value || "未命名资料")
    .replace(/\|/g, " ")
    .replace(/^\s*#{1,6}\s*/, "")
    .replace(/\s+/g, " ")
    .replace(/^[\s\-·]+|[\s\-·]+$/g, "")
    .slice(0, 140) || "未命名资料";
}

function reportSourceDetails(source) {
  if (source.kind === "note") {
    const note = (state.allNotes || []).find((item) => Number(item.id) === Number(source.id));
    return { ...source, label: cleanReportSourceLabel(note?.title || "已删除笔记"), meta: "笔记" };
  }
  const document = (state.documents || []).find((item) => Number(item.id) === Number(source.id));
  return { ...source, label: cleanReportSourceLabel(document?.original_name || "已删除文档"), meta: "文档" };
}

function renderReportSelectedSources() {
  const sources = selectedReportSources().map(reportSourceDetails);
  const list = $("#report-selected-list");
  if (!list) return;
  list.innerHTML = sources.map((source) => `<div class="report-selected-item"><span><strong>${escapeHtml(source.label)}</strong><small>${escapeHtml(source.meta)}</small></span><button class="ghost" type="button" data-report-remove-source="${escapeHtml(reportSourceKey(source.kind, source.id))}" title="移除此资料">×</button></div>`).join("") || `<p class="muted">尚未选择资料。</p>`;
}

function updateReportTemplateHint() {
  const current = reportTemplates.find((item) => item.id === $("#report-template")?.value);
  const hint = $("#report-template-hint");
  if (hint) hint.textContent = current?.description || "";
}

function evidenceButton(item) {
  return `<button class="report-evidence" type="button" contenteditable="false" data-report-evidence-kind="${escapeHtml(item.source_kind)}" data-report-evidence-id="${item.source_id}" data-report-evidence-target-note-id="${item.target_note_id || ""}" data-report-evidence-chunk-id="${item.chunk_id || ""}" data-report-evidence-title="${escapeHtml(item.source_label)}" data-report-evidence-text="${escapeHtml(item.text)}">来源 · ${escapeHtml(item.source_label)} / ${escapeHtml(item.location)}</button>`;
}

function reportClaimSourceButton(claim, index) {
  return `<button class="report-evidence" type="button" contenteditable="false" data-report-evidence-kind="${escapeHtml(claim.source_kind)}" data-report-evidence-id="${claim.source_id}" data-report-evidence-target-note-id="${claim.target_note_id || ""}" data-report-evidence-chunk-id="${claim.chunk_id || ""}" data-report-evidence-title="${escapeHtml(claim.source_label)}" data-report-evidence-text="${escapeHtml(claim.quote)}">来源[${index}] · ${escapeHtml(claim.source_label)} / ${escapeHtml(claim.location)}</button>`;
}

function renderReportDraft(draft) {
  const sourceNames = (draft.sources || []).map((source) => escapeHtml(source.label)).join("、");
  const orderedClaims = (draft.sections || []).flatMap((section) => section.claims || []);
  const claimById = new Map(orderedClaims.map((claim) => [claim.id, claim]));
  const referenceNumbers = new Map(orderedClaims.map((claim, index) => [claim.id, index + 1]));
  const citationButtons = (claimIds = []) => claimIds.map((claimId) => {
    const claim = claimById.get(claimId);
    return claim ? reportClaimSourceButton(claim, referenceNumbers.get(claimId)) : "";
  }).join("");
  const sections = (draft.sections || []).map((section, index) => {
    const claims = (section.claims || []).map((claim) => {
      const referenceNumber = referenceNumbers.get(claim.id);
      return `<article class="report-claim"><div class="report-claim-topline"><p class="report-claim-text">${escapeHtml(claim.text)}</p></div><details class="report-evidence-detail"><summary>查看原文证据</summary><blockquote>“${escapeHtml(claim.quote)}”</blockquote>${reportClaimSourceButton(claim, referenceNumber)}</details></article>`;
    }).join("");
    return `<section class="report-section"><div class="report-section-number">0${index + 1}</div><div><h2>${escapeHtml(section.title)}</h2><p class="report-section-lead">${escapeHtml(section.intro || "")}</p><div class="report-section-citations">${citationButtons(section.claim_ids || [])}</div><div class="report-claim-list">${claims}</div></div></section>`;
  }).join("");
  const summary = draft.summary || {};
  return `<article class="report-sheet"><header class="report-sheet-header"><p>POCKET MEMORY · 本地专题报告</p><h1>${escapeHtml(draft.title)}</h1><div>${escapeHtml(draft.template_label)}</div></header><section class="report-intro report-summary"><span class="report-kicker">AI 归纳摘要</span><p>${escapeHtml(summary.text || "已根据所选资料完成编排。")}</p><div class="report-summary-citations">${citationButtons(summary.claim_ids || [])}</div></section>${sections}<footer class="report-sheet-footer"><strong>资料范围</strong><br>${sourceNames || "无"}</footer></article>`;
}

function showReportGenerationProgress() {
  const preview = $("#report-preview");
  const startedAt = Date.now();
  const stages = [
    [0, "核验资料", "检查每条内容是否能回到原文定位。"],
    [2600, "提取事实", "从资料中提炼可引用的事实，不把推测写进报告。"],
    [10500, "组织章节", "根据资料主题编排摘要与章节结构。"],
  ];
  const render = () => {
    const elapsed = Math.floor((Date.now() - startedAt) / 1000);
    const activeIndex = stages.reduce((current, stage, index) => (
      elapsed * 1000 >= stage[0] ? index : current
    ), 0);
    preview.innerHTML = `<section class="report-generation" role="status" aria-live="polite">
      <div class="report-generation-orbit" aria-hidden="true"><span></span><i></i><b></b></div>
      <div class="report-generation-copy"><p class="report-generation-kicker">本地 AI 正在工作</p><h3>正在编排这份专题报告</h3><p>资料越长、事实越多，所需时间会不同。你可以先核对左侧已选资料。</p></div>
      <ol class="report-generation-steps">${stages.map(([_, label, detail], index) => {
        const stateName = index < activeIndex ? "done" : index === activeIndex ? "active" : "";
        return `<li class="${stateName}"><span>${index < activeIndex ? "✓" : `0${index + 1}`}</span><div><strong>${escapeHtml(label)}</strong><small>${escapeHtml(detail)}</small></div></li>`;
      }).join("")}</ol>
      <div class="report-generation-elapsed">已用时 <strong>${elapsed}</strong> 秒</div>
    </section>`;
  };
  preview.contentEditable = "false";
  preview.classList.add("is-generating");
  render();
  const timer = window.setInterval(render, 500);
  return () => {
    window.clearInterval(timer);
    preview.classList.remove("is-generating");
  };
}

async function openReportDialog() {
  if (state.reportSelection.size < 1) {
    showToast("请选择资料", "在中间列表中勾选笔记或文档后继续");
    return;
  }
  try {
    reportTemplates = await request("/api/reports/templates");
    const select = $("#report-template");
    select.innerHTML = reportTemplates.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label)}</option>`).join("");
    updateReportTemplateHint();
    renderReportSelectedSources();
    const title = $("#report-title");
    if (!title.value.trim()) title.value = "我的专题报告";
    $("#report-dialog").showModal();
  } catch (error) {
    showToast("无法打开专题报告", error.message);
  }
}

function leaveReportSelection({ clear = false } = {}) {
  state.reportSelectionMode = false;
  if (clear) state.reportSelection.clear();
  renderTimeline();
}

function exportReportHtml() {
  const preview = $("#report-preview");
  const title = $("#report-title").value.trim() || "Pocket Memory 专题报告";
  const exportNode = preview.cloneNode(true);
  exportNode.querySelectorAll("[data-report-evidence-id]").forEach((button) => {
    const source = button.dataset.reportEvidenceTitle || "原始资料";
    const location = button.textContent.replace(/^来源\[\d+\]\s*·\s*/, "");
    const reference = document.createElement("p");
    reference.className = "report-export-reference";
    reference.textContent = `核验来源：${location || source}`;
    button.replaceWith(reference);
  });
  exportNode.querySelectorAll("[contenteditable]").forEach((element) => element.removeAttribute("contenteditable"));
  const stylesheet = `body{margin:0;background:#f3f7f6;color:#193338;font:16px/1.8 "Microsoft YaHei",Arial,sans-serif}.report-sheet{max-width:920px;margin:0 auto;padding:42px 7vw 70px}.report-sheet-header{padding:25px 30px;background:linear-gradient(135deg,#0c837d,#29aca3);color:#fff;border-radius:12px}.report-sheet-header p{margin:0 0 4px;font-size:10px;letter-spacing:1.2px}.report-sheet-header h1{margin:0 0 6px;font-size:31px;line-height:1.2}.report-sheet-header div{font-size:13px}.report-intro{margin:26px 0 34px;padding:20px 24px;border-left:4px solid #1b9e95;background:#fff;border-radius:8px;box-shadow:0 8px 22px rgba(25,82,77,.06)}.report-intro p{margin:5px 0 0}.report-kicker{color:#158c84;font-size:11px;font-weight:800;letter-spacing:.9px}.report-summary>p{max-width:790px;color:#24494a;font-size:16px;font-weight:600;line-height:1.85}.report-summary-citations,.report-section-citations{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px}.report-quality{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}.report-quality span{padding:3px 10px;border-radius:999px;background:#e3f4ef;color:#177a73;font-size:12px}.report-section{display:grid;grid-template-columns:50px 1fr;gap:18px;margin:34px 0}.report-section-number{color:#10978e;font-size:18px;font-weight:700;padding-top:5px}.report-section h2{margin:0 0 12px;font-size:25px}.report-section-lead{max-width:790px;margin:0;color:#53706d;font-size:15px;line-height:1.8}.report-claim-list{display:grid;gap:12px;margin-top:14px}.report-claim{padding:18px 20px;background:#fff;border:1px solid #dbe9e5;border-radius:8px;box-shadow:0 8px 20px rgba(25,82,77,.045)}.report-claim-topline{display:flex;gap:12px;align-items:flex-start}.report-claim-text{margin:0;font-weight:600}.report-evidence-detail{margin-top:10px}.report-evidence-detail summary{color:#5d8680;font-size:13px;cursor:pointer}.report-evidence-detail blockquote{margin:10px 0 0;padding:9px 12px;border-left:3px solid #a7ddd4;background:#f3faf8;color:#49696a;font-size:14px}.report-export-reference{margin:10px 0 0;color:#167f77;font-size:12px}.report-sheet-footer{margin-top:48px;padding-top:20px;border-top:1px solid #cfe2dd;color:#607a7a;font-size:13px}@media(max-width:640px){.report-sheet{padding:24px 18px}.report-sheet-header{padding:22px}.report-sheet-header h1{font-size:27px}.report-section{grid-template-columns:1fr}.report-section-number{padding:0}}`;
  const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${escapeHtml(title)}</title><style>${stylesheet}</style></head><body>${exportNode.innerHTML}</body></html>`;
  const blob = new Blob([html], { type: "text/html;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `${title.replace(/[\\/:*?"<>|]/g, "-")}.html`;
  link.click();
  URL.revokeObjectURL(link.href);
}

$("#report-button").addEventListener("click", () => {
  state.reportSelectionMode = true;
  state.reportSelection.clear();
  state.reportDraft = null;
  renderTimeline();
  showToast("选择报告资料", "可在笔记、分区、搜索结果和文档间切换选择", false, "report-selection-toast");
});

$("#report-cancel-selection").addEventListener("click", () => leaveReportSelection({ clear: true }));
$("#report-next").addEventListener("click", openReportDialog);
$("#report-back-to-selection").addEventListener("click", () => {
  $("#report-dialog").close();
  state.reportSelectionMode = true;
  renderTimeline();
});
$("#report-template").addEventListener("change", updateReportTemplateHint);
$("#report-selected-list").addEventListener("click", (event) => {
  const button = event.target.closest("[data-report-remove-source]");
  if (!button) return;
  state.reportSelection.delete(button.dataset.reportRemoveSource);
  renderReportSelectedSources();
  renderReportSelectionBar();
});
$("#report-generate").addEventListener("click", async () => {
  const title = $("#report-title").value.trim();
  if (!title) { showToast("请输入专题名称", "例如：项目复盘或方案介绍"); return; }
  const button = $("#report-generate");
  button.disabled = true;
  button.classList.add("is-loading");
  button.textContent = "正在编排报告...";
  const stopProgress = showReportGenerationProgress();
  try {
    const draft = await request("/api/reports/draft", { method: "POST", body: JSON.stringify({ title, template: $("#report-template").value, sources: selectedReportSources() }) });
    state.reportDraft = draft;
    const preview = $("#report-preview");
    preview.innerHTML = renderReportDraft(draft);
    preview.contentEditable = "true";
    $("#report-export").disabled = false;
    showToast("报告草稿已生成", "正文可直接编辑；每一项都保留原始资料入口");
  } catch (error) {
    $("#report-preview").innerHTML = `<p class="muted">报告未能生成：${escapeHtml(error.message || "请检查已选资料后重试")}</p>`;
    showToast("生成失败", error.message);
  } finally {
    stopProgress();
    button.disabled = false;
    button.classList.remove("is-loading");
    button.textContent = "生成报告草稿";
  }
});
$("#report-export").addEventListener("click", exportReportHtml);
$("#report-preview").addEventListener("click", async (event) => {
  const evidence = event.target.closest("[data-report-evidence-id]");
  if (!evidence) return;
  const id = Number(evidence.dataset.reportEvidenceId);
  const kind = evidence.dataset.reportEvidenceKind;
  const chunkId = Number(evidence.dataset.reportEvidenceChunkId) || null;
  const targetNoteId = Number(evidence.dataset.reportEvidenceTargetNoteId) || null;
  const url = kind === "document"
    ? `/api/documents/${id}/file${chunkId ? `?chunk_id=${chunkId}` : ""}`
    : `/api/notes/${id}${targetNoteId && targetNoteId !== id ? `?tab_id=${targetNoteId}` : ""}`;
  await openDoc(url, evidence.dataset.reportEvidenceTitle, evidence.dataset.reportEvidenceText);
});
