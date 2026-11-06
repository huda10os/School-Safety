"use strict";
// Single-page UI. Every request uses a relative URL ("api/...") so the app
// works at / and behind a path prefix. Record content is only ever inserted
// as text nodes, never as HTML.

const main = document.getElementById("main");
let routeToken = 0;
let refreshTimer = null;
let fieldsCache = null;
let facetsCache = null;

const STATE_LABELS = {
  not_mapped: "Not checked yet",
  no_platform: "No meeting platform found",
  waiting_discovery: "Waiting for discovery",
  discovery_error: "Discovery error",
  no_records: "No records on listing",
  partial_discovery: "More platforms to read",
  discovered: "Discovered, not collected",
  collecting: "Collecting",
  complete: "Complete",
};
const STATE_HELP = {
  not_mapped: "We have not yet looked for this district's meeting platform.",
  no_platform: "We looked and found no online meeting platform for this district.",
  waiting_discovery: "The platform is known; its meeting listing has not been read yet.",
  discovery_error: "Reading the meeting listing failed. See the error below.",
  no_records: "The meeting listing was read but contained no document links.",
  partial_discovery: "This district uses more than one platform and at least one has not been read yet.",
  discovered: "Document links are found; none have been downloaded yet.",
  collecting: "Some documents are downloaded; more are waiting.",
  complete: "Every discovered document has been downloaded.",
};
const FLAG_LABELS = {
  no_minutes: "No minutes found",
  late_start: "Minutes start after 2018",
  short_span: "Under 3 years of minutes",
  gap: "Gap over 6 months",
  multi_platform: "More than one platform",
};
const FLAG_HELP = {
  no_minutes: "Records were found, but none of them are minutes.",
  late_start: "The earliest minutes are dated after January 1, 2018.",
  short_span: "The first and last minutes are less than three years apart.",
  gap: "At least one stretch of more than six months has no meeting at all.",
  multi_platform: "Records come from more than one platform, so coverage may be split.",
};
const TEXT_LABELS = {
  pending: "Not collected yet",
  ok: "Text extracted",
  needs_ocr: "Needs OCR",
  partial_ocr: "Partly needs OCR",
  empty: "Empty",
  too_large: "Too large",
  unsupported: "Unsupported format",
  unavailable: "No longer published",
  recording: "Recording (link only)",
  error: "Download error",
  unknown: "Unknown",
};
const TEXT_HELP = {
  pending: "The link is known but the document has not been downloaded.",
  ok: "The document's text was read successfully.",
  needs_ocr: "A scanned image with no text layer. It needs OCR (optical character recognition) to be read.",
  partial_ocr: "Some pages have text and some are scanned images that need OCR.",
  empty: "The document downloaded but contained no text.",
  too_large: "Saved, but too big to read automatically. It will be handled later.",
  unsupported: "A file type we do not read, such as a video or spreadsheet.",
  unavailable: "The district's platform no longer serves this document, so there is nothing to download.",
  recording: "An audio or video recording. The link is kept for a later transcription step; it is not downloaded.",
  error: "The download or reading failed. It is retried on the next run.",
};
const DETAIL_HELP = [
  ["Agenda", "The list of items the board plans to discuss, posted before the meeting."],
  ["Minutes", "The official record of what the board decided, usually approved at a later meeting."],
  ["Projector", "BoardBook's on-screen view of the agenda, which often includes each item's full description."],
  ["Extras", "Other documents attached to a meeting, such as packets or supporting files."],
  ["Other", "A document that does not fit the types above."],
];

