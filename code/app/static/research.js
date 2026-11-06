"use strict";
// Research views: the landing story, Explore, coverage, events, district,
// map, search and method. Uses h(), api(), render(), num(), qs() and
// routeToken from app.js and the chart functions in charts.js. Every number
// in the prose is computed from the same API rows the charts draw.

const SHORT = { uvalde: "Uvalde", santa_fe: "Santa Fe", sutherland_springs: "Sutherland Springs", el_paso_walmart: "El Paso",
  odessa_midland: "Midland–Odessa", allen_mall: "Allen", parkland: "Parkland", sandy_hook: "Sandy Hook", oxford: "Oxford",
  nashville_covenant: "Nashville", apalachee: "Apalachee" };
const SUB_SHORT = { armed_staff: "Armed staff", police_sro: "Police and SROs", hardening: "Building security", threat_assessment: "Threat assessment",
  drills_emergency: "Drills and plans", mental_health: "Mental health" };
const MEASURE_OPTS = [["safety", "Any safety subtopic"], ["temperament", "Temperament (how students or staff feel or behave)"],
  ["event_reference", "Reference to a named shooting"], ["closed_session", "Closed security session cited (Gov't Code 551.076)"]];
const STANDING = "A mention in a meeting record is not proof a policy was adopted or carried out.";
const HEDGE = "Observational. Districts near an event may differ from the rest of Texas, and other things changed at the same time.";
let metaCache = null, metaAt = 0, outlineCache = null, districtNames = null;

async function meta() {
  if (!metaCache || Date.now() - metaAt > 60000) { metaCache = await api("meta"); metaAt = Date.now(); }
  const banner = document.getElementById("banner");
  banner.replaceChildren(metaCache.synthetic ? h("div", { class: "synthetic" },
    "Synthetic test data. Every number on these pages is made up for testing the app and is not a finding.") : "");
  return metaCache;
}
function evShort(e) { return SHORT[e.key] || e.label.split(",")[0]; }
function evRules(events, opts = {}) {
  return events.map(e => ({ x: opts.rel ? 0 : ms(e.date), label: `${evShort(e)} · ${dateLabel(e.date)}`, short: evShort(e), cls: opts.cls }));
}
function sel(name, options, value, attrs = {}) {
  const el = h("select", { name, ...attrs });
  for (const [v, label] of options) {
    const o = h("option", { value: v }, label);
    if (String(v) === String(value ?? "")) o.selected = true;
    el.appendChild(o);
  }
  return el;
}
function radio(name, value, current, label, onchange) {
  const id = `${name}-${value}`.replace(/[^a-z0-9-]/gi, "");
  const input = h("input", { type: "radio", name, value, id, onchange });
  if (String(value) === String(current)) input.checked = true;
  return h("label", { class: "opt", for: id }, input, label);
}
function pending(what) {
  const m = metaCache || {};
  const sc = m.screening;
  if (m.measures_status === "partial" && sc) {
    return h("div", { class: "pending" }, h("p", { class: "over" }, "Labelling in progress"),
      h("p", {}, `${what} The measures file exists, but only ${num(sc.screened)} of ${num(sc.readable)} readable meetings (${PCT(sc.share)}) are screened so far. `,
        "The meetings still waiting are the ones with candidate passages, so a share computed now would be far too low. ",
        `Shares appear here by themselves once at least ${PCT(sc.required)} are screened; until then this view shows only how many meetings we can read.`));
  }
  return h("div", { class: "pending" }, h("p", { class: "over" }, "Measures not computed yet"),
    h("p", {}, `${what} The safety, temperament and event measures are being computed from the meeting text. `,
      "Until data/measures/meetings.parquet exists, this view shows only how many meetings we can read. ",
      "It will fill in by itself once the file appears; no numbers here are guesses."));
}
function chipEl(chip, extra) {
  const unread = chip.minutes_unread_share;
  return h("a", { class: "chip", href: "#/coverage" },
    h("span", {}, `Showing ${num(chip.meetings)} readable meetings from ${num(chip.districts)} districts`),
    unread !== null && unread !== undefined ? h("span", { class: "k" }, `${PCT(unread)} of minutes not yet read`) : null,
    extra ? h("span", { class: "k" }, extra) : null);
}
function setHash(path, params) {
  const q = qs(params);
  history.replaceState(null, "", `#/${path}${q ? "?" + q : ""}`);
}
function shareText(v) { return v === null || v === undefined ? "–" : `${Math.round(v * 100)}%`; }
function byYear(rows) {
  const out = {};
  for (const r of rows) {
    const y = r.period.slice(0, 4);
    out[y] = out[y] || { year: +y, meetings: 0, yes: 0 };
    out[y].meetings += r.meetings; out[y].yes += r.yes;
  }
  return Object.values(out).map(r => ({ ...r, share: r.meetings ? r.yes / r.meetings : null }));
}
function trendFinding(rows, label, measures) {
  const years = byYear(rows).filter(r => r.meetings >= 30);
  if (!measures || years.length < 2) return null;
  const a = years[0], b = years[years.length - 1];
  const now = new Date().getUTCFullYear();
  const diff = b.share - a.share;
  const dir = Math.abs(diff) < 0.02 ? "about the same as" : diff > 0 ? "up from" : "down from";
  return [`${label[0].toUpperCase()}${label.slice(1)} came up in at least `, h("span", { class: "accent" }, `${shareText(b.share)} of readable meetings`),
    ` in ${b.year}${b.year === now ? " so far" : ""}, ${dir} ${shareText(a.share)} in ${a.year}`];
}
function asOf(m) {
  return h("p", { class: "cap" }, `Data as of ${m.data_as_of || "–"}. `,
    m.measures ? `Measures computed ${m.measures_as_of} with definitions version ${m.definition_versions.join(", ") || m.definitions_version}. `
      : m.measures_status === "partial" ? `Measures labelling in progress (${PCT(m.screening.share)} of readable meetings screened). ` : "Measures not computed yet. ",
    "Numbers will change as scanned documents are read by OCR. ", h("a", { href: "#/method" }, "How we built this"), ".");
}

