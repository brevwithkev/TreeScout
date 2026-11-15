// TreeScout frontend: plan a hike and draw it on the map.

const DEFAULT_CENTER = [42.2411, -83.6130]; // Ypsilanti, MI
const $ = (sel) => document.querySelector(sel);

const els = {
  status: $("#status"),
  form: $("#plan-form"),
  request: $("#request"),
  useMock: $("#use-mock"),
  button: $("#plan-btn"),
  info: $("#info"),
};

/* ---------- Backend health ---------- */

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

/* ---------- Map ---------- */

let map = null;
let routeLayer = null;

function initMap() {
  if (typeof L === "undefined") {
    showError("The map library (Leaflet) could not be loaded. Check your internet connection and reload.");
    return;
  }
  map = L.map("map", { zoomControl: true }).setView(DEFAULT_CENTER, 12);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  routeLayer = L.layerGroup().addTo(map);
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function endpointIcon(kind, letter) {
  return L.divIcon({
    className: "",
    html: `<span class="marker marker--${kind}" aria-hidden="true">${letter}</span>`,
    iconSize: [24, 24],
    iconAnchor: [12, 12],
  });
}

function drawTrail(coords, name) {
  if (!map) return;
  routeLayer.clearLayers();
  const casing = L.polyline(coords, { className: "route-line--casing", color: cssVar("--bg"), weight: 10, opacity: 1 });
  const line = L.polyline(coords, { className: "route-line", color: cssVar("--accent"), weight: 6, opacity: 1 });
  routeLayer.addLayer(casing).addLayer(line);

  const start = coords[0];
  const end = coords[coords.length - 1];
  L.marker(start, { icon: endpointIcon("start", "S"), title: `Start: ${name}`, keyboard: true })
    .bindTooltip("Start").addTo(routeLayer);
  L.marker(end, { icon: endpointIcon("end", "E"), title: `End: ${name}`, keyboard: true })
    .bindTooltip("End").addTo(routeLayer);

  map.fitBounds(line.getBounds(), { padding: [40, 40] });
}

/* ---------- Planning ---------- */

let requestSeq = 0; // ignore responses from superseded requests

async function fetchPlan(text, useMock) {
  const response = useMock
    ? await fetch("/mock_plan.json", { cache: "no-store" })
    : await fetch("/plan", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request: text, lat: DEFAULT_CENTER[0], lon: DEFAULT_CENTER[1] }),
      });

  let body = null;
  try { body = await response.json(); } catch { /* non-JSON body */ }

  if (!response.ok) {
    const detail = body && typeof body.detail === "string" ? body.detail : response.statusText;
    const where = useMock ? "/mock_plan.json" : "/plan";
    throw new Error(`${where} returned ${response.status}${detail ? `: ${detail}` : ""}`);
  }
  if (!body) throw new Error("The server response was not valid JSON.");
  return body;
}

function validTrail(plan) {
  const trail = plan && plan.trail;
  if (!trail || !Array.isArray(trail.coords)) return null;
  const coords = trail.coords
    .filter((p) => Array.isArray(p) && p.length >= 2)
    .map(([lat, lon]) => [Number(lat), Number(lon)])
    .filter(([lat, lon]) => Number.isFinite(lat) && Number.isFinite(lon));
  return coords.length >= 2 ? { ...trail, coords } : null;
}

async function onPlan(event) {
  event.preventDefault();
  const text = els.request.value.trim();
  if (!text) {
    showError("Type a hike request first.");
    els.request.focus();
    return;
  }

  const seq = ++requestSeq;
  setLoading(true, els.useMock.checked);
  try {
    const plan = await fetchPlan(text, els.useMock.checked);
    if (seq !== requestSeq) return;
    const trail = validTrail(plan);
    if (!trail) throw new Error("The plan has no usable trail coordinates.");
    drawTrail(trail.coords, trail.name || "Trail");
    showTrail(trail, els.useMock.checked);
  } catch (err) {
    if (seq !== requestSeq) return;
    const offline = err instanceof TypeError; // fetch network failure
    showError(offline ? "Could not reach the server. Is it running?" : err.message);
  } finally {
    if (seq === requestSeq) setLoading(false);
  }
}

/* ---------- Panel rendering ---------- */

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  node.append(...children);
  return node;
}

function fmt(value, digits, unit) {
  const n = Number(value);
  return Number.isFinite(n) ? `${n.toFixed(digits)} ${unit}` : "—";
}

function setLoading(on, mock) {
  els.button.disabled = on;
  els.info.setAttribute("aria-busy", String(on));
  if (on) {
    els.info.replaceChildren(
      el("div", { class: "loading" },
        el("span", { class: "spinner", "aria-hidden": "true" }),
        el("span", {}, mock ? "Loading mock plan…" : "Finding trails… this can take up to a minute."))
    );
  }
}

function showTrail(trail, mock) {
  const badge = el("span", { class: `badge ${mock ? "badge--mock" : "badge--live"}` }, mock ? "Mock data" : "Live");
  els.info.replaceChildren(
    el("article", { class: "card" },
      el("div", { class: "card-head" }, el("h2", { class: "trail-name" }, trail.name || "Unnamed trail"), badge),
      el("dl", { class: "kv" },
        el("dt", {}, "Distance"), el("dd", {}, fmt(trail.distance_km, 1, "km")),
        el("dt", {}, "Elevation gain"), el("dd", {}, fmt(trail.elevation_gain_m, 0, "m")),
      ))
  );
}

function showError(message) {
  els.info.setAttribute("aria-busy", "false");
  els.info.replaceChildren(
    el("div", { class: "error-box", role: "alert" },
      el("strong", {}, "Error: "), el("span", {}, message))
  );
}

/* ---------- Start ---------- */

initMap();
els.form.addEventListener("submit", onPlan);
checkHealth();