// ------------------------------------------------------------------ helpers
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "href") el.setAttribute("href", safeHref(value));
    else el.setAttribute(key, value === true ? "" : value);
  }
  append(el, kids);
  return el;
}
function append(el, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.appendChild(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
function safeHref(value) {
  const s = String(value);
  if (s.startsWith("#") || s.startsWith("api/")) return s;
  try {
    const url = new URL(s);
    if (url.protocol === "http:" || url.protocol === "https:") return url.href;
  } catch (e) { /* fall through */ }
  return "#";
}
function ext(href, label) {
  if (!href) return h("span", { class: "muted" }, "none");
  return h("a", { href, target: "_blank", rel: "noopener noreferrer" }, label || href);
}
const nf = new Intl.NumberFormat("en-US");
function num(n) { return n === null || n === undefined ? "–" : nf.format(n); }
function pct(a, b) { return b ? `${(100 * a / b).toFixed(1)}%` : "–"; }
function status(kind, label) { return h("span", { class: `status s-${kind}` }, label); }
function fmtDate(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString("en-US", { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
}
function ago(iso) {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return `${Math.round(s)} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${(s / 3600).toFixed(1)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
function duration(minutes) {
  if (minutes === null || minutes === undefined) return null;
  if (minutes < 1) return "under a minute";
  if (minutes < 90) return `about ${Math.round(minutes)} min`;
  const hours = minutes / 60;
  if (hours < 48) return `about ${Math.floor(hours)} h ${Math.round(minutes % 60)} min`;
  return `about ${Math.floor(hours / 24)} days ${Math.round(hours % 24)} h`;
}
function qs(obj) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(obj)) if (v !== "" && v !== null && v !== undefined) p.set(k, v);
  return p.toString();
}
async function api(path, params) {
  const query = params ? qs(params) : "";
  const res = await fetch(`api/${path}${query ? "?" + query : ""}`, { headers: { Accept: "application/json" } });
  const body = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}
function render(...nodes) {
  main.replaceChildren(...nodes.flat().filter(Boolean));
}
function failure(err) {
  render(h("h1", { text: "This page did not load" }), h("p", { class: "bad-text" }, String(err.message || err)),
    h("p", { class: "muted" }, "The server may be restarting. Reload the page in a moment."));
}
function field(label, control) {
  const id = control.id || `f-${Math.random().toString(36).slice(2, 8)}`;
  control.id = id;
  return h("div", { class: "field" }, h("label", { for: id }, label), control);
}
function select(name, options, value, allLabel) {
  const el = h("select", { name });
  el.appendChild(h("option", { value: "" }, allLabel || "All"));
  for (const opt of options) {
    const [v, label] = Array.isArray(opt) ? opt : [opt, opt];
    const o = h("option", { value: v }, label);
    if (String(v) === String(value || "")) o.selected = true;
    el.appendChild(o);
  }
  return el;
}
function highlight(text, q) {
  if (!text) return [];
  if (!q) return [text];
  const out = [];
  const lower = text.toLowerCase();
  const needle = q.toLowerCase();
  let i = 0;
  for (;;) {
    const j = lower.indexOf(needle, i);
    if (j < 0) break;
    out.push(text.slice(i, j), h("mark", {}, text.slice(j, j + q.length)));
    i = j + q.length;
  }
  out.push(text.slice(i));
  return out;
}
async function facets() {
  if (!facetsCache) facetsCache = await api("facets");
  return facetsCache;
}

// ------------------------------------------------------------------ router
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, query] = raw.split("?");
  return { parts: path.split("/").filter(Boolean).map(decodeURIComponent), params: new URLSearchParams(query || "") };
}
function route() {
  const token = ++routeToken;
  clearInterval(refreshTimer);
  refreshTimer = null;
  const { parts, params } = parseHash();
  const page = parts[0] || "story";
  const navFor = { district: "districts", record: "records", board: "explore", coverage: "explore", event: "events" };
  for (const a of document.querySelectorAll(".top nav a")) {
    const active = a.dataset.nav === page || a.dataset.nav === navFor[page];
    if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  // On narrow screens the nav scrolls sideways; keep the current page's link in view.
  const nav = document.querySelector(".top nav"), cur = nav?.querySelector('[aria-current="page"]');
  if (cur && nav.scrollWidth > nav.clientWidth) nav.scrollLeft = Math.max(0, cur.offsetLeft - nav.offsetLeft - (nav.clientWidth - cur.offsetWidth) / 2);
  const pages = { progress: pageProgress, districts: pageDistricts, district: pageDistrict, records: pageRecords, record: pageRecord,
    ...(window.RESEARCH_PAGES || {}) };
  const fn = pages[page] || pages.story || pageProgress;
  Promise.resolve(fn(token, parts.slice(1), params)).catch(err => token === routeToken && failure(err));
}
window.addEventListener("hashchange", () => { route(); main.focus({ preventScroll: true }); window.scrollTo(0, 0); });

// ------------------------------------------------------------------ progress
async function pageProgress(token) {
  const draw = async () => {
    const p = await api("progress");
    if (token !== routeToken) return;
    document.title = `${p.collected_pct}% collected · Texas board records`;
    render(progressView(p));
  };
  await draw();
  refreshTimer = setInterval(() => { if (!document.hidden) draw().catch(() => {}); }, 30000);
}

function progressView(p) {
  const stale = p.last_data_change && (Date.now() - new Date(p.last_data_change)) > 15 * 60 * 1000;
  const rate = p.rate_per_min;
  const eta = p.records_backlog === 0 ? "Nothing waiting"
    : rate ? duration(p.eta_minutes) : "Unknown";
  const statusRows = ["ok", "needs_ocr", "partial_ocr", "empty", "too_large", "unsupported", "unavailable", "recording", "error"]
    .concat(Object.keys(p.by_status).filter(k => !(k in TEXT_LABELS)));
  const unchecked = p.districts_total - p.districts_mapped;
  return [
    h("h1", { text: "Collection progress" }),
    h("p", { class: "lede" },
      `We are collecting board meeting agendas, minutes and projector views for the ${num(p.districts_total)} regular `,
      "Texas school districts. This page refreshes every 30 seconds."),
    h("div", { class: "hero" },
      h("span", { class: "hero-num" }, `${p.collected_pct}%`),
      h("span", {}, `of discovered records collected successfully: ${num(p.records_done)} of ${num(p.records_discovered)}` +
        (p.records_collected > p.records_done
          ? ` (${num(p.records_collected - p.records_done)} failed downloads are not counted; they are retried).`
          : "."))),
    h("dl", { class: "facts" },
      fact("Records discovered", num(p.records_discovered), "Document links found on district meeting pages"),
      fact("Records collected", num(p.records_collected), "Downloaded, including ones that failed"),
      fact("Still to collect", num(p.records_backlog), "Discovered but not yet collected, plus errors to retry"),
      fact("Collection rate", rate === null ? "–" : `${num(rate)} / min`, `From ${p.rate_basis}`),
      fact("Time to clear what is discovered", eta,
        p.records_backlog && !rate ? "No records were collected in the last 30 minutes" : "At the current rate"),
      fact("Last data change", ago(p.last_data_change),
        stale ? h("span", { class: "warn-text" }, "No new files in over 15 minutes. The run may be stalled or finished.")
          : fmtDate(p.last_data_change))),
    p.ocr ? ocrSection(p.ocr) : null,
    (p.mapping_incomplete || !p.platforms_file) ? h("p", { class: "note" },
      p.platforms_file
        ? `Discovery is still growing the backlog: ${num(p.districts_mapped)} of ${num(p.districts_total)} districts have been checked for a meeting platform (${num(unchecked)} to go), so the totals and the time estimate will keep rising.`
        : "The district-to-platform map is not ready yet, so most districts have not been searched. Totals and the estimate will rise as it fills in.") : null,
    h("h2", { text: "Collected records over time" }),
    h("p", { class: "muted" }, `${num(p.collected_last_30_min)} records collected in the last 30 minutes.`),
    chart(p.chart),
    h("div", { class: "cols" },
      h("div", {},
        h("h2", { text: "Districts by stage" }),
        h("table", {},
          h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Stage"), h("th", { class: "num", scope: "col" }, "Districts"), h("th", { class: "num", scope: "col" }, "Share"))),
          h("tbody", {},
            stageRow("Checked for a meeting platform", p.districts_mapped, p.districts_total),
            stageRow("Have a meeting platform", p.districts_with_platform, p.districts_total),
            stageRow("Meeting listing read", p.districts_discovered, p.districts_total),
            stageRow("At least one record collected", p.districts_collected, p.districts_total),
            stageRow("Platform listings that failed to read", p.discovery_errors, null))),
        h("h2", { text: "Districts by meeting platform" }),
        h("p", { class: "muted small" }, "Where each district posts its board documents. “none” means we looked and found no platform."),
        h("table", {},
          h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Platform"), h("th", { class: "num", scope: "col" }, "Districts"), h("th", { scope: "col", class: "hide-sm" }, h("span", { class: "skip" }, "Share bar")))),
          h("tbody", {}, Object.entries(p.platform_counts).map(([name, n]) => h("tr", {},
            h("td", {}, name), h("td", { class: "num" }, num(n)),
            h("td", { class: "hide-sm", style: null }, bar(n, p.districts_total))))))),
      h("div", {},
        h("h2", { text: "Collected records by text result" }),
        h("table", {},
          h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Result"), h("th", { class: "num", scope: "col" }, "Records"), h("th", { class: "num", scope: "col" }, "Share"))),
          h("tbody", {}, statusRows.map(k => h("tr", {},
            h("td", {}, status(k, TEXT_LABELS[k] || k), TEXT_HELP[k] ? h("div", { class: "muted small" }, TEXT_HELP[k]) : null),
            h("td", { class: "num" }, num(p.by_status[k] || 0)),
            h("td", { class: "num" }, pct(p.by_status[k] || 0, p.records_collected)))))))),
    h("h2", { text: "Terms used here" }),
    terms(),
    h("p", { class: "muted small", style: null },
      `Index last updated ${ago(p.index.last_pass_at)}`,
      p.index.running ? ` · indexing ${p.index.files_done} of ${p.index.files_total} changed files` : "",
      ` · page generated ${fmtDate(p.generated_at)}`),
  ];
}
function ocrSection(o) {
  const stale = o.phase === "running" && o.updated_at && (Date.now() - new Date(o.updated_at)) > 10 * 60 * 1000;
  const state = o.phase === "finished" ? "Finished" : stale ? "Stalled" : "Running";
  return h("section", { "aria-labelledby": "ocr-h" },
    h("h2", { id: "ocr-h", text: "Reading scanned documents (OCR)" }),
    h("p", { class: "muted" },
      "Scanned PDFs have no text layer, so their pages are read by an OCR model (",
      o.model ? o.model.split(";")[0] : "Indra OCR", `), on the computer's processor with a ${o.cpus}-core cap. `,
      "Pages that already have text keep it."),
    h("div", { class: "meter" },
      h("p", { class: "meter-count" }, h("b", {}, num(o.documents_done)), ` of ${num(o.documents_total)} scanned documents read`,
        h("span", {}, pct(o.documents_done, o.documents_total))),
      bar(o.documents_done, o.documents_total, "ocr")),
    h("dl", { class: "facts" },
      fact("Status", stale ? h("span", { class: "warn-text" }, state) : state,
        stale ? "No update in over 10 minutes" : `Updated ${ago(o.updated_at)}`),
      fact("Pages read this run", num(o.ocr_pages_done), "Only pages without a text layer"),
      fact("Speed", o.pages_per_second ? `${o.pages_per_second} pages / s` : "–", "Over this run so far"),
      fact("Time left", o.phase === "finished" ? "None" : o.eta_minutes != null ? duration(o.eta_minutes) : "Estimating",
        "For the scanned documents still waiting"),
      fact("Problems", num(o.errors), "Documents the OCR could not read; they keep their earlier status")));
}
function fact(label, value, note) {
  return h("div", {}, h("dt", {}, label), h("dd", {}, value, note ? h("span", { class: "muted small" }, note) : null));
}
function stageRow(label, n, total) {
  return h("tr", {}, h("td", {}, label), h("td", { class: "num" }, num(n)),
    h("td", { class: "num" }, total ? pct(n, total) : h("span", { class: "muted" }, "–")));
}
// A bar named by `key` slides from the value it showed on the last refresh.
const barShown = {};
function bar(n, total, key) {
  const share = total ? Math.max(0.01, n / total) : 0;
  const inner = h("span", {});
  inner.style.transform = `scaleX(${share})`;
  if (key) {
    const from = barShown[key];
    barShown[key] = share;
    if (from !== undefined && from !== share && !reducedMotion())
      inner.animate([{ transform: `scaleX(${from})` }, { transform: `scaleX(${share})` }], { duration: 400, easing: EASE_OUT });
  }
  return h("div", { class: "bar", role: "presentation" }, inner);
}
function terms() {
  return h("dl", { class: "terms" },
    DETAIL_HELP.map(([t, d]) => [h("dt", {}, t), h("dd", {}, d)]),
    h("dt", {}, "Discovered"), h("dd", {}, "We found a link to the document on the district's meeting page."),
    h("dt", {}, "Collected"), h("dd", {}, "We downloaded the document and tried to read its text."),
    h("dt", {}, "Needs OCR"), h("dd", {}, TEXT_HELP.needs_ocr));
}

function chart(points) {
  const W = 900, H = 200, L = 64, R = 12, T = 12, B = 28;
  const pts = (points || []).filter(p => p.t && p.collected !== null);
  if (pts.length < 2) {
    return h("p", { class: "muted" }, "Not enough history yet. The server records a sample every minute while it runs.");
  }
  const xs = pts.map(p => new Date(p.t).getTime());
  const x0 = xs[0], x1 = xs[xs.length - 1] || x0 + 1;
  const hasDisc = pts.some(p => p.discovered);
  const ymax = Math.max(1, ...pts.map(p => Math.max(p.collected, hasDisc ? p.discovered || 0 : 0)));
  const sx = t => L + (W - L - R) * (x1 === x0 ? 1 : (t - x0) / (x1 - x0));
  const sy = v => T + (H - T - B) * (1 - v / ymax);
  const NS = "http://www.w3.org/2000/svg";
  const s = (tag, attrs, text) => {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
    if (text !== undefined) el.textContent = text;
    return el;
  };
  const line = key => pts.map((p, i) => `${i ? "L" : "M"}${sx(xs[i]).toFixed(1)},${sy(p[key] || 0).toFixed(1)}`).join("");
  const last = pts[pts.length - 1];
  const svg = s("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `Collected records rose from ${num(pts[0].collected)} to ${num(last.collected)} between ${fmtDate(pts[0].t)} and ${fmtDate(last.t)}.` });
  for (const v of [0, ymax / 2, ymax]) {
    svg.appendChild(s("line", { class: "axis", x1: L, x2: W - R, y1: sy(v), y2: sy(v) }));
    svg.appendChild(s("text", { x: L - 8, y: sy(v) + 4, "text-anchor": "end" }, num(Math.round(v))));
  }
  svg.appendChild(s("path", { class: "area", d: `${line("collected")}L${sx(x1)},${sy(0)}L${sx(x0)},${sy(0)}Z` }));
  if (hasDisc) svg.appendChild(s("path", { class: "line2", d: line("discovered") }));
  svg.appendChild(s("path", { class: "line", d: line("collected") }));
  const span = x1 - x0;
  const tfmt = t => new Date(t).toLocaleString("en-US", span > 36 * 3600e3
    ? { month: "short", day: "numeric" } : { hour: "numeric", minute: "2-digit" });
  [[x0, "start"], [(x0 + x1) / 2, "middle"], [x1, "end"]].forEach(([t, anchor]) =>
    svg.appendChild(s("text", { x: sx(t), y: H - 8, "text-anchor": anchor }, tfmt(t))));
  return h("figure", { style: null, class: "fig" }, svg,
    h("figcaption", { class: "legend" },
      h("span", {}, h("i", {}), "Collected"),
      hasDisc ? h("span", {}, h("i", { class: "dash" }), "Discovered") : null));
}

// ------------------------------------------------------------------ districts
const DISTRICT_COLS = [
  { key: "school_district", label: "District" },
  { key: "platforms", label: "Platform", cls: "hide-sm" },
  { key: "state", label: "Status" },
  { key: "discovered", label: "Discovered", num: true, cls: "hide-sm" },
  { key: "collected", label: "Collected", num: true, cls: "hide-sm" },
  { key: "collected", label: "Records", num: true, cls: "show-sm", id: "records" },
  { key: "minutes_first", label: "Minutes, first to last", cls: "hide-sm" },
  { key: "max_gap_days", label: "Longest gap", num: true, cls: "hide-sm" },
  { key: "flags", label: "Flags", cls: "hide-sm" },
  { key: "errors", label: "Errors", num: true, cls: "hide-sm" },
];

async function pageDistricts(token, _parts, params) {
  const [data, f] = await Promise.all([api("districts"), facets()]);
  if (token !== routeToken) return;
  document.title = "Districts · Texas board records";
  const items = data.items;
  const state = {
    q: params.get("q") || "", county: params.get("county") || "", platform: params.get("platform") || "",
    state: params.get("state") || "", flag: params.get("flag") || "",
    sort: params.get("sort") || "school_district", dir: params.get("dir") || "asc",
  };
  const platforms = [...new Set(items.flatMap(d => d.platforms))].sort();
  const tbody = h("tbody");
  const count = h("p", { class: "muted", "aria-live": "polite" });
  const headRow = h("tr");
  const sync = () => {
    history.replaceState(null, "", "#/districts?" + qs(state));
    const q = state.q.toLowerCase();
    let rows = items.filter(d =>
      (!q || d.school_district.toLowerCase().includes(q) || d.tea_id.includes(q)) &&
      (!state.county || d.county_name === state.county) &&
      (!state.platform || d.platforms.includes(state.platform)) &&
      (!state.state || d.state === state.state) &&
      (!state.flag || (state.flag === "any" ? d.flags.length : d.flags.includes(state.flag))));
    const key = state.sort, dir = state.dir === "desc" ? -1 : 1;
    const val = d => key === "platforms" || key === "flags" ? d[key].join(",") : d[key];
    rows.sort((a, b) => {
      const va = val(a), vb = val(b);
      if (va === vb) return a.school_district.localeCompare(b.school_district);
      if (va === null || va === undefined || va === "") return 1;
      if (vb === null || vb === undefined || vb === "") return -1;
      return (typeof va === "number" ? va - vb : String(va).localeCompare(String(vb))) * dir;
    });
    count.textContent = `Showing ${num(rows.length)} of ${num(items.length)} districts.`;
    tbody.replaceChildren(...rows.map(districtRow));
    headRow.replaceChildren(...DISTRICT_COLS.map(c => {
      const sorted = state.sort === c.key && c.id !== "records";
      const th = h("th", { scope: "col", class: [c.num ? "num" : "", c.cls || ""].join(" "),
        "aria-sort": sorted ? (state.dir === "asc" ? "ascending" : "descending") : null });
      th.appendChild(h("button", { class: "sort", type: "button", onclick: () => {
        state.dir = state.sort === c.key && state.dir === "asc" ? "desc" : (c.num ? "desc" : "asc");
        state.sort = c.key;
        sync();
      } }, c.label, sorted ? (state.dir === "asc" ? " ↑" : " ↓") : ""));
      return th;
    }));
  };
  const input = (key) => (e) => { state[key] = e.target.value; sync(); };
  const search = h("input", { type: "search", placeholder: "District name or TEA id", value: state.q, oninput: input("q") });
  const form = h("form", { class: "filters", role: "search", onsubmit: e => e.preventDefault() },
    h("div", { class: "field grow" }, h("label", { for: "d-q" }, "Search"), Object.assign(search, { id: "d-q" })),
    field("County", select("county", f.counties, state.county, "All counties")),
    field("Platform", select("platform", platforms, state.platform, "All platforms")),
    field("Status", select("state", Object.entries(STATE_LABELS), state.state, "Any status")),
    field("Flag", select("flag", [["any", "Any flag"], ...Object.entries(FLAG_LABELS)], state.flag, "No filter")));
  for (const el of form.querySelectorAll("select")) el.addEventListener("change", input(el.name));
  sync();
  render(
    h("h1", { text: "Districts" }),
    h("p", { class: "lede" }, "Every regular Texas school district, where it posts board documents, and how much we have collected. Select a column heading to sort."),
    form,
    h("details", {}, h("summary", {}, "What do the statuses and flags mean?"),
      h("dl", { class: "terms" },
        Object.entries(STATE_LABELS).map(([k, v]) => [h("dt", {}, status(k, v)), h("dd", {}, STATE_HELP[k])]),
        Object.entries(FLAG_LABELS).map(([k, v]) => [h("dt", {}, v), h("dd", {}, FLAG_HELP[k])]))),
    count,
    h("div", { class: "table-wrap" }, h("table", {}, h("caption", { class: "skip" }, "Districts"), h("thead", {}, headRow), tbody)),
  );
}

function districtRow(d) {
  return h("tr", {},
    h("td", {}, h("a", { href: `#/district/${d.tea_id}` }, d.school_district),
      h("div", { class: "muted small" }, `${d.county_name} · ${d.tea_id}`)),
    h("td", { class: "hide-sm" }, d.platforms.join(", ") || h("span", { class: "muted" }, "–")),
    h("td", {}, status(d.state, STATE_LABELS[d.state] || d.state),
      d.flags.length ? h("div", { class: "show-sm" }, d.flags.map(f => h("span", { class: "flag" }, FLAG_LABELS[f] || f))) : null),
    h("td", { class: "num hide-sm" }, num(d.discovered)),
    h("td", { class: "num hide-sm" }, num(d.collected)),
    h("td", { class: "num show-sm" }, num(d.collected), h("div", { class: "muted small" }, `of ${num(d.discovered)}`)),
    h("td", { class: "hide-sm nowrap" }, d.minutes_first ? `${d.minutes_first} to ${d.minutes_last}` : h("span", { class: "muted" }, "–")),
    h("td", { class: "num hide-sm" }, d.max_gap_days === null ? "–" : `${num(d.max_gap_days)} days`),
    h("td", { class: "hide-sm" }, d.flags.map(f => h("span", { class: "flag", title: FLAG_HELP[f] }, FLAG_LABELS[f] || f))),
    h("td", { class: "num hide-sm" }, d.errors || d.discovery_errors.length
      ? h("span", { class: "bad-text", title: d.discovery_errors.join("\n") || null }, num(d.errors + d.discovery_errors.length)) : "0"),
  );
}

// ------------------------------------------------------------------ district detail
async function pageDistrict(token, parts, params) {
  const teaId = parts[0];
  const d = await api("district", { tea_id: teaId });
  if (token !== routeToken) return;
  document.title = `${d.school_district} · Texas board records`;
  const details = ["Agenda", "Minutes", "Projector", "Extras", "Other"];
  const years = Object.keys(d.timeline).sort();
  const maxYear = Math.max(1, ...years.map(y => Object.values(d.timeline[y]).reduce((a, b) => a + b, 0)));
  const recordsBox = h("div");
  render(
    h("p", { class: "crumbs" }, h("a", { href: "#/districts" }, "Districts"), " / ", d.school_district),
    h("h1", { text: d.school_district }),
    h("p", { class: "muted" }, `${d.county_name} · TEA ${d.tea_id} · `, d.website ? ext(d.website, "district website") : "no website listed",
      " · ", h("a", { href: `#/board/${d.tea_id}` }, "Meetings and measures over time")),
    h("p", {}, status(d.state, STATE_LABELS[d.state]), " ", h("span", { class: "muted" }, STATE_HELP[d.state] || "")),
    d.flags.length ? h("ul", {}, d.flags.map(f => h("li", {}, h("strong", {}, FLAG_LABELS[f] || f), `: ${FLAG_HELP[f] || ""}`))) : null,
    h("dl", { class: "facts" },
      fact("Records discovered", num(d.discovered)),
      fact("Records collected", num(d.collected), pct(d.done, d.discovered) + " of discovered"),
      fact("Minutes", num(d.minutes), d.minutes_first ? `${d.minutes_first} to ${d.minutes_last}` : "none found"),
      fact("Longest gap between meetings", d.max_gap_days === null ? "–" : `${num(d.max_gap_days)} days`, d.gap_end ? `ending ${d.gap_end}` : null),
      fact("Need OCR", num(d.needs_ocr), "Scanned documents without a text layer"),
      fact("Errors", num(d.errors + d.discovery_errors.length))),
    h("h2", { text: "Platforms" }),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Platform", "Meeting page", "Listing read", "Discovered", "Collected", "Text extracted", "Errors", "Dates"].map((l, i) =>
        h("th", { scope: "col", class: [i >= 3 && i <= 6 ? "num" : "", i === 1 || i === 2 || i === 7 ? "hide-sm" : ""].join(" ") }, l)))),
      h("tbody", {}, d.platform_rows.map(p => [
        h("tr", {},
          h("td", {}, p.platform),
          h("td", { class: "hide-sm" }, p.entry_url ? ext(p.entry_url, "open") : h("span", { class: "muted" }, "–")),
          h("td", { class: "hide-sm nowrap" }, p.discovered_at ? fmtDate(p.discovered_at) : h("span", { class: "muted" }, "not yet")),
          h("td", { class: "num" }, num(p.discovered)), h("td", { class: "num" }, num(p.collected)),
          h("td", { class: "num" }, num(p.ok)), h("td", { class: "num" }, num(p.errors)),
          h("td", { class: "hide-sm nowrap" }, p.first_date ? `${p.first_date} to ${p.last_date}` : "–")),
        (p.discovery_error || p.note || p.evidence) ? h("tr", {}, h("td", { colspan: 8, class: "small" },
          p.discovery_error ? h("div", { class: "bad-text" }, `Listing error: ${p.discovery_error}`) : null,
          p.note ? h("div", { class: "muted" }, `Note: ${p.note}`) : null,
          p.evidence ? h("div", { class: "muted" }, "Evidence: ", /^https?:/.test(p.evidence) ? ext(p.evidence) : p.evidence) : null)) : null,
      ])))),
    h("h2", { text: "Meetings by year" }),
    h("p", { class: "muted small" }, "Number of discovered documents per year, by type."),
    years.length ? h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", { scope: "col" }, "Year"), details.map(t => h("th", { scope: "col", class: "num" }, t)),
        h("th", { scope: "col", class: "num" }, "Total"), h("th", { scope: "col", class: "hide-sm" }, h("span", { class: "skip" }, "Bar")))),
      h("tbody", {}, years.map(y => {
        const row = d.timeline[y];
        const total = Object.values(row).reduce((a, b) => a + b, 0);
        return h("tr", {}, h("th", { scope: "row" }, y), details.map(t => h("td", { class: "num" }, row[t] ? num(row[t]) : h("span", { class: "muted" }, "·"))),
          h("td", { class: "num" }, num(total)), h("td", { class: "hide-sm", style: null }, bar(total, maxYear)));
      })))) : h("p", { class: "muted" }, "No records discovered yet."),
    d.coverage && d.coverage.length ? [h("h2", { text: "Coverage review" }), genericTable(d.coverage)] : null,
    h("h2", { text: "Records" }),
    h("p", {}, h("a", { href: `#/records?${qs({ tea_id: d.tea_id })}` }, "Filter or search this district's records")),
    recordsBox,
  );
  await recordList(recordsBox, { tea_id: d.tea_id }, parseInt(params.get("offset") || "0", 10), token,
    offset => `#/district/${d.tea_id}?offset=${offset}`);
}

