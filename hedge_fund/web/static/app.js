/* Fund Lab — 单页前端 · Trading Terminal Fintech
 * 无依赖原生 JS，SSE 实时事件流 */

"use strict";

const $ = (id) => document.getElementById(id);
const MARK = { bullish: ["推荐买入", "mark-buy"],
               neutral: ["可观望", "mark-hold"],
               bearish: ["不建议", "mark-avoid"] };
const PCT = (v, signed) => v == null ? "—"
  : (signed && v > 0 ? "+" : "") + (v * 100).toFixed(1) + "%";

let theme = null;
let taskId = null;
let es = null;
let rows = new Map();
let order = [];
const fundsByCode = new Map();
let dailyPick = null;

async function init() {
  startClock();
  loadDaily();
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
  $("fund-code-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") startSingleFund();
  });
}

function startClock() {
  const el = $("clock");
  if (!el) return;
  const tick = () => {
    const now = new Date();
    el.textContent = now.toLocaleTimeString("zh-CN", { hour12: false });
  };
  tick();
  setInterval(tick, 1000);
}

function toggleFullscreen() {
  if (!document.fullscreenElement) {
    document.documentElement.requestFullscreen().catch(() => {});
  } else {
    document.exitFullscreen();
  }
}

/* ── Daily Recommendation ─────────────────────── */

async function loadDaily() {
  try {
    const res = await fetch("/api/daily");
    const data = await res.json();
    if (data.pick) showDailyBanner(data.pick);
  } catch (_) {}
}

function showDailyBanner(pick) {
  dailyPick = pick;
  const banner = $("daily-banner");
  banner.classList.remove("hidden");
  $("daily-theme").textContent = `#${pick.theme}`;
  $("daily-fund").textContent = `${pick.name}（${pick.code}）`;
  const [label, cls] = MARK[pick.signal] || MARK.neutral;
  $("daily-verdict").className = `daily-verdict ${cls}`;
  $("daily-verdict").textContent = `${label} · ${Math.round(pick.confidence)}%`;
}

function showDailyDetail() {
  if (!dailyPick || !dailyPick.snapshot) return;
  showDetail({
    code: dailyPick.code,
    name: dailyPick.name,
    signal: dailyPick.signal,
    confidence: dailyPick.confidence,
    reasoning: dailyPick.reasoning,
    quant: dailyPick.quant_total != null ? {
      total: dailyPick.quant_total,
      raw: {}
    } : null,
    snapshot: dailyPick.snapshot,
  });
}

/* ── Theme Selection ───────────────────────────── */

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

/* ── Theme Analysis ────────────────────────────── */

