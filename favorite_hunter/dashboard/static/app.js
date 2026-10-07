"use strict";

// ------------------------------------------------------------------ helpers

const $ = (sel, root = document) => root.querySelector(sel);
const SVG_NS = "http://www.w3.org/2000/svg";

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function svg(tag, attrs = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

const isNum = (v) => typeof v === "number" && Number.isFinite(v);
const fmt = {
  pct: (v, d = 1) => (isNum(v) ? `${(v * 100).toFixed(d)}%` : "–"),
  pts: (v, d = 1) => (isNum(v) ? `${v >= 0 ? "+" : ""}${(v * 100).toFixed(d)}pt` : "–"),
  signedPct: (v, d = 1) => (isNum(v) ? `${v >= 0 ? "+" : ""}${(v * 100).toFixed(d)}%` : "–"),
  price: (v, d = 3) => (isNum(v) ? v.toFixed(d) : "–"),
  num: (v, d = 0) => (isNum(v) ? v.toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }) : "–"),
  usd(v) {
    if (!isNum(v)) return "–";
    const a = Math.abs(v);
    const sign = v < 0 ? "-" : "";
    if (a >= 1e6) return `${sign}$${(a / 1e6).toFixed(1)}M`;
    if (a >= 1e4) return `${sign}$${(a / 1e3).toFixed(1)}K`;
    return `${sign}$${a.toLocaleString(undefined, a < 100 ? { minimumFractionDigits: 2, maximumFractionDigits: 2 } : { maximumFractionDigits: 0 })}`;
  },
  signedUsd: (v) => (isNum(v) ? `${v >= 0 ? "+" : "-"}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 })}` : "–"),
  hours(h) {
    if (!isNum(h)) return "unknown";
    if (h < 0) return `ended ${fmt.hours(-h)} ago`;
    const m = Math.round(h * 60);
    if (m < 60) return `${m}m`;
    if (m < 1440) return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
    return `${Math.floor(m / 1440)}d ${Math.floor((m % 1440) / 60)}h`;
  },
  time(iso) {
    if (!iso) return "–";
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  },
  ago(seconds) {
    if (!isNum(seconds)) return "–";
    if (seconds < 90) return `${Math.round(seconds)}s ago`;
    if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
    return `${(seconds / 3600).toFixed(1)}h ago`;
  },
};
const signClass = (v) => (isNum(v) ? (v > 0 ? "pos" : v < 0 ? "neg" : "") : "");

const STATUS_META = {
  TRADE: { cls: "good", icon: "✓" },
  HOLDING: { cls: "accent", icon: "●" },
  FILTERED: { cls: "warning", icon: "!" },
  "NO EDGE": { cls: "muted", icon: "–" },
  CONFLICT: { cls: "serious", icon: "⚠" },
  "DATA UNAVAILABLE": { cls: "muted", icon: "∅" },
};
const VERDICT_META = {
  "WINNING STRATEGY": { cls: "good", icon: "✓" },
  "LOSING STRATEGY": { cls: "critical", icon: "✕" },
  "BREAK-EVEN": { cls: "muted", icon: "=" },
  "INSUFFICIENT DATA": { cls: "muted", icon: "…" },
};

function pill(text, meta) {
  const m = meta || { cls: "muted", icon: "" };
  return el("span", { class: `pill pill-${m.cls}` }, m.icon ? el("span", { "aria-hidden": "true" }, m.icon) : null, text);
}

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

// ----------------------------------------------------------------- tooltip

const tooltip = $("#tooltip");
function showTooltip(event, title, rows) {
  tooltip.replaceChildren(el("div", { class: "tt-title" }, title));
  for (const row of rows) {
    tooltip.appendChild(
      el("div", { class: "tt-row" },
        row.color ? el("span", { class: "tt-key", style: `background:${row.color}` }) : null,
        el("strong", {}, row.value),
        el("span", {}, row.label || ""))
    );
  }
  tooltip.hidden = false;
  const rect = tooltip.getBoundingClientRect();
  let x = (event.clientX ?? 0) + 14;
  let y = (event.clientY ?? 0) + 14;
  if (event.clientX === undefined && event.target?.getBoundingClientRect) {
    const r = event.target.getBoundingClientRect();
    x = r.right + 8;
    y = r.top;
  }
  if (x + rect.width > window.innerWidth - 8) x = x - rect.width - 28;
  if (y + rect.height > window.innerHeight - 8) y = window.innerHeight - rect.height - 8;
  tooltip.style.left = `${Math.max(8, x)}px`;
  tooltip.style.top = `${Math.max(8, y)}px`;
}
const hideTooltip = () => { tooltip.hidden = true; };

// ------------------------------------------------------------------ charts

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

function niceTicks(min, max, count = 4) {
  const span = max - min || 1;
  const step = Math.pow(10, Math.floor(Math.log10(span / count)));
  const err = (span / count) / step;
  const mult = err >= 7.5 ? 10 : err >= 3.5 ? 5 : err >= 1.5 ? 2 : 1;
  const s = step * mult;
  const ticks = [];
  for (let v = Math.ceil(min / s) * s; v <= max + s * 1e-9; v += s) ticks.push(+v.toFixed(10));
  return ticks;
}

function legend(items) {
  return el("div", { class: "legend" }, items.map((it) =>
    el("span", { class: "key" },
      el("span", { class: it.kind === "dot" ? "swatch-dot" : "swatch-line", style: it.hollow ? `border:2px solid ${it.color};background:transparent` : `background:${it.color}` }),
      it.name)));
}