function genericTable(rows) {
  const cols = Object.keys(rows[0]);
  const show = v => v === null || v === undefined || v === "" ? "–" : typeof v === "object" ? JSON.stringify(v) : String(v);
  if (rows.length === 1) {
    return h("dl", { class: "meta" }, cols.map(c => [h("dt", {}, h("span", { class: "mono" }, c)), h("dd", {}, show(rows[0][c]))]));
  }
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, cols.map(c => h("th", { scope: "col" }, c)))),
    h("tbody", {}, rows.map(r => h("tr", {}, cols.map(c => h("td", {}, r[c] === null ? "–" : typeof r[c] === "object" ? JSON.stringify(r[c]) : String(r[c]))))))));
}

// ------------------------------------------------------------------ records
const RECORD_FILTERS = ["tea_id", "county", "platform", "detail", "meeting_type", "text_status", "date_from", "date_to", "q", "sort"];

async function pageRecords(token, _parts, params) {
  const f = await facets();
  if (token !== routeToken) return;
  document.title = "Records · Texas board records";
  const filters = {};
  for (const k of RECORD_FILTERS) if (params.get(k)) filters[k] = params.get(k);
  const offset = parseInt(params.get("offset") || "0", 10);
  const go = (extra) => { location.hash = "#/records?" + qs({ ...filters, ...extra }); };
  const form = h("form", { class: "filters", role: "search", onsubmit: e => {
    e.preventDefault();
    const data = new FormData(form);
    for (const k of RECORD_FILTERS) { const v = (data.get(k) || "").trim(); if (v) filters[k] = v; else delete filters[k]; }
    go({});
  } },
    h("div", { class: "field grow" }, h("label", { for: "r-q" }, "Search document text"),
      h("input", { id: "r-q", name: "q", type: "search", value: filters.q || "", placeholder: "e.g. school resource officer" })),
    field("District", select("tea_id", f.districts.map(d => [d.tea_id, d.name]), filters.tea_id, "All districts")),
    field("County", select("county", f.counties, filters.county, "All counties")),
    field("Document type", select("detail", f.details, filters.detail, "All types")),
    field("Meeting type", select("meeting_type", f.meeting_types, filters.meeting_type, "All meetings")),
    field("Text result", select("text_status", f.text_statuses.map(s => [s, TEXT_LABELS[s] || s]), filters.text_status, "Any result")),
    field("Platform", select("platform", f.platforms, filters.platform, "All platforms")),
    field("From", h("input", { type: "date", name: "date_from", value: filters.date_from || "" })),
    field("To", h("input", { type: "date", name: "date_to", value: filters.date_to || "" })),
    field("Order", select("sort", [["asc", "Oldest first"]], filters.sort, "Newest first")),
    h("div", { class: "field" }, h("button", { class: "primary", type: "submit" }, "Search")),
    Object.keys(filters).length ? h("div", { class: "field" }, h("a", { class: "button", href: "#/records" }, "Clear")) : null);
  for (const el of form.querySelectorAll("select, input[type=date]")) el.addEventListener("change", () => form.requestSubmit());
  const box = h("div");
  render(
    h("h1", { text: "Records" }),
    h("p", { class: "lede" }, "One row per document: an agenda, minutes, projector view or extra file for one meeting. ",
      "Text search looks inside collected documents and may take a few seconds."),
    form,
    h("details", {}, h("summary", {}, "What do these terms mean?"), terms()),
    box,
  );
  await recordList(box, filters, offset, token, off => "#/records?" + qs({ ...filters, offset: off || "" }), true);
}

