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
  mic: $("#mic-btn"),
  demoField: $("#demo-plans-field"),
  demoSelect: $("#demo-plans"),
  micStatus: $("#mic-status"),
  caption: $("#caption"),
  turnback: $("#turnback"),
  turnbackText: $("#turnback-text"),
  hikeBar: $("#hike-bar"),
  hikeProgress: $("#hike-bar .hike-progress"),
  hikeProgressFill: $("#hike-progress-fill"),
  simPlay: $("#sim-play"),
  simReset: $("#sim-reset"),
  pace: $("#pace"),
  paceSlow: $("#pace-slow"),
  paceFast: $("#pace-fast"),
  playbackBtns: [...document.querySelectorAll("[data-playback]")],
  headBack: $("#head-back"),
  onSchedule: $("#onschedule"),
  simEta: $("#sim-eta"),
  simEtaBox: $("#sim-eta-box"),
  simClock: $("#sim-clock"),
  simKm: $("#sim-km"),
  simSunset: $("#sim-sunset"),
  simSunsetBox: $("#sim-sunset-box"),
  simPace: $("#sim-pace"),
  hikeNote: $("#hike-note"),
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

/** osmId: a trail chosen from "Other options"; then only the request text, location and osm_id are sent. */
async function fetchPlan(text, useMock, osmId = null) {
  const extra = osmId === null ? formValues() : { osm_id: osmId };
  const response = useMock
    ? await fetch("/mock_plan.json", { cache: "no-store" })
    : await fetch("/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request: text, lat: DEFAULT_CENTER[0], lon: DEFAULT_CENTER[1], ...extra }),
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
let currentPlan = null; // { request, mock } of the plan on screen, for "Other options"

function renderPlan(plan, mock) {
  currentPlan = { request: str(plan.request) || els.request.value.trim(), mock: !!mock };
  clearTimeout(traceTimer);
  fillForm(plan.constraints);

  const coords = validCoords(plan.trail);
  const trailName = (isObj(plan.trail) && str(plan.trail.name)) || "Trail";
  drawTrail(coords, trailName);
  hikeLoad(plan, coords); // resets any running simulation and pre-fetches narration audio

  const hazards = renderHazards(plan, coords ? coords[0] : DEFAULT_CENTER); // {eonet, serious}
  renderSources(plan);
  renderModeBadges(plan, mock);

  fill(els.results,
    hazardsSummary(hazards), // short alert banner first: NWS alert Moderate or worse, or EONET events nearby
    warningsBanner(plan.warnings),
    trailCard(plan, coords),
    timingCard(plan),
    weatherCard(plan),
    climateCard(plan),
    spaceWeatherCard(plan),
    alternativesCard(plan),
    reportsCard(plan),
    traceCard(plan),
  );
}

function setLoading(on, mock) {
  els.button.disabled = on;
  els.results.setAttribute("aria-busy", String(on));
  if (on) {
    clearTimeout(traceTimer);
    hikeUnload(); // stop the old route's simulation and audio while the new plan loads
    els.results.replaceChildren(
      el("div", { class: "loading" },
        el("span", { class: "spinner", "aria-hidden": "true" }),
        el("span", {}, mock === "switch" ? "Switching to that trail…"
                         : mock === "demo" ? "Loading recorded plan…"
                         : mock ? "Loading mock plan…" : "Planning… finding trails can take up to a minute.")),
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
  const partial = ["trail", "timing", "weather", "climate", "space_weather", "checker", "alerts", "hike_window", "seasonal"]
    .some((k) => isObj(plan[k]) && plan[k].available === false);
  const recordedAt = str(plan.demo_recorded_at);
  const recordedLocal = recordedAt && fmtLocal(recordedAt);
  fill(els.modeBadges,
    recordedLocal
      ? el("span", { class: "badge badge--recorded", title: "Saved by demo mode; not live data" },
           "Recorded data from ", el("time", { datetime: recordedAt }, recordedLocal))
      : el("span", { class: `badge ${mock ? "badge--mock" : "badge--live"}` }, mock ? "Mock data" : "Live"),
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
    date: [osm, t && t.cached === true ? "cached copy" : null,
           t && str(t.elevation_source) ? `elevation: ${str(t.elevation_source)}` : null].filter(Boolean).join(" · ") || null,
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
  const daily = w ? kv([
    ["Forecast", str(w.summary)],
    ["Precipitation chance", Number.isFinite(pct) ? `${Math.round(pct)}%` : null],
  ]) : unavailable("Daily forecast unavailable");
  const day = isObj(plan.constraints) ? fmtDay(plan.constraints.date) : null;
  const date = (w && fmtLocal(w.checked_at)) || (day && `forecast for ${day}`);
  return card("Weather", { body: el("div", {}, hikeWindowBlock(plan), daily), source: (w && str(w.source)) || "NWS", date });
}

/** "During your hike: 20% rain, 38-44F, wind 10 mph" from plan.hike_window (NWS hourly). */
function hikeWindowBlock(plan) {
  const hw = phaseH(plan, "hike_window");
  if (hw.state === "missing") {
    return el("p", { class: "hike-window" }, "During your hike: ", el("span", { class: "state state--info" }, NOT_CHECKED));
  }
  if (hw.state === "unavailable") {
    return el("p", { class: "hike-window unavailable" }, el("span", { "aria-hidden": "true" }, "⊘ "),
      `During your hike: hourly forecast unavailable${hw.error ? ` (${hw.error})` : ""}`);
  }
  const v = hw.value;
  const conditions = Array.isArray(v.conditions) ? v.conditions.map(str).filter(Boolean) : [];
  return el("div", { class: "hike-window" },
    el("p", { class: "hike-window-summary" }, str(v.summary) || "During your hike: —"),
    conditions.length && el("p", { class: "hazard-meta" }, conditions.join(" · ")),
    el("p", { class: "source-line" }, `Source: ${hw.source || "NWS hourly"}`, str(v.window) ? ` · ${str(v.window)}` : null));
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
    const mockPlan = !!(currentPlan && currentPlan.mock);
    for (const a of alts) {
      const score = toNum(a.score);
      const osmId = Number(a.osm_id);
      const name = str(a.name) || "Unnamed trail";
      const usable = !mockPlan && Number.isInteger(osmId) && osmId > 0;
      const btn = el("button", {
          type: "button", class: "alt", disabled: !usable,
          title: usable ? `Plan ${name} instead` : "Switching trails needs live or recorded data, not mock data",
        },
        el("span", { class: "alt-name" }, name),
        el("span", { class: "alt-meta mono" },
          [kmMi(a.distance_km), Number.isFinite(score) ? `score ${score.toFixed(2)}` : null].filter(Boolean).join(" · ")),
        el("span", { class: "alt-go" }, "Switch →"));
      if (usable) btn.addEventListener("click", () => switchTrail(osmId));
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

/** Short Plan-tab banner when an NWS alert is Moderate or worse, or NASA EONET events are nearby. */
function hazardsSummary(hazards) {
  const serious = (hazards && hazards.serious) || [];
  const eonet = (hazards && hazards.eonet) || [];
  if (!serious.length && !eonet.length) return null;
  const bad = serious.some((a) => a.severity === "Severe" || a.severity === "Extreme");
  const parts = serious.slice(0, 2).map((a) => `${str(a.event) || "Weather alert"} (${a.severity})`);
  if (serious.length > 2) parts.push(`${serious.length - 2} more NWS ${serious.length - 2 === 1 ? "alert" : "alerts"}`);
  if (eonet.length) parts.push(`${eonet.length} NASA EONET ${eonet.length === 1 ? "event" : "events"} nearby`);
  return el("button", { type: "button", class: `banner ${bad ? "banner--bad" : "banner--warn"} banner-btn`,
                        on: { click: () => selectTab($("#tab-hazards")) } },
    el("span", { class: "banner-icon", "aria-hidden": "true" }, bad ? "✕" : "▲"),
    el("span", {}, el("strong", {}, bad ? "Severe alert: " : serious.length ? "Alert: " : "Hazards: "),
      `${parts.join("; ")}. Open the Hazards tab.`));
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

// Phase H sections. Recordings made before them don't have the keys: "Not checked in this recording".
const NOT_CHECKED = "Not checked in this recording";
const SEVERITY = { // icon (from .state--*) + word, never color alone
  Extreme: { cls: "bad", word: "Extreme" }, Severe: { cls: "bad", word: "Severe" },
  Moderate: { cls: "warn", word: "Moderate" }, Minor: { cls: "info", word: "Minor" },
  Unknown: { cls: "info", word: "Unknown severity" },
};
const MODERATE_OR_WORSE = ["Moderate", "Severe", "Extreme"];
const CONDITION_ICONS = { "low light": "🌇", "freezing temperatures": "🧊" };

/** {state: "missing" | "unavailable" | "ok", value, items, source, error} for alerts, hike_window, seasonal, conditions. */
function phaseH(plan, key) {
  if (!isObj(plan) || plan[key] === null || plan[key] === undefined) return { state: "missing" };
  const v = plan[key];
  if (Array.isArray(v)) return { state: "ok", value: v, items: v.filter(isObj) };
  if (!isObj(v)) return { state: "missing" };
  if (v.available === false) return { state: "unavailable", source: str(v.source), error: str(v.error) };
  return { state: "ok", value: v, items: Array.isArray(v.items) ? v.items.filter(isObj) : [], source: str(v.source) };
}

function hazardGroup(title, icon, body, source) {
  return el("section", { class: "hazard-group", "aria-label": title },
    el("h3", { class: "hazard-group-title" }, el("span", { "aria-hidden": "true" }, icon), " ", title),
    body,
    source && el("p", { class: "source-line" }, source));
}

/** Body for a group whose section is missing or unavailable; null when it is ok. */
function groupProblem(s, label) {
  if (s.state === "missing") return el("p", { class: "state state--info" }, NOT_CHECKED);
  if (s.state === "unavailable") return unavailable(`${label} unavailable${s.error ? `: ${s.error}` : ""}`);
  return null;
}

function staticHazard(icon, title, tag, meta, link) {
  return el("li", { class: "hazard hazard--static" },
    el("span", { class: "hazard-icon", "aria-hidden": "true" }, icon),
    el("span", { class: "hazard-text" },
      el("span", { class: "hazard-title" }, el("strong", {}, title), " ", tag),
      meta.filter(Boolean).map((m) => el("span", { class: "hazard-meta" }, m)),
      link && el("a", { class: "hazard-link", href: link.href, target: "_blank", rel: "noopener" }, link.text)));
}

function sourceChecked(s, fallback) {
  const at = s.state === "ok" && isObj(s.value) ? fmtLocal(s.value.checked_at) : null;
  return `Source: ${s.source || fallback}${at ? ` · checked ${at}` : ""}`;
}

/** Hazards tab: NASA EONET events (with map markers), NWS alerts, Seasonal, Trail conditions.
    Returns {eonet, serious} for the Plan-tab banner (serious = NWS alerts Moderate or worse). */
function renderHazards(plan, origin) {
  if (hazardLayer) hazardLayer.clearLayers();
  const eonet = renderEonet(plan, origin);
  const alerts = phaseH(plan, "alerts"), seasonal = phaseH(plan, "seasonal"), conditions = phaseH(plan, "conditions");

  const alertItems = alerts.items || [];
  const alertList = () => el("ul", { class: "hazard-list" }, alertItems.map((a) => {
    const sev = SEVERITY[a.severity] || SEVERITY.Unknown;
    const when = [fmtLocal(a.onset) && `from ${fmtLocal(a.onset)}`, fmtLocal(a.ends) && `until ${fmtLocal(a.ends)}`]
      .filter(Boolean).join(" ");
    return staticHazard("⚠️", str(a.event) || "Weather alert", el("span", { class: `state state--${sev.cls} sev` }, sev.word),
      [str(a.headline), when], str(a.link) && { href: str(a.link), text: "NWS alert details" });
  }));
  const alertsGroup = hazardGroup("NWS alerts", "📢",
    groupProblem(alerts, "NWS alerts")
      || (alertItems.length ? alertList() : el("p", { class: "state state--ok" }, "No active NWS alerts for this point")),
    alerts.state === "ok" && sourceChecked(alerts, "NWS"));

  const seasonalItems = seasonal.items || [];
  const seasonalList = () => el("ul", { class: "hazard-list" }, seasonalItems.map((i) =>
    staticHazard(/deer|hunt/i.test(i.title || "") ? "🦌" : "📅", str(i.title) || "Seasonal hazard",
      el("span", { class: "state state--warn sev" }, "Seasonal"),
      [str(i.advice), str(i.source) && `Source: ${str(i.source)}`],
      str(i.link) && { href: str(i.link), text: "More information" })));
  const seasonalGroup = hazardGroup("Seasonal", "📅",
    groupProblem(seasonal, "Seasonal hazards")
      || (seasonalItems.length ? seasonalList() : el("p", { class: "state state--ok" }, "Nothing in season for this date and place")),
    seasonal.state === "ok" && `Source: ${seasonal.source || "TreeScout seasonal calendar"}`);

  const conditionItems = conditions.items || [];
  const conditionList = () => el("ul", { class: "hazard-list" }, conditionItems.map((c) =>
    staticHazard(CONDITION_ICONS[String(c.title || "").toLowerCase()] || "🥾", str(c.title) || "Condition",
      el("span", { class: "state state--warn sev" }, "Condition"),
      [str(c.advice), str(c.source) && `Source: ${str(c.source)}`])));
  const timed = !!section(plan, "timing"); // conditions are derived from the timing; none without a planned hike
  const conditionsGroup = hazardGroup("Trail conditions", "🥾",
    groupProblem(conditions, "Trail conditions")
      || (conditionItems.length ? conditionList()
        : timed ? el("p", { class: "state state--ok" }, "No low light or freezing temperatures expected")
        : el("p", { class: "state state--info" }, "Not checked: no hike timing for this plan")),
    conditions.state === "ok" && timed && "Source: derived from the hike timing and the NWS hourly forecast");

  const count = (eonet.items ? eonet.items.length : 0) + alertItems.length + seasonalItems.length + conditionItems.length;
  els.hazardCount.textContent = count ? String(count) : "";
  els.hazardCount.title = count ? `${count} hazard ${count === 1 ? "item" : "items"}` : "";
  els.hazardsBody.replaceChildren(eonet.group, alertsGroup, seasonalGroup, conditionsGroup);
  return { eonet: eonet.items || [], serious: alertItems.filter((a) => MODERATE_OR_WORSE.includes(a.severity)) };
}

/** NASA EONET group + map markers; items is null when EONET is unavailable. */
function renderEonet(plan, origin) {
  const checked = fmtLocal(plan.hazards_checked_at);
  const sourceDate = `Source: NASA EONET${checked ? ` · checked ${checked}` : ""}`;
  const group = (body) => hazardGroup("NASA EONET events", "🛰️", body, sourceDate);

  if (!Array.isArray(plan.hazards)) return { items: null, group: group(unavailable("NASA EONET hazards unavailable")) };

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

  if (!hazards.length) {
    return { items: hazards, group: group(el("p", { class: "state state--ok" }, "No active NASA EONET events within 100 km")) };
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
      el("span", { class: "hazard-text" },
        el("span", { class: "hazard-title" }, el("strong", {}, h.title), " ", el("span", { class: "state state--warn sev" }, "Event")),
        el("span", { class: "hazard-meta" }, meta)));
    if (marker) item.addEventListener("click", () => { map.setView(h.at, Math.max(map.getZoom(), 8)); marker.openPopup(); });
    list.append(el("li", {}, item));
  }
  return { items: hazards, group: group(list) };
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
  // ok: true | false | "missing" (a recording made before that source existed)
  const row = (what, source, ok, date) => el("tr", {},
    el("th", { scope: "row" }, what),
    el("td", {}, source),
    el("td", {}, ok === "missing" ? el("span", { class: "state state--info" }, "Not checked")
      : el("span", { class: `state state--${ok ? "ok" : "bad"}` }, ok ? "Available" : "Unavailable")),
    el("td", { class: "mono" }, date || "—"));
  const hRow = (what, key, fallback) => {
    const h = phaseH(plan, key);
    const date = h.state === "ok" && isObj(h.value) ? fmtLocal(h.value.checked_at) : null;
    return row(what, h.source || fallback, h.state === "missing" ? "missing" : h.state === "ok", date);
  };

  const s = (k) => section(plan, k);
  const day = isObj(plan.constraints) ? fmtDay(plan.constraints.date) : null;
  const rows = [
    row("Trail", "OpenStreetMap", !!validCoords(plan.trail), null),
    row("Timing", (s("timing") && str(s("timing").source)) || "TreeScout planner", !!s("timing"), day),
    row("Weather", (s("weather") && str(s("weather").source)) || "NWS", !!s("weather"), (s("weather") && fmtLocal(s("weather").checked_at)) || day),
    row("Climate", (s("climate") && str(s("climate").source)) || "NASA POWER", !!s("climate"), s("climate") && str(s("climate").years)),
    row("Space weather", (s("space_weather") && str(s("space_weather").source)) || "NASA DONKI", !!s("space_weather"), s("space_weather") && fmtLocal(s("space_weather").checked_at)),
    row("Hazards", "NASA EONET", Array.isArray(plan.hazards), fmtLocal(plan.hazards_checked_at)),
    hRow("Weather alerts", "alerts", "NWS"),
    hRow("Hike-window forecast", "hike_window", "NWS hourly"),
    hRow("Seasonal hazards", "seasonal", "TreeScout seasonal calendar"),
    row("Satellite map", "NASA GIBS · MODIS Terra", true, gibsDay()),
  ];
  els.sourcesBody.replaceChildren(
    el("div", { class: "table-wrap" },
      el("table", { class: "sources" },
        el("thead", {}, el("tr", {}, ["What", "Source", "Status", "Date"].map((h) => el("th", { scope: "col" }, h)))),
        el("tbody", {}, rows))));
}

/* ==========================================================================
   Hike simulation, narration audio, late check, voice input
   ========================================================================== */

// Backend contracts (backend/main.py TTSRequest / LateNarrationRequest, backend/narration.py):
const TTS_URL = "/tts";           // POST {"text": "..." (1-1000 chars)}                     -> audio/mpeg; 502 if TTS fails
const LATE_URL = "/narrate/late"; // POST {"plan": {...}, "current_km": n, "elapsed_min": n} -> {"late": false} | {"late": true, "text": "..."}
// PLAYBACK speed: how fast the demo plays (the whole hike at planned pace takes about this many real
// seconds). It never changes the hiker's pace or the late math. ?simSeconds=N sets Normal (tests).
const SIM_SECONDS = Math.max(1, Number(new URLSearchParams(location.search).get("simSeconds")) || 60);
const PLAYBACK = { normal: SIM_SECONDS, fast: SIM_SECONDS / 3, veryfast: SIM_SECONDS / 10 }; // 60 / 20 / 6 s
// HIKER PACE vs plan: 1 = the planned pace (est_duration_min for the trail). At 0.5x each km takes twice the clock.
const PACE_MIN = 0.5, PACE_MAX = 1.5, PACE_SLOW = 0.6, PACE_FAST = 1.3;
const LATE_CHECK_MS = 3000;
const TURN_BACK_FALLBACK = "You're behind schedule. Turn back now to finish before sunset.";
// Late rule, identical to backend/narration.py check_late_pace (used here when /narrate/late is unavailable):
// late when (start + elapsed) + remaining_km * current pace + LATE_BUFFER_MIN > sunset.
// "current pace" there is the average so far (elapsed / km hiked), so after speeding up the hiker is back on
// schedule once the average has recovered, not the moment the slider moves.
const LATE_BUFFER_MIN = 30;
const LATE_MIN_KM = 0.25; // too early to judge pace before this
// Fixed voice cues, pre-fetched when a plan loads and kept for the page. The late text itself only arrives
// with /narrate/late; the short TURN BACK cue plays instantly while it downloads.
const TURN_BACK_CUE = "Turn back now.";
const BACK_ON_SCHEDULE_CUE = "You're back on schedule.";
const HEADING_BACK_CUE = "Heading back to the trailhead.";
const cueUrls = {}; // text -> Promise<string|null>

const hike = {
  gen: 0,              // run generation: bumps on every reset; playback/late checks from older runs are ignored
  planGen: 0,          // plan generation: bumps when a plan is unloaded; prefetched audio belongs to one plan
  plan: null, coords: null, cumKm: null, totalKm: 0,
  startMin: 600, durationMin: 60, sunsetMin: null,
  planStartMin: null, sunsetText: null, // timing.start / timing.sunset exactly as the plan gives them
  posKm: 0, elapsedMin: 0, state: "idle", // idle | running | paused | done
  pace: 1, playback: "normal",
  direction: "out",    // out | back (after "Head back")
  turnKm: 0,           // where the hiker turned around
  late: false,         // inside a late episode (TURN BACK shown)
  lastTs: null, raf: 0,
  narration: [],       // [{key, coordIndex, text, url: Promise<string|null>}]
  spoken: new Set(),
  blobUrls: [], prefetch: null,
  ttsDown: null, lateDown: null,
  lateTimer: 0, lateInFlight: false, lateAgain: false,
  onScheduleTimer: 0,
  marker: null, layer: null,
};

/* ---------- small helpers ---------- */

function hhmmToMin(value) {
  const m = /^(\d{1,2}):(\d{2})/.exec(str(value) || "");
  return m ? Number(m[1]) * 60 + Number(m[2]) : null;
}

function minToHhmm(total) {
  const t = ((Math.round(total) % 1440) + 1440) % 1440;
  return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
}

function setHikeNote() {
  const notes = [hike.ttsDown && `Voice unavailable (${hike.ttsDown}); showing captions only.`,
                 hike.lateDown && `Late check: ${hike.lateDown}; using the same rule in the browser.`].filter(Boolean);
  els.hikeNote.textContent = notes.join(" ");
  els.hikeNote.hidden = !notes.length;
}

/* ---------- narration audio: prefetch, queue, captions ---------- */

const player = new Audio();
player.preload = "auto";
let audioQueue = [];
let audioBusy = false;
let captionTimer = 0;

/** POST /tts and return a blob URL (or null). planGen ties the audio to one plan (revoked when that
 *  plan unloads); planGen null means page-level audio that is kept (the turn-back cue). */
async function fetchTts(text, signal, planGen, { quiet = false } = {}) {
  if (hike.ttsDown) return null;
  // quiet: a failure doesn't switch voice off (e.g. a cue that demo mode has no recording for)
  const current = () => planGen === null || planGen === hike.planGen;
  const report = () => !quiet && current();
  try {
    const response = await fetch(TTS_URL, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }), signal,
    });
    if (!response.ok) {
      if (report()) { hike.ttsDown = `${TTS_URL} returned ${response.status}`; setHikeNote(); }
      return null;
    }
    const type = response.headers.get("content-type") || "";
    if (!/^audio\/|octet-stream/.test(type)) {
      if (report()) { hike.ttsDown = `${TTS_URL} sent ${type || "no content type"}, not audio`; setHikeNote(); }
      return null;
    }
    const url = URL.createObjectURL(await response.blob());
    if (!current()) { URL.revokeObjectURL(url); return null; } // the plan changed meanwhile
    if (planGen !== null) hike.blobUrls.push(url);
    return url;
  } catch (err) {
    if (err.name !== "AbortError") console.warn("TTS failed:", err);
    return null;
  }
}

/** Start fetching audio for every narration line in parallel as soon as a plan loads. */
function prefetchNarration(plan, planGen) {
  hike.prefetch = new AbortController();
  const lastIndex = hike.coords.length - 1;
  hike.narration = (Array.isArray(plan.narration) ? plan.narration : [])
    .filter((n) => isObj(n) && str(n.text))
    .map((n, key) => {
      const idx = Number.isInteger(Number(n.coord_index)) ? Number(n.coord_index) : 0;
      return { key, coordIndex: Math.min(Math.max(idx, 0), lastIndex), text: str(n.text),
               url: fetchTts(str(n.text), hike.prefetch.signal, planGen) };
    })
    .sort((a, b) => a.coordIndex - b.coordIndex);
}

/** Fetch the fixed cues once per page (a failed one is tried again with the next plan). */
function prefetchCues() {
  for (const text of [TURN_BACK_CUE, BACK_ON_SCHEDULE_CUE, HEADING_BACK_CUE]) {
    if (cueUrls[text]) continue;
    cueUrls[text] = fetchTts(text, undefined, null, { quiet: true }).then((url) => {
      if (!url) delete cueUrls[text];
      return url;
    });
  }
}

function cue(text) {
  return cueUrls[text] || Promise.resolve(null); // no audio: the caption shows instead
}

function showCaption(text) {
  clearTimeout(captionTimer);
  els.caption.textContent = text;
  els.caption.hidden = false;
}

function hideCaptionSoon(ms = 4000) {
  clearTimeout(captionTimer);
  captionTimer = setTimeout(() => { els.caption.hidden = true; }, ms);
}

function readingMs(text) {
  return Math.min(8000, 1500 + text.split(/\s+/).length * 320); // caption-only pacing
}

function enqueueLine(line, { interrupt = false } = {}) {
  if (interrupt) {
    audioQueue = [];
    player.pause();
    audioBusy = false;
  }
  audioQueue.push(line);
  if (!audioBusy) playNextLine();
}

async function playNextLine() {
  const line = audioQueue.shift();
  if (!line) {
    audioBusy = false;
    hideCaptionSoon();
    return;
  }
  audioBusy = true;
  showCaption(line.text);
  const url = await line.url; // already resolved when prefetched, so there is no gap
  if (line.gen !== hike.gen) return; // reset or new plan while waiting
  if (!url) {
    captionTimer = setTimeout(playNextLine, readingMs(line.text));
    return;
  }
  player.src = url;
  player.onended = () => playNextLine();
  player.onerror = () => playNextLine();
  try {
    await player.play();
    if (hike.state === "paused") player.pause();
  } catch (err) {
    console.warn("Audio playback failed:", err);
    captionTimer = setTimeout(playNextLine, readingMs(line.text));
  }
}

function stopAudio() {
  audioQueue = [];
  audioBusy = false;
  player.pause();
  player.removeAttribute("src");
  player.load();
  clearTimeout(captionTimer);
  els.caption.hidden = true;
}

/* ---------- route geometry ---------- */

function cumulativeKm(coords) {
  const cum = [0];
  for (let i = 1; i < coords.length; i++) cum.push(cum[i - 1] + haversineKm(coords[i - 1], coords[i]));
  return cum;
}

/** Position and coordinate index at a fraction (0-1) of the route length. */
function positionAt(fraction) {
  const { coords, cumKm } = hike;
  const target = fraction * cumKm[cumKm.length - 1];
  let lo = 0, hi = cumKm.length - 1;
  while (lo < hi - 1) {
    const mid = (lo + hi) >> 1;
    if (cumKm[mid] <= target) lo = mid; else hi = mid;
  }
  const span = cumKm[hi] - cumKm[lo] || 1;
  const t = Math.min(1, Math.max(0, (target - cumKm[lo]) / span));
  const [a, b] = [coords[lo], coords[hi]];
  return { latlng: [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t], index: fraction >= 1 ? hi : lo };
}

/* ---------- simulation lifecycle ---------- */

function hikeUnload() {
  hike.planGen++;
  hikeReset();
  hike.prefetch?.abort();
  for (const url of hike.blobUrls) URL.revokeObjectURL(url);
  hike.blobUrls = [];
  hike.narration = [];
  hike.plan = hike.coords = null;
  els.hikeBar.hidden = true;
}

function setSimControlsDisabled(disabled) {
  for (const b of [els.simPlay, els.simReset, els.pace, els.paceSlow, els.paceFast, ...els.playbackBtns]) b.disabled = disabled;
}

function showEmptyHikeBar() {
  els.hikeBar.hidden = false;
  els.hikeProgressFill.style.width = "0%";
  els.hikeProgress.setAttribute("aria-valuenow", "0");
  els.simClock.textContent = "--:--";
  els.simKm.textContent = "—";
  els.simSunset.textContent = "—";
  els.simSunsetBox.classList.remove("readout--warn", "readout--bad");
  els.simPace.textContent = "—";
  els.simEtaBox.hidden = true;
  els.simPlay.textContent = "▶ Simulate hike";
  setSimControlsDisabled(true);
  els.hikeNote.textContent = "No route to simulate for this plan.";
  els.hikeNote.hidden = false;
}

function hikeLoad(plan, coords) {
  hikeUnload();
  if (!coords || !map) {
    showEmptyHikeBar(); // e.g. outside coverage: no stale distance, Simulate disabled
    return;
  }
  setSimControlsDisabled(false);
  hike.plan = plan;
  hike.coords = coords;
  hike.cumKm = cumulativeKm(coords);
  const routeKm = hike.cumKm[hike.cumKm.length - 1];
  const statedKm = toNum(isObj(plan.trail) ? plan.trail.distance_km : NaN);
  hike.totalKm = Number.isFinite(statedKm) && statedKm > 0 ? statedKm : routeKm;

  const timing = section(plan, "timing") || {};
  const constraints = isObj(plan.constraints) ? plan.constraints : {};
  hike.startMin = hhmmToMin(timing.start) ?? hhmmToMin(constraints.start) ?? 600;
  const minutes = toNum(timing.est_duration_min);
  hike.durationMin = Number.isFinite(minutes) && minutes > 0 ? minutes : (hike.totalKm / 5) * 60;
  hike.sunsetMin = hhmmToMin(timing.sunset);
  hike.planStartMin = hhmmToMin(timing.start);
  hike.sunsetText = str(timing.sunset);

  hike.ttsDown = null;
  hike.lateDown = null;
  setHikeNote();
  prefetchNarration(plan, hike.planGen);
  prefetchCues();
  els.hikeBar.hidden = false;
  renderHike();
}

function clearBanners() {
  hike.late = false;
  els.turnback.hidden = true;
  clearTimeout(hike.onScheduleTimer);
  els.onSchedule.hidden = true;
}

function hikeReset() {
  hike.gen++;
  cancelAnimationFrame(hike.raf);
  stopLateChecks();
  stopAudio();
  clearBanners();
  hike.state = "idle";
  hike.posKm = 0;
  hike.elapsedMin = 0;
  hike.pace = 1;
  hike.direction = "out";
  hike.turnKm = 0;
  hike.lastTs = null;
  hike.spoken = new Set();
  hike.lateInFlight = false;
  hike.lateAgain = false;
  if (hike.layer) hike.layer.clearLayers();
  hike.marker = null;
  setHikeNote(); // drops a "Back at the trailhead" note from the last run
  if (hike.coords) renderHike();
}

function hikeStart() {
  if (!hike.coords) return;
  if (hike.state === "done") hikeReset();
  if (!hike.layer) hike.layer = L.layerGroup().addTo(map);
  if (!hike.marker) {
    hike.marker = L.marker(hike.coords[0], {
      icon: divIcon("marker--hiker", ""), interactive: false, keyboard: false, zIndexOffset: 2000,
    }).addTo(hike.layer);
  }
  hike.state = "running";
  hike.lastTs = null;
  if (hike.direction === "out") startLateChecks();
  if (player.src && player.paused && audioBusy) player.play().catch(() => {});
  hike.raf = requestAnimationFrame(hikeFrame);
  renderHike();
}

/** Pause also puts the hiker back on planned pace and clears the TURN BACK / back-on-schedule banners. */
function hikePause() {
  hike.state = "paused";
  cancelAnimationFrame(hike.raf);
  stopLateChecks();
  player.pause();
  hike.pace = 1;
  clearBanners();
  renderHike();
}

function setPlayback(mode) {
  if (!(mode in PLAYBACK)) return;
  hike.playback = mode;
  renderHike();
}

/** Hiker pace vs plan (0.5-1.5). A change triggers a late check right away. */
function setPace(value) {
  const pace = Math.min(PACE_MAX, Math.max(PACE_MIN, Math.round(Number(value) * 100) / 100));
  if (!Number.isFinite(pace) || pace === hike.pace) { renderHike(); return; }
  hike.pace = pace;
  renderHike();
  if (hike.state === "running" && hike.direction === "out") checkLate();
}

function paceWords(pace) {
  return pace < 0.975 ? "Slower than planned" : pace > 1.025 ? "Faster than planned" : "On pace";
}

/** Turn around: walk back along the route already hiked, to the trailhead. The clock keeps running. */
function headBack() {
  if (!hike.coords || hike.direction === "back" || hike.state === "idle" || hike.state === "done") return;
  hike.direction = "back";
  hike.turnKm = hike.posKm;
  stopLateChecks();
  clearBanners();
  enqueueLine({ text: HEADING_BACK_CUE, url: cue(HEADING_BACK_CUE), gen: hike.gen }, { interrupt: true });
  renderHike();
}

function kmPerMin() {
  return (hike.totalKm / hike.durationMin) * hike.pace; // at pace 1 the trail takes est_duration_min
}

function hikeFrame(ts) {
  if (hike.state !== "running") return;
  const dt = hike.lastTs === null ? 0 : Math.min(0.25, (ts - hike.lastTs) / 1000);
  hike.lastTs = ts;
  // Playback sets how many simulated minutes pass per real second; pace sets how far the hiker gets in them.
  const minutes = (dt * hike.durationMin) / PLAYBACK[hike.playback];
  hike.elapsedMin += minutes;
  const km = minutes * kmPerMin();

  if (hike.direction === "out") {
    hike.posKm = Math.min(hike.totalKm, hike.posKm + km);
    const { index } = positionAt(hike.posKm / hike.totalKm);
    for (const line of hike.narration) {
      if (line.coordIndex <= index && !hike.spoken.has(line.key)) {
        hike.spoken.add(line.key);
        enqueueLine({ text: line.text, url: line.url, gen: hike.gen });
      }
    }
  } else {
    hike.posKm = Math.max(0, hike.posKm - km);
  }
  hike.marker.setLatLng(positionAt(hike.posKm / hike.totalKm).latlng);

  const finished = hike.direction === "out" ? hike.posKm >= hike.totalKm : hike.posKm <= 0;
  if (finished) {
    hike.state = "done";
    stopLateChecks();
  } else {
    hike.raf = requestAnimationFrame(hikeFrame);
  }
  renderHike();
}

function fmtLeft(min) {
  const m = Math.floor(Math.abs(min));
  return m >= 60 ? `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min` : `${m} min`;
}

function renderHike() {
  const pct = Math.round((hike.posKm / (hike.totalKm || 1)) * 1000) / 10;
  els.hikeProgressFill.style.width = `${pct}%`;
  els.hikeProgress.setAttribute("aria-valuenow", String(Math.round(pct)));
  const clock = hike.startMin + hike.elapsedMin;
  els.simClock.textContent = minToHhmm(clock);
  const back = hike.direction === "back";
  els.simKm.textContent = back
    ? `${(2 * hike.turnKm - hike.posKm).toFixed(2)} km · ${hike.posKm.toFixed(2)} to start`
    : `${hike.posKm.toFixed(2)} / ${hike.totalKm.toFixed(1)} km`;

  // Pace: slider, readout and its word (never color alone)
  els.pace.value = String(hike.pace);
  const words = paceWords(hike.pace);
  els.pace.setAttribute("aria-valuetext", `${words}, ${hike.pace.toFixed(2)} times planned`);
  els.simPace.textContent = `${hike.pace.toFixed(1)}× · ${words.replace(" than planned", "")}`;
  els.paceSlow.setAttribute("aria-pressed", String(hike.pace === PACE_SLOW));
  els.paceFast.setAttribute("aria-pressed", String(hike.pace === PACE_FAST));
  for (const b of els.playbackBtns) b.setAttribute("aria-pressed", String(b.dataset.playback === hike.playback));

  els.simSunsetBox.classList.remove("readout--warn", "readout--bad");
  if (hike.sunsetMin === null) {
    els.simSunset.textContent = "—";
  } else {
    const left = hike.sunsetMin - clock;
    if (left <= 0) {
      els.simSunset.textContent = "After sunset";
      els.simSunsetBox.classList.add("readout--bad");
    } else {
      els.simSunset.textContent = fmtLeft(left);
      if (left < 30) els.simSunsetBox.classList.add("readout--warn");
    }
  }

  // Heading back: (projected) arrival at the trailhead vs sunset
  els.simEtaBox.hidden = !back;
  if (back) {
    const arrive = hike.state === "done" ? clock : clock + hike.posKm / kmPerMin();
    const vsSunset = hike.sunsetMin === null ? ""
      : hike.sunsetMin - arrive >= 0 ? ` · ${fmtLeft(hike.sunsetMin - arrive)} before sunset`
      : ` · ${fmtLeft(arrive - hike.sunsetMin)} after sunset`;
    els.simEta.textContent = `${hike.state === "done" ? "" : "~"}${minToHhmm(arrive)}${vsSunset}`;
    els.simEtaBox.classList.toggle("readout--bad", hike.sunsetMin !== null && arrive > hike.sunsetMin);
    if (hike.state === "done") {
      els.hikeNote.textContent = `Back at the trailhead at ${minToHhmm(arrive)}${vsSunset}.`;
      els.hikeNote.hidden = false;
    }
  }

  els.simPlay.textContent = { idle: "▶ Simulate hike", running: "❚❚ Pause", paused: "▶ Resume",
                              done: "↺ Simulate again" }[hike.state];
  const active = hike.state === "running" || hike.state === "paused";
  els.headBack.disabled = !active || back;
}

/* ---------- late check: every 3 s and on every pace change ---------- */

function startLateChecks() {
  stopLateChecks();
  if (hike.direction !== "out") return;
  hike.lateTimer = setInterval(checkLate, LATE_CHECK_MS);
}

function stopLateChecks() {
  clearInterval(hike.lateTimer);
  hike.lateTimer = 0;
}

/** Same rule as the backend's check_late_pace. Returns {late, text?}. */
function isLateLocally({ startMin, sunsetMin, sunsetText, totalKm, currentKm, elapsedMin }) {
  if (startMin === null || sunsetMin === null) return { late: false };
  if (!(totalKm > 0) || !(elapsedMin > 0) || !(currentKm >= LATE_MIN_KM)) return { late: false };
  const pace = elapsedMin / currentKm; // current minutes per km
  const finishMin = startMin + elapsedMin + Math.max(0, totalKm - currentKm) * pace;
  if (finishMin + LATE_BUFFER_MIN > sunsetMin) {
    return { late: true,
             text: `You're behind schedule. Turn back now to reach the trailhead before sunset at ${sunsetText}.` };
  }
  return { late: false };
}

async function checkLate() {
  if (hike.direction !== "out" || hike.state !== "running") return;
  if (hike.lateInFlight) { hike.lateAgain = true; return; } // e.g. pace changed mid-request: check again after
  const gen = hike.gen;
  const currentKm = Math.round(hike.posKm * 100) / 100;
  const elapsedMin = Math.round(hike.elapsedMin * 10) / 10;
  const local = () => isLateLocally({ startMin: hike.planStartMin, sunsetMin: hike.sunsetMin,
                                      sunsetText: hike.sunsetText, totalKm: hike.totalKm, currentKm, elapsedMin });
  hike.lateInFlight = true;
  try {
    let result;
    if (hike.lateDown) {
      result = local();
    } else {
      try {
        const response = await fetch(LATE_URL, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ plan: hike.plan, current_km: currentKm, elapsed_min: elapsedMin }),
        });
        if (!response.ok) throw new Error(`${LATE_URL} returned ${response.status}`);
        result = await response.json();
      } catch (err) {
        if (gen !== hike.gen) return;
        hike.lateDown = err.message.startsWith(LATE_URL) ? err.message : `${LATE_URL} unreachable`;
        setHikeNote();
        result = local();
      }
    }
    if (gen !== hike.gen || !isObj(result) || hike.direction !== "out" || hike.state !== "running") return;
    if (result.late === true && !hike.late) turnBack(str(result.text) || TURN_BACK_FALLBACK, gen);
    else if (result.late === false && hike.late) backOnSchedule(gen);
  } finally {
    if (gen === hike.gen) {
      hike.lateInFlight = false;
      if (hike.lateAgain) { hike.lateAgain = false; checkLate(); }
    }
  }
}

/** Start of a late episode: banner + voice once (until back on schedule, Pause or Reset). */
function turnBack(text, gen) {
  hike.late = true;
  clearTimeout(hike.onScheduleTimer);
  els.onSchedule.hidden = true;
  els.turnbackText.textContent = text;
  els.turnback.hidden = false;
  renderHike();
  const spoken = fetchTts(text, undefined, hike.planGen); // starts now, downloads while the cue plays
  enqueueLine({ text: TURN_BACK_CUE, url: cue(TURN_BACK_CUE), gen }, { interrupt: true });
  enqueueLine({ text, url: spoken, gen });
}

/** End of a late episode (the hiker sped up): hide TURN BACK, show and say "back on schedule" once. */
function backOnSchedule(gen) {
  hike.late = false;
  els.turnback.hidden = true;
  els.onSchedule.hidden = false;
  clearTimeout(hike.onScheduleTimer);
  hike.onScheduleTimer = setTimeout(() => { els.onSchedule.hidden = true; }, 6000);
  enqueueLine({ text: BACK_ON_SCHEDULE_CUE, url: cue(BACK_ON_SCHEDULE_CUE), gen }, { interrupt: true });
}

/* ---------- voice input ---------- */

function initVoice() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) {
    els.mic.hidden = true; // unsupported browser (e.g. Firefox): no button at all
    return;
  }
  els.mic.hidden = false;
  let recognition = null;

  const setListening = (on) => {
    els.mic.setAttribute("aria-pressed", String(on));
    els.mic.querySelector(".mic-word").textContent = on ? "Stop" : "Speak";
    els.mic.title = on ? "Stop listening" : "Speak your request";
  };
  const say = (text) => {
    els.micStatus.textContent = text || "";
    els.micStatus.hidden = !text;
  };

  els.mic.addEventListener("click", () => {
    if (recognition) {
      recognition.stop();
      return;
    }
    let finalText = "";
    let failed = false;
    recognition = new Recognition();
    recognition.lang = "en-US";
    recognition.interimResults = true;
    recognition.continuous = false;
    recognition.maxAlternatives = 1;

    recognition.onresult = (event) => {
      let interim = "";
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i];
        if (result.isFinal) finalText += result[0].transcript;
        else interim += result[0].transcript;
      }
      els.request.value = `${finalText} ${interim}`.trim();
    };
    recognition.onerror = (event) => {
      failed = true;
      say({
        "not-allowed": "Microphone access was blocked. Allow it in the browser's site settings.",
        "service-not-allowed": "Speech recognition is blocked in this browser.",
        "no-speech": "Didn't hear anything. Press Speak and try again.",
        "audio-capture": "No microphone found.",
        network: "Speech recognition needs an internet connection.",
        aborted: "",
      }[event.error] ?? `Speech recognition error: ${event.error}`);
    };
    recognition.onend = () => {
      recognition = null;
      setListening(false);
      const text = finalText.trim();
      if (text && !failed) {
        say("");
        els.request.value = text;
        els.form.requestSubmit(); // same as clicking Plan
      }
    };

    say("Listening… say your hike request.");
    setListening(true);
    try {
      recognition.start();
    } catch (err) {
      recognition = null;
      setListening(false);
      say(`Couldn't start the microphone: ${err.message}`);
    }
  });
}

