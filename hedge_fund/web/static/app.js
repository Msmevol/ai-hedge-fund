/* 基金选购 · 单页前端 —— 无依赖原生 JS。
 * 排序字段由后端 done 事件的 order 提供（与 TUI 共用 _sort_verdicts），
 * 前端只按序渲染，不重复实现排序。 */

"use strict";

const $ = (id) => document.getElementById(id);
const MARK = { bullish: ["推荐买入", "mark-buy"],
               neutral: ["可观望", "mark-hold"],
               bearish: ["不建议", "mark-avoid"] };
const PCT = (v, signed) => v == null ? "数据缺失"
  : (signed && v > 0 ? "+" : "") + (v * 100).toFixed(1) + "%";

let theme = null;
let taskId = null;
let es = null;
let rows = new Map();      // code -> {rowEl, data}
let order = [];            // [{code, rank, signal, label}]
const fundsByCode = new Map();

async function init() {
  const res = await fetch("/api/themes");
  const { themes } = await res.json();
  const grid = $("theme-grid");
  for (const t of themes) {
    const card = document.createElement("div");
    card.className = "theme-card";
    card.textContent = t;
    card.onclick = () => selectTheme(t, card);
    grid.appendChild(card);
  }
  $("detail-close").onclick = closeDetail;
}

function selectTheme(t, card) {
  theme = t;
  document.querySelectorAll(".theme-card").forEach(c => c.classList.remove("sel"));
  card.classList.add("sel");
  const btn = ensureStartBtn();
  btn.disabled = false;
}

function ensureStartBtn() {
  let btn = $("btn-start");
  if (btn) return btn;
  btn = document.createElement("button");
  btn.id = "btn-start";
  btn.className = "primary";
  btn.textContent = "开始分析";
  btn.disabled = true;
  btn.onclick = start;
  $("step-theme").appendChild(btn);
  return btn;
}

async function start() {
  if (!theme || es) return;
  resetView();
  $("step-progress").classList.remove("hidden");
  $("status-line").textContent = `已提交「${theme}」，正在拉取基金池…`;
  $("btn-start").disabled = true;
  const res = await fetch("/api/analyze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ theme }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    $("status-line").textContent = `启动失败：${err.detail || res.status}`;
    setConn(false);
    $("btn-start").disabled = false;
    return;
  }
  const { task_id } = await res.json();
  taskId = task_id;
  es = new EventSource(`/api/stream/${task_id}`);
  es.onmessage = onEvent;
  es.onerror = () => setConn(false);
}

function onEvent(e) {
  const ev = JSON.parse(e.data);
  setConn(true);
  switch (ev.type) {
    case "pool":
      $("step-progress").classList.remove("hidden");
      $("status-line").textContent =
        `候选池 ${ev.count} 只（规模≥5亿、成立≥3年），正在逐个分析…`;
      break;
    case "fund_start":
      addPendingRow(ev);
      break;
    case "fund_done":
      addFundRow(ev);
      break;
    case "done":
      finish(ev.order);
      break;
    case "error":
      $("status-line").textContent = "分析失败：" + ev.message;
      setConn(false);
      es.close(); es = null;
      break;
    case "done_ack":
      break;
  }
}

function addPendingRow(ev) {
  const row = document.createElement("div");
  row.className = "fund-row pending";
  row.dataset.code = ev.code;
  row.innerHTML = `
    <span class="st">⏳</span>
    <span class="code">${ev.code}</span>
    <span class="name">${escapeHtml(ev.name)}</span>
    <span class="mark">分析中…</span>`;
  $("progress-list").appendChild(row);
}

function addFundRow(ev) {
  const existing = document.querySelector(
    `.fund-row[data-code="${ev.code}"]`);
  const row = existing || document.createElement("div");
  row.className = "fund-row";
  row.dataset.code = ev.code;
  const [label, cls] = MARK[ev.signal] || MARK.neutral;
  row.innerHTML = `
    <span class="st">✓</span>
    <span class="code">${ev.code}</span>
    <span class="name">${escapeHtml(ev.name)}</span>
    <span class="mark ${cls}">${label}</span>
    <span class="conf ${cls}">${Math.round(ev.confidence)}%</span>`;
  if (!existing) $("progress-list").appendChild(row);
  rows.set(ev.code, { row, data: ev });
  fundsByCode.set(ev.code, ev);
  $("status-line").textContent = `已分析 ${ev.done}/${ev.total} 只…`;
}