function lineChart(container, { series, yFormat = fmt.pct, height = 220, yDomain = null, zeroLine = false, title = null }) {
  container.replaceChildren();
  const visible = series.filter((s) => s.points.length);
  if (!visible.length) {
    container.appendChild(el("p", { class: "empty" }, "No data yet."));
    return;
  }
  if (visible.length >= 2) container.appendChild(legend(visible.map((s) => ({ name: s.name, color: s.color }))));
  const width = Math.max(container.clientWidth, 320);
  const m = { l: 52, r: 18, t: 10, b: 26 };
  const xs = visible.flatMap((s) => s.points.map((p) => p.x.getTime()));
  const ys = visible.flatMap((s) => s.points.map((p) => p.y));
  let [y0, y1] = yDomain || [Math.min(...ys), Math.max(...ys)];
  if (zeroLine) { y0 = Math.min(y0, 0); y1 = Math.max(y1, 0); }
  if (y0 === y1) { y0 -= 0.01; y1 += 0.01; }
  const pad = (y1 - y0) * 0.08;
  if (!yDomain) { y0 -= pad; y1 += pad; }
  const x0 = Math.min(...xs);
  const x1 = Math.max(...xs) === x0 ? x0 + 1 : Math.max(...xs);
  const X = (t) => m.l + ((t - x0) / (x1 - x0)) * (width - m.l - m.r);
  const Y = (v) => m.t + (1 - (v - y0) / (y1 - y0)) * (height - m.t - m.b);
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": title || "line chart" });
  const grid = svg("g", { class: "grid" });
  const axis = svg("g", { class: "axis" });
  for (const t of niceTicks(y0, y1, 4)) {
    grid.appendChild(svg("line", { x1: m.l, x2: width - m.r, y1: Y(t), y2: Y(t) }));
    axis.appendChild(svg("text", { x: m.l - 6, y: Y(t) + 4, "text-anchor": "end" }, yFormat(t)));
  }
  const timeTicks = 4;
  for (let i = 0; i <= timeTicks; i++) {
    const t = x0 + ((x1 - x0) * i) / timeTicks;
    axis.appendChild(svg("text", { x: X(t), y: height - 6, "text-anchor": i === 0 ? "start" : i === timeTicks ? "end" : "middle" }, fmt.time(new Date(t).toISOString())));
  }
  root.append(grid, axis);
  if (zeroLine) root.appendChild(svg("line", { x1: m.l, x2: width - m.r, y1: Y(0), y2: Y(0), stroke: css("--text-muted"), "stroke-width": 1 }));
  for (const s of visible) {
    const d = s.points.map((p, i) => `${i ? "L" : "M"}${X(p.x.getTime()).toFixed(1)},${Y(p.y).toFixed(1)}`).join("");
    root.appendChild(svg("path", { d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    const last = s.points[s.points.length - 1];
    root.appendChild(svg("circle", { cx: X(last.x.getTime()), cy: Y(last.y), r: 4, fill: s.color, stroke: css("--surface-1"), "stroke-width": 2 }));
  }
  const cross = svg("line", { y1: m.t, y2: height - m.b, stroke: css("--text-muted"), "stroke-width": 1, visibility: "hidden" });
  const overlay = svg("rect", { x: m.l, y: m.t, width: width - m.l - m.r, height: height - m.t - m.b, fill: "transparent", tabindex: 0 });
  root.append(cross, overlay);
  const nearest = (pts, t) => pts.reduce((best, p) => (Math.abs(p.x.getTime() - t) < Math.abs(best.x.getTime() - t) ? p : best), pts[0]);
  const onMove = (event) => {
    const box = root.getBoundingClientRect();
    const scale = width / box.width;
    const t = x0 + (((event.clientX - box.left) * scale - m.l) / (width - m.l - m.r)) * (x1 - x0);
    const anchor = nearest(visible[0].points, t);
    cross.setAttribute("x1", X(anchor.x.getTime()));
    cross.setAttribute("x2", X(anchor.x.getTime()));
    cross.setAttribute("visibility", "visible");
    showTooltip(event, fmt.time(anchor.x.toISOString()), visible.map((s) => {
      const p = nearest(s.points, anchor.x.getTime());
      return { color: s.color, value: yFormat(p.y), label: s.name };
    }));
  };
  overlay.addEventListener("pointermove", onMove);
  overlay.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTooltip(); });
  container.appendChild(root);
}

function dumbbellChart(container, rows) {
  container.replaceChildren();
  const data = rows.filter((r) => r.n_bets > 0 && isNum(r.win_rate) && isNum(r.break_even_win_rate));
  if (!data.length) {
    container.appendChild(el("p", { class: "empty" }, "No settled bets yet."));
    return;
  }
  container.appendChild(legend([
    { name: "Required win rate (break-even, incl. fees)", color: css("--text-secondary"), kind: "dot", hollow: true },
    { name: "Actual win rate (95% interval)", color: css("--series-1"), kind: "dot" },
  ]));
  const width = Math.max(container.clientWidth, 320);
  const rowH = 34;
  const m = { l: 92, r: 150, t: 8, b: 26 };
  const height = m.t + m.b + rowH * data.length;
  const lo = Math.max(0, Math.min(...data.flatMap((r) => [r.break_even_win_rate, r.win_rate_ci_low ?? r.win_rate])) - 0.03);
  const X = (v) => m.l + ((v - lo) / (1 - lo)) * (width - m.l - m.r);
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "required versus actual win rate" });
  const grid = svg("g", { class: "grid" });
  const axis = svg("g", { class: "axis" });
  for (const t of niceTicks(lo, 1, 5)) {
    grid.appendChild(svg("line", { x1: X(t), x2: X(t), y1: m.t, y2: height - m.b }));
    axis.appendChild(svg("text", { x: X(t), y: height - 6, "text-anchor": "middle" }, fmt.pct(t, 0)));
  }
  root.append(grid, axis);
  data.forEach((r, i) => {
    const y = m.t + rowH * i + rowH / 2;
    const meta = VERDICT_META[r.verdict] || VERDICT_META["INSUFFICIENT DATA"];
    const connector = r.verdict === "INSUFFICIENT DATA" ? css("--text-muted") : r.win_rate >= r.break_even_win_rate ? css("--good") : css("--critical");
    root.appendChild(svg("text", { x: m.l - 10, y: y + 4, "text-anchor": "end", fill: css("--text-secondary"), "font-size": 12 }, r.group));
    if (isNum(r.win_rate_ci_low)) {
      root.appendChild(svg("line", { x1: X(r.win_rate_ci_low), x2: X(r.win_rate_ci_high), y1: y, y2: y, stroke: css("--text-muted"), "stroke-width": 1 }));
    }
    root.appendChild(svg("line", { x1: X(r.break_even_win_rate), x2: X(r.win_rate), y1: y, y2: y, stroke: connector, "stroke-width": 2 }));
    const req = svg("circle", { cx: X(r.break_even_win_rate), cy: y, r: 5, fill: css("--surface-1"), stroke: css("--text-secondary"), "stroke-width": 2, tabindex: 0 });
    const act = svg("circle", { cx: X(r.win_rate), cy: y, r: 5, fill: css("--series-1"), stroke: css("--surface-1"), "stroke-width": 2, tabindex: 0 });
    const hit = svg("rect", { x: m.l, y: y - rowH / 2, width: width - m.l - m.r, height: rowH, fill: "transparent" });
    const tip = (event) => showTooltip(event, `${r.group} · ${r.n_bets} bets`, [
      { value: fmt.pct(r.win_rate), label: `actual (${fmt.pct(r.win_rate_ci_low)}–${fmt.pct(r.win_rate_ci_high)})`, color: css("--series-1") },
      { value: fmt.pct(r.break_even_win_rate), label: "required (break-even)", color: css("--text-secondary") },
      { value: fmt.signedPct(r.roi, 2), label: "ROI" },
      { value: `${meta.icon} ${r.verdict}`, label: r.significant ? "" : "(not significant)" },
    ]);
    for (const node of [hit, req, act]) {
      node.addEventListener("pointermove", tip);
      node.addEventListener("focus", tip);
      node.addEventListener("pointerleave", hideTooltip);
      node.addEventListener("blur", hideTooltip);
    }
    root.append(hit, req, act);
    root.appendChild(svg("text", { x: width - m.r + 10, y: y + 4, fill: css("--text-secondary"), "font-size": 12 }, `${meta.icon} ${r.verdict === "INSUFFICIENT DATA" ? `n=${r.n_bets}` : r.verdict.split(" ")[0]}`));
  });
  container.appendChild(root);
}