function initHike() {
  els.simPlay.addEventListener("click", () => (hike.state === "running" ? hikePause() : hikeStart()));
  els.simReset.addEventListener("click", hikeReset);
  els.pace.addEventListener("input", () => setPace(els.pace.value));
  els.paceSlow.addEventListener("click", () => setPace(PACE_SLOW));
  els.paceFast.addEventListener("click", () => setPace(PACE_FAST));
  for (const b of els.playbackBtns) b.addEventListener("click", () => setPlayback(b.dataset.playback));
  els.headBack.addEventListener("click", headBack);
}

/* ==========================================================================
   Other options: re-plan with a chosen trail
   ========================================================================== */

async function switchTrail(osmId) {
  if (!currentPlan || currentPlan.mock) return;
  const seq = ++requestSeq;
  setLoading(true, "switch");
  selectTab($("#tab-plan"));
  try {
    const plan = await fetchPlan(currentPlan.request, false, osmId);
    if (seq !== requestSeq) return;
    renderPlan(plan, false); // redraws the route, every card, narration and the audio prefetch
    els.results.scrollIntoView({ block: "start" });
  } catch (err) {
    if (seq !== requestSeq) return;
    const offline = err instanceof TypeError && /fetch/i.test(err.message);
    showError(offline ? "Could not reach the server. Is it running?" : err.message);
  } finally {
    if (seq === requestSeq) setLoading(false);
  }
}