function finish(orderList) {
  order = orderList;
  es.close(); es = null;
  $("step-progress").classList.add("hidden");
  $("step-report").classList.remove("hidden");
  const list = $("report-list");
  list.innerHTML = "";
  for (const item of order) {
    const ev = fundsByCode.get(item.code);
    const [label, cls] = MARK[item.signal] || MARK.neutral;
    const row = document.createElement("div");
    row.className = "fund-row";
    row.innerHTML = `
      <span class="st">${item.rank}.</span>
      <span class="code">${ev.code}</span>
      <span class="name">${escapeHtml(ev.name)}</span>
      <span class="mark ${cls}">${label}</span>
      <span class="conf ${cls}">${Math.round(ev.confidence)}%</span>`;
    row.onclick = () => showDetail(ev);
    row.style.cursor = "pointer";
    list.appendChild(row);
  }
  $("status-line").textContent = "";  // no longer visible
}

function showDetail(ev) {
  const body = $("detail-body");
  const [label, cls] = MARK[ev.signal] || MARK.neutral;
  const snap = ev.snapshot || {};
  const quant = ev.quant;
  const holdings = (snap.holdings || []).map(h =>
    `<div>${escapeHtml(h.name)} <span style="color:var(--muted)">${escapeHtml(h.code)}</span> ` +
    `<span style="color:var(--cyan)">${h.percent == null ? "—" : h.percent.toFixed(1) + "%"}</span></div>`
  ).join("");

  const quantBar = quant
    ? `<div class="quant-bar">
        量化综合 <b>${quant.total >= 0 ? "+" : ""}${quant.total.toFixed(2)}</b>
        <span>动量 ${fmtRaw(quant.raw.momentum_12m)}</span>
        <span>Alpha年化 ${fmtRaw(quant.raw.alpha_annualized)}</span>
        <span>集中度 ${quant.raw.concentration == null ? "—" : Math.round(quant.raw.concentration * 100) + "%"}</span>
       </div>`
    : "";

  body.innerHTML = `
    <h3>${escapeHtml(ev.name)} <span style="color:var(--muted);font-size:0.85rem">${ev.code}</span></h3>
    <div class="mark ${cls}" style="font-size:1.05rem;margin-bottom:0.8rem">${label} · ${Math.round(ev.confidence)}%</div>
    ${quantBar}
    <h4>量化与 LLM 理由</h4>
    <div class="reason">${escapeHtml(ev.reasoning)}</div>
    <h4>基本面快照</h4>
    <dl class="kv">
      <dt>规模</dt><dd>${snap.scale_billion == null ? "数据缺失" : snap.scale_billion.toFixed(1) + " 亿元"}</dd>
      <dt>成立</dt><dd>${escapeHtml(snap.inception || "数据缺失")}</dd>
      <dt>类型</dt><dd>${escapeHtml(snap.fund_type || "数据缺失")}</dd>
      <dt>费率</dt><dd>申购 ${snap.purchase_fee == null ? "—" : snap.purchase_fee.toFixed(2) + "%"} · 管理 ${snap.mgmt_fee == null ? "—" : snap.mgmt_fee.toFixed(2) + "%"} · 托管 ${snap.custody_fee == null ? "—" : snap.custody_fee.toFixed(2) + "%"}</dd>
      <dt>近1年</dt><dd>${PCT(snap.return_1y, true)}</dd>
      <dt>近3年</dt><dd>${PCT(snap.return_3y, true)}</dd>
      <dt>今年</dt><dd>${PCT(snap.ytd, true)}</dd>
      <dt>最大回撤</dt><dd>${PCT(snap.max_drawdown)}</dd>
      <dt>经理</dt><dd>${escapeHtml(snap.manager || "数据缺失")}${snap.manager_tenure ? "（" + escapeHtml(snap.manager_tenure) + "）" : ""}</dd>
    </dl>
    ${holdings ? `<h4>前十大重仓</h4><div class="holdings">${holdings}</div>` : ""}
  `;
  $("detail").classList.remove("hidden");
}

function closeDetail() {
  $("detail").classList.add("hidden");
}

function resetView() {
  rows.clear(); order = [];
  $("progress-list").innerHTML = "";
  $("report-list").innerHTML = "";
  $("step-progress").classList.add("hidden");
  $("step-report").classList.add("hidden");
  closeDetail();
}

function fmtRaw(v) {
  if (v == null) return "—";
  return (v >= 0 ? "+" : "") + (v * 100).toFixed(1) + "%";
}

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function setConn(ok) {
  $("conn").className = "conn " + (ok ? "ok" : "bad");
  $("conn").title = ok ? "分析服务已连接" : "连接已断开，正在重连…";
}

init();