function calibrationChart(container, series) {
  container.replaceChildren();
  const visible = series.filter((s) => s.bins.some((b) => b.n > 0));
  if (!visible.length) {
    container.appendChild(el("p", { class: "empty" }, "No settled bets yet."));
    return;
  }
  if (visible.length >= 2) container.appendChild(legend(visible.map((s) => ({ name: s.name, color: s.color, kind: "dot" }))));
  const width = Math.max(container.clientWidth, 320);
  const height = 260;
  const m = { l: 48, r: 14, t: 10, b: 30 };
  const vals = visible.flatMap((s) => s.bins.filter((b) => b.n).flatMap((b) => [b.predicted, b.actual]));
  const lo = Math.max(0, Math.floor((Math.min(0.75, ...vals) - 0.02) * 20) / 20);
  const clampY = (v) => Math.max(lo, Math.min(1, v));
  const X = (v) => m.l + ((v - lo) / (1 - lo)) * (width - m.l - m.r);
  const Y = (v) => m.t + (1 - (v - lo) / (1 - lo)) * (height - m.t - m.b);
  const root = svg("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "calibration chart" });
  const grid = svg("g", { class: "grid" });
  const axis = svg("g", { class: "axis" });
  for (const t of niceTicks(lo, 1, 5)) {
    grid.appendChild(svg("line", { x1: m.l, x2: width - m.r, y1: Y(t), y2: Y(t) }));
    axis.appendChild(svg("text", { x: m.l - 6, y: Y(t) + 4, "text-anchor": "end" }, fmt.pct(t, 0)));
    axis.appendChild(svg("text", { x: X(t), y: height - 12, "text-anchor": "middle" }, fmt.pct(t, 0)));
  }
  axis.appendChild(svg("text", { x: width - m.r, y: height - 1, "text-anchor": "end" }, "predicted →"));
  axis.appendChild(svg("text", { x: m.l + 4, y: m.t + 10 }, "↑ actual win rate"));
  root.append(grid, axis);
  root.appendChild(svg("line", { x1: X(lo), y1: Y(lo), x2: X(1), y2: Y(1), stroke: css("--text-muted"), "stroke-width": 1 }));
  const labelAt = lo + (1 - lo) * 0.18;
  root.appendChild(svg("text", { x: X(labelAt) + 6, y: Y(labelAt) - 6, fill: css("--text-muted"), "font-size": 11, transform: `rotate(${-Math.atan2(Y(lo) - Y(1), X(1) - X(lo)) * 180 / Math.PI} ${X(labelAt) + 6} ${Y(labelAt) - 6})` }, "perfect calibration"));
  for (const s of visible) {
    for (const b of s.bins.filter((bin) => bin.n)) {
      root.appendChild(svg("line", { x1: X(b.predicted), x2: X(b.predicted), y1: Y(clampY(b.ci_low)), y2: Y(clampY(b.ci_high)), stroke: s.color, "stroke-width": 1, opacity: 0.6 }));
      const dot = svg("circle", { cx: X(b.predicted), cy: Y(b.actual), r: 5, fill: s.color, stroke: css("--surface-1"), "stroke-width": 2, tabindex: 0 });
      const tip = (event) => showTooltip(event, `${s.name} · bin ${b.label}`, [
        { value: fmt.pct(b.actual), label: "actual", color: s.color },
        { value: fmt.pct(b.predicted), label: "predicted" },
        { value: `${fmt.pct(b.ci_low)}–${fmt.pct(b.ci_high)}`, label: "95% interval" },
        { value: String(b.n), label: "bets" },
      ]);
      dot.addEventListener("pointermove", tip);
      dot.addEventListener("focus", tip);
      dot.addEventListener("pointerleave", hideTooltip);
      dot.addEventListener("blur", hideTooltip);
      root.appendChild(dot);
    }
  }
  container.appendChild(root);
}

function tableView(headers, rows) {
  return el("details", { class: "table-view" },
    el("summary", {}, "Table view"),
    el("table", { class: "data" },
      el("thead", {}, el("tr", {}, headers.map((h) => el("th", {}, h)))),
      el("tbody", {}, rows.map((r) => el("tr", {}, r.map((c) => el("td", {}, c)))))));
}

// --------------------------------------------------------------- top bar

async function loadStatus() {
  try {
    const s = await api("/api/status");
    $("#synthetic-banner").hidden = !s.synthetic_data;
    const last = s.last_scan;
    const poly = (s.sources || []).find((x) => x.source === "polymarket scan");
    const node = $("#scan-status");
    node.replaceChildren();
    if (!last) {
      node.append("No scans yet — start `favorite-hunter run`");
    } else {
      const ok = last.data_available;
      node.append(
        pill(ok ? "Polymarket data OK" : "POLYMARKET DATA UNAVAILABLE", ok ? { cls: "good", icon: "✓" } : { cls: "critical", icon: "✕" }),
        el("span", { class: "muted" }, ` last scan ${fmt.ago(s.last_scan_age_seconds)} · ${last.candidates ?? 0} favorites`)
      );
      if (!ok && poly?.last_error) node.title = poly.last_error;
    }
    const b = s.bankroll;
    $("#bankroll-status").textContent = `Paper equity ${fmt.usd(b.equity_marked)} · realized ${fmt.signedUsd(b.realized_pnl)} · open ${fmt.usd(b.open_cost)}`;
  } catch (error) {
    $("#scan-status").textContent = `dashboard API error: ${error.message}`;
  }
}

// ---------------------------------------------------------- opportunities

const state = { opps: [], sort: { key: "ev_per_share", dir: "desc" } };
const ACTIONABLE = new Set(["TRADE", "HOLDING", "FILTERED"]);

function fillSelect(select, values) {
  const current = select.value;
  const first = select.options[0];
  select.replaceChildren(first, ...values.map((v) => el("option", { value: v }, v)));
  if (values.includes(current)) select.value = current;
}

async function loadOpportunities() {
  const table = $("#opp-table");
  table.classList.add("stale");
  try {
    const data = await api("/api/opportunities");
    state.opps = data.opportunities;
    fillSelect($("#f-category"), [...new Set(state.opps.map((o) => o.category).filter(Boolean))].sort());
    const order = ["<1h", "1-6h", "6-24h", "1-3d", "3d+", "past_end", "unknown"];
    fillSelect($("#f-bucket"), order.filter((b) => state.opps.some((o) => o.time_bucket === b)));
    renderOpportunities();
  } catch (error) {
    $("#opp-empty").hidden = false;
    $("#opp-empty").textContent = `Could not load opportunities: ${error.message}`;
  } finally {
    table.classList.remove("stale");
  }
}

function filteredOpportunities() {
  const status = $("#f-status").value;
  const category = $("#f-category").value;
  const bucket = $("#f-bucket").value;
  const q = $("#f-search").value.trim().toLowerCase();
  return state.opps.filter((o) => {
    if (status === "actionable" && !ACTIONABLE.has(o.status)) return false;
    if (status === "TRADE" && o.status !== "TRADE") return false;
    if (status === "estimated" && !isNum(o.est_prob)) return false;
    if (category && o.category !== category) return false;
    if (bucket && o.time_bucket !== bucket) return false;
    if (q && !`${o.market} ${o.side} ${o.event || ""}`.toLowerCase().includes(q)) return false;
    return true;
  });
}

function sortRows(rows) {
  const { key, dir } = state.sort;
  const sign = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const va = a[key];
    const vb = b[key];
    if (va === vb) return 0;
    if (va === null || va === undefined) return 1;
    if (vb === null || vb === undefined) return -1;
    return (typeof va === "string" ? va.localeCompare(vb) : va - vb) * sign;
  });
}

