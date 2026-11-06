"use strict";
// Hand-built SVG charts for the research views. Charts are drawn at the
// container's pixel width (re-laid out, not shrunk, on small screens) and
// redrawn when that width changes. Text only ever enters the DOM as text nodes.

const SVGNS = "http://www.w3.org/2000/svg";
function sv(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.setAttribute("class", v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "css") for (const [p, val] of Object.entries(v)) el.style.setProperty(p, val);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
function lin(d0, d1, r0, r1) {
  const f = v => r0 + (d1 === d0 ? 0 : (v - d0) / (d1 - d0)) * (r1 - r0);
  f.inv = p => d0 + (r1 === r0 ? 0 : (p - r0) / (r1 - r0)) * (d1 - d0);
  return f;
}
const PCT = v => v === null || v === undefined ? "–" : `${Math.round(v * 100)}%`;
const PTS = v => v === null || v === undefined ? "–" : `${v >= 0 ? "+" : "−"}${Math.abs(Math.round(v * 100))} pts`;
const NUM = new Intl.NumberFormat("en-US");
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
function ms(iso) { const [y, m, d] = iso.split("-").map(Number); return Date.UTC(y, (m || 1) - 1, d || 1); }
function dateLabel(iso, style) {
  if (!iso) return "–";
  const [y, m, d] = iso.split("-").map(Number);
  if (style === "month") return `${MONTHS[m - 1]} ${y}`;
  if (style === "year") return String(y);
  return `${d} ${MONTHS[m - 1]} ${y}`;
}
function niceMax(v) {
  const steps = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.1, 0.12, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.8, 1];
  return steps.find(s => s >= v * 1.08) || 1;
}
function pctTicks(max) {
  const step = max <= 0.1 ? 0.025 : max <= 0.25 ? 0.05 : max <= 0.5 ? 0.1 : 0.25;
  const out = [];
  for (let v = 0; v <= max + 1e-9; v += step) out.push(+v.toFixed(4));
  return out;
}
function reducedMotion() { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; }
function narrow(width) { return width < 560; }

// One hidden <svg> holds shared patterns so every chart can use url(#hatch).
function ensureDefs() {
  if (document.getElementById("chart-defs")) return;
  const defs = sv("svg", { id: "chart-defs", width: 0, height: 0, "aria-hidden": "true", class: "c", focusable: "false" },
    sv("defs", {}, sv("pattern", { id: "hatch", width: 4, height: 4, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" },
      sv("rect", { width: 4, height: 4, fill: "var(--bg)" }), sv("line", { x1: 0, y1: 0, x2: 0, y2: 4, class: "hatch-line" }))));
  defs.style.setProperty("position", "absolute");
  document.body.appendChild(defs);
}

// ------------------------------------------------------------------ tooltip / bottom sheet
const Tip = (() => {
  let el = null, sheet = false;
  function node() {
    if (!el) {
      el = document.createElement("div");
      el.className = "tip";
      el.setAttribute("role", "status");
      el.setAttribute("aria-live", "polite");
      document.body.appendChild(el);
      document.addEventListener("keydown", e => { if (e.key === "Escape") hide(); });
    }
    return el;
  }
  function show(content, at, touch) {
    const t = node();
    sheet = !!touch;
    t.className = "tip on" + (sheet ? " sheet" : "");
    t.replaceChildren(...content);
    if (sheet) {
      const close = document.createElement("button");
      close.className = "close";
      close.setAttribute("aria-label", "Close");
      close.textContent = "×";
      close.addEventListener("click", hide);
      t.appendChild(close);
      return;
    }
    const pad = 14, w = t.offsetWidth, hgt = t.offsetHeight;
    let x = at.x + pad, y = at.y + pad;
    if (x + w > window.innerWidth - 8) x = at.x - w - pad;
    if (y + hgt > window.innerHeight - 8) y = at.y - hgt - pad;
    t.style.setProperty("left", `${Math.max(8, x)}px`);
    t.style.setProperty("top", `${Math.max(8, y)}px`);
  }
  function hide() { if (el) el.className = "tip"; }
  function lines(title, ...rest) {
    const out = [];
    const a = document.createElement("div"); a.className = "t1"; a.textContent = title; out.push(a);
    for (const r of rest.filter(Boolean)) {
      if (r instanceof Node) { out.push(r); continue; }
      const b = document.createElement("div"); b.className = "t2"; b.textContent = r; out.push(b);
    }
    return out;
  }
  function atElement(target) {
    const r = target.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top };
  }
  return { show, hide, lines, atElement, get sheet() { return sheet; } };
})();

// Draw at the box's width now and again whenever it changes by more than 24 px,
// or when the height of `tall` does (a chart sized to the window). Returns a
// redraw function for new data, which morphs the chart already on screen.
function mount(box, draw, tall) {
  ensureDefs();
  let lastW = 0, lastH = 0;
  const run = data => {
    const w = Math.round(box.clientWidth), hgt = tall ? Math.round(tall.clientHeight) : 0;
    if (!w || (!data && Math.abs(w - lastW) < 24 && Math.abs(hgt - lastH) < 24)) return;
    lastW = w; lastH = hgt;
    if (data) morph(box, draw(w));
    else box.replaceChildren(draw(w));
  };
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(() => run());
    ro.observe(box);
    if (tall) ro.observe(tall);
  }
  requestAnimationFrame(() => run());
  return () => run(true);
}

// ------------------------------------------------------------------ figure wrapper
let figSeq = 0;
function figure({ title, titleNodes, sub, chart, legend, caption, table, csv, summary, extra }) {
  const id = `fig${++figSeq}`;
  const t = h("h3", { class: "fig-title", id: `${id}-t` }, titleNodes || title);
  const d = h("p", { class: "skip", id: `${id}-d` }, summary || "");
  if (chart) {
    chart.setAttribute("role", "group");
    chart.setAttribute("aria-labelledby", `${id}-t`);
    chart.setAttribute("aria-describedby", `${id}-d`);
  }
  const tools = h("div", { class: "fig-tools" },
    table ? h("details", { class: "as-table" }, h("summary", {}, "Show as table"), dataTable(table)) : null,
    csv ? h("a", { href: csv, download: "" }, "Download CSV") : null,
    extra || null);
  return h("figure", { class: "r-fig" }, t, sub ? h("p", { class: "fig-sub" }, sub) : null, d, chart, legend || null,
    caption ? h("figcaption", { class: "cap" }, caption) : null, tools);
}
function dataTable({ columns, rows, caption }) {
  return h("div", { class: "table-scroll", tabindex: 0, role: "region", "aria-label": caption || "Data table" },
    h("table", {}, caption ? h("caption", {}, caption) : null,
      h("thead", {}, h("tr", {}, columns.map(c => h("th", { scope: "col" }, c)))),
      h("tbody", {}, rows.map(r => h("tr", {}, r.map((v, i) => i === 0 ? h("th", { scope: "row" }, v ?? "–") : h("td", {}, v ?? "–")))))));
}
function legendItem(kind, label) {
  const g = sv("svg", { width: 18, height: 12, class: "c lg", "aria-hidden": "true" });
  const shapes = {
    line: () => sv("line", { x1: 0, x2: 18, y1: 6, y2: 6, class: "line" }),
    grey: () => sv("line", { x1: 0, x2: 18, y1: 6, y2: 6, class: "line grey" }),
    thin: () => sv("line", { x1: 0, x2: 18, y1: 6, y2: 6, class: "line thin" }),
    dotted: () => sv("line", { x1: 0, x2: 18, y1: 6, y2: 6, class: "pretrend" }),
    rule: () => sv("line", { x1: 9, x2: 9, y1: 0, y2: 12, class: "rule" }),
    band: () => sv("rect", { x: 0, y: 2, width: 18, height: 8, class: "band" }),
    filled: () => sv("circle", { cx: 9, cy: 6, r: 4.5, class: "dot-fill" }),
    open: () => sv("circle", { cx: 9, cy: 6, r: 4.5, class: "dot-open" }),
    readable: () => sv("rect", { x: 3, y: 1, width: 10, height: 10, class: "cell-R" }),
    scan: () => sv("rect", { x: 3, y: 1, width: 10, height: 10, class: "cell-S", stroke: "var(--ink)", "stroke-width": 0.75 }),
    gridScan: () => sv("rect", { x: 3, y: 1, width: 10, height: 10, class: "grid-S" }),
    gridUnavailable: () => sv("rect", { x: 3, y: 1, width: 10, height: 10, class: "grid-U" }),
    unavailable: () => sv("rect", { x: 3.5, y: 1.5, width: 9, height: 9, class: "cell-U" }),
    none: () => sv("rect", { x: 3.5, y: 1.5, width: 9, height: 9, fill: "none", stroke: "var(--mark)", "stroke-dasharray": "1.5 1.5" }),
    yes: () => sv("rect", { x: 7, y: 0, width: 3, height: 12, class: "tick-yes" }),
    no: () => sv("rect", { x: 7, y: 0, width: 3, height: 12, class: "tick-no" }),
    unc: () => sv("rect", { x: 7, y: 0.5, width: 3, height: 11, class: "tick-unc" }),
    neg: () => sv("rect", { x: 2, y: 2, width: 14, height: 8, class: "neg" }),
    pos: () => sv("rect", { x: 2, y: 2, width: 14, height: 8, class: "pos" }),
    mixed: () => sv("rect", { x: 2, y: 2, width: 14, height: 8, class: "mixed" }),
    hollow: () => sv("circle", { cx: 9, cy: 6, r: 4.5, class: "md hollow" }),
  };
  g.appendChild((shapes[kind] || shapes.line)());
  return h("li", {}, g, label);
}
function legendList(items) { return h("ul", { class: "legend-inline" }, items.map(([k, l]) => legendItem(k, l))); }

