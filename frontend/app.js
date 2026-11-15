// TreeScout frontend: plan a hike, render every section of the plan, draw it on the map.
// Every field is treated as optional: a missing or `available: false` section shows "Unavailable".
"use strict";

const DEFAULT_CENTER = [42.2411, -83.6130]; // Ypsilanti, MI
const KM_PER_MI = 1.609344;
const TRACE_STEP_MS = 300;
const PREFS = ["easy", "hilly", "shaded", "dog-friendly"];
const AGENTS = { planner: "Planner", checker: "Safety checker" };
const TRACE_STEPS = {
  tool:   { icon: "⚙", label: "Tool call" },
  reject: { icon: "✕", label: "Rejected" },
  decide: { icon: "✓", label: "Decision" },
};
// NASA EONET category ids (lowercased, spaces removed) -> icon
const HAZARD_ICONS = {
  wildfires: "🔥", severestorms: "🌀", floods: "🌊", volcanoes: "🌋", earthquakes: "〰️",
  landslides: "⛰️", snow: "❄️", sealakeice: "🧊", dusthaze: "🌫️", drought: "☀️",
  tempextremes: "🌡️", watercolor: "💧", manmade: "⚠️",
};
const GIBS_URL =
  "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/MODIS_Terra_CorrectedReflectance_TrueColor/default/{time}/GoogleMapsCompatible_Level9/{z}/{y}/{x}.jpg";

const $ = (sel) => document.querySelector(sel);
const els = {
  status: $("#status"),
  modeBadges: $("#mode-badges"),
  form: $("#plan-form"),
  request: $("#request"),
  distance: $("#distance"),
  distanceOut: $("#distance-out"),
  date: $("#date"),
  start: $("#start"),
  prefs: $("#prefs"),
  useMock: $("#use-mock"),
  parsedBy: $("#parsed-by"),
  button: $("#plan-btn"),
  results: $("#results"),
  hazardsBody: $("#hazards-body"),
  hazardCount: $("#hazard-count"),
  sourcesBody: $("#sources-body"),
  gibsLabel: $("#gibs-label"),
};

/* ==========================================================================
   Small helpers
   ========================================================================== */

const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const toNum = (v) => (v === null || v === undefined || v === "" ? NaN : Number(v));
const str = (v) => (typeof v === "string" && v.trim() ? v.trim() : typeof v === "number" && Number.isFinite(v) ? String(v) : null);