// ------------------------------------------------------------------ landing story
async function pageStory(token) {
  const [m, s] = await Promise.all([meta(), api("story")]);
  if (token !== routeToken) return;
  document.title = "Texas school boards after school shootings";
  const unread = s.chip.minutes_unread_share;
  const counts = { readable: 0, scan: 0, unavailable: 0, none: 0 };
  s.dots.forEach(d => counts[d.state]++);
  const firsts = s.dots.filter(d => d.first_year_raw).map(d => d.first_year_raw).sort((a, b) => a - b);
  const median = firsts[Math.floor(firsts.length / 2)];
  const after2018 = firsts.filter(y => y > 2018).length;
  const yt = s.year_trend.filter(r => r.share !== null && !r.thin);
  const pick = y => yt.find(r => r.period.startsWith(String(y)));
  const lastFull = yt.filter(r => +r.period.slice(0, 4) < new Date().getUTCFullYear()).slice(-1)[0] || yt.slice(-1)[0];
  const early = pick(2015) || yt[0];
  const santa = s.events.find(e => e.key === "santa_fe"), uv = s.events.find(e => e.key === "uvalde");
  const subs = s.subtopics, sub = key => {
    const pts = (subs?.series[key] || []).filter(p => p.share !== null && !p.thin);
    return pts.length ? [pts[0], pts[pts.length - 1]] : null;
  };
  const armed = sub("armed_staff"), mh = sub("mental_health");
  const tone = s.tone;
  const quotes = tone?.quotes || [];
  const pre = s.pretrend;
  const steps = [
    { scene: "dots", body: [`Texas school boards have published at least ${num(s.documents)} meeting documents since ${s.first_year}. We collected them from ${num(s.districts_with_records)} of the state's ${num(s.districts_total)} districts.`],
      count: "Each dot is one district" },
    { scene: "dots", body: ["But we can't read all of them."], key: [
      ["readable", `Text we can read: ${num(counts.readable)} districts`], ["scan", `Mostly scans not yet read by OCR: ${num(counts.scan)}`],
      ["unavailable", `Listed, but the text is unavailable: ${num(counts.unavailable)}`], ["none", `No record found online: ${num(counts.none)}`]],
      cap: `${PCT(unread)} of minutes are scanned PDFs waiting for OCR. A district is drawn by the state most of its documents are in.` },
    { scene: "dots", body: [`Most districts only post recent years. Half of the districts' records start in ${median} or later, and ${num(after2018)} start after 2018.`],
      count: `Dots line up by first year on record; earlier years are bunched at ${s.first_year}` },
    { scene: "line", body: m.measures && early && lastFull
      ? [`In the meetings we can read, safety came up in `, h("span", { class: "accent" }, `${shareText(early.share)} of ${early.period.slice(0, 4)} meetings`), ` and ${shareText(lastFull.share)} of ${lastFull.period.slice(0, 4)} meetings.`]
      : [`Safety measures are not computed yet. What we can show is how many meetings each month we can read, which is the denominator every share will use.`],
      cap: "Bars under the line: readable meetings per month. The line is dashed where a month has fewer than 30." },
    { scene: "line", body: [`Two Texas school shootings are marked: Santa Fe High School on ${santa ? dateLabel(santa.date) : "18 May 2018"}, and Robb Elementary in Uvalde on ${uv ? dateLabel(uv.date) : "24 May 2022"}. The year after each is shaded.`] },
    { scene: "line", body: pre && pre.after_actual !== null
      ? [`Before Uvalde the share was ${pre.slope_per_year > 0.005 ? "already rising" : pre.slope_per_year < -0.005 ? "falling" : "roughly flat"}, by about ${Math.abs(Math.round(pre.slope_per_year * 100))} point${Math.abs(Math.round(pre.slope_per_year * 100)) === 1 ? "" : "s"} a year. In the 12 months after, it averaged `,
        h("span", { class: "accent" }, shareText(pre.after_actual)), `, against ${shareText(pre.after_projected)} if the earlier trend had simply continued (dotted line).`]
      : ["When the measures are ready, this step compares the year after Uvalde with the trend in the two years before it, so a rise that started earlier is not credited to the shooting."],
      cap: HEDGE },
    { scene: "multiples", body: armed ? [`What they talked about changed. Armed staff and the Guardian program came up in `, h("span", { class: "accent" }, `${shareText(armed[1].share)} of meetings in ${armed[1].year}`), `, against ${shareText(armed[0].share)} in ${armed[0].year}.`]
      : ["What they talked about: six safety subtopics, each drawn on the same scale. They appear here once the measures are computed."] },
    { scene: "multiples", body: mh ? [`Mental health and counseling, when tied to safety, came up in `, h("span", { class: "accent" }, `${shareText(mh[1].share)} of meetings in ${mh[1].year}`), `, against ${shareText(mh[0].share)} in ${mh[0].year}.`]
      : ["Mental health and counseling count only when a passage ties them to safety, threats or crisis."] },
    { scene: "tone", body: tone && tone.rows.length ? ["How board members described students and staff. Bars to the left are negative descriptions (fear, stress, misbehavior); to the right, positive ones."]
      : ["How board members described students and staff: negative, positive or mixed. This appears once the measures are computed."],
      quotes },
  ];
  const sticky = h("div", { class: "chart-box" });
  const graphic = h("div", { class: "graphic", "aria-hidden": "false" }, sticky);
  const stepEls = steps.map((st, i) => h("div", { class: "step", "data-step": i + 1 },
    h("div", { class: "card" }, h("p", {}, st.body),
      st.key ? h("ul", { class: "key" }, st.key.map(([k, l]) => legendItem(k, l))) : null,
      st.cap ? h("p", { class: "cap" }, st.cap) : null,
      (st.quotes || []).map(q => quoteCard(q)),
      h("p", { class: "count" }, `${st.count ? st.count + " · " : ""}Step ${i + 1} of ${steps.length}`)),
    h("div", { class: "figure-static chart-box" })));
  const hero = h("header", { class: "hero" },
    h("p", { class: "over" }, `Texas school boards · meeting records ${s.first_year}–${s.last_year}`),
    h("h1", {}, "What Texas school boards said after school shootings"),
    h("p", { class: "lede" }, `We collected ${num(s.documents)} agendas, minutes and other meeting documents from ${num(s.districts_with_records)} of Texas's ${num(s.districts_total)} school districts. Here is what they say, and what we can't read yet.`),
    h("div", { class: "factrow" },
      h("div", {}, h("b", {}, num(s.districts_total)), h("span", {}, "districts")),
      h("div", {}, h("b", {}, num(s.documents)), h("span", {}, "documents")),
      h("div", {}, h("b", {}, `${s.first_year}–${s.last_year}`), h("span", {}, "years")),
      h("div", {}, h("b", {}, PCT(unread)), h("span", {}, "of minutes unread"))),
    h("p", { class: "cap" }, h("a", { href: "#/explore" }, "Skip the story and explore the data →")));
  render(h("div", { class: "r" }, hero, m.measures ? null : pending("The story's first steps use the archive itself; the findings steps wait for the measures."),
    h("section", { class: "scrolly", "aria-label": "Story" }, graphic, h("div", { class: "steps" }, stepEls)),
    h("h2", {}, "Look for yourself"),
    h("nav", { class: "closing", "aria-label": "Where to go next" },
      h("a", { href: "#/explore" }, h("b", {}, "Explore every district"), h("span", {}, "The statewide trend with filters, and every district's meetings.")),
      h("a", { href: "#/events" }, h("b", {}, "See any shooting before and after"), h("span", {}, "Nearby districts against the rest of Texas, and the words that changed.")),
      h("a", { href: "#/method" }, h("b", {}, "How we built this"), h("span", {}, "Definitions, accuracy, coverage and what the records cannot show."))),
    asOf(m)));
  const draw = (w, step) => storyGraphic(w, Math.min(560, Math.max(300, (sticky.clientHeight || window.innerHeight * 0.55))), s, m, step);
  if (reducedMotion()) {
    stepEls.forEach((el, i) => mount(el.querySelector(".figure-static"), w => draw(w, i + 1)));
    return;
  }
  sticky.style.setProperty("height", "100%");
  let current = 1;
  mount(sticky, w => { const g = draw(w, current); return g; });
  const io = new IntersectionObserver(entries => {
    for (const en of entries) {
      if (!en.isIntersecting) continue;
      current = +en.target.dataset.step;
      stepEls.forEach(el => el.classList.toggle("on", el === en.target));
      const svg = sticky.querySelector("svg.sv");
      if (svg) { svg.dataset.step = current; svg.dataset.scene = steps[current - 1].scene; }
    }
  }, { rootMargin: "-45% 0px -45% 0px" });
  stepEls.forEach(el => io.observe(el));
  stepEls[0].classList.add("on");
}
function quoteCard(q) {
  return h("blockquote", { class: "quote" }, `“${q.excerpt}”`,
    h("footer", {}, `${q.school_district} · ${dateLabel(q.date)} · ${q.detail || "document"} · `,
      h("span", { class: "tone-tag" }, q.tone === "negative" ? "− negative" : q.tone === "positive" ? "+ positive" : "mixed"), " ",
      q.source_link ? h("a", { href: q.source_link, target: "_blank", rel: "noopener noreferrer" }, "source ↗") : null));
}
function storyGraphic(width, height, s, m, step) {
  const H = Math.max(300, Math.min(height, 620));
  const scene = ["dots", "dots", "dots", "line", "line", "line", "multiples", "multiples", "tone"][step - 1];
  const svg = sv("svg", { class: "c sv", viewBox: `0 0 ${width} ${H}`, width, height: H, "data-step": step, "data-scene": scene, role: "img",
    "aria-label": "Story chart; each step's text describes what it shows." });
  // dots: grid order, then columns by first year on record
  const dots = s.dots;
  const years = []; for (let y = s.first_year; y <= s.last_year; y++) years.push(y);
  const axisH = 30;
  const cell = Math.sqrt((width * (H - 20)) / dots.length);
  const cols = Math.max(1, Math.floor(width / cell));
  const cw = (width - 10) / years.length;
  const maxCount = Math.max(1, ...Object.values(s.first_year_hist));
  let sp = Math.min(cell, 14);
  while (sp > 3 && Math.ceil(maxCount / Math.max(1, Math.floor(cw / sp))) * sp > H - axisH - 10) sp -= 0.5;
  const r = Math.max(1.2, Math.min(cell * 0.34, sp * 0.42));
  const perCol = Math.max(1, Math.floor(cw / sp));
  const stack = {};
  const gd = sv("g", { class: "scene scene-dots" });
  dots.forEach((d, i) => {
    const x1 = (i % cols) * cell + cell / 2, y1 = Math.floor(i / cols) * cell + cell / 2 + 6;
    let x3 = -20, y3 = H - axisH;
    if (d.first_year) {
      const k = stack[d.first_year] = (stack[d.first_year] || 0) + 1;
      const yi = years.indexOf(d.first_year);
      x3 = 5 + yi * cw + ((k - 1) % perCol) * sp + (cw - perCol * sp) / 2 + sp / 2;
      y3 = H - axisH - Math.floor((k - 1) / perCol) * sp - sp / 2;
    }
    gd.appendChild(sv("circle", { class: `dot ${d.state}`, cx: 0, cy: 0, r: r.toFixed(2),
      css: { "--x1": `${x1.toFixed(1)}px`, "--y1": `${y1.toFixed(1)}px`, "--x3": `${x3.toFixed(1)}px`, "--y3": `${y3.toFixed(1)}px` } }));
  });
  const ax = sv("g", { class: "yearaxis" });
  ax.appendChild(sv("line", { class: "base", x1: 5, x2: width - 5, y1: H - axisH + 4, y2: H - axisH + 4 }));
  years.forEach((y, i) => { if (y % 5 === 0 || i === 0 || i === years.length - 1) ax.appendChild(sv("text", { x: 5 + i * cw + cw / 2, y: H - axisH + 18, "text-anchor": "middle" }, i === 0 ? `≤${y}` : String(y))); });
  gd.appendChild(ax);
  svg.appendChild(gd);
  // line scene
  const month = s.month_trend;
  const ev = s.events;
  const xDomain = [ms("2010-01-01"), ms(`${s.last_year}-12-31`)];
  const lineSvg = lineChart(width, {
    height: H, xDomain,
    series: m.measures ? [{ key: "state", cls: "", points: month.map(r => ({ x: ms(r.period), y: r.share, lo: r.lo, hi: r.hi, thin: r.thin })) }] : [],
    yMax: m.measures ? undefined : 0.5, placeholder: m.measures ? null : "Safety share not computed yet",
    denom: month.map(r => ({ x: ms(r.period), n: r.meetings, thin: r.thin })),
    rules: evRules(ev, { cls: "ev" }),
    spans: ev.map(e => ({ x0: ms(e.date), x1: ms(e.date) + 365.25 * 864e5 })),
    projections: s.pretrend ? [{ cls: "pretrend", points: s.pretrend.line.map(p => ({ x: ms(p.period), y: p.share })),  }] : [],
    xTicks: Array.from({ length: s.last_year - 2010 + 1 }, (_, i) => ({ x: Date.UTC(2010 + i, 0, 1), label: String(2010 + i), short: `'${String(2010 + i).slice(2)}` })),
  });
  svg.appendChild(sv("g", { class: "scene scene-line" }, lineSvg));
  // subtopic multiples
  const center = el => { el.setAttribute("y", Math.max(0, (H - +el.getAttribute("height")) / 2).toFixed(0)); return el; };
  const gm = sv("g", { class: "scene scene-multiples" });
  if (s.subtopics) {
    const labels = s.subtopics.labels;
    gm.appendChild(center(multiples(width, Object.keys(labels).map(k => ({ key: k, label: labels[k].replace(/ \(safety-related\)/, ""), short: SUB_SHORT[k],
      points: s.subtopics.series[k].map(p => ({ x: p.year, y: p.share, thin: p.thin })) })), { rules: ev.map(e => ({ x: +e.date.slice(0, 4) + 0.4 })), xLabel: x => String(x) })));
  } else gm.appendChild(sv("text", { class: "placeholder", x: width / 2, y: H / 2, "text-anchor": "middle" }, "Subtopics not computed yet"));
  svg.appendChild(gm);
  const gt = sv("g", { class: "scene scene-tone" });
  if (s.tone && s.tone.rows.length) gt.appendChild(center(diverging(width, s.tone.rows.filter(r => r.year >= 2014))));
  else gt.appendChild(sv("text", { class: "placeholder", x: width / 2, y: H / 2, "text-anchor": "middle" }, "Temperament not computed yet"));
  svg.appendChild(gt);
  return svg;
}