// ------------------------------------------------------------------ line chart (time or event time)
// series: [{key, label, cls, points: [{x, y, lo, hi, thin, n, tip}]}]; the first series gets the band,
// the hover focus and the end label. rules: [{x, label, short, policy}]. spans: [{x0, x1}].
function lineChart(width, o) {
  const small = narrow(width);
  const denomH = o.denom ? (small ? 34 : 44) : 0;
  const H = o.height || (small ? 260 : 340);
  const M = { t: o.rules && o.rules.length ? 34 : 14, r: o.endLabel && !small ? 104 : 14, b: 24 + (denomH ? denomH + 30 : 0), l: 40 };
  const all = o.series.flatMap(s => s.points).filter(p => p.y !== null && p.y !== undefined);
  const xs = o.xDomain || [Math.min(...all.map(p => p.x)), Math.max(...all.map(p => p.x))];
  const ymax = o.yMax || niceMax(Math.max(0.05, ...all.map(p => Math.max(p.y, o.bandMax ? 0 : (p.hi ?? 0) * 0.8))));
  const x = lin(xs[0], xs[1], M.l, width - M.r);
  const plotB = H - M.b;
  const y = lin(0, ymax, plotB, M.t);
  const svg = sv("svg", { class: "c", viewBox: `0 0 ${width} ${H}`, width, height: H });
  for (const t of pctTicks(ymax)) {
    svg.appendChild(sv("line", { class: t === 0 ? "base" : "grid", x1: M.l, x2: width - M.r, y1: y(t), y2: y(t) }));
    svg.appendChild(sv("text", { x: M.l - 6, y: y(t) + 4, "text-anchor": "end" }, PCT(t)));
  }
  const xticks = o.xTicks || [];
  const tickEvery = small ? Math.ceil(xticks.length / 5) : Math.ceil(xticks.length / 10);
  xticks.forEach((t, i) => {
    if (i % tickEvery) return;
    svg.appendChild(sv("text", { x: x(t.x), y: plotB + 16, "text-anchor": "middle" }, small && t.short ? t.short : t.label));
  });
  for (const sp of o.spans || []) {
    const x0 = Math.max(M.l, x(sp.x0)), x1 = Math.min(width - M.r, x(sp.x1));
    if (x1 > x0) svg.appendChild(sv("rect", { class: "after-hl", x: x0, y: M.t, width: x1 - x0, height: plotB - M.t }));
  }
  const main = o.series[0];
  if (main && o.band !== false) {
    const pts = main.points.filter(p => p.lo !== null && p.lo !== undefined && p.y !== null);
    if (pts.length > 1) {
      const top = pts.map((p, i) => `${i ? "L" : "M"}${x(p.x).toFixed(1)},${y(Math.min(p.hi, ymax)).toFixed(1)}`).join("");
      const bot = pts.slice().reverse().map(p => `L${x(p.x).toFixed(1)},${y(p.lo).toFixed(1)}`).join("");
      svg.appendChild(sv("path", { class: `band ${main.cls === "grey" ? "grey" : ""}`, d: `${top}${bot}Z` }));
    }
  }
  for (const s of o.series.slice(1).concat(main ? [main] : [])) drawSegments(svg, s, x, y);
  for (const p of o.projections || []) {
    const pts = p.points.filter(q => q.y !== null);
    if (pts.length > 1) svg.appendChild(sv("path", { class: p.cls || "pretrend", d: pts.map((q, i) => `${i ? "L" : "M"}${x(q.x).toFixed(1)},${y(Math.min(q.y, ymax)).toFixed(1)}`).join("") }));
    if (p.label && !small) {
      const last = pts[pts.length - 1];
      svg.appendChild(sv("text", { x: x(last.x) + 4, y: y(Math.min(last.y, ymax)) + 4 }, p.label));
    }
  }
  for (const s of o.series) {
    if (!s.markers) continue;
    for (const p of s.points) if (p.y !== null) svg.appendChild(sv("circle", { class: s.cls === "grey" ? "pt grey" : "pt", "data-key": `${s.key}${p.x}`, cx: x(p.x), cy: y(p.y), r: small ? 2.5 : 3 }));
  }
  // event rules with labels stacked to avoid collisions
  const placed = [];
  for (const r of (o.rules || []).filter(r => r.x >= xs[0] && r.x <= xs[1])) {
    const rx = x(r.x);
    svg.appendChild(sv("line", { class: `rule ${r.policy ? "policy" : ""} ${r.cls || ""}`, x1: rx, x2: rx, y1: M.t - 4, y2: plotB }));
    const text = small ? (r.short || r.label) : r.label;
    const wEst = text.length * 6.6;
    const anchorEnd = rx + wEst > width - M.r + (small ? 0 : 60);
    const x0 = anchorEnd ? rx - wEst - 4 : rx + 4;
    let row = 0;
    while (placed.some(p => p.row === row && !(x0 + wEst < p.x0 || x0 > p.x1))) row++;
    placed.push({ row, x0, x1: x0 + wEst });
    svg.appendChild(sv("text", { class: `lab ${r.cls || ""}`, x: anchorEnd ? rx - 4 : rx + 4, y: M.t - 22 + row * 13 + 8, "text-anchor": anchorEnd ? "end" : "start" }, text));
  }
  if (o.endLabel && main && !small) {
    const last = main.points.filter(p => p.y !== null).slice(-1)[0];
    if (last) svg.appendChild(sv("text", { class: "lab-accent", x: x(last.x) + 8, y: y(last.y) + 4 }, o.endLabel(last)));
  }
  if (o.placeholder) svg.appendChild(sv("text", { class: "placeholder", x: (M.l + width - M.r) / 2, y: (M.t + plotB) / 2, "text-anchor": "middle" }, o.placeholder));
  if (o.denom) {
    const top = plotB + 40, dmax = Math.max(1, ...o.denom.map(d => d.n));
    const dy = lin(0, dmax, top + denomH, top);
    const bw = Math.max(1, (width - M.l - M.r) / Math.max(1, o.denom.length) - (o.denom.length > 60 ? 0.5 : 2));
    for (const d of o.denom) svg.appendChild(sv("rect", { class: `denom ${d.thin ? "thin" : ""}`, "data-key": `n${d.x}`, x: x(d.x) - bw / 2, y: dy(d.n), width: bw, height: Math.max(0.5, top + denomH - dy(d.n)) }));
    svg.appendChild(sv("text", { x: M.l, y: top - 3 }, `${o.denomLabel || "readable meetings"} (scale to ${NUM.format(dmax)})`));
  }
  // hover and keyboard focus over the first series
  if (main && o.tip) {
    const pts = main.points.filter(p => p.y !== null);
    const xhair = sv("line", { class: "xhair", x1: 0, x2: 0, y1: M.t, y2: plotB, display: "none" });
    const ring = sv("circle", { class: "focus-ring", r: 4, cx: 0, cy: 0, display: "none" });
    svg.appendChild(xhair);
    svg.appendChild(ring);
    let idx = pts.length - 1;
    const focus = (i, at, touch) => {
      idx = Math.max(0, Math.min(pts.length - 1, i));
      const p = pts[idx];
      xhair.setAttribute("x1", x(p.x)); xhair.setAttribute("x2", x(p.x)); xhair.removeAttribute("display");
      ring.setAttribute("cx", x(p.x)); ring.setAttribute("cy", y(p.y)); ring.removeAttribute("display");
      Tip.show(o.tip(p), at || Tip.atElement(ring), touch);
    };
    const unfocus = () => { Tip.hide(); ring.setAttribute("display", "none"); xhair.setAttribute("display", "none"); };
    const hit = sv("rect", { class: "hit", x: M.l, y: M.t, width: width - M.l - M.r, height: plotB - M.t, tabindex: 0,
      "aria-label": "Chart values. Use left and right arrow keys to read each point." });
    const nearest = ev => {
      const r = svg.getBoundingClientRect();
      const px = (ev.clientX - r.left) * (width / r.width);
      const xv = x.inv(px);
      let best = 0;
      pts.forEach((p, i) => { if (Math.abs(p.x - xv) < Math.abs(pts[best].x - xv)) best = i; });
      return best;
    };
    hit.addEventListener("pointermove", ev => { if (ev.pointerType === "mouse") focus(nearest(ev), { x: ev.clientX, y: ev.clientY }); });
    hit.addEventListener("pointerdown", ev => { if (ev.pointerType !== "mouse") focus(nearest(ev), null, true); });
    hit.addEventListener("pointerleave", ev => { if (ev.pointerType === "mouse") unfocus(); });
    hit.addEventListener("focus", () => focus(idx));
    hit.addEventListener("blur", unfocus);
    hit.addEventListener("keydown", ev => {
      const step = { ArrowLeft: -1, ArrowRight: 1, Home: -1e6, End: 1e6 }[ev.key];
      if (step !== undefined) { ev.preventDefault(); focus(idx + step); }
      if (ev.key === "Enter" && o.onEnter) o.onEnter(pts[idx]);
    });
    if (o.onEnter) hit.addEventListener("click", ev => { if (ev.pointerType === "mouse") o.onEnter(pts[nearest(ev)]); });
    svg.insertBefore(hit, xhair);
  }
  return svg;
}
function drawSegments(svg, s, x, y) {
  // Consecutive points join; a segment touching a thin point is dashed; nulls break the line.
  let cur = null;
  const flush = () => { if (cur && cur.d.split(/[ML]/).length > 2) svg.appendChild(sv("path", { class: `line ${s.cls || ""} ${cur.thin ? "thin" : ""}`, d: cur.d })); };
  let prev = null;
  for (const p of s.points) {
    if (p.y === null || p.y === undefined) { flush(); cur = null; prev = null; continue; }
    const thin = !!(p.thin || (prev && prev.thin));
    const pt = `${x(p.x).toFixed(1)},${y(p.y).toFixed(1)}`;
    if (!cur || cur.thin !== thin) {
      flush();
      cur = { thin, d: prev ? `M${x(prev.x).toFixed(1)},${y(prev.y).toFixed(1)}L${pt}` : `M${pt}` };
    } else cur.d += `L${pt}`;
    prev = p;
  }
  flush();
  if (s.points.filter(p => p.y !== null).length === 1) {
    const p = s.points.find(q => q.y !== null);
    svg.appendChild(sv("circle", { class: "pt", cx: x(p.x), cy: y(p.y), r: 3 }));
  }
}