/** A plan section, or null when it is missing or marked available:false. */
function section(plan, key) {
  const s = plan[key];
  return isObj(s) && s.available !== false ? s : null;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v === null || v === undefined) continue;
    if (k === "on") for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
    else node.setAttribute(k, v === true ? "" : v);
  }
  node.append(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

/** replaceChildren that skips null/false/undefined (which replaceChildren would print as text). */
function fill(node, ...children) {
  node.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
}

function kmMi(km, digits = 1) {
  const n = toNum(km);
  if (!Number.isFinite(n)) return null;
  return `${n.toFixed(digits)} km · ${(n / KM_PER_MI).toFixed(digits)} mi`;
}

function fmtUnit(v, digits, unit) {
  const n = toNum(v);
  return Number.isFinite(n) ? `${n.toFixed(digits)}${unit}` : null;
}

function fmtDuration(min) {
  const n = Math.round(toNum(min));
  if (!Number.isFinite(n) || n < 0) return null;
  const h = Math.floor(n / 60), m = n % 60;
  return h ? `${h} h ${String(m).padStart(2, "0")} min` : `${m} min`;
}

function cToF(c) {
  const n = toNum(c);
  return Number.isFinite(n) ? `${n.toFixed(1)} °C · ${(n * 9 / 5 + 32).toFixed(0)} °F` : null;
}

/** ISO timestamp -> local date + time, e.g. "Nov 14, 2026, 3:14 PM". */
function fmtLocal(iso) {
  if (!str(iso)) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

/** "YYYY-MM-DD" -> local calendar date, e.g. "Sun, Nov 15". */
function fmtDay(ymd) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(str(ymd) || "");
  if (!m) return null;
  return new Date(+m[1], +m[2] - 1, +m[3]).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}

function isoDay(date) {
  const p = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())}`;
}

function haversineKm([lat1, lon1], [lat2, lon2]) {
  const r = Math.PI / 180;
  const h = Math.sin(((lat2 - lat1) * r) / 2) ** 2 +
    Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin(((lon2 - lon1) * r) / 2) ** 2;
  return 6371.0088 * 2 * Math.asin(Math.min(1, Math.sqrt(h)));
}

function toLatLon(lat, lon) {
  const a = toNum(lat), b = toNum(lon);
  return Number.isFinite(a) && Number.isFinite(b) && Math.abs(a) <= 90 && Math.abs(b) <= 180 ? [a, b] : null;
}

const capitalize = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

/* ==========================================================================
   Backend health
   ========================================================================== */

async function checkHealth() {
  try {
    const response = await fetch("/health");
    if (!response.ok) throw new Error("Health check failed");
    const result = await response.json();
    setStatus(result.ok ? "ok" : "bad", result.ok ? "Backend connected" : "Backend unavailable");
  } catch {
    setStatus("bad", "Unable to connect to the backend. Please try again.");
  }
}

function setStatus(kind, text) {
  els.status.className = `status state state--${kind}`;
  els.status.textContent = text;
}

/* ==========================================================================
   Tabs (ARIA tabs with arrow-key navigation)
   ========================================================================== */

const tabs = [...document.querySelectorAll('[role="tab"]')];

function selectTab(tab, focus = false) {
  for (const t of tabs) {
    const on = t === tab;
    t.setAttribute("aria-selected", String(on));
    t.tabIndex = on ? 0 : -1;
    document.getElementById(t.getAttribute("aria-controls")).hidden = !on;
  }
  if (focus) tab.focus();
}

function initTabs() {
  for (const t of tabs) {
    t.addEventListener("click", () => selectTab(t));
    t.addEventListener("keydown", (e) => {
      const i = tabs.indexOf(t);
      const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
      if (next === undefined) return;
      e.preventDefault();
      selectTab(tabs[(next + tabs.length) % tabs.length], true);
    });
  }
}

/* ==========================================================================
   Form: distance slider, date, start time, preference chips
   ========================================================================== */

function updateDistanceOut() {
  els.distanceOut.textContent = kmMi(els.distance.value);
}

function setPref(chip, on) {
  chip.setAttribute("aria-pressed", String(on));
}

// Form controls the user changed since the last plan loaded. Only these are sent to /plan as
// explicit fields (which override the text); everything else is left to the request text.
// Setting .value from code fires no input events, so fillForm() never marks anything.
const touched = new Set();

function initForm() {
  const tomorrow = new Date();
  tomorrow.setDate(tomorrow.getDate() + 1);
  els.date.value = isoDay(tomorrow);
  updateDistanceOut();
  els.distance.addEventListener("input", () => { touched.add("distance_km"); updateDistanceOut(); });
  for (const [control, field] of [[els.date, "date"], [els.start, "start"]]) {
    control.addEventListener("input", () => touched.add(field));
    control.addEventListener("change", () => touched.add(field));
  }
  els.prefs.addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    setPref(chip, chip.getAttribute("aria-pressed") !== "true");
    touched.add("prefs");
  });
}

/** Explicit form fields for POST /plan: only the controls the user changed since the last plan. */
function formValues() {
  const values = {};
  if (touched.has("distance_km")) values.distance_km = Number(els.distance.value);
  if (touched.has("date") && els.date.value) values.date = els.date.value;
  if (touched.has("start") && els.start.value) values.start = els.start.value;
  if (touched.has("prefs")) {
    values.prefs = [...els.prefs.querySelectorAll('.chip[aria-pressed="true"]')].map((c) => c.dataset.pref);
  }
  return values;
}

/** Fill the form from plan.constraints, ignoring anything malformed. A loaded plan resets "touched". */
function fillForm(constraints) {
  touched.clear();
  els.parsedBy.hidden = true;
  if (!isObj(constraints)) return;

  const km = toNum(constraints.distance_km);
  if (Number.isFinite(km)) {
    els.distance.value = String(Math.min(20, Math.max(1, km)));
    updateDistanceOut();
  }
  if (fmtDay(constraints.date)) els.date.value = constraints.date;
  const start = str(constraints.start);
  if (start && /^\d{2}:\d{2}/.test(start)) els.start.value = start.slice(0, 5);
  if (Array.isArray(constraints.prefs)) {
    const wanted = new Set(constraints.prefs.map((p) => String(p).toLowerCase()));
    for (const chip of els.prefs.querySelectorAll(".chip")) setPref(chip, wanted.has(chip.dataset.pref));
  }

  const by = str(constraints.parsed_by);
  if (by) {
    const name = { gemini: "Gemini", rules: "rules" }[by.toLowerCase()] || by;
    els.parsedBy.replaceChildren(el("span", { class: "parsed-by-icon", "aria-hidden": "true" }, "✓"), ` Understood by ${name}`);
    els.parsedBy.hidden = false;
  }
}

/* ==========================================================================
   Map: CARTO dark + NASA GIBS bases, route, hazard markers
   ========================================================================== */

let map = null;
let routeLayer = null;
let hazardLayer = null;

function gibsDay() {
  const d = new Date();
  d.setDate(d.getDate() - 1); // yesterday in the viewer's local calendar: that MODIS pass is complete
  return isoDay(d);
}

function initMap() {
  if (typeof L === "undefined") {
    showError("The map library (Leaflet) could not be loaded. Check your internet connection and reload.");
    return;
  }
  map = L.map("map", { zoomControl: true }).setView(DEFAULT_CENTER, 11);

  // CARTO dark_all tiles now return an "API KEY REQUIRED" placeholder, and keys must not live in
  // frontend code. Until there is a backend tile proxy, use OSM tiles darkened by CSS (.tiles-dark).
  const darkBase = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    className: "tiles-dark",
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  });

  const gibsDate = gibsDay();
  const gibs = L.tileLayer(GIBS_URL, {
    time: gibsDate,
    maxNativeZoom: 9,
    maxZoom: 19,
    attribution: 'Imagery: <a href="https://earthdata.nasa.gov/gibs">NASA GIBS</a> · MODIS Terra',
  });
  els.gibsLabel.textContent = `NASA GIBS · MODIS Terra true color · ${gibsDate}`;

  darkBase.addTo(map);
  routeLayer = L.layerGroup().addTo(map);
  hazardLayer = L.layerGroup().addTo(map);

  L.control.layers(
    { "Dark map (OpenStreetMap)": darkBase, "Satellite (NASA GIBS)": gibs },
    { "Route": routeLayer, "Hazards (NASA EONET)": hazardLayer },
    { position: "topright" },
  ).addTo(map);

  map.on("baselayerchange", (e) => { els.gibsLabel.hidden = e.layer !== gibs; });
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function divIcon(cls, glyph, size = 24) {
  return L.divIcon({
    className: "",
    html: `<span class="marker ${cls}" aria-hidden="true">${glyph}</span>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
    popupAnchor: [0, -size / 2],
  });
}