// ------------------------------------------------------------------ explore
async function pageExplore(token, _parts, params) {
  const m = await meta();
  if (token !== routeToken) return;
  document.title = "Explore · Texas school boards";
  const st = { measure: "safety", subtopic: "", region: "", size: "", from: "2010", to: String(m.last_year), sample: "all", grain: "month", allev: "", ...Object.fromEntries(params) };
  const mainCol = h("div", { "aria-live": "polite" });
  const rail = h("form", { class: "rail", "aria-label": "Filters", onsubmit: e => e.preventDefault() });
  const toggle = h("button", { type: "button", class: "filters-toggle", "aria-expanded": "false", onclick: () => {
    const open = rail.classList.toggle("open"); toggle.setAttribute("aria-expanded", String(open)); } }, "Filters");
  const update = () => { setHash("explore", st); drawRail(); drawMain(); };
  function drawRail() {
    const active = ["subtopic", "region", "size"].filter(k => st[k]).length + (st.measure !== "safety") + (st.sample !== "all");
    toggle.textContent = active ? `Filters (${active})` : "Filters";
    const change = key => e => { st[key] = e.target.value; if (key === "measure" && st.measure !== "safety") st.subtopic = ""; update(); };
    rail.replaceChildren(
      h("fieldset", {}, h("legend", {}, "Measure"),
        radio("measure", "safety", st.measure === "safety" && !st.subtopic ? "safety" : "", "Any safety subtopic", () => { st.measure = "safety"; st.subtopic = ""; update(); }),
        Object.entries(m.subtopics).map(([k, l]) => radio("measure", `sub-${k}`, st.measure === "safety" ? `sub-${st.subtopic}` : "", l.replace(/ \(safety-related\)/, ""), () => { st.measure = "safety"; st.subtopic = k; update(); })),
        MEASURE_OPTS.slice(1).map(([k, l]) => radio("measure", k, st.measure, l, () => { st.measure = k; st.subtopic = ""; update(); }))),
      h("fieldset", {}, h("legend", {}, "Sample"),
        radio("sample", "all", st.sample, "All readable meetings", change("sample")),
        radio("sample", "stable", st.sample, "Same districts every year in range", change("sample"))),
      m.regions.length ? h("div", {}, h("label", { class: "lbl", for: "f-region" }, "ESC region"),
        sel("region", [["", "All regions"], ...m.regions.map(r => [r, `Region ${r}`])], st.region, { id: "f-region", onchange: change("region") })) : null,
      h("div", {}, h("label", { class: "lbl", for: "f-size" }, "District size"),
        sel("size", [["", "All sizes"], ...m.size_bands.map(b => [b, b])], st.size, { id: "f-size", onchange: change("size") })),
      h("div", {}, h("span", { class: "lbl" }, "Years"), h("div", { class: "years" },
        h("input", { type: "number", min: m.first_year, max: m.last_year, value: st.from, "aria-label": "From year", onchange: change("from") }), "to",
        h("input", { type: "number", min: m.first_year, max: m.last_year, value: st.to, "aria-label": "To year", onchange: change("to") }))),
      h("div", {}, h("label", { class: "lbl", for: "f-grain" }, "Time step"),
        sel("grain", [["month", "Month"], ["quarter", "Quarter"], ["year", "Year"]], st.grain, { id: "f-grain", onchange: change("grain") })),
      h("label", { class: "opt" }, (() => { const c = h("input", { type: "checkbox", onchange: e => { st.allev = e.target.checked ? "1" : ""; update(); } }); c.checked = !!st.allev; return c; })(), "Mark every event, not only Santa Fe and Uvalde"));
  }
  async function drawMain() {
    const q = { measure: st.measure, subtopic: st.subtopic, region: st.region, size: st.size, from: st.from, to: st.to, sample: st.sample, grain: st.grain };
    const [t, subs, tone] = await Promise.all([api("trend", q),
      m.measures ? api("subtopics", { region: st.region, size: st.size, from: st.from, to: st.to }) : null,
      m.measures ? api("tone", { region: st.region, size: st.size, from: st.from, to: st.to }) : null]);
    if (token !== routeToken) return;
    const chips = [];
    const clear = k => () => { st[k] = k === "sample" ? "all" : k === "measure" ? "safety" : ""; if (k === "measure") st.subtopic = ""; update(); };
    if (st.subtopic) chips.push(["subtopic", m.subtopics[st.subtopic]]);
    if (st.measure !== "safety") chips.push(["measure", MEASURE_OPTS.find(o => o[0] === st.measure)[1]]);
    if (st.region) chips.push(["region", `Region ${st.region}`]);
    if (st.size) chips.push(["size", st.size]);
    if (st.sample !== "all") chips.push(["sample", "Same districts every year"]);
    const events = st.allev ? m.events.filter(e => e.in_texas || e.key === "parkland" || e.key === "sandy_hook") : m.events.filter(e => ["santa_fe", "uvalde"].includes(e.key));
    const label = t.label;
    const finding = trendFinding(t.rows, label, t.measures);
    const fmt = st.grain === "year" ? "year" : "month";
    const pLabel = p => st.grain === "quarter" ? `Q${Math.floor((+p.slice(5, 7) - 1) / 3) + 1} ${p.slice(0, 4)}` : dateLabel(p, fmt);
    const box = h("div", { class: "chart-box" });
    const stableNote = st.sample === "stable" ? ` Same-district sample: ${num(t.chip.stable_districts)} districts with readable meetings in every year ${st.from}–${st.to}.` : "";
    mount(box, w => lineChart(w, {
      series: t.measures ? [{ key: "share", points: t.rows.map(r => ({ x: ms(r.period), y: r.share, lo: r.lo, hi: r.hi, thin: r.thin, r })) }] : [],
      yMax: t.measures ? undefined : 0.5, placeholder: t.measures ? null : "Share not computed yet",
      xDomain: [ms(`${st.from}-01-01`), ms(`${st.to}-12-31`)],
      denom: t.rows.map(r => ({ x: ms(r.period), n: r.meetings, thin: r.thin })),
      rules: evRules(events),
      xTicks: Array.from({ length: +st.to - +st.from + 1 }, (_, i) => ({ x: Date.UTC(+st.from + i, 0, 1), label: String(+st.from + i), short: `'${String(+st.from + i).slice(2)}` })),
      endLabel: p => `${pLabel(p.r.period)}: ${PCT(p.y)}`,
      tip: p => Tip.lines(pLabel(p.r.period), `${PCT(p.y)} of ${num(p.r.meetings)} readable meetings (${num(p.r.yes)})`,
        p.r.lo !== null ? `95% interval ${PCT(p.r.lo)} to ${PCT(p.r.hi)}` : null, p.r.uncertain ? `${num(p.r.uncertain)} more were uncertain` : null,
        `${num(p.r.districts)} districts`, p.r.thin ? "Thin: fewer than 30 meetings" : null),
    }));
    const rows = t.rows.map(r => [pLabel(r.period), num(r.meetings), t.measures ? num(r.yes) : "–", t.measures ? num(r.uncertain) : "–", shareText(r.share), r.lo !== null ? `${shareText(r.lo)}–${shareText(r.hi)}` : "–", num(r.districts)]);
    const csv = `api/trend?${qs({ ...q, format: "csv" })}`;
    const parts = [
      h("div", { class: "toolbar" }, chipEl(t.chip), toggle),
      chips.length ? h("div", { class: "chips", "aria-label": "Active filters" }, chips.map(([k, l]) => h("button", { type: "button", onclick: clear(k), "aria-label": `Remove filter ${l}` }, l))) : null,
      t.measures ? null : pending("The line will show the share of readable meetings with a mention."),
      figure({
        titleNodes: finding || (t.measures ? `Share of readable meetings mentioning ${label}` : "Readable meetings per month, the denominator for every share"),
        summary: finding ? finding.map(x => typeof x === "string" ? x : x.textContent).join("") : "Counts of readable meetings over time.",
        chart: box,
        legend: legendList(t.measures ? [["line", "Share of readable meetings"], ["band", "95% interval"], ["thin", "Fewer than 30 meetings"], ["rule", "Shooting"]] : [["rule", "Shooting"]]),
        caption: [t.measures ? `Share of readable minutes and agendas mentioning ${label}, one meeting counted once. ` : "Readable minutes and agendas per month, one meeting counted once. ",
          `${num(t.chip.districts)} districts, ${num(t.chip.meetings)} meetings.${stableNote} `,
          t.measures && t.chip.not_screened ? `${num(t.chip.not_screened)} readable meetings are not screened yet. ` : "",
          `Scans not yet read by OCR are excluded (${PCT(t.chip.minutes_unread_share)} of minutes), so counts are lower bounds. ${STANDING}`],
        table: { columns: [st.grain === "year" ? "Year" : st.grain === "quarter" ? "Quarter" : "Month", "Readable meetings", "Mentions", "Uncertain", "Share", "95% interval", "Districts"], rows, caption: label },
        csv,
      }),
    ];
    if (subs) {
      const sbox = h("div", { class: "chart-box" });
      const labels = subs.labels;
      mount(sbox, w => multiples(w, Object.keys(labels).map(k => ({ key: k, label: labels[k].replace(/ \(safety-related\)/, ""), short: SUB_SHORT[k],
        points: subs.series[k].map(p => ({ x: p.year, y: p.share, thin: p.thin })) })),
        { selected: st.subtopic || null, rules: events.map(e => ({ x: +e.date.slice(0, 4) + (+e.date.slice(5, 7) - 1) / 12 })), xLabel: x => String(Math.round(x)),
          onSelect: k => { st.measure = "safety"; st.subtopic = st.subtopic === k ? "" : k; update(); window.scrollTo({ top: 0 }); } }));
      parts.push(h("h2", {}, "What they talked about"), figure({
        title: "Each safety subtopic, as a share of readable meetings per year",
        sub: "Same scale in every panel. One meeting can mention several subtopics. Select a panel to put it in the main chart.",
        chart: sbox, caption: `Share of readable meetings each year mentioning the subtopic. ${STANDING}`,
        table: { columns: ["Subtopic", ...subs.series[Object.keys(labels)[0]].map(p => String(p.year))],
          rows: Object.keys(labels).map(k => [labels[k], ...subs.series[k].map(p => shareText(p.share))]) },
        csv: `api/subtopics?${qs({ region: st.region, size: st.size, from: st.from, to: st.to, format: "csv" })}` }));
    }
    if (tone) {
      const tbox = h("div", { class: "chart-box" });
      mount(tbox, w => diverging(w, tone.rows));
      parts.push(h("h2", {}, "How they described students and staff"), figure({
        title: "Negative, mixed and positive descriptions of how students and staff feel or behave",
        sub: "Share of readable meetings each year. Negative extends left of the centre line, positive right; mixed sits across the centre.",
        chart: tbox, legend: legendList([["neg", "− negative"], ["mixed", "mixed"], ["pos", "+ positive"]]),
        caption: `Temperament: any statement about how students or staff feel or behave, as defined in the method. Figures of speech such as "happy to report" are excluded. ${STANDING}`,
        table: { columns: ["Year", "Group", "Meetings", "Negative", "Mixed", "Positive"], rows: tone.rows.map(r => [r.year, r.who, num(r.meetings), num(r.negative), num(r.mixed), num(r.positive)]) },
        csv: `api/tone?${qs({ region: st.region, size: st.size, from: st.from, to: st.to, format: "csv" })}` }),
        tone.quotes.length ? h("div", { class: "col" }, tone.quotes.map(quoteCard)) : null);
    }
    parts.push(h("h2", {}, "Districts"), h("p", { class: "col" }, "Every district has a page with one mark per meeting. Find one on the ",
      h("a", { href: "#/map" }, "map"), ", in the ", h("a", { href: "#/districts" }, "district list"), ", or see ", h("a", { href: "#/coverage" }, "what we can read for every district and year"), "."));
    mainCol.replaceChildren(...parts.filter(Boolean));
  }
  drawRail();
  render(h("div", { class: "r" }, h("p", { class: "over" }, "Explore"), h("h1", {}, "Safety in Texas school board meetings, statewide"),
    h("div", { class: "explore" }, rail, mainCol), asOf(m)));
  await drawMain();
}