// ------------------------------------------------------------------ dumbbell
// rows: [{key, label, sub, before, after, n, thin, note}]
function dumbbell(width, rows, o = {}) {
  const small = narrow(width);
  const rowH = small ? 62 : 46;
  const M = { t: 26, r: small ? 16 : 150, b: 8, l: small ? 16 : 220 };
  const H = M.t + rows.length * rowH + M.b;
  const vals = rows.flatMap(r => [r.before, r.after]).filter(v => v !== null && v !== undefined);
  const xmax = niceMax(Math.max(0.1, ...vals));
  const x = lin(0, xmax, M.l, width - M.r);
  const svg = sv("svg", { class: "c", viewBox: `0 0 ${width} ${H}`, width, height: H });
  for (const t of pctTicks(xmax)) {
    svg.appendChild(sv("line", { class: "grid", x1: x(t), x2: x(t), y1: M.t - 6, y2: H - M.b }));
    svg.appendChild(sv("text", { x: x(t), y: M.t - 12, "text-anchor": "middle" }, PCT(t)));
  }
  rows.forEach((r, i) => {
    const top = M.t + i * rowH;
    const cy = small ? top + 40 : top + rowH / 2;
    const g = sv("g", { "data-key": r.key });
    const hit = sv("rect", { class: "row-hit", x: 0, y: top + 2, width, height: rowH - 4, rx: 6, tabindex: o.onSelect ? 0 : null,
      role: o.onSelect ? "link" : null,
      "aria-label": `${r.label}, ${r.sub}: ${PCT(r.before)} before, ${PCT(r.after)} after${r.n !== undefined ? `, ${r.n} districts` : ""}` });
    if (o.onSelect) {
      hit.addEventListener("click", () => o.onSelect(r));
      hit.addEventListener("keydown", ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); o.onSelect(r); } });
    }
    g.appendChild(hit);
    const lx = small ? 16 : 12;
    g.appendChild(sv("text", { class: r.selected ? "lab-accent" : "lab", x: lx, y: small ? top + 16 : cy - 3 }, r.label));
    g.appendChild(sv("text", { x: lx, y: small ? top + 28 : cy + 11 }, small ? `${r.sub} · ${r.nLabel || ""}` : r.sub));
    if (r.before !== null && r.before !== undefined && r.after !== null && r.after !== undefined) {
      g.appendChild(sv("line", { class: "bar-link", x1: x(r.before), x2: x(r.after), y1: cy, y2: cy, "stroke-dasharray": r.thin ? "3 3" : null }));
      g.appendChild(sv("circle", { class: "dot-open", cx: x(r.before), cy, r: 5.5 }));
      g.appendChild(sv("circle", { class: "dot-fill", cx: x(r.after), cy, r: 5.5, "fill-opacity": r.thin ? 0.45 : 1 }));
      const right = r.after >= r.before;
      const lab = PTS(r.after - r.before);
      const tx = right ? x(r.after) + 10 : x(r.after) - 10;
      g.appendChild(sv("text", { class: "lab", x: tx, y: cy + 4, "text-anchor": right ? "start" : "end" }, lab));
    } else {
      g.appendChild(sv("text", { x: M.l, y: cy + 4 }, r.empty || "Not enough records on both sides"));
    }
    if (!small) g.appendChild(sv("text", { x: width - 8, y: cy + 4, "text-anchor": "end" }, r.nLabel || ""));
    svg.appendChild(g);
  });
  return svg;
}