async function recordList(box, filters, offset, token, pageHref, withExport) {
  box.replaceChildren(h("p", { class: "muted", "aria-live": "polite" }, filters.q ? "Searching text…" : "Loading…"));
  const res = await api("records", { ...filters, offset, limit: 50 });
  if (token !== routeToken) return;
  const first = res.items.length ? offset + 1 : 0;
  const last = offset + res.items.length;
  const summary = res.total === null
    ? `Showing ${num(first)}–${num(last)} matching documents${res.has_more ? " (more on the next page)" : ""}. Text matches are not counted, to keep search fast.`
    : `Showing ${num(first)}–${num(last)} of ${num(res.total)} documents.`;
  const pager = h("div", { class: "pager" },
    offset > 0 ? h("a", { class: "button", href: pageHref(Math.max(0, offset - res.limit)) }, "Previous") : h("button", { disabled: true, type: "button" }, "Previous"),
    res.has_more ? h("a", { class: "button", href: pageHref(offset + res.limit) }, "Next") : h("button", { disabled: true, type: "button" }, "Next"));
  const exportLink = withExport ? h("a", { class: "button", href: `api/export.csv?${qs(filters)}`, download: "board-records.csv" }, "Download CSV (metadata only)") : null;
  box.replaceChildren(
    h("div", { class: "toolbar" }, h("p", { class: "muted", "aria-live": "polite" }, summary), h("div", { class: "pager" }, exportLink, pager)),
    res.items.length ? h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {},
        h("th", { scope: "col" }, "Date"), h("th", { scope: "col" }, "Document"), h("th", { scope: "col", class: "hide-sm" }, "District"),
        h("th", { scope: "col", class: "hide-sm" }, "Meeting type"), h("th", { scope: "col" }, "Text"), h("th", { scope: "col", class: "num hide-sm" }, "Characters"))),
      h("tbody", {}, res.items.map(r => h("tr", {},
        h("td", { class: "nowrap" }, r.date || h("span", { class: "muted" }, "no date")),
        h("td", {}, h("a", { href: `#/record/${r.record_id}` }, r.detail || "Document"),
          r.meeting_title ? h("div", { class: "small" }, r.meeting_title) : null,
          h("div", { class: "muted small show-sm" }, r.school_district),
          r.snippet ? h("div", { class: "snippet" }, "… ", highlight(r.snippet, filters.q), " …") : null),
        h("td", { class: "hide-sm" }, h("a", { href: `#/district/${r.tea_id}` }, r.school_district), h("div", { class: "muted small" }, r.platform)),
        h("td", { class: "hide-sm" }, r.meeting_type || h("span", { class: "muted" }, "–")),
        h("td", {}, status(r.collected ? r.text_status : "pending", TEXT_LABELS[r.collected ? r.text_status : "pending"] || r.text_status)),
        h("td", { class: "num hide-sm" }, r.collected ? num(r.char_count) : "–")))))) : h("p", {}, "No documents match these filters."),
    res.items.length > 20 ? h("div", { class: "toolbar" }, h("span"), pager.cloneNode(true)) : null,
  );
}