// ------------------------------------------------------------------ coverage grid
async function pageCoverage(token, _parts, params) {
  const [m, g] = await Promise.all([meta(), api("coverage_grid")]);
  if (token !== routeToken) return;
  document.title = "What we can read · Texas school boards";
  const sort = params.get("sort") || "first";
  const sorters = { first: (a, b) => (a.first_year || 9999) - (b.first_year || 9999) || a.school_district.localeCompare(b.school_district),
    enrollment: (a, b) => (b.enrollment || 0) - (a.enrollment || 0), name: (a, b) => a.school_district.localeCompare(b.school_district) };
  const grid = { ...g, districts: g.districts.slice().sort(sorters[sort] || sorters.first) };
  const box = h("div", { class: "chart-box" });
  mount(box, w => coverageGrid(w, grid, { bands: m.size_bands, onSelect: d => { location.hash = `#/board/${d.tea_id}`; } }));
  const last = g.per_year[g.per_year.length - 2] || g.per_year[g.per_year.length - 1];
  const flagLabels = { late_start: "Minutes start after 2018", short_span: "Under 3 years of minutes", long_gap: "A gap over 6 months", multi_platform: "More than one platform", no_records: "No records found" };
  render(h("div", { class: "r" }, h("p", { class: "crumbs" }, h("a", { href: "#/explore" }, "Explore"), " / What we can read"),
    h("p", { class: "over" }, "Coverage"), h("h1", {}, "What we can read"),
    h("p", { class: "lede" }, `In ${last.year}, ${num(last.readable)} of ${num(g.districts.length)} districts posted minutes or agendas we can read. Earlier years are thinner: most districts only post recent ones.`),
    h("div", { class: "toolbar" }, h("div", { class: "seg", role: "group", "aria-label": "Sort rows" },
      [["first", "First year on record"], ["enrollment", "Enrollment"], ["name", "Name"]].map(([k, l]) =>
        h("button", { type: "button", "aria-pressed": String(sort === k), onclick: () => { location.hash = `#/coverage?sort=${k}`; } }, l)))),
    figure({ title: "One row per district, one column per year", sub: "On phones the rows are grouped by district size; open the table for every district.",
      chart: box, legend: legendList([["readable", "Readable minutes or agenda"], ["gridScan", "Only scans awaiting OCR"], ["gridUnavailable", "Listed, but text unavailable"], ["none", "No record found"]]),
      caption: "Rows are too thin for the hatching used elsewhere, so scans are drawn in mid grey and unavailable documents in pale grey here. Minutes, agendas and BoardBook projector views only. A cell takes the best state of any document that year. The bars at the top count districts per year.",
      table: { columns: ["District", "County", "First year", ...g.years.map(String)], rows: grid.districts.map(d => [d.school_district, d.county_name, d.first_year || "–", ...d.cells.split("").map(c => ({ R: "■", S: "▨", U: "□", ".": "·" })[c])]) },
      csv: "api/coverage_grid?format=csv" }),
    h("h3", {}, "Coverage flags"),
    h("ul", { class: "col" }, Object.entries(g.flags).map(([k, n]) => h("li", {}, `${flagLabels[k] || k}: ${num(n)} districts`))),
    asOf(m)));
}