function renderOpportunities() {
  const rows = sortRows(filteredOpportunities());
  const all = state.opps;
  const withEstimate = all.filter((o) => isNum(o.est_prob));
  const trades = all.filter((o) => o.status === "TRADE" || o.status === "HOLDING");
  const best = withEstimate.reduce((m, o) => (isNum(o.ev_per_share) && (!m || o.ev_per_share > m.ev_per_share) ? o : m), null);
  $("#opp-tiles").replaceChildren(
    tile("Favorites in price band", fmt.num(all.length), "both outcomes of every market"),
    tile("With an external estimate", fmt.num(withEstimate.length), `${fmt.num(all.length - withEstimate.length)} DATA UNAVAILABLE`),
    tile("Passing every filter", fmt.num(trades.length), "TRADE / HOLDING"),
    tile("Best EV per share", best ? fmt.pts(best.ev_per_share) : "–", best ? best.market : "no estimates yet")
  );
  document.querySelectorAll("#opp-table th[data-sort]").forEach((th) => {
    th.classList.toggle("sorted", th.dataset.sort === state.sort.key);
    th.classList.toggle("asc", th.dataset.sort === state.sort.key && state.sort.dir === "asc");
  });
  const body = $("#opp-table tbody");
  body.replaceChildren(...rows.map((o) => {
    const unavailable = !isNum(o.est_prob);
    return el("tr", { class: "clickable", tabindex: 0, onclick: () => openDetail(o.key), onkeydown: (e) => { if (e.key === "Enter") openDetail(o.key); } },
      el("td", {}, el("div", { class: "market-name" }, o.market), o.event && o.event !== o.market ? el("div", { class: "sub" }, o.event) : null),
      el("td", {}, o.side),
      el("td", { class: "num" }, fmt.price(o.price), el("div", { class: "sub" }, `ask ${fmt.price(o.best_ask)}`)),
      el("td", { class: "num" }, unavailable ? el("span", { class: "muted" }, o.status === "CONFLICT" ? "CONFLICT" : "DATA UNAVAILABLE") : fmt.pct(o.est_prob),
        unavailable ? null : el("div", { class: "sub" }, `± ${fmt.pct(o.uncertainty)}`)),
      el("td", { class: `num ${signClass(o.edge)}` }, fmt.pts(o.edge)),
      el("td", { class: "num" }, isNum(o.confidence) ? `${Math.round(o.confidence)}/100` : "–"),
      el("td", { class: "num" }, fmt.hours(o.hours_to_resolution)),
      el("td", { class: "num" }, fmt.usd(o.liquidity), el("div", { class: "sub" }, `${fmt.usd(o.ask_depth_usd)} ≤2¢`)),
      el("td", { class: "num" }, fmt.usd(o.max_exec_usd)),
      el("td", { class: `num ${signClass(o.expected_roi)}` }, fmt.signedPct(o.expected_roi)),
      el("td", { class: "num" }, isNum(o.score) ? Math.round(o.score) : "–"),
      el("td", {}, pill(o.status, STATUS_META[o.status]), o.opportunity_type ? el("div", { class: "sub" }, o.opportunity_type) : null)
    );
  }));
  const empty = $("#opp-empty");
  empty.hidden = rows.length > 0;
  empty.textContent = state.opps.length ? "No favorites match these filters." : "No scan results yet. Run `favorite-hunter run` (or `dashboard --run`) to start scanning.";
}

function tile(label, value, sub) {
  return el("div", { class: "tile" }, el("div", { class: "label" }, label), el("div", { class: "value" }, value), sub ? el("div", { class: "sub" }, sub) : null);
}

// ------------------------------------------------------------ detail view

async function openDetail(key) {
  const drawer = $("#drawer");
  const body = $("#drawer-body");
  body.replaceChildren(el("p", { class: "muted" }, "Loading…"));
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  try {
    const data = await api(`/api/opportunity?key=${encodeURIComponent(key)}`);
    renderDetail(body, data);
  } catch (error) {
    body.replaceChildren(el("p", { class: "neg" }, `Could not load details: ${error.message}`));
  }
}

function closeDetail() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  hideTooltip();
}

function section(title, ...children) {
  return el("section", {}, el("h3", {}, title), ...children);
}

function kv(items) {
  return el("div", { class: "kv" }, items.map(([k, v, cls]) => el("div", {}, el("div", { class: "k" }, k), el("div", { class: `v ${cls || ""}` }, v))));
}