// ------------------------------------------------------------------ record
async function pageRecord(token, parts) {
  const [r, fields] = await Promise.all([api("record", { id: parts[0] }), fieldsCache || api("fields")]);
  fieldsCache = fields;
  if (token !== routeToken) return;
  const kind = r.collected ? r.text_status : "pending";
  document.title = `${r.detail} ${r.date || ""} · ${r.school_district}`;
  const order = Object.keys(fields).filter(k => k !== "text");
  render(
    h("p", { class: "crumbs" }, h("a", { href: "#/records" }, "Records"), " / ",
      h("a", { href: `#/district/${r.tea_id}` }, r.school_district)),
    h("h1", {}, `${r.detail || "Document"} · ${r.date || "no date"}`),
    h("p", {}, r.meeting_title || "", r.meeting_type ? h("span", { class: "muted" }, ` · ${r.meeting_type} meeting`) : null),
    h("p", {}, status(kind, TEXT_LABELS[kind] || kind), " ", h("span", { class: "muted" }, TEXT_HELP[kind] || "")),
    h("div", { class: "actions" },
      r.source_link ? h("a", { class: "button primary", href: r.source_link, target: "_blank", rel: "noopener noreferrer" }, "Open the original ↗") : null,
      r.document_link && r.document_link !== r.source_link ? h("a", { class: "button", href: r.document_link, target: "_blank", rel: "noopener noreferrer" }, "Open the parsed file ↗") : null,
      r.text_length ? h("a", { class: "button", href: `api/record_text?${qs({ id: r.record_id })}`, target: "_blank", rel: "noopener" }, "Plain text") : null),
    h("h2", { text: "Listing row" }),
    h("p", { class: "muted small" }, "The exact text of the row on the district's meeting page where this link was found."),
    r.listing_row_text ? h("pre", { class: "rowtext" }, r.listing_row_text) : h("p", { class: "muted" }, "Not recorded."),
    h("h2", { text: "Extracted text" }),
    r.text ? [
      h("p", { class: "muted small" }, `${num(r.text_length)} characters${r.page_count ? ` from ${num(r.page_count)} pages` : ""}.`,
        r.text_truncated ? [" Showing the first 200,000. ", h("a", { href: `api/record_text?${qs({ id: r.record_id })}`, target: "_blank", rel: "noopener" }, "Open the full text.")] : ""),
      h("pre", { class: "text", tabindex: 0, "aria-label": "Extracted text" }, r.text),
    ] : h("p", { class: "muted" }, r.error ? `No text. Error: ${r.error}` : "No text for this record."),
    h("h2", { text: "All fields" }),
    h("dl", { class: "meta" }, order.filter(k => k in r).map(k => [
      h("dt", {}, h("span", { class: "mono" }, k), fields[k] ? h("div", { class: "small" }, fields[k].description) : null),
      h("dd", {}, fieldValue(k, r[k])),
    ])),
  );
}
function fieldValue(key, value) {
  if (value === null || value === undefined || value === "") return h("span", { class: "muted" }, "–");
  if (Array.isArray(value)) return value.length ? h("ul", {}, value.map(v => h("li", {}, /^https?:/.test(v) ? ext(v) : String(v)))) : h("span", { class: "muted" }, "none");
  const s = String(value);
  if (/^https?:\/\//.test(s)) return ext(s);
  if (key === "listing_row_text") return h("pre", { class: "rowtext" }, s);
  return s;
}

// Deferred scripts all run before DOMContentLoaded, so research.js has registered its pages by then.
document.addEventListener("DOMContentLoaded", route);