// ------------------------------------------------------------------ motion
// Every helper animates FROM a hidden state TO the state already in the DOM, with
// fill "backwards", so a mark that never animates (reduced motion, no WAAPI,
// background tab) is simply shown. Nothing is hidden in CSS waiting for script.
const EASE_OUT = "cubic-bezier(0.23, 1, 0.32, 1)";
function canAnimate(el) { return !reducedMotion() && el && typeof el.animate === "function"; }
// Solid line segments draw left to right like a pen along time; dashed (thin)
// segments, bands and points fade in behind it. Total about 700 ms, a data reveal.
function drawIn(root, total = 700) {
  const paths = [...root.querySelectorAll("path.line")];
  if (!paths.length || !canAnimate(paths[0])) return;
  const boxes = paths.map(p => { try { return p.getBBox(); } catch { return null; } });
  const x0 = Math.min(...boxes.filter(Boolean).map(b => b.x)), x1 = Math.max(...boxes.filter(Boolean).map(b => b.x + b.width));
  const span = Math.max(1, x1 - x0);
  paths.forEach((p, i) => {
    const b = boxes[i];
    if (!b) return;
    const delay = total * (b.x - x0) / span, duration = Math.max(40, total * b.width / span);
    if (p.classList.contains("thin")) {
      p.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 200, delay: delay + duration * 0.5, easing: EASE_OUT, fill: "backwards" });
      return;
    }
    const len = p.getTotalLength();
    p.animate([{ strokeDasharray: `${len} ${len}`, strokeDashoffset: len }, { strokeDasharray: `${len} ${len}`, strokeDashoffset: 0 }],
      { duration, delay, easing: "linear", fill: "backwards" });
  });
  for (const el of root.querySelectorAll(".band, .pt"))
    el.animate([{ opacity: 0 }, { opacity: 1 }], { duration: 300, delay: total * 0.6, easing: EASE_OUT, fill: "backwards" });
}
// Siblings settle in one after another: 4 px rise and a fade, 200 ms each, 50 ms apart.
function stagger(els, step = 50) {
  els = [...els];
  if (!els.length || !canAnimate(els[0])) return;
  els.forEach((el, i) => el.animate([{ opacity: 0, transform: "translateY(4px)" }, { opacity: 1, transform: "none" }],
    { duration: 200, delay: i * step, easing: EASE_OUT, fill: "backwards" }));
}
// A value that changed in place slides up into position (150 ms). No count-up:
// the final number is in the DOM from the first frame.
function swapIn(el) {
  if (!canAnimate(el)) return;
  el.animate([{ opacity: 0, transform: "translateY(4px)" }, { opacity: 1, transform: "none" }], { duration: 150, easing: EASE_OUT });
}
// The strong ease-out (--ease-out) for motion driven frame by frame.
function bezier(x1, y1, x2, y2) {
  const at = (t, a, b) => ((1 - 3 * b + 3 * a) * t + (3 * b - 6 * a)) * t * t + 3 * a * t;
  return p => {
    let lo = 0, hi = 1, t = p;
    for (let i = 0; i < 20; i++) { t = (lo + hi) / 2; if (at(t, x1, x2) < p) lo = t; else hi = t; }
    return at(t, y1, y2);
  };
}
const easeOut = bezier(0.23, 1, 0.32, 1);
// A chart redrawn with new data glides there (220 ms) instead of being swapped.
// Marks pair by data-key (their own, else their row's), tag, class and order;
// unclassed text, the axis ticks, pairs by its words so "10%" slides to where
// 10% now is. Numbers in each geometry attribute tween when the rest of the
// string matches; paths that differ in shape crossfade; unpaired marks fade.
// A redraw mid-tween starts from the frame on screen.
const GEOM = { svg: ["width", "height", "viewBox"], circle: ["cx", "cy", "r"], rect: ["x", "y", "width", "height"], line: ["x1", "y1", "x2", "y2"], text: ["x", "y"], path: ["d"] };
const NUMS = /-?\d*\.?\d+/g;
const morphing = new WeakMap();
function morph(box, next, duration = 220) {
  morphing.get(box)?.();
  const prev = box.firstElementChild;
  const marks = svg => [...svg.querySelectorAll("circle, rect, line, text, path")].filter(el => !el.closest(".ghosts"));
  if (!prev || prev.tagName !== "svg" || next.tagName !== "svg" || !canAnimate(next) || marks(prev).length > 1500) {
    box.replaceChildren(next);
    return;
  }
  const key = el => {
    const own = el.getAttribute("data-key");
    if (own) return `${el.tagName}#${own}`;
    const cls = el.getAttribute("class") || "";
    return `${el.closest("[data-key]")?.getAttribute("data-key") || ""}|${el.tagName}.${cls}${el.tagName === "text" && !cls ? `|${el.textContent}` : ""}`;
  };
  const pool = new Map();
  for (const el of marks(prev)) { const k = key(el); if (!pool.has(k)) pool.set(k, []); pool.get(k).push(el); }
  const tweens = [], fades = [], ghosts = sv("g", { class: "ghosts", "pointer-events": "none" });
  const plan = (from, el, attr) => {
    const a = from.getAttribute(attr), b = el.getAttribute(attr);
    if (a === null || b === null || a === b) return true;
    const pa = a.split(NUMS), pb = b.split(NUMS);
    if (pa.length !== pb.length || pa.some((s, i) => s !== pb[i])) return false;
    tweens.push({ el, attr, parts: pb, from: a.match(NUMS).map(Number), to: b.match(NUMS).map(Number), end: b });
    el.setAttribute(attr, a);
    return true;
  };
  const opacity = el => +(el.style.opacity || 1);
  const fadeOut = old => { fades.push([old, opacity(old), 0]); ghosts.appendChild(old); };
  let refocus = null;
  for (const a of GEOM.svg) plan(prev, next, a);
  for (const el of marks(next)) {
    const old = pool.get(key(el))?.shift();
    if (!old) { fades.push([el, 0, 1]); continue; }
    if (old === document.activeElement) refocus = el;
    if (opacity(old) < 1) fades.push([el, opacity(old), 1]);
    if (!GEOM[el.tagName].every(a => plan(old, el, a))) { fadeOut(old); fades.push([el, 0, 1]); }
  }
  for (const rest of pool.values()) rest.forEach(fadeOut);
  next.insertBefore(ghosts, next.firstChild);
  for (const [el, a] of fades) el.style.setProperty("opacity", a);
  box.replaceChildren(next);
  if (refocus) refocus.focus({ preventScroll: true });
  const t0 = performance.now();
  let raf = 0;
  const stop = done => {
    cancelAnimationFrame(raf);
    morphing.delete(box);
    if (!done) return;
    ghosts.remove();
    for (const t of tweens) t.el.setAttribute(t.attr, t.end);
    for (const [el] of fades) el.style.removeProperty("opacity");
  };
  const frame = now => {
    const p = Math.min(1, (now - t0) / duration), e = easeOut(p);
    for (const t of tweens) {
      let s = t.parts[0];
      for (let i = 0; i < t.to.length; i++) s += +(t.from[i] + (t.to[i] - t.from[i]) * e).toFixed(2) + t.parts[i + 1];
      t.el.setAttribute(t.attr, s);
    }
    for (const [el, a, b] of fades) el.style.setProperty("opacity", (a + (b - a) * e).toFixed(3));
    if (p < 1) raf = requestAnimationFrame(frame);
    else stop(true);
  };
  morphing.set(box, () => stop(false));
  raf = requestAnimationFrame(frame);
}
// Run fn once when el is a quarter in view, with a timeout backstop so a chart
// below the fold in a screenshot or background tab is never left mid-state.
function whenSeen(el, fn) {
  if (!("IntersectionObserver" in window)) return;
  let done = false;
  const go = () => { if (!done) { done = true; io.disconnect(); fn(); } };
  const io = new IntersectionObserver(es => { if (es.some(e => e.isIntersecting)) go(); }, { threshold: 0.25 });
  io.observe(el);
  setTimeout(() => { done = true; io.disconnect(); }, 8000);
}

