/* Copyright 2026 AshleySama
 * SPDX-License-Identifier: Apache-2.0 */
// Canvas rendering, physics and mouse behavior restored from the pre-change app.js.
// ---------- 知识图谱 ----------
// 基于 Canvas 的轻量力导向图：节点=笔记+标签，边=标签归属+[[双链]]。
// 优化：收敛机制(alpha decay)、孤立节点过滤、搜索高亮、标签开关、节点大小按 degree 缩放。
const GRAPH_DEFAULT_SCALE = 1.4;
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
  scale: GRAPH_DEFAULT_SCALE,
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
  category: "",
  toolbarBound: false,
  requestId: 0,
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
  // The new category control filters input only; Canvas rendering / physics stay original.
  if (graph.category) {
    const noteIds = new Set(graph.nodes.filter(n => n.type === "note" && graphNodeCategory(n) === graph.category).map(n => n.id));
    const attachedIds = new Set();
    for (const edge of graph.rawEdges) {
      if (noteIds.has(edge.source)) attachedIds.add(edge.target);
      if (noteIds.has(edge.target)) attachedIds.add(edge.source);
    }
    graph.nodes = graph.nodes.filter(n => n.type === "note" ? noteIds.has(n.id) : attachedIds.has(n.id));
  }
  const nodeIds = new Set(graph.nodes.map((n) => n.id));
  graph.edges = graph.rawEdges.filter((e) => nodeIds.has(e.source) && nodeIds.has(e.target));
  graph.nodeMap = {};
  graph.nodes.forEach((n) => { graph.nodeMap[n.id] = n; });
  renderGraphControls();
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
  setGraphDefaultView();
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
        showToast("失效双链", `失效链接：目标笔记「${graph.dragNode.label}」不存在或已删除`);
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
  if (dialog.open) return;
  dialog.showModal();
  graph.canvas = $("#graph-canvas");
  graph.ctx = graph.canvas.getContext("2d");
  // 重置工具栏状态
  $("#graph-search").value = "";
  graph.searchTerm = "";
  graph.showIsolated = true;
  graph.showTags = true;
  graph.showBroken = false;
  graph.category = "";
  graph.rawNodes = []; graph.rawEdges = []; graph.nodes = []; graph.edges = []; graph.nodeMap = {};
  graph.hoverNode = null; graph.dragNode = null;
  $("#graph-show-isolated").checked = true;
  $("#graph-show-tags").checked = true;
  $("#graph-show-broken").checked = false;
  resizeGraphCanvas();
  bindGraphEvents();
  bindGraphControls();
  renderGraphControls();
  resetGraphView();
  graphNodeStatus();
  $("#graph-count").textContent = "正在读取关系…";
  $("#graph-empty").classList.add("hidden");
  $("#graph-empty").textContent = "暂无足够数据生成图谱（至少需要 2 篇笔记或 1 条双链）。";
  const requestId = ++graph.requestId;
  try {
    const data = await request("/api/graph");
    if (requestId !== graph.requestId || !dialog.open) return;
    initGraph(data);
    updateGraphZoom();
    if (!graph.nodes.length) {
      $("#graph-empty").classList.remove("hidden");
      stopGraphSimulation();
      drawGraph();
      return;
    }
    startGraphSimulation();
  } catch (err) {
    if (requestId !== graph.requestId || !dialog.open) return;
    $("#graph-count").textContent = "暂时无法读取";
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

// Thin adapters for the newer toolbar. No extra Canvas animation or hit-testing.
function graphNodeCategory(node) {
  return Object.hasOwn(CATEGORY_COLORS, node.category) ? node.category : "其他";
}

function renderGraphControls() {
  const categories = ["技术", "工作", "学习", "生活", "灵感", "其他"];
  const counts = new Map();
  for (const node of graph.rawNodes) {
    if (node.type === "note") counts.set(graphNodeCategory(node), (counts.get(graphNodeCategory(node)) || 0) + 1);
  }
  const total = [...counts.values()].reduce((sum, value) => sum + value, 0);
  $("#graph-categories").innerHTML = `<button class="graph-category" type="button" data-graph-category="" aria-pressed="${!graph.category}">全部 <span>${total}</span></button>`
    + categories.filter(category => counts.has(category)).map(category => `<button class="graph-category" type="button" data-graph-category="${category}" aria-pressed="${graph.category === category}"><i style="--category-color:${CATEGORY_COLORS[category]}" aria-hidden="true"></i>${category} <span>${counts.get(category)}</span></button>`).join("");
  $("#graph-count").textContent = `${graph.nodes.filter(n => n.type === "note").length} 篇笔记 · ${graph.edges.length} 条关系`;
  $("#graph-empty").textContent = graph.rawNodes.length ? "当前筛选下暂无节点，试试其他分类或显示孤立笔记。" : "暂无足够数据生成图谱（至少需要 2 篇笔记或 1 条双链）。";
  $("#graph-empty").classList.toggle("hidden", graph.nodes.length > 0);
}

function updateGraphZoom() {
  $("#graph-zoom-value").textContent = `${Math.round(graph.scale * 100)}%`;
  $("#graph-zoom-out").disabled = graph.scale <= 0.3;
  $("#graph-zoom-in").disabled = graph.scale >= 3;
}

function zoomGraph(delta) {
  const mx = graph.width / 2, my = graph.height / 2;
  const newScale = Math.max(0.3, Math.min(3, graph.scale * delta));
  graph.offsetX = mx - (mx - graph.offsetX) * (newScale / graph.scale);
  graph.offsetY = my - (my - graph.offsetY) * (newScale / graph.scale);
  graph.scale = newScale;
  updateGraphZoom(); drawGraph();
}

function graphDefaultScale(height = graph.height) {
  // Leave label space on shorter laptop canvases; full-height views retain 140%.
  if (height < 380) return 1;
  if (height < 560) return 1.15;
  if (height < 650) return 1.25;
  return GRAPH_DEFAULT_SCALE;
}

function setGraphDefaultView() {
  // Enlarge the original drawing around its center; physics and label rendering stay intact.
  graph.scale = graphDefaultScale();
  graph.offsetX = graph.width * (1 - graph.scale) / 2;
  graph.offsetY = graph.height * (1 - graph.scale) / 2;
}

function resetGraphView() {
  setGraphDefaultView();
  updateGraphZoom(); drawGraph();
}

function graphNodeStatus() {
  const node = graph.hoverNode;
  const status = $("#graph-node-status");
  status.textContent = node ? `${node.label} · ${node.type === "note" ? "点击打开原文" : node.type === "tag" ? `${node.count || 0} 篇笔记的标签` : "失效目标"}` : "拖动节点 · 滚轮缩放 · 点击笔记打开原文";
  status.title = node ? node.label : "";
}

function bindGraphControls() {
  if (graph.toolbarBound) return;
  graph.toolbarBound = true;
  graph.canvas.addEventListener("wheel", updateGraphZoom);
  graph.canvas.addEventListener("mousemove", graphNodeStatus);
  graph.canvas.addEventListener("mouseleave", graphNodeStatus);
  $("#graph-categories").addEventListener("click", event => {
    const button = event.target.closest("[data-graph-category]");
    if (!button) return;
    graph.category = button.dataset.graphCategory;
    graph.hoverNode = null;
    filterGraphNodes(); layoutCircular();
    if (graph.nodes.length) restartGraphSimulation();
    else { stopGraphSimulation(); drawGraph(); }
    graphNodeStatus();
  });
  // The original switch handlers remain intact; guard their empty-result lifecycle.
  for (const id of ["graph-show-isolated", "graph-show-tags", "graph-show-broken", "graph-relayout"]) {
    $("#" + id).addEventListener(id === "graph-relayout" ? "click" : "change", () => {
      if (!graph.nodes.length) { stopGraphSimulation(); drawGraph(); }
    });
  }
  $("#graph-zoom-out").addEventListener("click", () => zoomGraph(0.9));
  $("#graph-zoom-in").addEventListener("click", () => zoomGraph(1.1));
  $("#graph-reset-view").addEventListener("click", resetGraphView);
  $("#graph-dialog").addEventListener("close", () => {
    stopGraphSimulation();
    graph.requestId += 1;
  });
  let width = graph.width, height = graph.height;
  graph.resizeObserver = new ResizeObserver(() => {
    if (!$("#graph-dialog").open) return;
    const rect = graph.canvas.getBoundingClientRect();
    if (rect.width === width && rect.height === height) return;
    // Preserve the current zoom and pan relative to the canvas center on resize.
    const previousWidth = graph.width, previousHeight = graph.height;
    const wasDefaultView = Math.abs(graph.scale - graphDefaultScale(previousHeight)) < 0.001
      && Math.abs(graph.offsetX - previousWidth * (1 - graph.scale) / 2) < 0.001
      && Math.abs(graph.offsetY - previousHeight * (1 - graph.scale) / 2) < 0.001;
    width = rect.width; height = rect.height;
    resizeGraphCanvas();
    if (wasDefaultView) setGraphDefaultView();
    else {
      graph.offsetX += (graph.width - previousWidth) * (1 - graph.scale) / 2;
      graph.offsetY += (graph.height - previousHeight) * (1 - graph.scale) / 2;
    }
    updateGraphZoom();
    layoutCircular();
    if (graph.nodes.length) restartGraphSimulation();
    else drawGraph();
  });
  graph.resizeObserver.observe(graph.canvas);
}

$("#graph-button")?.addEventListener("click", openGraph);
$("#graph-close")?.addEventListener("click", closeGraph);