function renderDetail(body, data) {
  const d = data.detail;
  const est = d.estimate || {};
  const edge = d.edge || {};
  const available = isNum(est.probability);
  const cost = edge.effective_cost;
  const wins = isNum(cost) && cost < 1 ? cost / (1 - cost) : null;

  const header = el("div", {},
    el("h2", {}, d.question),
    el("div", { class: "muted" },
      `${d.event_title && d.event_title !== d.question ? d.event_title + " · " : ""}Side: `, el("strong", {}, d.outcome),
      ` · ${d.category} · resolves ${fmt.time(d.resolution_time)} (${fmt.hours(d.hours_to_resolution)}, from ${d.time_source}) `,
      d.url ? el("a", { href: d.url, target: "_blank", rel: "noopener noreferrer" }, "Open on Polymarket ↗") : null),
    el("div", { style: "margin-top:8px;display:flex;gap:8px;flex-wrap:wrap" },
      pill(data.status, STATUS_META[data.status]),
      d.opportunity_type ? pill(d.opportunity_type, { cls: "accent", icon: "" }) : null,
      pill("PAPER TRADE ONLY", { cls: "warning", icon: "⚠" }))
  );

  let why;
  if (available) {
    why = el("div", { class: "why" },
      el("div", { class: "headline" }, est.main_reason || "Model estimate available"),
      el("p", {}, `Estimated P(${d.outcome}) = ${fmt.pct(est.probability)} ± ${fmt.pct(est.uncertainty)} versus an all-in cost of ${fmt.pct(cost, 2)} per share (price ${fmt.price(edge.purchase_price, 4)} + fee ${fmt.price(edge.fee_per_share, 4)}). `,
        `Edge after fees ${fmt.pts(edge.probability_edge)}, expected value ${fmt.price(edge.ev_per_share, 4)} per $1 share, expected ROI ${fmt.signedPct(edge.expected_roi, 2)}.`),
      d.skip_reasons?.length ? el("p", { class: "muted" }, `Not traded because: ${d.skip_reasons.join("; ")}`) : null);
  } else {
    why = el("div", { class: "why" },
      el("div", { class: "headline" }, est.data_status === "CONFLICTING SOURCES" ? "CONFLICTING SOURCES — no estimate" : "DATA UNAVAILABLE — no estimate, so no mispricing claim"),
      el("p", {}, est.reason || (d.skip_reasons || []).join("; ") || "No external data source for this market."),
      el("p", { class: "muted" }, "The bot never uses the Polymarket price as its prediction and never fills missing data."));
  }

  const numbers = kv([
    ["Purchase price (VWAP)", fmt.price(d.entry_price, 4)],
    ["Best ask", fmt.price(d.best_ask, 3)],
    ["Taker fee / share", fmt.price(d.fee_per_share, 4)],
    ["Break-even probability", fmt.pct(edge.break_even_probability ?? d.break_even, 2)],
    ["Estimated probability", available ? `${fmt.pct(est.probability, 2)} ± ${fmt.pct(est.uncertainty, 1)}` : "DATA UNAVAILABLE"],
    ["Edge after fees", fmt.pts(edge.probability_edge), signClass(edge.probability_edge)],
    ["Edge at lower bound", fmt.pts(edge.lower_bound_edge), signClass(edge.lower_bound_edge)],
    ["EV per share", isNum(edge.ev_per_share) ? `$${edge.ev_per_share.toFixed(4)}` : "–", signClass(edge.ev_per_share)],
    ["Expected ROI", fmt.signedPct(edge.expected_roi, 2), signClass(edge.expected_roi)],
    ["Payout if correct", "$1.00"],
    ["Profit if correct", isNum(edge.profit_if_correct) ? `$${edge.profit_if_correct.toFixed(4)}` : "–"],
    ["Loss if wrong", isNum(edge.loss_if_wrong) ? `$${edge.loss_if_wrong.toFixed(4)}` : "–"],
    ["Risk / reward", isNum(edge.risk_reward_ratio) ? `risk ${edge.risk_reward_ratio.toFixed(1)} to make 1` : "–"],
    ["One loss erases", isNum(wins) ? `${wins.toFixed(1)} wins` : "–"],
    ["Full Kelly", fmt.pct(edge.kelly_fraction)],
    ["Max executable (keeps min edge)", fmt.usd(d.max_exec_usd)],
    ["Ask depth ≤ 2¢", fmt.usd(d.ask_depth_usd)],
    ["Spread", fmt.price(d.spread)],
    ["Market liquidity", fmt.usd(d.liquidity)],
    ["Volume / 24h", `${fmt.usd(d.volume)} / ${fmt.usd(d.volume_24hr)}`],
  ]);

  const evidence = (est.evidence || []).length
    ? el("ul", { class: "plain" }, est.evidence.map((e) => el("li", {},
        el("strong", {}, e.source), `: ${e.description}`,
        el("span", { class: "muted" }, ` · observed ${fmt.time(e.observed_at)} · reliability ${fmt.pct(e.quality, 0)} `),
        e.url ? el("a", { href: e.url, target: "_blank", rel: "noopener noreferrer" }, "source ↗") : null)))
    : el("p", { class: "muted" }, "No evidence collected — DATA UNAVAILABLE.");

  const components = (est.components && est.components.length ? est.components : [est]).filter((c) => c && c.engine);
  const sources = el("ul", { class: "plain" }, components.map((c) => el("li", {},
    el("strong", {}, `${c.engine}: ${c.method}`), " — ",
    isNum(c.probability) && c.data_status === "OK" ? `P = ${fmt.pct(c.probability, 2)} (${(c.sources || []).join(", ") || "sources n/a"})` : `${c.data_status}: ${c.reason || ""}`)));

  const calc = el("pre", { class: "calc" }, (est.calculation || []).join("\n") || "No calculation: DATA UNAVAILABLE.");

  const historyChart = el("div", { class: "chart" });
  const book = d.entry_fill ? renderBook(data, d) : el("p", { class: "muted" }, "No book.");

  const conf = d.confidence;
  const confidenceView = conf && conf.factors && available
    ? el("div", {},
        el("p", {}, el("strong", {}, `${Math.round(conf.value)}/100`), el("span", { class: "muted" }, " — confidence in the edge, not a probability")),
        ...Object.entries(conf.factors).map(([k, v]) => factorRow(k, v, conf.weights[k])),
        conf.penalties?.length ? el("p", { class: "muted" }, `Penalties: ${conf.penalties.join("; ")}`) : null)
    : el("p", { class: "muted" }, "No confidence score without an estimate.");
  const score = d.score;
  const scoreView = score
    ? el("div", {},
        el("p", {}, el("strong", {}, `${Math.round(score.value)}/100`), el("span", { class: "muted" }, " — FAVORITE EDGE SCORE")),
        ...Object.entries(score.components).map(([k, v]) => factorRow(k, v / 100, score.weights[k])))
    : el("p", { class: "muted" }, "No Favorite Edge Score without an estimate.");

  const risks = [...new Set([...(d.risk_flags || [])])];
  const riskView = el("div", {},
    risks.length ? el("ul", { class: "plain" }, risks.map((r) => el("li", {}, r))) : el("p", { class: "muted" }, "No specific risk flags."),
    d.skip_reasons?.length ? el("div", {}, el("p", { class: "muted" }, "Filters not passed:"), el("ul", { class: "plain" }, d.skip_reasons.map((r) => el("li", {}, r)))) : null);

  const lossItems = [...(est.loss_scenarios || [])];
  if (isNum(edge.loss_if_wrong)) lossItems.push(`A loss costs $${edge.loss_if_wrong.toFixed(2)} per share and erases ~${isNum(wins) ? wins.toFixed(1) : "?"} winning bets at this price.`);
  lossItems.push("The resolution source or rule interpretation differs from the model's assumptions.");
  const lossView = el("ul", { class: "plain" }, lossItems.map((r) => el("li", {}, r)));

  const rules = d.rules || {};
  const rulesView = el("div", {},
    el("p", {}, `Rule clarity ${Math.round(rules.score ?? 0)}/100 (heuristic). Named sources: ${(rules.sources || []).join(", ") || "none detected"}.`),
    rules.flags?.length ? el("p", { class: "muted" }, `Flags: ${rules.flags.join("; ")}`) : null,
    el("div", { class: "rules" }, data.detail.description || "(full rules text in the stored market record)"));

  const trades = data.trades?.length
    ? el("ul", { class: "plain" }, data.trades.map((t) => el("li", {}, `#${t.trade_id} ${t.kind} opened ${fmt.time(t.opened_at)} at ${fmt.price(t.entry_price)} for ${fmt.usd(t.amount_invested)} — ${t.status}${isNum(t.pnl) ? ` (${fmt.signedUsd(t.pnl)})` : ""}`)))
    : el("p", { class: "muted" }, "No paper position on this market side.");

  body.replaceChildren(
    header,
    section("Why the bot thinks it is mispriced", why),
    section("Numbers", numbers),
    section("Current evidence", evidence),
    section("Data sources", sources),
    section("Probability calculation", calc),
    section("Price vs estimate history", historyChart),
    section("Current Polymarket order book", book),
    section("Confidence", confidenceView),
    section("Favorite Edge Score", scoreView),
    section("Risks", riskView),
    section("What could cause this bet to lose", lossView),
    section("Resolution rules", rulesView),
    section("Paper positions", trades)
  );

  const askSeries = data.price_history.filter((p) => isNum(p.best_ask)).map((p) => ({ x: new Date(p.ts), y: p.best_ask }));
  const estSeries = data.estimate_history.filter((p) => isNum(p.est_prob)).map((p) => ({ x: new Date(p.ts), y: p.est_prob }));
  lineChart(historyChart, {
    series: [
      { name: "Best ask", color: css("--series-1"), points: askSeries },
      { name: "Estimated probability", color: css("--series-2"), points: estSeries },
    ],
    yFormat: (v) => fmt.pct(v, 1),
    height: 200,
    title: "best ask and estimated probability over time",
  });
  historyChart.appendChild(tableView(["Time", "Best bid", "Best ask"], data.price_history.slice(-30).reverse().map((p) => [fmt.time(p.ts), fmt.price(p.best_bid), fmt.price(p.best_ask)])));
}