// ------------------------------------------------------------------ events
async function pageEvents(token, parts, params) {
  const m = await meta();
  if (token !== routeToken) return;
  const key = parts[0] || params.get("key") || "uvalde";
  const e = m.events.find(x => x.key === key);
  if (!e) { render(h("p", {}, "Unknown event. ", h("a", { href: "#/events" }, "All events"))); return; }
  document.title = `${e.label} · Texas school boards`;
  const st = { radius: "50", same_county: "", window: "12", sample: "balanced", measure: "safety", subtopic: "", scope: "", ...Object.fromEntries(params) };
  const body = h("div", { "aria-live": "polite" });
  const controls = h("form", { class: "ev-controls", "aria-label": "Event settings", onsubmit: ev => ev.preventDefault() });
  const update = () => { setHash(`events/${key}`, st); drawControls(); drawBody(); };
  function drawControls() {
    const out = h("output", { for: "f-radius" }, `${st.radius} miles`);
    const range = h("input", { type: "range", id: "f-radius", min: 10, max: 200, step: 5, value: st.radius, disabled: st.same_county ? true : null,
      oninput: ev => { out.textContent = `${ev.target.value} miles`; }, onchange: ev => { st.radius = ev.target.value; update(); } });
    const county = h("input", { type: "checkbox", id: "f-county", onchange: ev => { st.same_county = ev.target.checked ? "1" : ""; update(); } });
    county.checked = !!st.same_county;
    controls.replaceChildren(
      e.in_texas ? h("div", {}, h("label", { class: "lbl", for: "f-radius" }, "Nearby means within"), range, out,
        h("label", { class: "opt", for: "f-county" }, county, `Same county instead (${e.county})`)) :
        h("div", {}, h("span", { class: "lbl" }, "Nearby"), h("p", { class: "small" }, "Outside Texas, so every Texas district is compared before and after.")),
      h("div", {}, h("label", { class: "lbl", for: "f-window" }, "Window"),
        sel("window", [["6", "± 6 months"], ["12", "± 12 months"], ["24", "± 24 months"]], st.window, { id: "f-window", onchange: ev => { st.window = ev.target.value; update(); } })),
      h("div", {}, h("span", { class: "lbl" }, "Districts"),
        radio("sample", "balanced", st.sample, "Readable meetings both before and after", ev => { st.sample = ev.target.value; update(); }),
        radio("sample", "all", st.sample, "All readable meetings", ev => { st.sample = ev.target.value; update(); })),
      h("div", {}, h("label", { class: "lbl", for: "f-measure" }, "Measure"),
        sel("measure", [["safety|", "Any safety subtopic"], ...Object.entries(m.subtopics).map(([k, l]) => [`safety|${k}`, l.replace(/ \(safety-related\)/, "")]), ...MEASURE_OPTS.slice(1).map(([k, l]) => [`${k}|`, l])],
          `${st.measure}|${st.subtopic}`, { id: "f-measure", onchange: ev => { [st.measure, st.subtopic] = ev.target.value.split("|"); update(); } })));
  }
  async function drawBody() {
    const q = { key, radius: st.radius, same_county: st.same_county, window: st.window, sample: st.sample, measure: st.measure, subtopic: st.subtopic };
    const [r, all] = await Promise.all([api("event", q), api("events_summary", { radius: st.radius, same_county: st.same_county, window: st.window, sample: st.sample, measure: st.measure, subtopic: st.subtopic })]);
    if (token !== routeToken) return;
    const W = +st.window;
    const near = r.summary.nearby || {}, rest = r.summary.comparison || {};
    const main = e.in_texas ? near : rest;
    const rule = e.in_texas ? (r.same_county ? `in ${e.county}` : `within ${r.radius} miles`) : "in Texas";
    let finding;
    if (!r.measures) finding = [`${num(main.districts_qualifying || 0)} districts ${rule} have readable meetings both in the ${W} months before and after. The measures are not computed yet.`];
    else if (main.before === null || main.after === null || main.before === undefined) finding = [`Not enough readable meetings ${rule} on both sides of the event to compare.`];
    else finding = [`In the ${num(main.districts_qualifying)} ${e.in_texas ? "nearby " : ""}districts with readable meetings on both sides, ${r.label} came up in `,
      h("span", { class: "accent" }, `${shareText(main.after)} of meetings in the ${W} months after`), `, against ${shareText(main.before)} in the ${W} months before.`,
      e.in_texas && rest.before !== null && rest.before !== undefined ? ` Elsewhere in Texas: ${shareText(rest.before)} before, ${shareText(rest.after)} after.` : ""];
    const box = h("div", { class: "chart-box" });
    const pt = (row, grp) => ({ x: row.rel + 0.5, y: row.share, lo: row.lo, hi: row.hi, thin: row.thin, row, grp });
    const relLabel = rel => rel >= 0 ? `Month ${rel + 1} after` : `${-rel} month${rel === -1 ? "" : "s"} before`;
    const series = [];
    if (r.measures) {
      if (e.in_texas) series.push({ key: "nearby", points: r.series.nearby.map(x => pt(x, "nearby")), markers: true });
      series.push({ key: "comparison", cls: e.in_texas ? "grey" : "", points: r.series.comparison.map(x => pt(x, "comparison")), markers: true });
    }
    mount(box, w => lineChart(w, {
      series, xDomain: [-W, W], yMax: r.measures ? undefined : 0.5, band: true, placeholder: r.measures ? null : "Share not computed yet",
      rules: [{ x: 0, label: `${evShort(e)} · ${dateLabel(e.date)}`, short: evShort(e) }],
      projections: r.pretrend ? [{ cls: "pretrend", points: r.pretrend.map(p => ({ x: p.rel + 0.5, y: p.share })) }] : [],
      denom: (e.in_texas ? r.series.nearby : r.series.comparison).map(x => ({ x: x.rel + 0.5, n: x.meetings, thin: x.thin })),
      denomLabel: e.in_texas ? "readable meetings, nearby" : "readable meetings",
      xTicks: [-W, -W / 2, 0, W / 2, W].map(v => ({ x: v, label: v === 0 ? "event" : `${v > 0 ? "+" : "−"}${Math.abs(v)} months`, short: v === 0 ? "event" : `${v > 0 ? "+" : "−"}${Math.abs(v)}` })),
      tip: p => Tip.lines(`${p.grp === "nearby" ? "Nearby" : e.in_texas ? "Rest of Texas" : "All Texas"}, ${relLabel(p.row.rel)}`,
        `${PCT(p.y)} of ${num(p.row.meetings)} meetings (${num(p.row.yes)})`, p.row.lo !== null ? `95% interval ${PCT(p.row.lo)} to ${PCT(p.row.hi)}` : null,
        `${num(p.row.districts)} districts`, p.row.thin ? "Thin: fewer than 30 meetings" : null),
    }));
    const dbox = h("div", { class: "chart-box" });
    const sortBy = st.sort || "severity";
    const rowsAll = all.rows.slice().sort(sortBy === "date" ? (a, b) => b.date.localeCompare(a.date) : sortBy === "change" ? (a, b) => (b.change ?? -9) - (a.change ?? -9) : (a, b) => (b.deaths || 0) - (a.deaths || 0));
    mount(dbox, w => dumbbell(w, rowsAll.map(x => ({ key: x.key, label: SHORT[x.key] || x.label, sub: dateLabel(x.date), before: all.measures ? x.before : null, after: all.measures ? x.after : null,
      thin: x.thin, selected: x.key === key, nLabel: x.in_texas ? `n=${x.districts}${x.thin ? " (thin)" : ""}` : `all Texas, n=${x.districts}`,
      empty: all.measures ? "Not enough records on both sides" : "Measures not computed yet" })), { onSelect: row => { location.hash = `#/events/${row.key}?${qs({ ...st, key: undefined })}`; } }));
    const words = h("div", {});
    const parts = [
      h("p", { class: "finding" }, finding),
      r.measures ? null : pending("The before and after lines will show the share of meetings with a mention."),
      figure({ title: e.in_texas ? `Nearby districts against the rest of Texas, month by month` : "All Texas districts, month by month",
        summary: finding.map(x => typeof x === "string" ? x : x.textContent).join(""), chart: box,
        legend: legendList([...(r.measures ? (e.in_texas ? [["filled", "Nearby districts"], ["open", "Rest of Texas"]] : [["filled", "All Texas"]]) : []), ...(r.pretrend ? [["dotted", "Trend before, carried forward"]] : []), ["rule", "Event"]]),
        caption: [`${HEDGE} Nearby = ${e.in_texas ? (r.same_county ? `same county (${e.county})` : `district office within ${r.radius} miles of ${e.place}`) : "not applicable"}. `,
          e.in_texas ? `${num(near.districts_qualifying || 0)} of ${num(near.districts_total || 0)} nearby districts qualify; ${num(near.districts_no_before || 0)} lack readable meetings before and ${num(near.districts_no_after || 0)} after. ` : "",
          `${r.sample === "balanced" ? "Only districts with readable meetings on both sides are counted." : "All readable meetings are counted."} ${STANDING}`],
        table: { columns: ["Month", "Group", "Meetings", "Mentions", "Share", "95% interval", "Districts"],
          rows: ["nearby", "comparison"].flatMap(g => r.series[g].map(x => [relLabel(x.rel), g === "nearby" ? "Nearby" : "Rest of Texas", num(x.meetings), r.measures ? num(x.yes) : "–", shareText(x.share), x.lo !== null ? `${shareText(x.lo)}–${shareText(x.hi)}` : "–", num(x.districts)])) },
        csv: `api/event?${qs({ ...q, format: "csv" })}` }),
      h("h2", { id: "all-events" }, "Every event, before and after"),
      figure({ title: all.measures ? `Share of meetings mentioning ${all.label}, ${W} months before ○ and after ●` : `Every event, ${W} months before and after (shares appear when the measures are ready)`,
        chart: dbox, extra: h("label", { class: "opt" }, "Sort by ", sel("sort", [["severity", "Deaths (severity)"], ["date", "Date"], ["change", "Change"]], sortBy,
          { onchange: ev => { st.sort = ev.target.value; update(); } })),
        caption: `Texas events compare districts nearby (${r.same_county ? "same county" : `within ${r.radius} miles`}); events outside Texas compare all Texas districts. n = districts with readable meetings on both sides; rows under ${all.min_districts} are marked thin. ${HEDGE}`,
        table: { columns: ["Event", "Date", "Group", "Before", "After", "Change", "Districts"], rows: all.rows.map(x => [x.label, x.date, x.group, shareText(x.before), shareText(x.after), x.change !== null ? PTS(x.change) : "–", num(x.districts)]) },
        csv: `api/events_summary?${qs({ radius: st.radius, same_county: st.same_county, window: st.window, sample: st.sample, measure: st.measure, subtopic: st.subtopic, format: "csv" })}` }),
      h("h2", {}, "What changed in the words"), words,
      e.in_texas ? h("h2", {}, "Nearby districts") : null,
      e.in_texas ? districtTable(r.districts) : null,
      h("h3", {}, "Downloads"),
      h("ul", {}, h("li", {}, h("a", { href: `api/event?${qs({ ...q, format: "csv" })}` }, "This event, month by month (CSV)")),
        h("li", {}, h("a", { href: `api/panel?${qs({ format: "csv", window: st.window, radius: st.radius })}` }, "Event × district × meeting panel for every event (CSV)"), " · ",
          h("a", { href: `api/panel?${qs({ format: "parquet", window: st.window, radius: st.radius })}` }, "Parquet"),
          h("span", { class: "muted small" }, " Every district and meeting within the window, with distance, same-county and within-radius columns, for R or Stata."))),
    ];
    body.replaceChildren(...parts.filter(Boolean));
    drawWords(words);
  }
  async function drawWords(box) {
    box.replaceChildren(h("p", { class: "muted" }, "Comparing the words used before and after…"));
    let w;
    try {
      w = await api("event_words", { key, radius: st.radius, same_county: st.same_county, window: st.window, scope: st.scope });
    } catch (err) { box.replaceChildren(h("p", { class: "bad-text" }, String(err.message))); return; }
    if (token !== routeToken) return;
    const kwic = h("div", { class: "kwic", "aria-live": "polite" }, h("p", { class: "muted small" }, "Select a word to read it in context."));
    let pressed = null;
    const open = async (word, side, btn) => {
      if (pressed) pressed.setAttribute("aria-pressed", "false");
      pressed = btn; if (btn) btn.setAttribute("aria-pressed", "true");
      kwic.replaceChildren(h("p", { class: "muted small" }, `Finding “${word}”…`));
      const k = await api("kwic", { key, word, side, radius: st.radius, same_county: st.same_county, window: st.window, scope: w.scope });
      kwic.replaceChildren(h("h4", {}, `“${word}” ${side === "after" ? "after" : "before"} the event`),
        k.items.length ? h("ol", {}, k.items.map(it => h("li", {},
          h("div", { class: "ctx" }, it.before, h("mark", {}, it.match), it.after),
          h("div", { class: "src" }, `${it.school_district} · ${dateLabel(it.date)} · ${it.detail || "document"} · `,
            h("a", { href: `#/record/${it.record_id}` }, "full text"), it.source_link ? [" · ", h("a", { href: it.source_link, target: "_blank", rel: "noopener noreferrer" }, "source ↗")] : null)))) :
          h("p", { class: "muted" }, "No passages found."),
        h("p", { class: "cap" }, "Verbatim from the extracted text; spacing normalised."));
    };
    const list = (items, side) => {
      const zmax = Math.max(1, ...items.map(x => Math.abs(x.z)));
      return h("ol", { class: `wordlist ${side === "before" ? "down" : ""}` }, items.slice(0, 25).map((x, i) => {
        const b = h("button", { type: "button", "aria-pressed": "false", onclick: ev => open(x.word, side, ev.currentTarget),
          "aria-label": `${x.word}: z ${x.z}, in ${PCT(x.share_before)} of documents before and ${PCT(x.share_after)} after` },
          h("span", { class: "rank" }, i + 1), h("span", { class: "w" }, x.word),
          h("span", { class: "zbar", "aria-hidden": "true" }, (() => { const s = h("span"); s.style.setProperty("width", `${(100 * Math.abs(x.z) / zmax).toFixed(1)}%`); return s; })()),
          h("span", { class: "wv" }, `${PCT(x.share_before)} → ${PCT(x.share_after)} of docs · z ${x.z.toFixed(1)}`));
        return h("li", {}, b);
      }));
    };
    const top = w.words_after.slice(0, 24);
    const zmax = Math.max(1, ...top.map(x => x.z));
    const cloud = h("div", { class: "cloud", "aria-hidden": "true" }, top.map((x, i) => {
      const b = h("button", { type: "button", tabindex: -1, class: i < 3 ? "top" : "", onclick: () => open(x.word, "after", null) }, x.word);
      b.style.setProperty("font-size", `${(15 + 29 * Math.sqrt(Math.max(0, x.z) / zmax)).toFixed(0)}px`);
      return b;
    }));
    const scopeSel = w.scope === "passages" || metaCache.passages ? h("div", { class: "seg", role: "group", "aria-label": "Text compared" },
      [["all", "All meeting text"], ["passages", "Safety passages only"]].map(([k, l]) => h("button", { type: "button", "aria-pressed": String(w.scope === k),
        disabled: k === "passages" && !metaCache.passages ? true : null, onclick: () => { st.scope = k; setHash(`events/${key}`, st); drawWords(box); } }, l))) : null;
    box.replaceChildren(
      h("p", { class: "col" }, `Words that districts ${e.in_texas ? (w.same_county ? `in ${e.county}` : `within ${w.radius} miles`) : "across Texas"} used more often in the ${w.window} months after ${evShort(e)} than in the ${w.window} months before, from ${num(w.docs_after)} documents after and ${num(w.docs_before)} before.`),
      scopeSel,
      top.length ? cloud : null,
      top.length ? h("p", { class: "cap" }, "The same words as the list below, sized by how distinctive they are. The ranked list is the measure; read it, not the sizes.") : null,
      h("div", { class: "words" },
        h("div", {}, h("h3", {}, "More often after"), w.words_after.length ? list(w.words_after, "after") : h("p", { class: "muted" }, "No word passes the filters."),
          h("h3", {}, "Less often after"), w.words_before.length ? list(w.words_before, "before") : h("p", { class: "muted" }, "No word passes the filters.")),
        h("div", {}, kwic)),
      h("p", { class: "cap" }, `${w.method} A word that changes is not evidence of why it changed: new board members, new minute templates and other events all move words. Check the passages.`),
      h("div", { class: "fig-tools" }, h("a", { href: `api/event_words?${qs({ key, radius: st.radius, same_county: st.same_county, window: st.window, scope: w.scope, n: 200, format: "csv" })}` }, "Download the word scores (CSV)")));
  }
  const picker = sel("event", m.events.map(x => [x.key, `${x.label} · ${dateLabel(x.date)}`]), key, { "aria-label": "Choose an event", onchange: ev => { location.hash = `#/events/${ev.target.value}`; } });
  drawControls();
  render(h("div", { class: "r" },
    h("div", { class: "ev-head" }, h("p", { class: "crumbs" }, h("a", { href: "#all-events", onclick: ev => { ev.preventDefault(); document.getElementById("all-events")?.scrollIntoView(); } }, "← All events")), picker),
    h("p", { class: "over" }, `${e.in_texas ? (e.kind === "school" ? "School shooting in Texas" : "Shooting in a Texas community") : "Shooting outside Texas"} · ${e.place}`),
    h("h1", {}, `${e.label} · ${dateLabel(e.date)}`),
    controls, body, asOf(m)));
  await drawBody();
}
function districtTable(rows) {
  if (!rows.length) return h("p", { class: "muted" }, "No district offices in this area.");
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, ["District", "County", "Miles", "Meetings before", "Meetings after", "Counted"].map((c, i) => h("th", { scope: "col", class: i >= 2 && i <= 4 ? "num" : "" }, c)))),
    h("tbody", {}, rows.map(d => h("tr", {}, h("td", {}, h("a", { href: `#/board/${d.tea_id}` }, d.school_district)), h("td", {}, d.county_name),
      h("td", { class: "num" }, d.miles ?? "–"), h("td", { class: "num" }, num(d.meetings_before)), h("td", { class: "num" }, num(d.meetings_after)),
      h("td", {}, d.qualifies ? "yes" : "no, missing a side"))))));
}