function drawTrail(coords, name) {
  if (!map) return;
  routeLayer.clearLayers();
  if (!coords) return;

  // Both lines use the exact same coordinate array, so the route begins at coords[0],
  // the same point as the S marker. smoothFactor 0 stops Leaflet from simplifying points away.
  const opts = { opacity: 1, smoothFactor: 0, interactive: false };
  const casing = L.polyline(coords, { ...opts, className: "route-line--casing", color: cssVar("--bg"), weight: 10 });
  const line = L.polyline(coords, { ...opts, className: "route-line", color: cssVar("--accent"), weight: 6 });
  routeLayer.addLayer(casing).addLayer(line);

  const start = coords[0];
  const end = coords[coords.length - 1];
  const loop = haversineKm(start, end) < 0.05;
  if (!loop) {
    L.marker(end, { icon: divIcon("marker--end", "E"), title: `Finish: ${name}` })
      .bindTooltip("Finish").addTo(routeLayer);
  }
  // Added last with a z-index boost so S is always drawn on top (on a loop, S and E share a point).
  L.marker(start, {
    icon: divIcon("marker--start", "S"),
    title: loop ? `Start and finish (loop): ${name}` : `Start: ${name}`,
    zIndexOffset: 1000,
  }).bindTooltip(loop ? "Start & finish (loop)" : "Start").addTo(routeLayer);

  map.fitBounds(line.getBounds(), { padding: [40, 40] });
}