function factorRow(name, value, weight) {
  const v = Math.max(0, Math.min(1, value || 0));
  return el("div", { class: "factor-row" },
    el("span", {}, `${name.replaceAll("_", " ")} (${fmt.pct(weight, 0)})`),
    el("span", { class: "meter" }, el("span", { style: `width:${(v * 100).toFixed(0)}%` })),
    el("span", { style: "text-align:right" }, `${Math.round(v * 100)}`));
}

function renderBook(data, d) {
  const book = data.stored_book || {};
  const levels = (side, rows) => {
    let cum = 0;
    const maxCum = rows.reduce((s, [p, q]) => s + p * q, 0) || 1;
    return el("div", {},
      el("h3", {}, side === "Asks" ? "Asks (you buy here)" : "Bids"),
      el("table", { class: "data" },
        el("thead", {}, el("tr", {}, el("th", { class: "num" }, "Price"), el("th", { class: "num" }, "Shares"), el("th", { class: "num" }, "Cumulative USD"))),
        el("tbody", {}, rows.slice(0, 12).map(([p, q]) => {
          cum += p * q;
          return el("tr", {},
            el("td", { class: "num" }, fmt.price(p)), el("td", { class: "num" }, fmt.num(q, 2)),
            el("td", { class: "num bar-cell" }, el("span", { class: "bar", style: `width:${((cum / maxCum) * 100).toFixed(0)}%;background:${side === "Asks" ? css("--series-2") : css("--series-1")}` }), fmt.usd(cum)));
        }))));
  };
  const asks = book.asks || [];
  const bids = book.bids || [];
  const fill = d.entry_fill || {};
  return el("div", {},
    el("p", { class: "muted" }, `Stored snapshot ${fmt.time(book.ts)} (book server time ${fmt.time(book.book_server_ts)}).`),
    el("div", { class: "book-grid" }, levels("Asks", asks), levels("Bids", bids)),
    el("p", {}, `Simulated buy of $${data.settings.reference_stake_usd}: ${fmt.num(fill.shares, 2)} shares, VWAP ${fmt.price(fill.vwap, 4)}, fees ${fmt.usd(fill.fees)}, all-in ${fmt.price(fill.avg_cost_per_share, 4)}/share — `,
      (fill.levels || []).map((l) => `${fmt.num(l.shares, 2)} @ ${fmt.price(l.price)}`).join(", ") || "no fill"));
}

// ------------------------------------------------------------------ trades