// ------------------------------------------------------------------ district page
async function pageBoard(token, parts, params) {
  const [m, b] = await Promise.all([meta(), api("board", { tea_id: parts[0] })]);
  if (token !== routeToken) return;
  const d = b.district;
  document.title = `${d.school_district} · Texas school boards`;
  const s = b.summary;
  const years = []; for (let y = m.first_year; y <= m.last_year; y++) years.push(y);
  const strip = h("div", { class: "chart-box" });
  mount(strip, w => coverageStrip(w, b.strip, years));
  const ticks = h("div", { class: "chart-box" });
  const first = b.meetings[0]?.date || `${m.first_year}-01-01`;
  const evs = b.events.map(e => ({ ...e, short: evShort(e), labelShort: `${evShort(e)} ${e.date.slice(0, 4)}${e.miles !== null && e.miles < 200 ? ` · ${Math.round(e.miles)} mi` : ""}` }));
  mount(ticks, w => meetingTicks(w, b, { from: `${first.slice(0, 4)}-01-01`, events: evs, measures: b.measures, labels: m.subtopics,
    onOpen: mt => { if (mt.record_id) location.hash = `#/record/${mt.record_id}`; } }));
  const finding = b.measures
    ? [`${num(s.meetings)} meetings on record. `, h("span", { class: "accent" }, `${num(s.yes)} mention safety`), ` (at least; ${num(s.scans)} scanned meetings are unread${s.uncertain ? `, ${num(s.uncertain)} uncertain` : ""}).`]
    : [`${num(s.meetings)} meetings on record, ${num(s.readable)} readable. ${num(s.scans)} scanned meetings are not yet read by OCR.`];
  const list = h("div", {});
  const only = params.get("only") || "";
  render(h("div", { class: "r" },
    h("p", { class: "crumbs" }, h("a", { href: "#/explore" }, "Explore"), " / ", h("a", { href: "#/map" }, "Map"), " / District"),
    h("p", { class: "over" }, [d.county_name, d.esc_region ? `ESC region ${d.esc_region}` : null, d.enrollment ? `about ${num(Math.round(d.enrollment / 10) * 10)} students (NCES 2022)` : null].filter(Boolean).join(" · ")),
    h("h1", {}, d.school_district),
    b.measures ? null : pending("This page shows every meeting we found; mention marks appear when the measures are ready."),
    figure({ title: `Records: ${b.strip.length ? `${b.strip[0].year} to ${b.strip[b.strip.length - 1].year}` : "none found"}`, chart: strip,
      legend: legendList([["readable", "Readable"], ["scan", "Scans awaiting OCR"], ["unavailable", "Unavailable"], ["none", "No record"]]),
      caption: b.coverage && b.coverage.flags ? `Coverage flags: ${b.coverage.flags.split(",").join(", ")}.` : null,
      table: { columns: ["Year", "Readable", "Scans", "Unavailable", "Documents"], rows: b.strip.map(r => [r.year, num(r.readable), num(r.scan), num(r.unavailable), num(r.documents)]) } }),
    h("p", { class: "finding" }, finding),
    figure({ title: "One mark per meeting", sub: b.measures ? "Tall marks mention safety. The line is the share over the previous 12 months (drawn where those months hold at least 6 meetings)." : "Each mark is a readable meeting; hatched marks are scans not yet read.",
      chart: ticks, legend: legendList([...(b.measures ? [["yes", "Mentions safety"], ["no", "No mention found"], ["unc", "Uncertain"], ["line", "12-month share"]] : [["no", "Readable meeting"]]), ["scan", "Scan, not yet read"], ["rule", "Shooting (distance from the district office)"]]),
      caption: `${STANDING} A meeting with no mention may still have discussed safety in a closed session (Gov't Code 551.076), which public records do not show.`,
      csv: `api/board?${qs({ tea_id: d.tea_id, format: "csv" })}` }),
    h("h2", {}, "Meetings"),
    h("div", { class: "toolbar" }, h("div", { class: "seg", role: "group", "aria-label": "Show meetings" },
      [["", "All"], ["safety", "Safety mentions"], ["temperament", "Temperament"]].map(([k, l]) => h("button", { type: "button", "aria-pressed": String(only === k), disabled: k && !b.measures ? true : null,
        onclick: () => { location.hash = `#/board/${d.tea_id}${k ? `?only=${k}` : ""}`; } }, l))),
      h("span", { class: "small" }, h("a", { href: `#/search?${qs({ tea_id: d.tea_id })}` }, "Search this district's minutes"), " · ", h("a", { href: `#/district/${d.tea_id}` }, "Collection details"))),
    list, asOf(m)));
  await meetingList(list, d.tea_id, only, 0, token, m);
}
async function meetingList(box, teaId, only, offset, token, m) {
  const r = await api("board_meetings", { tea_id: teaId, only, offset, limit: 30 });
  if (token !== routeToken) return;
  const ul = box.querySelector("ul.meetings") || box.appendChild(h("ul", { class: "meetings" }));
  box.querySelector(".pager")?.remove();
  for (const it of r.items) {
    const flags = [];
    if (it.safety === "yes") flags.push(...(it.subtopics.length ? it.subtopics.map(s => (m.subtopics[s] || s).replace(/ \(safety-related\)/, "")) : ["safety"]));
    if (it.safety === "uncertain") flags.push("safety: uncertain");
    if (it.temperament === "yes") flags.push(`temperament: ${it.tone}${it.who && it.who !== "none" ? ` (${it.who})` : ""}`);
    if (it.event_keys.length) flags.push(`refers to ${it.event_keys.map(k => SHORT[k] || k).join(", ")}`);
    if (it.closed) flags.push("closed security session");
    ul.appendChild(h("li", {},
      h("span", { class: "d" }, dateLabel(it.date)),
      h("span", { class: "t" }, `${it.meeting_type || "Meeting"} · ${it.detail || "document"}${it.readable ? "" : " · scan, not yet read"}`, h("br"),
        flags.map(f => h("span", { class: "flagchip on" }, f))),
      h("span", { class: "src" }, it.record_id ? h("a", { href: `#/record/${it.record_id}` }, "text") : null, it.source_link ? [" · ", h("a", { href: it.source_link, target: "_blank", rel: "noopener noreferrer" }, "source ↗")] : null),
      it.evidence.length ? h("div", { class: "x" }, it.evidence.slice(0, 2).map(x => h("div", {}, `“${x.excerpt}”`))) : null));
  }
  if (!r.items.length && !offset) ul.appendChild(h("li", {}, h("span", { class: "muted" }, "No meetings match.")));
  if (offset + r.items.length < r.total) {
    box.appendChild(h("div", { class: "pager" }, h("button", { type: "button", onclick: () => meetingList(box, teaId, only, offset + 30, token, m) }, `Show more (${num(r.total - offset - r.items.length)} left)`)));
  }
}