/* ==========================================================================
   Fetching
   ========================================================================== */

let requestSeq = 0; // ignore responses from superseded requests

async function fetchPlan(text, useMock) {
  const response = useMock
    ? await fetch("/mock_plan.json", { cache: "no-store" })
    : await fetch("/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request: text, lat: DEFAULT_CENTER[0], lon: DEFAULT_CENTER[1], ...formValues() }),
      });

  let body = null;
  try { body = await response.json(); } catch { /* non-JSON body */ }

  if (!response.ok) {
    const d = body && body.detail;
    const detail = typeof d === "string" ? d
      : Array.isArray(d) ? d.map((x) => (isObj(x) && x.msg) || String(x)).join("; ")
      : response.statusText;
    const where = useMock ? "/mock_plan.json" : "/plan";
    throw new Error(`${where} returned ${response.status}${detail ? `: ${detail}` : ""}`);
  }
  if (!isObj(body)) throw new Error("The server response was not a plan object.");
  return body;
}

function validCoords(trail) {
  if (!isObj(trail) || !Array.isArray(trail.coords)) return null;
  const coords = trail.coords.map((p) => (Array.isArray(p) ? toLatLon(p[0], p[1]) : null)).filter(Boolean);
  return coords.length >= 2 ? coords : null;
}

async function onPlan(event) {
  event.preventDefault();
  const text = els.request.value.trim();
  if (!text) {
    showError("Type a hike request first.");
    els.request.focus();
    return;
  }

  const mock = els.useMock.checked;
  const seq = ++requestSeq;
  setLoading(true, mock);
  try {
    const plan = await fetchPlan(text, mock);
    if (seq !== requestSeq) return;
    renderPlan(plan, mock);
  } catch (err) {
    if (seq !== requestSeq) return;
    console.error(err);
    const offline = err instanceof TypeError && /fetch/i.test(err.message);
    showError(offline ? "Could not reach the server. Is it running?" : err.message);
  } finally {
    if (seq === requestSeq) setLoading(false);
  }
}

/* ==========================================================================
   Rendering: the whole plan
   ========================================================================== */

let traceTimer = null;

function renderPlan(plan, mock) {
  clearTimeout(traceTimer);
  fillForm(plan.constraints);

  const coords = validCoords(plan.trail);
  const trailName = (isObj(plan.trail) && str(plan.trail.name)) || "Trail";
  drawTrail(coords, trailName);

  const hazards = renderHazards(plan, coords ? coords[0] : DEFAULT_CENTER);
  renderSources(plan);
  renderModeBadges(plan, mock);

  fill(els.results,
    warningsBanner(plan.warnings),
    trailCard(plan, coords),
    timingCard(plan),
    weatherCard(plan),
    climateCard(plan),
    spaceWeatherCard(plan),
    alternativesCard(plan),
    reportsCard(plan),
    hazardsSummary(hazards),
    traceCard(plan),
  );
}

function setLoading(on, mock) {
  els.button.disabled = on;
  els.results.setAttribute("aria-busy", String(on));
  if (on) {
    clearTimeout(traceTimer);
    els.results.replaceChildren(
      el("div", { class: "loading" },
        el("span", { class: "spinner", "aria-hidden": "true" }),
        el("span", {}, mock ? "Loading mock plan…" : "Planning… finding trails can take up to a minute.")),
    );
  }
}

function showError(message) {
  els.results.setAttribute("aria-busy", "false");
  els.results.replaceChildren(
    el("div", { class: "error-box", role: "alert" }, el("strong", {}, "Error: "), el("span", {}, message)),
  );
}