async function start() {
  if (!theme || es) return;
  resetView();
  $("step-progress").classList.remove("hidden");
  $("status-text").textContent = `已提交「${theme}」，正在拉取基金池…`;
  $("btn-start").disabled = true;
  const res = await fetch("/api/analyze", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ theme }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    $("status-text").textContent = `启动失败：${err.detail || res.status}`;
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

/* ── Single Fund Analysis ──────────────────────── */

async function startSingleFund() {
  const input = $("fund-code-input");
  const code = input.value.trim();
  if (!code || !/^\d{6}$/.test(code)) {
    input.style.borderColor = "var(--red)";
    setTimeout(() => { input.style.borderColor = ""; }, 1500);
    return;
  }
  if (es) return;
  resetView();
  $("step-progress").classList.remove("hidden");
  $("status-text").textContent = `正在分析基金 ${code}…`;
  $("btn-analyze-fund").disabled = true;
  const res = await fetch("/api/analyze-fund", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    $("status-text").textContent = `启动失败：${err.detail || res.status}`;
    setConn(false);
    $("btn-analyze-fund").disabled = false;
    return;
  }
  const { task_id } = await res.json();
  taskId = task_id;
  es = new EventSource(`/api/stream/${task_id}`);
  es.onmessage = onEvent;
  es.onerror = () => setConn(false);
}

/* ── SSE Event Handler ─────────────────────────── */

function onEvent(e) {
  const ev = JSON.parse(e.data);
  setConn(true);
  switch (ev.type) {
    case "pool":
      $("step-progress").classList.remove("hidden");
      $("status-text").textContent =
        `候选池 ${ev.count} 只（规模≥5亿 · 成立≥3年），逐个分析中…`;
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
      $("status-text").textContent = "分析失败：" + ev.message;
      setConn(false);
      es.close(); es = null;
      $("btn-analyze-fund").disabled = false;
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
    <span class="st">–</span>
    <span class="code">${ev.code}</span>
    <span class="name">${escapeHtml(ev.name)}</span>
    <span class="mark">分析中…</span>`;
  $("progress-list").appendChild(row);
}

function addFundRow(ev) {
  const existing = document.querySelector(`.fund-row[data-code="${ev.code}"]`);
  const row = existing || document.createElement("div");
  row.className = "fund-row";
  row.dataset.code = ev.code;
  const [label, cls] = MARK[ev.signal] || MARK.neutral;
  const conf = Math.round(ev.confidence);
  row.innerHTML = `
    <span class="st">✓</span>
    <span class="code">${ev.code}</span>
    <span class="name">${escapeHtml(ev.name)}</span>
    <span class="mark ${cls}">${label}</span>
    <span class="conf ${cls}">${conf}%</span>`;
  if (!existing) $("progress-list").appendChild(row);
  rows.set(ev.code, { row, data: ev });
  fundsByCode.set(ev.code, ev);
  $("status-text").textContent = `${ev.done} / ${ev.total} 已分析`;
}

function finish(orderList) {
  order = orderList;
  es.close(); es = null;
  $("btn-analyze-fund").disabled = false;
  $("step-progress").classList.add("hidden");
  $("step-report").classList.remove("hidden");
  const list = $("report-list");
  list.innerHTML = "";
  for (const item of order) {
    const ev = fundsByCode.get(item.code);
    if (!ev) continue;
    const [label, cls] = MARK[item.signal] || MARK.neutral;
    const row = document.createElement("div");
    row.className = "fund-row";
    row.innerHTML = `
      <span class="st">${item.rank}</span>
      <span class="code">${ev.code}</span>
      <span class="name">${escapeHtml(ev.name)}</span>
      <span class="mark ${cls}">${label}</span>
      <span class="conf ${cls}">${Math.round(ev.confidence)}%</span>`;
    row.onclick = () => showDetail(ev);
    list.appendChild(row);
  }
}

/* ── Detail Panel ──────────────────────────────── */

function showDetail(ev) {
  const body = $("detail-body");
  const [label, cls] = MARK[ev.signal] || MARK.neutral;
  const snap = ev.snapshot || {};
  const quant = ev.quant;

  const holdingsRows = (snap.holdings || []).map(h =>
    `<div class="h-row">
      <span class="h-name">${escapeHtml(h.name)}</span>
      <span class="h-code">${escapeHtml(h.code)}</span>
      <span class="h-pct">${h.percent == null ? "—" : h.percent.toFixed(1) + "%"}</span>
    </div>`
  ).join("");

  let quantHtml = "";
  if (quant) {
    const r = quant.raw || {};
    quantHtml = `
    <div class="quant-bar">
      <div class="q-total ${quant.total >= 0 ? 'mark-buy' : 'mark-avoid'}">
        ${quant.total >= 0 ? "+" : ""}${quant.total.toFixed(2)}
      </div>
      <div class="q-item">
        <span class="q-label">动量</span>
        <span class="q-value">${fmtRaw(r.momentum_12m)}</span>
      </div>
      <div class="q-item">
        <span class="q-label">年化</span>
        <span class="q-value">${fmtRaw(r.alpha_annualized)}</span>
      </div>
      <div class="q-item">
        <span class="q-label">集中度</span>
        <span class="q-value">${r.concentration == null ? "—" : Math.round(r.concentration * 100) + "%"}</span>
      </div>
    </div>`;
  }

  body.innerHTML = `
    <h3>${escapeHtml(ev.name)}</h3>
    <span class="detail-code">${ev.code}</span>
    <div class="detail-verdict ${cls}">${label} · ${Math.round(ev.confidence)}%</div>
    ${quantHtml}
    <h4>分析理由</h4>
    <div class="reason">${escapeHtml(ev.reasoning)}</div>
    <h4>基本面快照</h4>
    <dl class="kv">
      <dt>规模</dt><dd>${snap.scale_billion == null ? "—" : snap.scale_billion.toFixed(1) + " 亿"}</dd>
      <dt>成立</dt><dd>${escapeHtml(snap.inception || "—")}</dd>
      <dt>类型</dt><dd>${escapeHtml(snap.fund_type || "—")}</dd>
      <dt>费率</dt><dd>申购 ${snap.purchase_fee == null ? "—" : snap.purchase_fee.toFixed(2) + "%"} · 管理 ${snap.mgmt_fee == null ? "—" : snap.mgmt_fee.toFixed(2) + "%"} · 托管 ${snap.custody_fee == null ? "—" : snap.custody_fee.toFixed(2) + "%"}</dd>
      <dt>近1年</dt><dd style="color:${colorForReturn(snap.return_1y)}">${PCT(snap.return_1y, true)}</dd>
      <dt>近3年</dt><dd style="color:${colorForReturn(snap.return_3y)}">${PCT(snap.return_3y, true)}</dd>
      <dt>今年</dt><dd style="color:${colorForReturn(snap.ytd)}">${PCT(snap.ytd, true)}</dd>
      <dt>最大回撤</dt><dd style="color:var(--red)">${PCT(snap.max_drawdown)}</dd>
      <dt>经理</dt><dd>${escapeHtml(snap.manager || "—")}${snap.manager_tenure ? "（" + escapeHtml(snap.manager_tenure) + "）" : ""}</dd>
    </dl>
    ${holdingsRows ? `<h4>前十大重仓</h4><div class="holdings">${holdingsRows}</div>` : ""}
  `;
  $("detail").classList.remove("hidden");
  $("detail-backdrop").classList.remove("hidden");
}

function closeDetail() {
  $("detail").classList.add("hidden");
  $("detail-backdrop").classList.add("hidden");
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

function colorForReturn(v) {
  if (v == null) return "var(--dim)";
  return v >= 0 ? "var(--green)" : "var(--red)";
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