// ------------------------------------------------------------------ small multiples
// panels: [{key, label, points: [{x, y, thin}]}] on a shared y scale.
function multiples(width, panels, o = {}) {
  const cols = width < 560 ? 2 : 3;
  const gap = 20, pw = (width - gap * (cols - 1)) / cols, ph = width < 560 ? 92 : 120;
  const rows = Math.ceil(panels.length / cols);
  const H = rows * (ph + 40);
  const all = panels.flatMap(p => p.points).filter(p => p.y !== null);
  const ymax = o.yMax || niceMax(Math.max(0.05, ...all.map(p => p.y)));
  const xs = o.xDomain || [Math.min(...all.map(p => p.x)), Math.max(...all.map(p => p.x))];
  const svg = sv("svg", { class: "c", viewBox: `0 0 ${width} ${H}`, width, height: H });
  panels.forEach((p, i) => {
    const ox = (i % cols) * (pw + gap), oy = Math.floor(i / cols) * (ph + 40);
    const x = lin(xs[0], xs[1], ox + 30, ox + pw - 6), y = lin(0, ymax, oy + 22 + ph - 18, oy + 22);
    const g = sv("g", { class: `mult ${p.key} ${o.selected === p.key ? "sel" : ""}` });
    const last = p.points.filter(q => q.y !== null).slice(-1)[0];
    g.appendChild(sv("text", { class: "ttl", x: ox, y: oy + 12 }, cols === 2 && p.short ? p.short : p.label));
    for (const t of [0, ymax]) {
      g.appendChild(sv("line", { class: t ? "grid" : "base", x1: ox + 30, x2: ox + pw - 6, y1: y(t), y2: y(t) }));
      g.appendChild(sv("text", { x: ox + 26, y: y(t) + 4, "text-anchor": "end" }, PCT(t)));
    }
    for (const r of o.rules || []) if (r.x >= xs[0] && r.x <= xs[1]) g.appendChild(sv("line", { class: "rule", x1: x(r.x), x2: x(r.x), y1: y(ymax), y2: y(0) }));
    drawSegments(g, { cls: o.selected && o.selected !== p.key ? "grey" : "", points: p.points }, x, y);
    g.appendChild(sv("text", { x: ox + 30, y: y(0) + 14 }, o.xLabel ? o.xLabel(xs[0]) : ""));
    g.appendChild(sv("text", { x: ox + pw - 6, y: y(0) + 14, "text-anchor": "end" }, last ? `${o.xLabel ? o.xLabel(last.x) : ""}: ${PCT(last.y)}` : ""));
    if (o.onSelect) {
      const hit = sv("rect", { class: "row-hit", x: ox, y: oy, width: pw, height: ph + 30, rx: 6, tabindex: 0, role: "button",
        "aria-pressed": String(o.selected === p.key), "aria-label": `${p.label}${last ? `, ${PCT(last.y)} in the latest year` : ""}. Select to filter.` });
      hit.addEventListener("click", () => o.onSelect(p.key));
      hit.addEventListener("keydown", ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); o.onSelect(p.key); } });
      g.insertBefore(hit, g.firstChild);
    }
    svg.appendChild(g);
  });
  return svg;
}

// ------------------------------------------------------------------ diverging tone bars
// rows: [{year, who, meetings, negative, positive, mixed}]; one panel per group.
function diverging(width, rows, o = {}) {
  const groups = o.groups || ["students", "staff"];
  const small = narrow(width);
  const years = [...new Set(rows.map(r => r.year))].sort();
  const panelW = small ? width : (width - 32) / groups.length;
  const rowH = 16, H = 40 + years.length * rowH + 14;
  const svgH = small ? H * groups.length + 10 : H;
  const shares = rows.map(r => r.meetings ? (r.negative + r.mixed / 2) / r.meetings : 0).concat(rows.map(r => r.meetings ? (r.positive + r.mixed / 2) / r.meetings : 0));
  const xmax = niceMax(Math.max(0.02, ...shares));
  const svg = sv("svg", { class: "c", viewBox: `0 0 ${width} ${svgH}`, width, height: svgH });
  groups.forEach((g, gi) => {
    const ox = small ? 0 : gi * (panelW + 32), oy = small ? gi * (H + 10) : 0;
    const cx = ox + 40 + (panelW - 40) / 2;
    const x = lin(0, xmax, 0, (panelW - 48) / 2);
    svg.appendChild(sv("text", { class: "ttl", x: ox + 40, y: oy + 12 }, g === "students" ? "About students" : "About teachers and staff"));
    svg.appendChild(sv("text", { x: cx - 4, y: oy + 28, "text-anchor": "end" }, "− negative"));
    svg.appendChild(sv("text", { x: cx + 4, y: oy + 28 }, "positive +"));
    svg.appendChild(sv("text", { x: cx - x(xmax), y: oy + 28, "text-anchor": "start" }, PCT(xmax)));
    svg.appendChild(sv("text", { x: cx + x(xmax), y: oy + 28, "text-anchor": "end" }, PCT(xmax)));
    for (const t of [-xmax, -xmax / 2, xmax / 2, xmax]) svg.appendChild(sv("line", { class: "grid", x1: cx + Math.sign(t) * x(Math.abs(t)), x2: cx + Math.sign(t) * x(Math.abs(t)), y1: oy + 34, y2: oy + 36 + years.length * rowH }));
    years.forEach((yr, i) => {
      const r = rows.find(q => q.year === yr && q.who === g);
      const y = oy + 36 + i * rowH;
      svg.appendChild(sv("text", { x: ox + 34, y: y + 11, "text-anchor": "end" }, String(yr)));
      if (!r || !r.meetings) return;
      const neg = r.negative / r.meetings, pos = r.positive / r.meetings, mix = r.mixed / r.meetings;
      const title = `${yr}, ${g}: ${PCT(neg)} negative, ${PCT(mix)} mixed, ${PCT(pos)} positive of ${NUM.format(r.meetings)} meetings`;
      const bar = (cls, x0, w) => sv("rect", { class: cls, x: x0, y: y + 2, width: Math.max(0, w), height: rowH - 5 }, sv("title", {}, title));
      svg.appendChild(bar("mixed", cx - x(mix / 2), x(mix)));
      svg.appendChild(bar("neg", cx - x(mix / 2) - x(neg), x(neg)));
      svg.appendChild(bar("pos", cx + x(mix / 2), x(pos)));
    });
    svg.appendChild(sv("line", { class: "base", x1: cx, x2: cx, y1: oy + 34, y2: oy + 36 + years.length * rowH }));
  });
  return svg;
}