function renderModeBadges(plan, mock) {
  const partial = ["trail", "timing", "weather", "climate", "space_weather", "checker"]
    .some((k) => isObj(plan[k]) && plan[k].available === false);
  fill(els.modeBadges,
    el("span", { class: `badge ${mock ? "badge--mock" : "badge--live"}` }, mock ? "Mock data" : "Live"),
    partial && el("span", { class: "badge badge--partial", title: "At least one data source returned available: false" }, "Partial data"),
  );
}

/* ---------- Card building blocks ---------- */

function sourceLine(source, date) {
  return el("p", { class: "source-line" }, `Source: ${source || "unknown"}`, date ? ` · ${date}` : null);
}

function unavailable(why = "Unavailable") {
  return el("p", { class: "unavailable" }, el("span", { "aria-hidden": "true" }, "⊘ "), why);
}

function card(title, { body, source, date, cls = "" }) {
  return el("article", { class: `card ${cls}` },
    el("header", { class: "card-head" }, el("h2", { class: "card-title" }, title)),
    body ?? unavailable(),
    sourceLine(source, date));
}

/** rows: [label, value, {highlight}]; a null value renders as an em dash. */
function kv(rows) {
  return el("dl", { class: "kv" }, rows.map(([label, value, opts = {}]) => [
    el("dt", { class: opts.highlight ? "kv-hl" : null }, label),
    el("dd", { class: opts.highlight ? "kv-hl" : null }, value ?? "—"),
  ]));
}

/* ---------- Plan-tab cards ---------- */

function warningsBanner(warnings) {
  const list = (Array.isArray(warnings) ? warnings : [])
    .map((w) => str(w) || (isObj(w) && (str(w.text) || str(w.message) || str(w.summary))))
    .filter(Boolean);
  if (!list.length) return null;
  return el("div", { class: "banner banner--bad", role: "alert" },
    el("span", { class: "banner-icon", "aria-hidden": "true" }, "⚠"),
    el("div", {},
      el("strong", {}, list.length === 1 ? "Warning" : `${list.length} warnings`),
      el("ul", {}, list.map((w) => el("li", {}, w)))));
}

function trailCard(plan, coords) {
  const t = section(plan, "trail");
  const explanation = str(plan.explanation);
  const body = t && el("div", {},
    el("p", { class: "trail-name" }, str(t.name) || "Unnamed trail"),
    kv([
      ["Distance", kmMi(t.distance_km)],
      ["Elevation gain", fmtUnit(t.elevation_gain_m, 0, " m")],
      ["Route points", coords ? String(coords.length) : null],
    ]),
    explanation && el("p", { class: "explanation" }, explanation),
    !coords && el("p", { class: "unavailable" }, "No usable route coordinates, so nothing is drawn on the map."));
  const osm = t && Number(t.osm_id) > 0 ? `OSM id ${t.osm_id}` : null;
  return card("Trail", {
    body,
    source: "OpenStreetMap",
    date: [osm, t && t.cached === true ? "cached copy" : null].filter(Boolean).join(" · ") || null,
  });
}

function timingCard(plan) {
  const t = section(plan, "timing");
  const body = t && kv([
    ["Start", str(t.start)],
    ["Est. duration", fmtDuration(t.est_duration_min)],
    ["Sunset", str(t.sunset)],
    ["Latest safe start", str(t.latest_safe_start)],
    ["Turnaround by", str(t.turnaround_by), { highlight: true }],
  ]);
  const day = isObj(plan.constraints) ? fmtDay(plan.constraints.date) : null;
  return card("Timing", { body, source: (t && str(t.source)) || "TreeScout planner", date: day && `for ${day}` });
}

function weatherCard(plan) {
  const w = section(plan, "weather");
  const pct = w ? toNum(w.precip_chance) : NaN;
  const body = w && kv([
    ["Forecast", str(w.summary)],
    ["Precipitation chance", Number.isFinite(pct) ? `${Math.round(pct)}%` : null],
  ]);
  const day = isObj(plan.constraints) ? fmtDay(plan.constraints.date) : null;
  const date = (w && fmtLocal(w.checked_at)) || (day && `forecast for ${day}`);
  return card("Weather", { body, source: (w && str(w.source)) || "NWS", date });
}