// ------------------------------------------------------------------ map
async function pageMap(token, _parts, params) {
  const m = await meta();
  const st = { measure: "safety", subtopic: "", from: String(m.last_year - 4), to: String(m.last_year), min: "12", ...Object.fromEntries(params) };
  const [d, outline] = await Promise.all([api("map", st), outlineCache || fetch("texas.json").then(r => r.json())]);
  outlineCache = outline;
  if (token !== routeToken) return;
  document.title = "Map · Texas school boards";
  const passing = d.districts.filter(x => x.passes && x.share !== null).map(x => x.share).sort((a, b) => a - b);
  const qs5 = [0.2, 0.4, 0.6, 0.8].map(p => passing[Math.floor(p * (passing.length - 1))] ?? 0);
  const bin = v => qs5.filter(t => v > t).length;
  const box = h("div", { class: "chart-box map-box" });
  const events = d.events.map(e => ({ ...e, short: evShort(e) }));
  const tip = (x, withLink) => Tip.lines(x.school_district, `${x.county_name}${x.enrollment ? ` · about ${num(Math.round(x.enrollment / 10) * 10)} students` : ""}`,
    !x.all_meetings ? "No record found" : d.measures && x.passes ? `${PCT(x.share)} of ${num(x.meetings)} readable meetings mention ${d.label}` : `${num(x.meetings)} readable meetings${x.passes ? "" : ` (fewer than ${d.min}, drawn hollow)`}`,
    withLink ? h("a", { href: `#/board/${x.tea_id}` }, "Open the district →") : "Click to open the district");
  mount(box, w => dotMap(w, { ...d, events }, outline, { bin, tip, onSelect: x => { location.hash = `#/board/${x.tea_id}`; } }));
  const update = () => { location.hash = `#/map?${qs(st)}`; };
  const ramp = h("ul", { class: "legend-inline" }, [0, 1, 2, 3, 4].map(i => {
    const g = sv("svg", { width: 14, height: 14, class: "c lg", "aria-hidden": "true" }, sv("circle", { cx: 7, cy: 7, r: 6, class: `md q${i}` }));
    const lo = i ? qs5[i - 1] : 0, hi = i < 4 ? qs5[i] : 1;
    return h("li", {}, g, `${PCT(lo)}–${PCT(hi)}`);
  }), legendItem("hollow", `Fewer than ${d.min} readable meetings`), legendItem("none", "No record found"));
  const top = d.districts.filter(x => x.passes && x.share !== null).sort((a, b) => b.share - a.share);
  render(h("div", { class: "r" }, h("p", { class: "over" }, "Map"), h("h1", {}, "Where safety came up"),
    h("form", { class: "ev-controls", onsubmit: ev => ev.preventDefault() },
      h("div", {}, h("label", { class: "lbl", for: "m-measure" }, "Measure"),
        sel("measure", [["safety|", "Any safety subtopic"], ...Object.entries(m.subtopics).map(([k, l]) => [`safety|${k}`, l.replace(/ \(safety-related\)/, "")]), ...MEASURE_OPTS.slice(1).map(([k, l]) => [`${k}|`, l])],
          `${st.measure}|${st.subtopic}`, { id: "m-measure", onchange: ev => { [st.measure, st.subtopic] = ev.target.value.split("|"); update(); } })),
      h("div", {}, h("span", { class: "lbl" }, "Years"), h("div", { class: "years" },
        h("input", { type: "number", min: m.first_year, max: m.last_year, value: st.from, "aria-label": "From year", onchange: ev => { st.from = ev.target.value; update(); } }), " to ",
        h("input", { type: "number", min: m.first_year, max: m.last_year, value: st.to, "aria-label": "To year", onchange: ev => { st.to = ev.target.value; update(); } }))),
      h("div", {}, h("label", { class: "lbl", for: "m-min" }, "Fill a dot when it has at least"),
        sel("min", [["6", "6 readable meetings"], ["12", "12 readable meetings"], ["24", "24 readable meetings"], ["48", "48 readable meetings"]], st.min, { id: "m-min", onchange: ev => { st.min = ev.target.value; update(); } }))),
    d.measures ? null : pending("Dots are filled where a district has enough readable meetings; colour by share appears with the measures."),
    figure({ title: d.measures ? `Share of readable meetings mentioning ${d.label}, ${st.from}–${st.to}` : `Districts with at least ${d.min} readable meetings, ${st.from}–${st.to}`,
      sub: "One dot per district at its central office, sized by enrollment. Large, sparse West Texas districts do not dominate as they would on a shaded map.",
      chart: box, legend: d.measures ? ramp : legendList([["readable", `At least ${d.min} readable meetings`], ["hollow", `Fewer than ${d.min}`], ["none", "No record found"]]),
      caption: `Colour bins are quintiles of the districts drawn filled. Rings mark shootings in Texas. Office points: TEA; enrollment: NCES Common Core of Data 2022; outline: US Census Bureau. ${STANDING}`,
      table: { columns: ["District", "County", "Enrollment", "Readable meetings", "Mentions", "Share"],
        rows: d.districts.slice().sort((a, b) => a.school_district.localeCompare(b.school_district)).map(x => [x.school_district, x.county_name, x.enrollment ? num(x.enrollment) : "–", num(x.meetings), d.measures ? num(x.yes) : "–", shareText(x.share)]) },
      csv: `api/map?${qs({ ...st, format: "csv" })}` }),
    d.measures && top.length ? [h("h3", {}, "Highest shares"), h("ol", { class: "col" }, top.slice(0, 10).map(x => h("li", {}, h("a", { href: `#/board/${x.tea_id}` }, x.school_district), ` · ${PCT(x.share)} of ${num(x.meetings)} meetings`)))] : null,
    asOf(m)));
}

// ------------------------------------------------------------------ search
async function pageSearch(token, _parts, params) {
  const m = await meta();
  if (!districtNames) districtNames = (await api("districts")).items.map(x => [x.tea_id, x.school_district]).sort((a, b) => a[1].localeCompare(b[1]));
  if (token !== routeToken) return;
  document.title = "Search the minutes · Texas school boards";
  const st = { q: "", scope: m.passages ? "passages" : "full", from: "", to: "", tea_id: "", subtopic: "", tone: "", detail: "", ...Object.fromEntries(params) };
  const results = h("div", { "aria-live": "polite" });
  const input = h("input", { type: "search", name: "q", value: st.q, placeholder: "guardian program, vestibule, Uvalde…", "aria-label": "Search the minutes", minlength: 3 });
  const form = h("form", { role: "search", onsubmit: ev => {
    ev.preventDefault();
    const f = new FormData(form);
    for (const k of ["q", "scope", "from", "to", "tea_id", "subtopic", "tone", "detail"]) st[k] = f.get(k) || "";
    location.hash = `#/search?${qs(st)}`;
  } },
    h("div", { class: "searchbar" }, input, h("button", { class: "primary", type: "submit" }, "Search")),
    h("div", { class: "ev-controls" },
      h("div", {}, h("span", { class: "lbl" }, "Search in"),
        radio("scope", "passages", m.passages ? st.scope : "full", "Safety and temperament passages", null),
        radio("scope", "full", st.scope, "Full text of every document (slower)", null)),
      h("div", {}, h("label", { class: "lbl", for: "s-district" }, "District"), sel("tea_id", [["", "All districts"], ...districtNames], st.tea_id, { id: "s-district" })),
      h("div", {}, h("span", { class: "lbl" }, "Years"), h("div", { class: "years" },
        h("input", { type: "number", name: "from", min: m.first_year, max: m.last_year, value: st.from, placeholder: String(m.first_year), "aria-label": "From year" }), " to ",
        h("input", { type: "number", name: "to", min: m.first_year, max: m.last_year, value: st.to, placeholder: String(m.last_year), "aria-label": "To year" }))),
      h("div", {}, h("label", { class: "lbl", for: "s-detail" }, "Document type"), sel("detail", [["", "Any"], ["Minutes", "Minutes"], ["Agenda", "Agenda"], ["Projector", "Projector"], ["Other", "Other"]], st.detail, { id: "s-detail" })),
      m.measures ? h("div", {}, h("label", { class: "lbl", for: "s-sub" }, "Subtopic"), sel("subtopic", [["", "Any"], ...Object.entries(m.subtopics)], st.subtopic, { id: "s-sub" })) : null,
      m.measures ? h("div", {}, h("label", { class: "lbl", for: "s-tone" }, "Tone"), sel("tone", [["", "Any"], ["negative", "Negative"], ["positive", "Positive"], ["mixed", "Mixed"]], st.tone, { id: "s-tone" })) : null));
  const passagesRadio = form.querySelector('input[value="passages"]');
  if (passagesRadio && !m.passages) passagesRadio.disabled = true;
  render(h("div", { class: "r" }, h("p", { class: "over" }, "Search"), h("h1", {}, "Search the minutes"),
    h("p", { class: "col" }, "Check any claim on these pages yourself. Results quote the extracted text and link to the original document.",
      m.passages ? "" : " Passages are not computed yet, so search reads the full text of every document; it can take a few seconds."),
    form, results, asOf(m)));
  if (st.q.length >= 3) await searchResults(results, st, 0, token);
  else input.focus();
}
async function searchResults(box, st, offset, token) {
  if (!offset) box.replaceChildren(h("p", { class: "muted" }, "Searching…"));
  let r;
  try { r = await api("search", { ...st, offset }); } catch (err) { box.replaceChildren(h("p", { class: "bad-text" }, String(err.message))); return; }
  if (token !== routeToken) return;
  const ol = offset ? box.querySelector("ol.results") : h("ol", { class: "results" });
  if (!offset) box.replaceChildren(h("p", { class: "cap" }, r.items.length ? `${r.more ? "More than " : ""}${num(offset + r.items.length)} result${r.items.length === 1 ? "" : "s"}, newest first · ${r.seconds} s`
    : `No results for “${st.q}”. ${st.scope === "passages" ? "Try a shorter word, or search the full text of every document." : "Try a shorter word or a wider range of years."}`), ol,
    h("div", { class: "fig-tools" }, h("a", { href: `api/search?${qs({ ...st, format: "csv" })}` }, "Download these results (CSV)")));
  box.querySelector(".pager")?.remove();
  for (const it of r.items) ol.appendChild(h("li", {},
    h("div", { class: "ctx" }, highlight(it.snippet, st.q)),
    h("div", { class: "src" }, h("a", { href: `#/board/${it.tea_id}` }, it.school_district || it.tea_id), ` · ${dateLabel(it.date)} · ${it.detail || "document"} · ${TEXT_LABELS[it.text_status] || it.text_status} · `,
      h("a", { href: `#/record/${it.record_id}` }, "full text"), it.source_link ? [" · ", h("a", { href: it.source_link, target: "_blank", rel: "noopener noreferrer" }, "source ↗")] : null)));
  if (r.more) box.appendChild(h("div", { class: "pager" }, h("button", { type: "button", onclick: () => searchResults(box, st, offset + 20, token) }, "Show more")));
}