// ------------------------------------------------------------------ district: one tick per meeting
function meetingTicks(width, m, o = {}) {
  const small = narrow(width);
  const H = small ? 200 : 230;
  const M = { t: 30, r: 14, b: 22, l: 40 };
  const x0 = ms(o.from || m.meetings[0]?.date || "2010-01-01"), x1 = ms(o.to || new Date().toISOString().slice(0, 10));
  const x = lin(x0, x1, M.l, width - M.r);
  // event labels stacked in rows so nearby events do not print over each other
  const placed = [], evLabels = [];
  for (const e of (o.events || []).slice().sort((a, b) => a.date.localeCompare(b.date))) {
    const ex = x(ms(e.date));
    if (ex < M.l || ex > width - M.r) continue;
    const text = small ? e.short : e.labelShort, wEst = text.length * 6.6;
    const anchorEnd = ex + 4 + wEst > width;
    const lx0 = anchorEnd ? ex - 4 - wEst : ex + 4;
    let row = 0;
    while (placed.some(p => p.row === row && !(lx0 + wEst < p.x0 || lx0 > p.x1))) row++;
    placed.push({ row, x0: lx0, x1: lx0 + wEst });
    evLabels.push({ ex, text, row, anchorEnd });
  }
  const rows = evLabels.length ? Math.max(...evLabels.map(l => l.row)) + 1 : 1;
  M.t += (rows - 1) * 13;
  const H2 = H + (rows - 1) * 13;
  const tickTop = H2 - M.b - 30, tickBot = H2 - M.b - 4;
  const y = lin(0, 1, tickTop - 10, M.t);
  const svg = sv("svg", { class: "c", viewBox: `0 0 ${width} ${H2}`, width, height: H2 });
  for (const t of [0, 0.5, 1]) {
    svg.appendChild(sv("line", { class: t ? "grid" : "base", x1: M.l, x2: width - M.r, y1: y(t), y2: y(t) }));
    svg.appendChild(sv("text", { x: M.l - 6, y: y(t) + 4, "text-anchor": "end" }, PCT(t)));
  }
  const y0 = new Date(x0).getUTCFullYear(), y1 = new Date(x1).getUTCFullYear();
  const every = Math.ceil((y1 - y0 + 1) / (small ? 4 : 9));
  for (let yr = y0; yr <= y1; yr++) if ((yr - y0) % every === 0) svg.appendChild(sv("text", { x: x(Date.UTC(yr, 0, 1)), y: H2 - 6, "text-anchor": "middle" }, String(yr)));
  for (const l of evLabels) {
    svg.appendChild(sv("line", { class: "rule", x1: l.ex, x2: l.ex, y1: 22 + l.row * 13, y2: tickBot }));
    svg.appendChild(sv("text", { class: "lab halo", x: l.anchorEnd ? l.ex - 4 : l.ex + 4, y: 16 + l.row * 13, "text-anchor": l.anchorEnd ? "end" : "start" }, l.text));
  }
  const tw = Math.max(1, Math.min(3, (width - M.l - M.r) / Math.max(1, m.meetings.length) * 0.8));
  for (const d of m.scan_dates || []) svg.appendChild(sv("rect", { class: "tick-scan", x: x(ms(d)) - tw / 2, y: tickTop, width: Math.max(tw, 2), height: tickBot - tickTop }));
  const marks = [];
  for (const mt of m.meetings) {
    if (!mt.screened) continue;
    const cls = mt.hit ? "tick-yes" : mt.uncertain ? "tick-unc" : "tick-no";
    const r = sv("rect", { class: cls, x: x(ms(mt.date)) - tw / 2, y: tickTop + (mt.hit ? 0 : 6), width: tw, height: tickBot - tickTop - (mt.hit ? 0 : 6) });
    svg.appendChild(r);
    marks.push({ mt, el: r });
  }
  // rolling 12-month share, drawn where the window holds at least 6 screened meetings
  if (o.measures) {
    const scr = m.meetings.filter(q => q.screened).map(q => ({ t: ms(q.date), hit: q.hit }));
    const pts = [];
    let lo = 0;
    for (let i = 0; i < scr.length; i++) {
      while (scr[i].t - scr[lo].t > 365.25 * 864e5) lo++;
      const win = scr.slice(lo, i + 1);
      if (win.length >= 6) pts.push({ x: scr[i].t, y: win.filter(q => q.hit).length / win.length });
      else pts.push({ x: scr[i].t, y: null });
    }
    const step = Math.max(1, Math.floor(pts.length / 400));
    drawSegments(svg, { cls: "", points: pts.filter((_, i) => i % step === 0 || i === pts.length - 1) }, x, y);
  }
  // hover / keyboard over meetings
  if (marks.length) {
    const ring = sv("rect", { class: "focus-ring", x: 0, display: "none", y: tickTop - 3, width: 6, height: tickBot - tickTop + 6, rx: 2 });
    svg.appendChild(ring);
    let idx = marks.length - 1;
    const focus = (i, at, touch) => {
      idx = Math.max(0, Math.min(marks.length - 1, i));
      const { mt } = marks[idx];
      ring.setAttribute("x", x(ms(mt.date)) - 3); ring.removeAttribute("display");
      Tip.show(Tip.lines(dateLabel(mt.date), mt.hit ? `Safety mention${mt.subtopics.length ? ": " + mt.subtopics.map(s => (o.labels || {})[s] || s).join(", ") : ""}` : mt.uncertain ? "Safety: uncertain" : "No safety mention found",
        mt.tone && mt.tone !== "none" ? `Temperament: ${mt.tone}` : null, mt.closed ? "Closed security session cited" : null,
        `${mt.detail || "Document"} · Enter or click to open`), at || Tip.atElement(ring), touch);
    };
    const hit = sv("rect", { class: "hit", x: M.l, y: M.t, width: width - M.l - M.r, height: tickBot - M.t, tabindex: 0,
      "aria-label": "Meetings over time. Use arrow keys to move between meetings and Enter to open one." });
    const nearest = ev => {
      const r = svg.getBoundingClientRect();
      const t = x.inv((ev.clientX - r.left) * (width / r.width));
      let best = 0;
      marks.forEach((q, i) => { if (Math.abs(ms(q.mt.date) - t) < Math.abs(ms(marks[best].mt.date) - t)) best = i; });
      return best;
    };
    hit.addEventListener("pointermove", ev => { if (ev.pointerType === "mouse") focus(nearest(ev), { x: ev.clientX, y: ev.clientY }); });
    hit.addEventListener("pointerdown", ev => { if (ev.pointerType !== "mouse") focus(nearest(ev), null, true); });
    hit.addEventListener("pointerleave", () => { if (!Tip.sheet) Tip.hide(); ring.setAttribute("display", "none"); });
    hit.addEventListener("click", ev => { if (ev.pointerType === "mouse" && o.onOpen) o.onOpen(marks[nearest(ev)].mt); });
    hit.addEventListener("focus", () => focus(idx));
    hit.addEventListener("blur", () => { Tip.hide(); ring.setAttribute("display", "none"); });
    hit.addEventListener("keydown", ev => {
      const step = { ArrowLeft: -1, ArrowRight: 1, Home: -1e6, End: 1e6, PageUp: -10, PageDown: 10 }[ev.key];
      if (step !== undefined) { ev.preventDefault(); focus(idx + step); }
      if (ev.key === "Enter" && o.onOpen) o.onOpen(marks[idx].mt);
    });
    svg.insertBefore(hit, ring);
  }
  return svg;
}

// ------------------------------------------------------------------ coverage strip (one district)
function coverageStrip(width, strip, years) {
  const H = 26, cw = (width - 2) / years.length;
  const svg = sv("svg", { class: "c", viewBox: `0 0 ${width} ${H + 14}`, width, height: H + 14 });
  years.forEach((yr, i) => {
    const r = strip.find(s => s.year === yr);
    const state = !r ? "none" : r.readable ? "R" : r.scan ? "S" : r.unavailable ? "U" : "none";
    const title = r ? `${yr}: ${r.readable} readable, ${r.scan} scans awaiting OCR, ${r.unavailable} unavailable` : `${yr}: no record found`;
    svg.appendChild(sv("rect", { class: state === "none" ? "" : `cell-${state}`, x: 1 + i * cw + 0.5, y: 1, width: Math.max(1, cw - 1.5), height: H,
      fill: state === "none" ? "none" : null, stroke: state === "none" ? "var(--hair)" : state === "S" ? "var(--ink)" : null, "stroke-width": state === "S" ? 0.5 : null },
      sv("title", {}, title)));
    if (i === 0 || i === years.length - 1 || (width > 560 && yr % 5 === 0)) svg.appendChild(sv("text", { x: 1 + i * cw + cw / 2, y: H + 13, "text-anchor": i === 0 ? "start" : i === years.length - 1 ? "end" : "middle" }, String(yr)));
  });
  return svg;
}