function climateCard(plan) {
  const c = section(plan, "climate");
  const mm = c ? fmtUnit(c.avg_precip_mm, 1, " mm/day") : null;
  const body = c && kv([
    ["Avg high", cToF(c.avg_high_c)],
    ["Avg low", cToF(c.avg_low_c)],
    ["Avg precipitation", mm],
  ]);
  const years = c && str(c.years);
  return card("Typical conditions this week", {
    body,
    source: (c && str(c.source)) || "NASA POWER",
    date: years && `${years.replace("-", "–")} averages`,
  });
}

function spaceWeatherCard(plan) {
  const s = section(plan, "space_weather");
  const kp = s ? toNum(s.kp_max) : NaN;
  const chance = s && str(s.aurora_chance);
  const body = s && kv([
    ["Aurora chance tonight", chance && capitalize(chance)],
    ["Max Kp index", Number.isFinite(kp) ? String(kp) : null],
  ]);
  return card("Space weather", {
    body,
    source: (s && str(s.source)) || "NASA DONKI",
    date: s && fmtLocal(s.checked_at) && `checked ${fmtLocal(s.checked_at)}`,
  });
}

function alternativesCard(plan) {
  const alts = Array.isArray(plan.alternatives) ? plan.alternatives.filter(isObj) : null;
  let body = null;
  if (alts && !alts.length) body = el("p", { class: "info-empty" }, "No other trails were close enough to suggest.");
  if (alts && alts.length) {
    const list = el("ul", { class: "alt-list" });
    for (const a of alts) {
      const score = toNum(a.score);
      const btn = el("button", { type: "button", class: "alt", "aria-pressed": "false" },
        el("span", { class: "alt-name" }, str(a.name) || "Unnamed trail"),
        el("span", { class: "alt-meta mono" },
          [kmMi(a.distance_km), Number.isFinite(score) ? `score ${score.toFixed(2)}` : null].filter(Boolean).join(" · ")),
        el("span", { class: "alt-selected" }, "✓ Selected"));
      btn.addEventListener("click", () => {
        const wasOn = btn.getAttribute("aria-pressed") === "true";
        for (const b of list.querySelectorAll(".alt")) b.setAttribute("aria-pressed", "false");
        btn.setAttribute("aria-pressed", String(!wasOn));
      });
      list.append(el("li", {}, btn));
    }
    body = list;
  }
  return card("Other options", { body, source: "OpenStreetMap", date: alts && alts.length ? "ranked by the planner" : null });
}

function reportsCard(plan) {
  if (!Array.isArray(plan.reports) || !plan.reports.length) return null; // hidden when empty
  const items = plan.reports.map((r) => {
    if (str(r)) return el("li", {}, str(r));
    if (!isObj(r)) return null;
    const meta = [str(r.source), fmtLocal(r.date || r.reported_at || r.checked_at)].filter(Boolean).join(" · ");
    return el("li", {},
      el("span", {}, str(r.title) || str(r.text) || str(r.summary) || "Report"),
      meta && el("span", { class: "source-line" }, ` ${meta}`));
  }).filter(Boolean);
  return card("Trail reports", {
    body: items.length ? el("ul", { class: "plain-list" }, items) : null,
    source: "Trail reports",
  });
}

function hazardsSummary(hazards) {
  if (!hazards || !hazards.length) return null;
  return el("button", { type: "button", class: "banner banner--warn banner-btn", on: { click: () => selectTab($("#tab-hazards")) } },
    el("span", { class: "banner-icon", "aria-hidden": "true" }, "▲"),
    el("span", {}, `${hazards.length} NASA EONET ${hazards.length === 1 ? "event" : "events"} nearby. Open the Hazards tab.`));
}

/* ---------- "How I planned this": agent trace replay ---------- */