/* ==========================================================================
   Demo plans (recordings saved by the backend's demo mode)
   ========================================================================== */

async function initDemoPlans() {
  let items = [];
  try {
    const response = await fetch("/demo/plans");
    if (response.ok) items = await response.json();
  } catch { /* no demo endpoint: keep the dropdown hidden */ }
  if (!Array.isArray(items) || !items.length) return;

  for (const item of items) {
    if (!isObj(item) || !str(item.slug)) continue;
    const when = fmtLocal(item.recorded_at);
    els.demoSelect.append(el("option", { value: item.slug },
      `${str(item.request) || item.slug}${when ? ` · ${when}` : ""}`));
  }
  els.demoField.hidden = els.demoSelect.options.length <= 1;
  els.demoSelect.addEventListener("change", () => {
    const slug = els.demoSelect.value;
    if (slug) loadDemoPlan(slug);
  });
}

async function loadDemoPlan(slug) {
  const seq = ++requestSeq;
  setLoading(true, "demo");
  try {
    const response = await fetch(`/demo/plans/${encodeURIComponent(slug)}`);
    if (!response.ok) throw new Error(`/demo/plans/${slug} returned ${response.status}`);
    const plan = await response.json();
    if (seq !== requestSeq) return;
    if (!isObj(plan)) throw new Error("The recording is not a plan object.");
    if (str(plan.request)) els.request.value = plan.request;
    renderPlan(plan, false);
  } catch (err) {
    if (seq === requestSeq) showError(err.message);
  } finally {
    if (seq === requestSeq) setLoading(false);
  }
}

/* ==========================================================================
   Start
   ========================================================================== */

initTabs();
initForm();
initMap();
initHike();
initVoice();
initDemoPlans();
els.form.addEventListener("submit", onPlan);
checkHealth();