// ------------------------------------------------------------------ coverage grid (all districts)
function coverageGrid(width, grid, o = {}) {
  const years = grid.years, n = grid.districts.length;
  const small = narrow(width);
  const M = { t: 70, l: small ? 40 : 46, r: 8 };
  const cw = (width - M.l - M.r) / years.length;
  if (small) {
    // Phones: one row per size band, cell darkness = share of the band's districts with readable records.
    const bands = o.bands || [];
    const H = M.t + bands.length * 30 + 20;
    const svg = sv("svg", { class: "c", viewBox: `0 0 ${width} ${H}`, width, height: H });
    years.forEach((yr, i) => { if (i % 6 === 0 || i === years.length - 1) svg.appendChild(sv("text", { x: M.l + i * cw + cw / 2, y: M.t - 8, "text-anchor": "middle" }, `'${String(yr).slice(2)}`)); });
    bands.forEach((b, bi) => {
      const ds = grid.districts.filter(d => (d.size_band || "Unknown size") === b);
      svg.appendChild(sv("text", { class: "ttl", x: 0, y: M.t + bi * 30 - 2 + 8 }, ""));
      years.forEach((yr, i) => {
        const share = ds.length ? ds.filter(d => d.cells[i] === "R").length / ds.length : 0;
        svg.appendChild(sv("rect", { x: M.l + i * cw + 0.5, y: M.t + bi * 30, width: cw - 1, height: 24, fill: "var(--ink)", "fill-opacity": (0.06 + 0.94 * share).toFixed(2) },
          sv("title", {}, `${b}, ${yr}: ${PCT(share)} of ${ds.length} districts have readable records`)));
      });
      svg.appendChild(sv("text", { x: M.l - 4, y: M.t + bi * 30 + 16, "text-anchor": "end" }, ({ "Under 500 students": "<500", "500 to 1,999": "500+", "2,000 to 9,999": "2k+", "10,000 to 49,999": "10k+", "50,000 or more": "50k+" })[b] || b));
    });
    return svg;
  }
  const rowH = o.rowH || 2;
  const H = M.t + n * rowH + 24;
  const svg = sv("svg", { class: "c", viewBox: `0 0 ${width} ${H}`, width, height: H });
  // marginal bars: districts per year by state
  const pmax = Math.max(1, ...grid.per_year.map(p => p.readable + p.scan + p.unavailable));
  const by = lin(0, pmax, M.t - 14, 8);
  grid.per_year.forEach((p, i) => {
    let base = M.t - 14;
    for (const [k, cls] of [["readable", "cell-R"], ["scan", "cell-S"], ["unavailable", "cell-U"]]) {
      const hgt = (M.t - 14) - by(p[k]);
      if (hgt > 0) svg.appendChild(sv("rect", { class: cls, x: M.l + i * cw + 1, y: base - hgt, width: cw - 2, height: hgt }, sv("title", {}, `${p.year}: ${p[k]} districts ${k === "readable" ? "with readable records" : k === "scan" ? "with only scans awaiting OCR" : "with listed but unavailable documents"}`)));
      base -= hgt;
    }
    if (i % 5 === 0 || i === years.length - 1) svg.appendChild(sv("text", { x: M.l + i * cw + cw / 2, y: M.t - 2, "text-anchor": "middle" }, String(p.year)));
  });
  svg.appendChild(sv("text", { x: M.l - 6, y: 16, "text-anchor": "end" }, NUM.format(pmax)));
  const g = sv("g", {});
  grid.districts.forEach((d, r) => {
    let start = 0;
    for (let i = 1; i <= years.length; i++) {
      if (i === years.length || d.cells[i] !== d.cells[start]) {
        const c = d.cells[start];
        // Rows are too thin for hatching, so the grid uses solid tones: ink, grey, pale.
        if (c !== ".") g.appendChild(sv("rect", { class: `grid-${c}`, x: M.l + start * cw, y: M.t + r * rowH, width: (i - start) * cw - 0.5, height: rowH }));
        start = i;
      }
    }
  });
  svg.appendChild(g);
  svg.appendChild(sv("text", { x: M.l - 6, y: M.t + 8, "text-anchor": "end" }, "1"));
  svg.appendChild(sv("text", { x: M.l - 6, y: M.t + n * rowH, "text-anchor": "end" }, NUM.format(n)));
  const hit = sv("rect", { class: "hit", x: M.l, y: M.t, width: width - M.l - M.r, height: n * rowH });
  const pick = ev => {
    const rect = svg.getBoundingClientRect();
    const py = (ev.clientY - rect.top) * (H / rect.height);
    const px = (ev.clientX - rect.left) * (width / rect.width);
    return { d: grid.districts[Math.floor((py - M.t) / rowH)], yi: Math.floor((px - M.l) / cw) };
  };
  hit.addEventListener("pointermove", ev => {
    const { d, yi } = pick(ev);
    if (!d) return;
    const state = grid.legend[d.cells[yi]] || "";
    Tip.show(Tip.lines(d.school_district, `${years[yi]}: ${state}`, d.first_year ? `First year on record: ${d.first_year}` : "No records", "Click to open the district"), { x: ev.clientX, y: ev.clientY }, ev.pointerType !== "mouse");
  });
  hit.addEventListener("pointerleave", () => Tip.hide());
  hit.addEventListener("click", ev => { const { d } = pick(ev); if (d && o.onSelect) o.onSelect(d); });
  svg.appendChild(hit);
  return svg;
}