function traceItem(step) {
  const type = TRACE_STEPS[step.step] || { icon: "•", label: capitalize(str(step.step) || "Step") };
  const agent = AGENTS[String(step.agent).toLowerCase()] || capitalize(str(step.agent) || "Agent");
  const ms = toNum(step.ms);
  const tool = str(step.tool);
  return el("li", { class: `trace-step trace-step--${TRACE_STEPS[step.step] ? step.step : "other"}` },
    el("span", { class: "trace-icon", "aria-hidden": "true" }, type.icon),
    el("div", { class: "trace-body" },
      el("div", { class: "trace-head" },
        el("strong", {}, agent), el("span", { class: "trace-type" }, type.label),
        tool && el("code", { class: "trace-tool" }, tool),
        Number.isFinite(ms) && el("span", { class: "trace-ms mono" }, `${Math.round(ms)} ms`)),
      el("p", { class: "trace-summary" }, str(step.summary) || "")));
}

function checkerLine(checker) {
  if (!isObj(checker) || checker.available === false) return el("p", { class: "unavailable" }, "Safety check result unavailable");
  const rounds = Math.round(toNum(checker.rounds));
  const after = Number.isFinite(rounds) && rounds > 0 ? ` after ${rounds} ${rounds === 1 ? "round" : "rounds"}` : "";
  if (checker.approved === true) return el("p", { class: "checker state state--ok" }, `Safety check passed${after}`);
  if (checker.approved === false) return el("p", { class: "checker state state--bad" }, `Safety check did not pass${after}`);
  return el("p", { class: "unavailable" }, "Safety check result unavailable");
}

function traceCard(plan) {
  const steps = Array.isArray(plan.agent_trace) ? plan.agent_trace.filter(isObj) : null;
  if (!steps || !steps.length) {
    return card("How I planned this", { body: null, source: "TreeScout agents" });
  }
  const list = el("ol", { class: "trace" });
  const checker = checkerLine(plan.checker);
  const replayBtn = el("button", { type: "button", class: "btn btn--small" }, "↻ Replay");

  const replay = () => {
    clearTimeout(traceTimer);
    list.replaceChildren();
    checker.hidden = true;
    replayBtn.disabled = true;
    let i = 0;
    const next = () => {
      if (i < steps.length) {
        list.append(traceItem(steps[i++]));
        traceTimer = setTimeout(next, TRACE_STEP_MS);
      } else {
        checker.hidden = false;
        replayBtn.disabled = false;
      }
    };
    next();
  };
  replayBtn.addEventListener("click", replay);

  const article = card("How I planned this", {
    body: el("div", {}, list, checker),
    source: "TreeScout agents",
    date: `${steps.length} ${steps.length === 1 ? "step" : "steps"}`,
  });
  article.querySelector(".card-head").append(replayBtn);
  queueMicrotask(replay);
  return article;
}

/* ==========================================================================
   Hazards tab + map markers
   ========================================================================== */

function hazardIcon(type) {
  const key = String(type || "").toLowerCase().replace(/[\s_-]+/g, "");
  return HAZARD_ICONS[key] || HAZARD_ICONS[key + "s"] || "⚠️";
}