async function loadTrades() {
  const kind = $("#t-kind").value;
  const data = await api(`/api/trades?kind=${kind}`);
  const open = data.trades.filter((t) => t.status === "open");
  const closedAll = data.trades.filter((t) => t.status !== "open");
  const closed = closedAll.slice(0, 100);
  const b = data.bankroll;
  const unrealized = open.reduce((s, t) => s + (isNum(t.unrealized_pnl) ? t.unrealized_pnl : 0), 0);
  const realized = closedAll.reduce((s, t) => s + (t.pnl || 0), 0);
  const tiles = kind === "model" && b
    ? [tile("Paper equity (marked)", fmt.usd(b.equity_marked), `start ${fmt.usd(b.starting)}`), tile("Realized PnL", fmt.signedUsd(b.realized_pnl)),
       tile("Open cost", fmt.usd(b.open_cost), `${open.length} positions`), tile("Unrealized (bid)", fmt.signedUsd(unrealized)), tile("Cash", fmt.usd(b.cash))]
    : [tile("Observations open", fmt.num(open.length)), tile("Settled", fmt.num(closedAll.length)), tile("PnL if bought blindly", fmt.signedUsd(realized), "$100 per observation")];
  $("#trade-tiles").replaceChildren(...tiles);
  const head = (cols) => el("thead", {}, el("tr", {}, cols.map(([c, n]) => el("th", { class: n ? "num" : "" }, c))));
  $("#open-table").replaceChildren(
    head([["Opened"], ["Market"], ["Side"], ["Entry", 1], ["Est. prob", 1], ["Edge", 1], ["Cost", 1], ["Shares", 1], ["Mark (bid)", 1], ["Unrealized", 1], ["Time bucket"], ["Type"]]),
    el("tbody", {}, open.map((t) => el("tr", { class: "clickable", onclick: () => openDetail(t.key) },
      el("td", {}, fmt.time(t.opened_at)), el("td", {}, t.question), el("td", {}, t.outcome), el("td", { class: "num" }, fmt.price(t.entry_price)),
      el("td", { class: "num" }, fmt.pct(t.est_prob)), el("td", { class: `num ${signClass(t.edge)}` }, fmt.pts(t.edge)), el("td", { class: "num" }, fmt.usd(t.amount_invested)),
      el("td", { class: "num" }, fmt.num(t.shares, 2)), el("td", { class: "num" }, fmt.price(t.last_mark)), el("td", { class: `num ${signClass(t.unrealized_pnl)}` }, fmt.signedUsd(t.unrealized_pnl)),
      el("td", {}, t.time_bucket || "–"), el("td", {}, t.opportunity_type || t.strategy || "–"))))
  );
  $("#open-empty").hidden = open.length > 0;
  $("#closed-table").replaceChildren(
    head([["Resolved"], ["Market"], ["Side"], ["Entry", 1], ["Est. prob", 1], ["Cost", 1], ["Result"], ["PnL", 1], ["ROI", 1], ["Entry range"], ["Time bucket"], ["Type"]]),
    el("tbody", {}, closed.map((t) => el("tr", {},
      el("td", {}, fmt.time(t.resolved_at)), el("td", {}, t.question), el("td", {}, t.outcome), el("td", { class: "num" }, fmt.price(t.entry_price)),
      el("td", { class: "num" }, fmt.pct(t.est_prob)), el("td", { class: "num" }, fmt.usd(t.amount_invested)),
      el("td", {}, pill(t.status.toUpperCase(), t.status === "won" ? { cls: "good", icon: "✓" } : t.status === "lost" ? { cls: "critical", icon: "✕" } : { cls: "muted", icon: "½" })),
      el("td", { class: `num ${signClass(t.pnl)}` }, fmt.signedUsd(t.pnl)), el("td", { class: `num ${signClass(t.roi)}` }, fmt.signedPct(t.roi, 1)),
      el("td", {}, t.entry_bucket || "–"), el("td", {}, t.time_bucket || "–"), el("td", {}, t.opportunity_type || t.strategy || "–"))))
  );
  $("#closed-empty").hidden = closed.length > 0;
  $("#closed-note").textContent = closedAll.length > closed.length ? `Showing the ${closed.length} most recent of ${closedAll.length} settled. Full breakdowns are in Analytics.` : "";
}

// --------------------------------------------------------------- analytics

const DIM_TITLES = {
  entry_range: "Entry price range", category: "Category", time_remaining: "Time remaining", estimated_edge: "Estimated edge",
  confidence: "Confidence", liquidity: "Liquidity", volume: "Volume", strategy: "Strategy type",
};

async function loadAnalytics() {
  const dataset = $("#a-dataset").value;
  const dim = $("#a-dim").value;
  const data = await api(`/api/analytics?dataset=${dataset}`);
  const o = data.overall;
  const meta = VERDICT_META[o.verdict] || VERDICT_META["INSUFFICIENT DATA"];
  $("#a-overall").replaceChildren(
    el("div", {}, el("div", { class: "muted small" }, "Overall verdict"), el("div", { class: "big" }, pill(o.verdict, meta))),
    el("div", {}, el("div", { class: "muted small" }, "Settled bets"), el("div", { class: "big" }, fmt.num(o.n_bets))),
    el("div", {}, el("div", { class: "muted small" }, "Win rate vs required"), el("div", { class: "big" }, `${fmt.pct(o.win_rate)} vs ${fmt.pct(o.break_even_win_rate)}`)),
    el("div", {}, el("div", { class: "muted small" }, "ROI · PnL"), el("div", { class: `big ${signClass(o.roi)}` }, `${fmt.signedPct(o.roi, 2)} · ${fmt.signedUsd(o.total_pnl)}`)),
    el("div", {}, el("div", { class: "muted small" }, "Max drawdown · largest loss · losing streak"), el("div", { class: "big" }, `${fmt.usd(o.max_drawdown)} · ${fmt.usd(o.largest_loss)} · ${o.longest_losing_streak}`)),
    el("p", { class: "muted", style: "flex-basis:100%;margin:0" }, `${o.note || ""}${isNum(o.wins_needed_per_loss) ? ` · at the average cost one loss erases ~${o.wins_needed_per_loss.toFixed(1)} wins` : ""}`)
  );
  $("#a-extreme").replaceChildren(...data.extreme.map((s) => {
    const m = VERDICT_META[s.verdict] || VERDICT_META["INSUFFICIENT DATA"];
    return el("div", { class: `extreme ${m.cls}` },
      el("h4", {}, s.group.replace("c+", "¢+")),
      el("dl", {},
        el("dt", {}, "Bets"), el("dd", {}, fmt.num(s.n_bets)),
        el("dt", {}, "Average entry"), el("dd", {}, fmt.price(s.avg_entry_price)),
        el("dt", {}, "Required win rate"), el("dd", {}, fmt.pct(s.break_even_win_rate)),
        el("dt", {}, "Actual win rate"), el("dd", {}, isNum(s.win_rate) ? `${fmt.pct(s.win_rate)} (${fmt.pct(s.win_rate_ci_low)}–${fmt.pct(s.win_rate_ci_high)})` : "–"),
        el("dt", {}, "ROI · PnL"), el("dd", { class: signClass(s.roi) }, `${fmt.signedPct(s.roi, 2)} · ${fmt.signedUsd(s.total_pnl)}`),
        el("dt", {}, "Largest loss"), el("dd", {}, fmt.usd(s.largest_loss))),
      el("div", { class: "verdict" }, pill(s.verdict === "INSUFFICIENT DATA" ? `INSUFFICIENT DATA (n=${s.n_bets})` : s.verdict, m)),
      el("div", { class: "note" }, s.verdict === "INSUFFICIENT DATA" ? "Need 30+ settled bets." : s.significant ? "Statistically significant at 95%." : "Not yet statistically significant."));
  }));
  $("#a-dumbbell-title").textContent = "Required vs actual win rate by entry price";
  dumbbellChart($("#a-dumbbell"), data.breakdowns.entry_range || []);
  calibrationChart($("#a-calibration"), [
    { name: "Model probability", color: css("--series-1"), bins: data.calibration_model || [] },
    { name: "Price-implied (all-in cost)", color: css("--series-2"), bins: data.calibration_price || [] },
  ]);
  $("#a-breakdown-title").textContent = `By ${DIM_TITLES[dim].toLowerCase()}`;
  const rows = data.breakdowns[dim] || [];
  $("#a-breakdown").replaceChildren(
    el("thead", {}, el("tr", {}, ["Group", "Bets", "Win rate", "95% CI", "Avg entry", "Required", "Expected", "Gap", "ROI", "PnL", "Max DD", "Largest loss", "Losing streak", "Verdict"].map((h, i) => el("th", { class: i && i < 13 ? "num" : "" }, h)))),
    el("tbody", {}, rows.map((s) => el("tr", {},
      el("td", {}, s.group), el("td", { class: "num" }, fmt.num(s.n_bets)), el("td", { class: "num" }, fmt.pct(s.win_rate)),
      el("td", { class: "num" }, isNum(s.win_rate_ci_low) ? `${fmt.pct(s.win_rate_ci_low)}–${fmt.pct(s.win_rate_ci_high)}` : "–"),
      el("td", { class: "num" }, fmt.price(s.avg_entry_price)), el("td", { class: "num" }, fmt.pct(s.break_even_win_rate)), el("td", { class: "num" }, fmt.pct(s.expected_win_rate)),
      el("td", { class: `num ${signClass(s.edge_vs_break_even)}` }, fmt.pts(s.edge_vs_break_even)), el("td", { class: `num ${signClass(s.roi)}` }, fmt.signedPct(s.roi, 2)),
      el("td", { class: `num ${signClass(s.total_pnl)}` }, fmt.signedUsd(s.total_pnl)), el("td", { class: "num" }, fmt.usd(s.max_drawdown)), el("td", { class: "num" }, fmt.usd(s.largest_loss)),
      el("td", { class: "num" }, String(s.longest_losing_streak)), el("td", {}, pill(s.verdict === "INSUFFICIENT DATA" ? `n=${s.n_bets}` : s.verdict.split(" ")[0], VERDICT_META[s.verdict])))))
  );
  lineChart($("#a-equity"), {
    series: [{ name: "Cumulative PnL", color: css("--series-1"), points: (data.equity_curve || []).map(([t, v]) => ({ x: new Date(t), y: v })) }],
    yFormat: (v) => fmt.usd(v),
    zeroLine: true,
    height: 220,
    title: "cumulative PnL",
  });
  $("#a-caveat").textContent = dataset === "backtest"
    ? "Backtest: entries use Polymarket price history plus assumed slippage, not executable asks; flat $100 stakes; no depth; blind favorites by price only (not the probability model)."
    : dataset === "baseline"
      ? "Baseline: every in-band favorite recorded once per market side and time bucket with a simulated $100 fill — what blindly buying favorites would have done. Never traded."
      : "Model picks: paper trades opened only when every filter passed. Fills walk the real order book.";
}