// ------------------------------------------------------------------ Texas dot map
// Zoom keeps dots at their on-screen size, so a dense metro spreads apart
// instead of every dot growing. District names appear once there is room.
function dotMap(width, data, outline, o = {}) {
  const lat0 = 31.2, kx = Math.cos(lat0 * Math.PI / 180);
  const pts = outline.rings.flat();
  const X = lon => lon * kx, Y = lat => -lat;
  const minX = Math.min(...pts.map(p => X(p[0]))), maxX = Math.max(...pts.map(p => X(p[0])));
  const minY = Math.min(...pts.map(p => Y(p[1]))), maxY = Math.max(...pts.map(p => Y(p[1])));
  const pad = 12;
  const scale = (width - 2 * pad) / (maxX - minX);
  const H = Math.round((maxY - minY) * scale + 2 * pad);
  const px = lon => pad + (X(lon) - minX) * scale, py = lat => pad + (Y(lat) - minY) * scale;
  const svg = sv("svg", { class: "c map", viewBox: `0 0 ${width} ${H}`, width, height: H });
  const land = sv("g", {});
  for (const ring of outline.rings) land.appendChild(sv("path", { class: "state", "vector-effect": "non-scaling-stroke", d: ring.map((p, i) => `${i ? "L" : "M"}${px(p[0]).toFixed(1)},${py(p[1]).toFixed(1)}`).join("") + "Z" }));
  svg.appendChild(land);
  const maxEnr = Math.max(1, ...data.districts.map(d => d.enrollment || 0));
  const rmax = Math.max(7, width / 70);
  const rad = e => Math.max(1.6, Math.sqrt((e || 0) / maxEnr) * rmax);
  const g = sv("g", {});
  const dots = data.districts.filter(d => d.lat !== null).sort((a, b) => (b.enrollment || 0) - (a.enrollment || 0))
    .map(d => ({ d, x: px(d.lon), y: py(d.lat), r: rad(d.enrollment) }));
  for (const p of dots) {
    const d = p.d;
    const cls = !d.all_meetings ? "none" : !d.passes ? "hollow" : d.share === null ? "ink" : `q${o.bin(d.share)}`;
    p.el = sv("circle", { class: `md ${cls}`, r: p.r.toFixed(1) });
    p.el.addEventListener("pointerenter", ev => Tip.show(o.tip(d), { x: ev.clientX, y: ev.clientY }, ev.pointerType !== "mouse"));
    p.el.addEventListener("pointerleave", ev => { if (ev.pointerType === "mouse") Tip.hide(); });
    p.el.addEventListener("click", ev => { if (moved) return; if (ev.pointerType === "mouse" || ev.detail === 0) o.onSelect && o.onSelect(d); else Tip.show(o.tip(d, true), null, true); });
    g.appendChild(p.el);
  }
  svg.appendChild(g);
  const names = sv("g", { class: "names", "aria-hidden": "true" }), rings = sv("g", {}), labels = sv("g", {});
  svg.append(names, rings, labels);
  const events = (data.events || []).slice().sort((a, b) => a.lon - b.lon).map(e => ({ e, x: px(e.lon), y: py(e.lat) }));
  for (const ev of events) { ev.ring = sv("circle", { class: "evring", r: 9 }); rings.appendChild(ev.ring); }

  // View state: screen = base * k + t. Clamped so Texas always fills the frame.
  let k = 1, tx = 0, ty = 0, moved = false;
  const KMAX = 12;
  const clamp = () => { k = Math.min(KMAX, Math.max(1, k)); tx = Math.min(0, Math.max(width - width * k, tx)); ty = Math.min(0, Math.max(H - H * k, ty)); };
  const overlaps = (b, list) => list.some(o => b.x0 < o.x1 && b.x1 > o.x0 && b.y0 < o.y1 && b.y1 > o.y0);
  const render = () => {
    land.setAttribute("transform", `translate(${tx.toFixed(2)},${ty.toFixed(2)}) scale(${k.toFixed(4)})`);
    const grow = Math.pow(k, 0.3);  // dots grow a little so a zoomed view still reads as zoomed
    for (const p of dots) { p.sx = p.x * k + tx; p.sy = p.y * k + ty; p.el.setAttribute("cx", p.sx.toFixed(1)); p.el.setAttribute("cy", p.sy.toFixed(1)); p.el.setAttribute("r", (p.r * grow).toFixed(1)); }
    // Event rings and labels: tried right, left, below and above until they clear.
    const boxes = [];
    for (const ev of events) { ev.sx = ev.x * k + tx; ev.sy = ev.y * k + ty; ev.ring.setAttribute("cx", ev.sx); ev.ring.setAttribute("cy", ev.sy); boxes.push({ x0: ev.sx - 10, x1: ev.sx + 10, y0: ev.sy - 10, y1: ev.sy + 10 }); }
    const evLabels = [];
    for (const ev of events) {
      const { sx: ex, sy: ey } = ev;
      if (ex < 0 || ex > width || ey < 0 || ey > H) continue;
      const text = narrow(width) ? ev.e.short : `${ev.e.short} · ${dateLabel(ev.e.date)}`;
      const w = text.length * 6.6, hgt = 12;
      const tries = [[ex + 12, ey + 4, "start"], [ex - 12, ey + 4, "end"], [ex - w / 2, ey + 24, "start"], [ex - w / 2, ey - 16, "start"], [ex + 12, ey + 18, "start"], [ex + 12, ey - 10, "start"]];
      const box = ([x, y, anchor]) => { const x0 = anchor === "end" ? x - w : x; return { x0, x1: x0 + w, y0: y - hgt + 2, y1: y + 2 }; };
      const pick = tries.find(t => { const b = box(t); return b.x0 >= 0 && b.x1 <= width && !overlaps(b, boxes); });
      if (!pick) continue;
      boxes.push(box(pick));
      evLabels.push(sv("text", { class: "lab halo", x: pick[0], y: pick[1], "text-anchor": pick[2] }, text));
    }
    labels.replaceChildren(...evLabels);
    // District names from about 3x, largest districts first, skipping any that would collide.
    const nameEls = [];
    if (k >= 3) {
      const marks = dots.filter(p => p.sx > -20 && p.sx < width + 20 && p.sy > -20 && p.sy < H + 20)
        .map(p => { const r = p.r * grow + 1; return { x0: p.sx - r, x1: p.sx + r, y0: p.sy - r, y1: p.sy + r }; });
      for (const p of dots) {
        if (p.sx < 0 || p.sx > width || p.sy < 0 || p.sy > H) continue;
        const text = p.d.school_district, w = text.length * 5.6, r = p.r * grow;
        const b = { x0: p.sx + r + 3, x1: p.sx + r + 3 + w, y0: p.sy - 6, y1: p.sy + 6 };
        if (b.x1 > width || overlaps(b, boxes) || overlaps(b, marks)) continue;
        boxes.push(b);
        nameEls.push(sv("text", { class: "dname halo", x: b.x0, y: p.sy + 4 }, text));
        if (nameEls.length >= 160) break;
      }
    }
    names.replaceChildren(...nameEls);
  };
  let raf = 0;
  const schedule = () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; render(); }); };
  // Zoom by factor f about the screen point (cx, cy); buttons and double-click glide, gestures follow directly.
  let anim = 0;
  const zoomAt = (f, cx, cy, glide) => {
    const k0 = k, t0x = tx, t0y = ty;
    k = k0 * f; clamp();
    const real = k / k0;
    tx = cx - (cx - t0x) * real; ty = cy - (cy - t0y) * real; clamp();
    if (!glide || reducedMotion()) { schedule(); sync(); return; }
    const k1 = k, t1x = tx, t1y = ty, start = performance.now();
    cancelAnimationFrame(anim);
    const step = now => {
      const pr = Math.min(1, (now - start) / 180), e = easeOut(pr);
      k = k0 + (k1 - k0) * e; tx = t0x + (t1x - t0x) * e; ty = t0y + (t1y - t0y) * e;
      render();
      if (pr < 1) anim = requestAnimationFrame(step); else sync();
    };
    anim = requestAnimationFrame(step);
  };
  const toLocal = (clientX, clientY) => { const r = svg.getBoundingClientRect(); return [(clientX - r.left) * width / r.width, (clientY - r.top) * H / r.height]; };
  // Trackpad pinch arrives as ctrl+wheel; a plain wheel scrolls the page as usual.
  svg.addEventListener("wheel", ev => {
    if (!ev.ctrlKey && !ev.metaKey) { hint.classList.add("show"); clearTimeout(hint.t); hint.t = setTimeout(() => hint.classList.remove("show"), 1400); return; }
    ev.preventDefault();
    const [cx, cy] = toLocal(ev.clientX, ev.clientY);
    zoomAt(Math.exp(-ev.deltaY * 0.01), cx, cy, false);
  }, { passive: false });
  svg.addEventListener("dblclick", ev => { ev.preventDefault(); const [cx, cy] = toLocal(ev.clientX, ev.clientY); zoomAt(ev.shiftKey ? 0.5 : 2, cx, cy, true); });
  // Pointer drag pans with one mouse button; on touch, one finger scrolls the page and two fingers pan and pinch.
  const down = new Map();
  let last = null;
  svg.addEventListener("pointerdown", ev => {
    down.set(ev.pointerId, toLocal(ev.clientX, ev.clientY));
    moved = false;
    if (ev.pointerType === "mouse" ? k > 1 : down.size === 2) { last = centroid(); svg.setPointerCapture(ev.pointerId); svg.classList.add("grabbing"); }
  });
  const centroid = () => { const v = [...down.values()]; const c = v.reduce((a, p) => [a[0] + p[0] / v.length, a[1] + p[1] / v.length], [0, 0]); const d = v.length === 2 ? Math.hypot(v[0][0] - v[1][0], v[0][1] - v[1][1]) : 0; return { c, d }; };
  svg.addEventListener("pointermove", ev => {
    if (!down.has(ev.pointerId)) return;
    down.set(ev.pointerId, toLocal(ev.clientX, ev.clientY));
    if (!last || (ev.pointerType !== "mouse" && down.size < 2)) return;
    const now = centroid();
    if (Math.hypot(now.c[0] - last.c[0], now.c[1] - last.c[1]) > 3) moved = true;
    tx += now.c[0] - last.c[0]; ty += now.c[1] - last.c[1];
    if (last.d && now.d) { const f = now.d / last.d; const k0 = k; k *= f; clamp(); const real = k / k0; tx = now.c[0] - (now.c[0] - tx) * real; ty = now.c[1] - (now.c[1] - ty) * real; moved = true; }
    clamp(); last = now; schedule();
  });
  const up = ev => { down.delete(ev.pointerId); if (down.size < (ev.pointerType === "mouse" ? 1 : 2)) { last = null; svg.classList.remove("grabbing"); sync(); } else last = centroid(); };
  svg.addEventListener("pointerup", up);
  svg.addEventListener("pointercancel", up);

  const btn = (label, text, fn) => h("button", { type: "button", "aria-label": label, title: label, onclick: fn }, text);
  const zin = btn("Zoom in", "+", () => zoomAt(2, width / 2, H / 2, true));
  const zout = btn("Zoom out", "−", () => zoomAt(0.5, width / 2, H / 2, true));
  const reset = btn("Show all of Texas", "Reset", () => zoomAt(1 / k, width / 2, H / 2, true));
  const sync = () => { zout.disabled = reset.disabled = k <= 1.001; zin.disabled = k >= KMAX - 0.001; };
  const hint = h("p", { class: "map-hint", "aria-hidden": "true" }, "Ctrl + scroll or pinch to zoom");
  const wrap = h("div", { class: "map-wrap", tabindex: 0, "aria-label": "Map of Texas districts. Plus and minus keys zoom, arrow keys pan.",
    onkeydown: ev => {
      const pan = { ArrowLeft: [60, 0], ArrowRight: [-60, 0], ArrowUp: [0, 60], ArrowDown: [0, -60] }[ev.key];
      if (ev.target !== wrap) return;
      if (ev.key === "+" || ev.key === "=") zoomAt(2, width / 2, H / 2, true);
      else if (ev.key === "-") zoomAt(0.5, width / 2, H / 2, true);
      else if (ev.key === "0") zoomAt(1 / k, width / 2, H / 2, true);
      else if (pan && k > 1) { tx += pan[0]; ty += pan[1]; clamp(); schedule(); }
      else return;
      ev.preventDefault();
    } }, svg, h("div", { class: "map-zoom" }, zin, zout, reset), hint);
  render(); sync();
  return wrap;
}