/** Normalizes plan.hazards; returns the drawn hazard list (or null when unavailable). */
function renderHazards(plan, origin) {
  if (hazardLayer) hazardLayer.clearLayers();
  const checked = fmtLocal(plan.hazards_checked_at);
  const sourceDate = `Source: NASA EONET${checked ? ` · checked ${checked}` : ""}`;

  if (!Array.isArray(plan.hazards)) {
    els.hazardCount.textContent = "";
    els.hazardsBody.replaceChildren(unavailable("NASA EONET hazards unavailable"), el("p", { class: "source-line" }, sourceDate));
    return null;
  }

  const hazards = plan.hazards.filter(isObj).map((h) => {
    const at = toLatLon(h.lat, h.lon) || (Array.isArray(h.coords) ? toLatLon(h.coords[0], h.coords[1]) : null);
    let dist = toNum(h.distance_km);
    if (!Number.isFinite(dist) && at) dist = haversineKm(origin, at);
    const type = str(h.type) || str(h.category) || "Event";
    return {
      at, type,
      title: str(h.title) || str(h.name) || "Untitled event",
      icon: hazardIcon(type),
      date: fmtDay(h.date) || fmtLocal(h.date) || str(h.date),
      dist: Number.isFinite(dist) ? `${Math.round(dist)} km away` : null,
    };
  });

  els.hazardCount.textContent = hazards.length ? `(${hazards.length})` : "";

  if (!hazards.length) {
    els.hazardsBody.replaceChildren(
      el("p", { class: "state state--ok" },
        `No active NASA EONET events within 100 km (checked ${checked || "at an unknown time"})`));
    return hazards;
  }

  const list = el("ul", { class: "hazard-list" });
  for (const h of hazards) {
    let marker = null;
    if (map && h.at) {
      marker = L.marker(h.at, { icon: divIcon("marker--hazard", h.icon, 30), title: `${h.type}: ${h.title}`, zIndexOffset: 500 })
        .bindPopup(hazardPopup(h)).addTo(hazardLayer);
    }
    const meta = [capitalize(h.type), h.date, h.dist || (h.at ? null : "location unknown")].filter(Boolean).join(" · ");
    const item = el("button", { type: "button", class: "hazard", disabled: !marker },
      el("span", { class: "hazard-icon", "aria-hidden": "true" }, h.icon),
      el("span", { class: "hazard-text" }, el("strong", {}, h.title), el("span", { class: "hazard-meta" }, meta)));
    if (marker) item.addEventListener("click", () => { map.setView(h.at, Math.max(map.getZoom(), 8)); marker.openPopup(); });
    list.append(el("li", {}, item));
  }
  els.hazardsBody.replaceChildren(list, el("p", { class: "source-line" }, sourceDate));
  return hazards;
}

function hazardPopup(h) {
  return el("div", { class: "popup" },
    el("strong", { class: "popup-title" }, h.icon, " ", h.title),
    el("p", {}, capitalize(h.type)),
    el("p", { class: "source-line" }, "NASA EONET"),
    el("dl", { class: "kv" },
      el("dt", {}, "Date"), el("dd", {}, h.date || "—"),
      el("dt", {}, "Distance"), el("dd", {}, h.dist || "—")));
}

/* ==========================================================================
   Sources tab
   ========================================================================== */

function renderSources(plan) {
  const row = (what, source, ok, date) => el("tr", {},
    el("th", { scope: "row" }, what),
    el("td", {}, source),
    el("td", {}, el("span", { class: `state state--${ok ? "ok" : "bad"}` }, ok ? "Available" : "Unavailable")),
    el("td", { class: "mono" }, date || "—"));

  const s = (k) => section(plan, k);
  const day = isObj(plan.constraints) ? fmtDay(plan.constraints.date) : null;
  const rows = [
    row("Trail", "OpenStreetMap", !!validCoords(plan.trail), null),
    row("Timing", (s("timing") && str(s("timing").source)) || "TreeScout planner", !!s("timing"), day),
    row("Weather", (s("weather") && str(s("weather").source)) || "NWS", !!s("weather"), (s("weather") && fmtLocal(s("weather").checked_at)) || day),
    row("Climate", (s("climate") && str(s("climate").source)) || "NASA POWER", !!s("climate"), s("climate") && str(s("climate").years)),
    row("Space weather", (s("space_weather") && str(s("space_weather").source)) || "NASA DONKI", !!s("space_weather"), s("space_weather") && fmtLocal(s("space_weather").checked_at)),
    row("Hazards", "NASA EONET", Array.isArray(plan.hazards), fmtLocal(plan.hazards_checked_at)),
    row("Satellite map", "NASA GIBS · MODIS Terra", true, gibsDay()),
  ];
  els.sourcesBody.replaceChildren(
    el("div", { class: "table-wrap" },
      el("table", { class: "sources" },
        el("thead", {}, el("tr", {}, ["What", "Source", "Status", "Date"].map((h) => el("th", { scope: "col" }, h)))),
        el("tbody", {}, rows))));
}

/* ==========================================================================
   Start
   ========================================================================== */

initTabs();
initForm();
initMap();
els.form.addEventListener("submit", onPlan);
checkHealth();