// ------------------------------------------------------------------ system

async function loadSystem() {
  const [status, scans, config] = await Promise.all([api("/api/status"), api("/api/scans?limit=25"), api("/api/config")]);
  $("#s-sources").replaceChildren(
    el("thead", {}, el("tr", {}, ["Source", "OK", "Errors", "Last OK", "Last error"].map((h) => el("th", {}, h)))),
    el("tbody", {}, status.sources.map((s) => el("tr", {}, el("td", {}, s.source), el("td", {}, String(s.ok_count)), el("td", {}, String(s.error_count)),
      el("td", {}, fmt.time(s.last_ok)), el("td", {}, s.last_error ? `${fmt.time(s.last_error_ts)}: ${s.last_error}` : "–")))));
  $("#s-scans").replaceChildren(
    el("thead", {}, el("tr", {}, ["#", "Started", "Markets", "Favorites", "Opportunities", "Trades", "Data"].map((h) => el("th", {}, h)))),
    el("tbody", {}, scans.scans.map((s) => el("tr", {}, el("td", {}, String(s.scan_id)), el("td", {}, fmt.time(s.started_at)), el("td", {}, fmt.num(s.markets_fetched)),
      el("td", {}, fmt.num(s.candidates)), el("td", {}, fmt.num(s.opportunities)), el("td", {}, fmt.num(s.trades_opened)),
      el("td", {}, s.data_available ? pill("OK", { cls: "good", icon: "✓" }) : pill("UNAVAILABLE", { cls: "critical", icon: "✕" }))))));
  const groups = ["filters", "scanner", "fees", "paper", "score_weights", "confidence_weights", "alerts", "secrets_configured"];
  $("#s-config").replaceChildren(...groups.map((g) => el("div", {}, el("h4", {}, g.replaceAll("_", " ")),
    el("table", {}, el("tbody", {}, Object.entries(config[g]).map(([k, v]) => el("tr", {}, el("td", {}, k), el("td", {}, typeof v === "object" ? JSON.stringify(v) : String(v)))))))));
}

// -------------------------------------------------------------------- tabs

let currentTab = "opportunities";
async function refresh() {
  await loadStatus();
  try {
    if (currentTab === "opportunities") await loadOpportunities();
    else if (currentTab === "trades") await loadTrades();
    else if (currentTab === "analytics") await loadAnalytics();
    else if (currentTab === "system") await loadSystem();
  } catch (error) {
    console.error(error);
  }
}

document.querySelectorAll(".tabs button").forEach((button) => button.addEventListener("click", () => {
  currentTab = button.dataset.tab;
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b === button)));
  document.querySelectorAll(".tab").forEach((t) => { t.hidden = t.id !== `tab-${currentTab}`; });
  refresh();
}));
document.querySelectorAll("#opp-table th[data-sort]").forEach((th) => th.addEventListener("click", () => {
  const key = th.dataset.sort;
  state.sort = state.sort.key === key ? { key, dir: state.sort.dir === "asc" ? "desc" : "asc" } : { key, dir: ["market", "side", "status", "hours_to_resolution"].includes(key) ? "asc" : "desc" };
  renderOpportunities();
}));
["#f-status", "#f-category", "#f-bucket"].forEach((s) => $(s).addEventListener("change", renderOpportunities));
$("#f-search").addEventListener("input", renderOpportunities);
$("#t-kind").addEventListener("change", loadTrades);
$("#a-dataset").addEventListener("change", loadAnalytics);
$("#a-dim").addEventListener("change", loadAnalytics);
$("#drawer-close").addEventListener("click", closeDetail);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDetail(); });

refresh();
setInterval(refresh, 30000);