// ------------------------------------------------------------------ method
async function pageMethod(token) {
  const [m, d] = await Promise.all([meta(), api("method")]);
  if (token !== routeToken) return;
  document.title = "Method and coverage · Texas school boards";
  const defs = d.definitions || {};
  const saf = defs.safety || {}, tem = defs.temperament || {}, evs = defs.events || {}, thr = defs.thresholds || {};
  const terms = list => h("p", { class: "terms-list" }, (list || []).join(" · "));
  const q = (title, ...body) => h("details", {}, h("summary", {}, title), h("div", {}, body));
  const cal = d.calibration;
  const calRows = cal ? Object.entries(cal.questions || cal).filter(([, v]) => v && typeof v === "object") : [];
  const ocr = d.ocr;
  const states = d.doc_states || {};
  const totalDocs = Object.values(states).reduce((a, b) => a + b, 0);
  render(h("div", { class: "r" }, h("p", { class: "over" }, "Method and coverage"), h("h1", {}, "How we built this"),
    h("p", { class: "lede" }, "Common questions about what the measures mean, which meetings are counted, and what the records cannot show."),
    h("p", { class: "cap" }, `Definitions version ${defs.version || "–"} (code/measures/definitions.yaml). ${m.measures ? `Measures computed ${m.measures_as_of}.` : "Measures not computed yet."} Data as of ${m.data_as_of}.`),
    h("div", { class: "faq" },
      q("What counts as a safety mention?",
        h("p", {}, saf.description || ""),
        h("p", {}, "A meeting is marked when at least one passage is judged to be about one of these subtopics:"),
        h("dl", {}, Object.entries(saf.subtopics || {}).map(([k, v]) => [h("dt", {}, v.label || k), h("dd", {}, terms(v.terms))])),
        h("p", {}, "General terms that can also make a passage a candidate:"), terms(saf.general_terms),
        h("p", {}, "Senses that never count as safety in this version (kept as separate flags):"),
        h("dl", {}, Object.entries(saf.excluded_senses || {}).map(([k, v]) => [h("dt", {}, k.replace(/_/g, " ")), h("dd", {}, terms(v))])),
        h("p", {}, "The question each candidate passage is asked:"), h("blockquote", { class: "quote" }, saf.question || "")),
      q("What counts as temperament?",
        h("p", {}, tem.description || ""),
        h("dl", {}, h("dt", {}, "Negative terms"), h("dd", {}, terms(tem.terms?.negative)), h("dt", {}, "Positive terms"), h("dd", {}, terms(tem.terms?.positive)),
          h("dt", {}, "Ignored phrases"), h("dd", {}, terms(tem.ignore_phrases))),
        h("p", {}, "Tone is one of negative, positive, mixed or none; who is students, staff, both or none."),
        h("blockquote", { class: "quote" }, tem.questions?.mention || "")),
      q("Which shootings are included, and what counts as nearby?",
        h("p", {}, "Events, with the dates used to align meetings. Nearby districts are those whose central office (TEA) is within the chosen radius of the town where the shooting happened, or in the same county. Town coordinates are approximate. Events outside Texas compare all Texas districts before and after."),
        h("ul", {}, m.events.map(e => h("li", {}, `${e.label}: ${dateLabel(e.date)}${e.in_texas ? ` (${e.county})` : " (outside Texas)"}`))),
        h("p", {}, "A meeting refers to an event when a passage names it or responds to it (for example a moment of silence). “Uvalde” is also a district and county name, so it counts only when the passage is about the shooting.")),
      q("What does a closed security session mean, and why does absence of discussion not mean absence of concern?",
        h("p", {}, "Texas Government Code section 551.076 lets a board deliberate in closed session on deploying security personnel or devices, and section 551.089 on security audits and assessments. Agendas must cite the section, so we flag meetings that do. What is said in those sessions is not in the public minutes."),
        h("p", {}, "So a meeting with no safety mention may still have discussed safety in private. Every share on these pages counts public discussion only, and is a lower bound for discussion of any kind.")),
      q("How do yes, no and uncertain work?",
        h("p", {}, "Each yes-or-no question gives a probability. Three zones turn it into a label:"),
        h("dl", {}, Object.entries(thr).filter(([, v]) => typeof v === "object").map(([k, v]) => [h("dt", {}, k), h("dd", {}, `yes at ${v.yes} or above, no at ${v.no} or below, uncertain in between`)])),
        h("p", {}, "Uncertain meetings are not counted as mentions; charts report how many there were. Starting thresholds are replaced by values calibrated on the hand-checked sample.")),
      q("How accurate is the classifier?",
        calRows.length ? [h("p", {}, `Against the hand-checked sample (${d.calibration_path}):`),
          dataTable({ columns: ["Question", ...Object.keys(calRows[0][1])], rows: calRows.map(([k, v]) => [k, ...Object.values(v).map(x => typeof x === "number" ? (x <= 1 && x > 0 && !Number.isInteger(x) ? x.toFixed(2) : num(x)) : String(x))]) })]
          : h("p", {}, "Not measured yet. Precision and recall per question will appear here from gold/calibration.json once the hand-checked sample is coded.")),
      q("Which meetings are in the denominator?",
        h("p", {}, "The unit is a board meeting, not a document: agendas and minutes of the same meeting count once. The measured document is the minutes when they can be read, else the agenda, else the BoardBook projector view. A meeting is readable when that document has text (extracted, or read by OCR)."),
        h("p", {}, `Shares divide meetings with a mention by readable meetings that were screened. ${d.chip.not_screened ? `${num(d.chip.not_screened)} readable meetings are not screened yet.` : ""} Months with fewer than ${d.thin_meetings} readable meetings are drawn dashed; event rows with fewer than ${d.min_event_districts} districts are marked thin.`)),
      q("What can't we read yet? (coverage and OCR)",
        h("p", {}, `Of ${num(totalDocs)} documents, ${num(states.readable || 0)} have text, ${num(states.scan || 0)} are scanned images waiting for OCR, ${num(states.unavailable || 0)} are listed but unavailable, and ${num(states.recording || 0)} are recordings (links only). ${PCT(d.chip.minutes_unread_share)} of minutes are unread scans.`),
        ocr ? h("p", {}, `OCR progress: ${ocr.done !== undefined ? `${num(ocr.done)} of ${num(ocr.total)} documents` : JSON.stringify(ocr)}.`) : null,
        h("p", {}, "Coverage is uneven: most districts post only recent years, larger districts post more, and small districts post more scans. Unread scans and missed wording can only add mentions, so counts are lower bounds. When OCR finishes, numbers will change; this page gives the date of the data."),
        h("p", {}, h("a", { href: "#/coverage" }, "See coverage for every district and year"), ".")),
      q("Why is a district hollow on the map?",
        h("p", {}, "It has fewer readable meetings in the chosen years than the threshold you set (12 by default), so a share would be too noisy to colour. A dotted outline means no records were found for it online.")),
      q("How are the words that changed chosen?",
        h("p", {}, "For each event we take the measured document of every meeting in nearby districts in the window, split into before and after, and count the documents containing each word. Words are ranked by the weighted log-odds ratio with an informative Dirichlet prior (Monroe, Colaresi and Quinn 2008, “Fightin’ Words”), using the pooled before-and-after counts as the prior, and its z-score."),
        h("p", {}, "Filters: common English words, months and weekdays are removed; a word must appear in at least 15% of the districts on the side it favours (at least 3); words almost never written in lower case, mostly people's names, are dropped. Select a word to read every passage. A word that changes is not evidence of why it changed.")),
      q("Assumptions in the current definitions",
        d.assumptions.length ? d.assumptions.map(a => h("div", { class: "assumption" }, h("p", { class: "over" }, (a.path || "general").replace(/\.label$/, "").replace(/_/g, " ").split(".").join(" › ")), h("p", {}, a.text))) : h("p", {}, "None listed.")),
      q("Can I download the data?",
        h("ul", {},
          h("li", {}, h("a", { href: "api/panel?format=csv" }, "Event × district × meeting panel (CSV)"), " · ", h("a", { href: "api/panel?format=parquet" }, "Parquet"), ": every meeting within 12 months of every event, with distances and measures, for R or Stata."),
          d.files.meetings ? h("li", {}, h("a", { href: "api/file?name=meetings" }, "meetings.parquet"), ": one row per meeting with every measure and excerpt.") : h("li", {}, "meetings.parquet: not computed yet."),
          d.files.passages ? h("li", {}, h("a", { href: "api/file?name=passages" }, "passages.parquet"), ": candidate passages.") : null,
          h("li", {}, h("a", { href: "api/file?name=coverage" }, "coverage.parquet"), " and ", h("a", { href: "api/coverage_grid?format=csv" }, "coverage by district and year (CSV)"), "."),
          h("li", {}, h("a", { href: "api/story?format=csv" }, "Statewide share by year (CSV)"), "."),
          h("li", {}, "Every chart has its own CSV link with the exact rows and the active rule.")),
        h("p", {}, "Column meanings are in code/measures/SCHEMA.md.")),
      q("Who is named, and how are quotes edited?",
        h("p", {}, "Excerpts are verbatim from the extracted text, at most 280 characters, with only spacing normalised. Names of students and private citizens are redacted in the measures; board members and staff are public officials. Full documents link to the district's own published copy."))),
    h("p", { class: "hedge" }, `Accurate as of ${m.data_as_of}. These are records we could collect and read; they are not every meeting held. Corrections: tell the research team, and corrected figures will be dated on this page.`)));
}

window.RESEARCH_PAGES = { story: pageStory, explore: pageExplore, coverage: pageCoverage, events: pageEvents, event: pageEvents,
  board: pageBoard, map: pageMap, search: pageSearch, method: pageMethod };
