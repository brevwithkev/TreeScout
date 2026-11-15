"""TrekChek planner + checker agents.

Flow for every POST /plan:
  1. Parse the request with rules (instant) and prefetch independent tool calls concurrently.
  2. PLANNER (Gemini function calling) extracts constraints, calls tools, picks a trail and explains.
     If Gemini is missing, errors, times out or rate-limits, a rule-based planner takes over.
  3. Python verifies the pick, assembles the plan from tool results only, and runs hard checks.
  4. CHECKER (a second Gemini call + the hard checks) approves or sends problems back. Max 2 rounds.
  5. The response is built in Python in exactly the mock_plan.json shape. Every number comes from a
     tool; the LLM only chooses and explains. mock_plan.json is never read here.
"""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import date as Date, datetime, timedelta
from threading import Lock
from time import perf_counter
import json
import logging
import os
import re

from backend.narration import generate_narration
from backend.tools.alerts import get_nws_alerts
from backend.tools.climate import get_climate
from backend.tools.common import now_iso, timezone_for, unavailable
from backend.tools.anomaly import anomaly_condition, compare_climate
from backend.tools.conditions import derive_conditions
from backend.tools.elevation import get_elevation_gain
from backend.tools.geocode import clean_place, geocode
from backend.tools.hazards import get_hazards
from backend.tools.ranking import _is_loop, rank_trails
from backend.tools.seasonal import get_seasonal_hazards
from backend.tools.space_weather import get_space_weather
from backend.tools.timing import compute_timing
from backend.tools.trails import get_trails, get_trails_anywhere, haversine
from backend.tools.weather import get_hike_window, get_weather

logger = logging.getLogger("treescout.agents")

KM_PER_MI = 1.609344
PREFS = ("easy", "hilly", "shaded", "dog-friendly")
DEFAULT_DISTANCE_KM = 8.0
DEFAULT_START = "10:00"
SEARCH_RADIUS_KM = 15
HAZARD_RADIUS_KM = 100
HAZARD_NEAR_KM = 5
PRECIP_LIMIT = 70            # % chance of precipitation during the hike window (NWS hourly) that rejects a plan
REJECT_SEVERITIES = ("Extreme", "Severe")  # NWS alert severities that reject a plan
WARN_SEVERITIES = ("Moderate",)            # NWS alert severities shown as warnings
MAX_ROUNDS = 2
# No coverage area: any place Nominatim finds and OpenStreetMap has trails for. These are the "no plan" warnings.
NOT_FOUND = "Couldn't find {place}"
LOOKUP_DOWN = "Couldn't find {place} (the place lookup is unavailable right now; try again)"
NO_TRAILS = "No mapped trails found near {place}"
TRAILS_DOWN = "Trail data for {place} is unavailable right now; try again"

LLM_HTTP_DEADLINE_MS = 10_000  # sent to the API as the server deadline; it rejects anything under 10 s
LLM_CALL_TIMEOUT_S = 10       # we stop waiting after this and fall back (fail fast on an overloaded model)
OVERLOAD_CODES = (429, 503, 504)
PLANNER_MAX_TURNS = 6
# No Gemini call (planner turn, checker, revision) starts after this; each is cut off at
# LLM_CALL_TIMEOUT_S, so the last one ends by about 9 + 10 = 19 s and /plan stays under 20 s.
LLM_START_DEADLINE_S = 9
GEMINI_TOP_N = 3  # Gemini may only choose among the top N ranked trails
PREFETCH_WAIT_S = 3  # prefetched non-trail results the planner sees; slower ones are left to the planner

PLACES = {  # town -> (lat, lon): a local gazetteer for the home area, so Ypsilanti plans need no geocoding
    "ypsilanti": (42.2411, -83.6130), "ann arbor": (42.2808, -83.7430), "dexter": (42.3384, -83.8886),
    "saline": (42.1667, -83.7816), "chelsea": (42.3181, -84.0205), "pinckney": (42.4570, -83.9460),
    "brighton": (42.5295, -83.7802), "whitmore lake": (42.4300, -83.7452), "canton": (42.3087, -83.4821),
    "plymouth": (42.3714, -83.4702), "belleville": (42.2048, -83.4852), "milford": (42.5934, -83.5996),
    "novi": (42.4806, -83.4755), "northville": (42.4311, -83.4833), "detroit": (42.3314, -83.0458),
    "dearborn": (42.3223, -83.1763), "livonia": (42.3684, -83.3527), "monroe": (41.9164, -83.3977),
    "howell": (42.6073, -83.9294), "jackson": (42.2459, -84.4013),
}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
NOT_PLACES = set(WEEKDAYS) | {"january", "february", "march", "april", "may", "june", "july", "august",
                              "september", "october", "november", "december", "the", "a", "my", "least"}


def local_place(name):
    """(lat, lon) when the place's first part ("Ypsilanti" in "Ypsilanti, MI") is in PLACES, else None."""
    return PLACES.get(str(name or "").split(",")[0].strip().lower())


def _mi(km):
    return km / KM_PER_MI


# ---------------------------------------------------------------------------------------------
# Trace and concurrent, memoized tool execution
# ---------------------------------------------------------------------------------------------

class Trace:
    def __init__(self):
        self.items = []
        self._lock = Lock()

    def add(self, agent, step, summary, tool=None, ms=None):
        entry = {"agent": agent, "step": step}
        if tool:
            entry["tool"] = tool
        entry["summary"] = summary
        if ms is not None:
            entry["ms"] = int(ms)
        with self._lock:
            self.items.append(entry)
        logger.info("[%s/%s]%s %s%s", agent, step, f" {tool}" if tool else "", summary,
                    f" ({int(ms)} ms)" if ms is not None else "")


def _failed(result):
    return isinstance(result, dict) and result.get("available") is False


def _summarize(name, result, args, label=None):
    if _failed(result):
        return f"{result.get('source')} unavailable: {result.get('error')}"
    if name == "get_trails":
        cached = " (saved copy)" if any(t.get("cached") for t in result["items"]) else ""
        snap = result.get("snapshot")
        if snap:
            when = f", OSM data from {str(snap['saved_at'])[:10]}" if snap.get("saved_at") else ""
            what = f"seeded snapshot of {snap['name']}" if snap["kind"] == "seeded" else "saved snapshot"
            cached = f" (from the {what}{when}; Overpass not queried)"
        radius = result.get("radius_km", args[2] if len(args) > 2 else 0)
        return f"Found {len(result['items'])} named trails within {radius:g} km{cached}"
    if name == "rank_trails":
        if not result:
            return "No trails to rank"
        top = result[0]
        return f"Ranked {len(result)} trails; top: {top['name']} ({top['distance_km']:.1f} km, score {top['score']:.2f})"
    if name == "get_weather":
        return f"NWS {result.get('period') or ''}: {result['summary']}, {result['precip_chance']}% chance of precipitation".replace("  ", " ")
    if name == "get_hazards":
        items = result["items"]
        if not items:
            return f"No active NASA EONET events within {result['radius_km']} km"
        return (f"{len(items)} active NASA EONET event{'s' if len(items) != 1 else ''} within {result['radius_km']} km; "
                f"nearest: {items[0]['title']} ({items[0]['distance_km']:.0f} km)")
    if name == "get_climate":
        return (f"NASA POWER {result['years']}: this week averages a high of {result['avg_high_c']} °C, "
                f"low {result['avg_low_c']} °C, {result['avg_precip_mm']} mm precipitation/day")
    if name == "get_space_weather":
        kp = f", max Kp {result['kp_max']:g}" if result.get("kp_max") is not None else ""
        return f"NASA DONKI: {result['storms']} geomagnetic storms {result['window']}{kp}; aurora chance {result['aurora_chance']}"
    if name == "get_elevation_gain":
        return f"{label or 'Route'}: {result['elevation_gain_m']} m elevation gain"
    if name == "get_nws_alerts":
        items = result["items"]
        cached = f" (cached from {result['checked_at']}; live check failed)" if result.get("stale") else ""
        if not items:
            return f"NWS: no active alerts for this point{cached}"
        return (f"NWS: {len(items)} active alert{'s' if len(items) != 1 else ''}; "
                + "; ".join(f"{a['event']} ({a['severity']})" for a in items[:3]) + cached)
    if name == "get_hike_window":
        conditions = f" ({', '.join(result['conditions'])})" if result.get("conditions") else ""
        cached = f" (cached from {result['checked_at']}; live check failed)" if result.get("stale") else ""
        return (f"NWS hourly {result['window']}: {result['summary'].removeprefix('During your hike: ')}"
                f"{conditions}{cached}")
    if name == "get_seasonal_hazards":
        titles = [i["title"] for i in result["items"]]
        return f"Seasonal: {', '.join(titles)} on {args[0]}" if titles else f"Seasonal: nothing in season on {args[0]}"
    if name == "compare_climate":
        return f"Compared NWS forecast with NASA POWER normals: {result['summary']}"
    if name == "derive_conditions":
        if not result:
            return "Trail conditions: no low light or freezing temperatures"
        return "Trail conditions: " + ", ".join(c["title"] for c in result)
    if name == "geocode":
        if not result.get("found"):
            return f"Nominatim: no match for {result.get('query')}"
        return f"Nominatim: {result['query']} -> {result['lat']:.4f}, {result['lon']:.4f} ({result['display_name']})"
    if name == "compute_timing":
        return (f"{label + ': ' if label else ''}{result['start']} start, about {result['est_duration_min']} min; "
                f"sunset {result['sunset']}, latest safe start {result['latest_safe_start']}")
    return f"{name} done"


def _safe_trails_anywhere(lat, lon):
    """Trails near a geocoded place: saved snapshot, else live Overpass at 10 km (20 km if under 3 trails)."""
    source = "OpenStreetMap (Overpass)"
    try:
        items, radius, snapshot = get_trails_anywhere(lat, lon)
        out = {"source": source, "items": items, "radius_km": radius, "checked_at": now_iso()}
        if snapshot:
            out["snapshot"] = snapshot
        return out
    except Exception as exc:  # noqa: BLE001 - OverpassError, ValueError
        return unavailable(source, str(exc))


def _safe_trails(lat, lon, radius_km):
    """The home area (Ypsilanti towns and the request's own lat/lon): unchanged since before "anywhere"."""
    source = "OpenStreetMap (Overpass)"
    try:
        return {"source": source, "items": get_trails(lat, lon, radius_km), "checked_at": now_iso()}
    except Exception as exc:  # noqa: BLE001 - OverpassError, ValueError
        return unavailable(source, str(exc))


class Tools:
    """Runs tools in a thread pool. Identical calls share one execution (and one trace line)."""

    def __init__(self, trace, pool):
        self.trace, self.pool = trace, pool
        self._futures, self._lock = {}, Lock()

    def submit(self, name, fn, *args, label=None):
        key = (name, json.dumps(args, default=str, sort_keys=True))
        with self._lock:
            if key not in self._futures:
                self._futures[key] = self.pool.submit(self._run, name, fn, args, label)
            return self._futures[key]

    def call(self, name, fn, *args, label=None):
        return self.submit(name, fn, *args, label=label).result()

    def _run(self, name, fn, args, label):
        started = perf_counter()
        try:
            result = fn(*args)
        except Exception as exc:  # noqa: BLE001 - tools should not raise, but never let one crash /plan
            result = unavailable(name, f"{type(exc).__name__}: {exc}")
        self.trace.add("planner", "tool", _summarize(name, result, args, label), tool=name,
                       ms=(perf_counter() - started) * 1000)
        return result


# ---------------------------------------------------------------------------------------------
# Rule-based request parsing (also the fallback planner's understanding)
# ---------------------------------------------------------------------------------------------

def parse_request(text, today):
    """Regex parse: distance (mi/km), date (today/tomorrow/weekday/ISO), start time, prefs, area."""
    t = text.lower()
    out = {}

    m = re.search(r"(\d+(?:\.\d+)?)\s*-?\s*(miles?|mi\b|kilometers?|kilometres?|km\b|k\b)", t)
    if m:
        value = float(m.group(1))
        km = value * KM_PER_MI if m.group(2).startswith("mi") else value
        if re.search(r"(under|less than|at most|no more than|up to|max(imum)?|below)\s*$", t[:m.start()]):
            km *= 0.85  # "under 5 km" -> aim a bit below the limit
        out["distance_km"] = round(km, 1)

    iso = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", t)
    if iso:
        out["date"] = iso.group(1)
    elif "tomorrow" in t:
        out["date"] = (today + timedelta(days=1)).isoformat()
    elif re.search(r"\btoday\b|\btonight\b", t):
        out["date"] = today.isoformat()
    else:
        for i, day in enumerate(WEEKDAYS):
            wd = re.search(rf"\b(next\s+)?{day}\b", t)
            if wd:
                ahead = (i - today.weekday()) % 7
                if wd.group(1) and ahead == 0:
                    ahead = 7
                out["date"] = (today + timedelta(days=ahead)).isoformat()
                break

    tm = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)", t)
    if tm:
        hour = int(tm.group(1)) % 12 + (12 if tm.group(3).startswith("p") else 0)
        out["start"] = f"{hour:02d}:{int(tm.group(2) or 0):02d}"
    elif "noon" in t:
        out["start"] = "12:00"
    elif "morning" in t:
        out["start"] = "09:00"
    elif "afternoon" in t:
        out["start"] = "13:00"
    elif "evening" in t:
        out["start"] = "16:00"

    prefs = []
    for pref, pattern in [("easy", r"\b(easy|easier|flat|gentle|beginner)\b"),
                          ("hilly", r"\b(hill|hills|hilly|climb|climbing|steep|elevation)\b"),
                          ("shaded", r"\b(shade|shaded|shady|forest|forested|woods|wooded)\b"),
                          ("dog-friendly", r"\b(dog|dogs|dog-friendly|pup|puppy)\b")]:
        if re.search(pattern, t):
            prefs.append(pref)
    out["prefs"] = prefs

    for place, (lat, lon) in PLACES.items():
        if re.search(rf"\b{place}\b", t):
            out["area_name"], out["area_lat"], out["area_lon"] = place.title(), lat, lon
            break
    else:
        named = re.search(r"\b(?:in|near|around|at|to)\s+(?:the\s+)?([A-Z][A-Za-z.'-]+(?:,?\s+[A-Z][A-Za-z.'-]+)*)",
                          text)
        words = named.group(1).split() if named else []
        while words and words[-1].strip(",").lower() in NOT_PLACES:  # "near Banff Saturday" -> "Banff"
            words.pop()
        if words:
            out["unknown_place"] = " ".join(words).strip(",")
    return out


def _valid_date(value, today):
    try:
        day = Date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    return day.isoformat() if today <= day <= today + timedelta(days=60) else None


def _valid_start(value):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", str(value or "").strip())
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m and int(m.group(1)) < 24 and int(m.group(2)) < 60 else None


def _valid_km(value):
    try:
        km = float(value)
    except (TypeError, ValueError):
        return None
    return round(km, 1) if 0.5 <= km <= 80 else None


def merge_constraints(form, llm, rules, today):
    """Explicit form fields > LLM extraction > rules > defaults. Every value is validated."""
    llm, form = llm or {}, form or {}

    def pick(key, valid):
        for source in (form, llm, rules):
            value = valid(source.get(key))
            if value is not None:
                return value
        return None

    prefs = [p for p in (form.get("prefs") or []) if p in PREFS]  # empty form prefs = "not set"
    if not prefs:
        prefs = [p for p in (llm.get("prefs") if isinstance(llm.get("prefs"), list) else rules.get("prefs") or [])
                 if p in PREFS]
    return {
        "distance_km": pick("distance_km", _valid_km) or DEFAULT_DISTANCE_KM,
        "date": pick("date", lambda v: _valid_date(v, today)) or (today + timedelta(days=1)).isoformat(),
        "start": pick("start", _valid_start) or DEFAULT_START,
        "prefs": prefs,
    }


# ---------------------------------------------------------------------------------------------
# Gemini planner and checker
# ---------------------------------------------------------------------------------------------

class LLMUnavailable(Exception):
    pass


def gemini_configured():
    return bool(os.environ.get("GEMINI_API_KEY", "").strip() and os.environ.get("GEMINI_MODEL", "").strip())


def _gemini():
    key, model = os.environ.get("GEMINI_API_KEY", "").strip(), os.environ.get("GEMINI_MODEL", "").strip()
    if not key or not model:
        raise LLMUnavailable("GEMINI_API_KEY or GEMINI_MODEL is not set")
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise LLMUnavailable(f"google-genai is not installed: {exc}") from exc
    http = types.HttpOptions(timeout=LLM_HTTP_DEADLINE_MS, retry_options=types.HttpRetryOptions(attempts=1))
    return genai.Client(api_key=key, http_options=http), model, types


# Gemini calls run here so we can stop waiting after LLM_CALL_TIMEOUT_S; an abandoned call simply
# finishes (or hits the 10 s server deadline) in the background and its result is ignored.
_LLM_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="gemini")


def _describe_llm_error(exc):
    code = getattr(exc, "code", None)
    if code in OVERLOAD_CODES:
        return f"{code} {getattr(exc, 'status', '') or ''} (overloaded or rate limited)".replace("  ", " ")
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def _llm_call(ctx, fn, what):
    """Run one Gemini call with fail-fast rules: at most LLM_CALL_TIMEOUT_S, no retries, and after any
    failure (429/503/504, timeout, other error) no further Gemini calls in this request."""
    if ctx.llm_down:
        raise LLMUnavailable(f"{what} skipped: Gemini already failed in this request ({ctx.llm_down})")
    future = _LLM_POOL.submit(fn)
    try:
        return future.result(timeout=LLM_CALL_TIMEOUT_S)
    except FuturesTimeout:
        reason = f"no answer within {LLM_CALL_TIMEOUT_S} s"
    except Exception as exc:  # noqa: BLE001 - google.genai ClientError/ServerError, network errors
        reason = _describe_llm_error(exc)
    ctx.llm_down = reason
    raise LLMUnavailable(f"{what}: {reason}")


def _thinking(types, model):
    # Gemini 3+ "low" thinking keeps latency down. Temperature/top_p/top_k are deliberately not set.
    return None if model.startswith("gemini-2") else types.ThinkingConfig(thinking_level="low")


def _json_from_text(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"No JSON object in model reply: {text[:120]!r}")
    return json.loads(text[start:end + 1])


PLANNER_TOOLS = [
    ("get_trails", "Named hiking trails >= 1 km near a place (OpenStreetMap). Give the most specific hikeable place "
                   "NAME with region and country (e.g. \"Grand Canyon Village, Arizona, USA\"); TrekChek looks it up. "
                   "Call first.",
     {"place": "string"}, ["place"]),
    ("rank_trails", "Score the trails from get_trails against the constraints (0-1), around the last get_trails place.",
     {"distance_km": "number", "prefs": "array"}, ["distance_km"]),
    ("get_elevation_gain", "Total ascent in meters along a trail from get_trails.", {"osm_id": "integer"}, ["osm_id"]),
    ("compute_timing", "Duration, sunset, latest safe start and turnaround time for a trail and start time.",
     {"osm_id": "integer", "date": "string", "start_time": "string"}, ["osm_id", "date", "start_time"]),
    ("get_weather", "NWS daytime forecast (US only) for the last get_trails place and a date (YYYY-MM-DD).",
     {"date": "string"}, ["date"]),
    ("get_hazards", "Open NASA EONET natural hazard events within radius_km of the last get_trails place.",
     {"radius_km": "number"}, []),
    ("get_climate", "NASA POWER 20-year averages for the same calendar week at the last get_trails place.",
     {"date": "string"}, ["date"]),
    ("get_space_weather", "NASA DONKI geomagnetic storms and aurora chance tonight.", {}, []),
]


def _declarations(types):
    decls = []
    for name, desc, props, required in PLANNER_TOOLS:
        schema = {"type": "object", "properties": {
            k: ({"type": "array", "items": {"type": "string", "enum": list(PREFS)}} if v == "array" else {"type": v})
            for k, v in props.items()}}
        if required:
            schema["required"] = required
        decls.append(types.FunctionDeclaration(name=name, description=desc, parameters_json_schema=schema))
    return [types.Tool(function_declarations=decls)]


PLANNER_PROMPT = """You are the TrekChek planner. You plan day hikes anywhere OpenStreetMap has mapped trails.
Today is {today} ({weekday}). Times are local to the place. If the request names no place, TrekChek searches around the
hiker's location ({lat}, {lon}).

Steps:
1. Extract constraints from the request: distance_km, date (YYYY-MM-DD), start (HH:MM, 24 h), prefs (subset of easy, hilly, shaded, dog-friendly), and place: the most specific hikeable place for the request, with region and country, as a search string (e.g. "Grand Canyon Village, Arizona, USA" for the Grand Canyon, "Banff, Alberta, Canada"): a town, village, park or trailhead rather than a natural feature such as a canyon or cliff. null if the request names no place. Never give coordinates: TrekChek looks the name up.
   These form values were set explicitly by the user and override the text: {form}
2. If get_trails finds no trails (or the place cannot be found), answer with "chosen_osm_id": null.
3. TrekChek has already run the tools for the area and constraints it guessed from the request; their results are in the
   first message ("prefetched"). Use them. Call tools only for what is missing or different: another place (get_trails, then
   rank_trails), different constraints (rank_trails), or timing for another trail or start time (compute_timing).
   Put independent calls in the same turn. If the prefetched results answer the request, reply right away.
4. Choose the trail. Matching the requested distance matters most: pick from the top {top_n} of rank_trails, and among
   them prefer the trail whose distance is closest to the request. A choice outside the top {top_n} is replaced by the
   top-ranked trail. Do not switch to a shorter trail just to fit a late start time: the safety checker moves the start
   earlier if needed.
5. Answer with JSON only, no prose:
{{"chosen_osm_id": <int or null>, "constraints": {{"distance_km": <number>, "date": "YYYY-MM-DD", "start": "HH:MM", "prefs": [...], "place": "<place name or null>"}}, "explanation": "<1-2 sentences>"}}

Rules: every number in the explanation must come from a tool result, never from memory. The explanation must name the chosen
trail and its distance in km and miles. There is no data about shade or dog rules: never claim a trail is shaded or dog-friendly."""

CHECKER_PROMPT = """You are the TrekChek safety checker. Review this assembled day-hike plan (all values come from data tools).
Approve unless the data shows a concrete safety problem: finishing after sunset, a start later than the latest safe start,
an active natural hazard near the trail, a Severe or Extreme NWS alert, a high chance of precipitation during the hike
(judge rain by hike_window.max_precip_chance, the NWS hourly forecast for the hike's hours, not the daily weather), or a
forecast that is dangerous for the hike (for example thunderstorms, or temperatures below freezing with precipitation).
Moderate or Minor alerts, seasonal items (such as hunting season), trail conditions (low light, freezing temperatures
alone, unusual conditions) and climate_anomaly (forecast vs 20-year normals) are shown to the hiker as warnings: they are
not reasons to reject. Missing data alone is not a problem.
Each problem is one sentence that quotes the numbers. Do not invent data."""


class Planner:
    """Gemini planner: function calling over a chat, so checker feedback can continue the conversation."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.client, self.model, self.types = _gemini()
        prompt = PLANNER_PROMPT.format(top_n=GEMINI_TOP_N, today=ctx.today.isoformat(), weekday=WEEKDAYS[ctx.today.weekday()].title(),
                                       lat=ctx.body_lat, lon=ctx.body_lon, form=json.dumps(ctx.form) or "{}")
        self.chat = self.client.chats.create(model=self.model, config=self.types.GenerateContentConfig(
            system_instruction=prompt, tools=_declarations(self.types), thinking_config=_thinking(self.types, self.model)))

    def run(self, message):
        types = self.types
        for _ in range(PLANNER_MAX_TURNS):
            if self.ctx.elapsed() > LLM_START_DEADLINE_S:
                raise LLMUnavailable(f"planner turn would start after {LLM_START_DEADLINE_S} s")
            reply = _llm_call(self.ctx, lambda m=message: self.chat.send_message(m), "planner")
            calls = reply.function_calls or []
            if not calls:
                return _json_from_text(reply.text)
            futures = [(call, self.ctx.dispatch(call.name, dict(call.args or {}))) for call in calls]
            message = [types.Part(function_response=types.FunctionResponse(
                id=call.id, name=call.name, response={"result": self.ctx.for_llm(call.name, future)}))
                for call, future in futures]
        raise LLMUnavailable(f"planner did not answer within {PLANNER_MAX_TURNS} turns")


def llm_check(ctx, summary):
    client, model, types = _gemini()
    schema = {"type": "object", "properties": {"approved": {"type": "boolean"},
                                               "problems": {"type": "array", "items": {"type": "string"}}},
              "required": ["approved", "problems"]}
    reply = _llm_call(ctx, lambda: client.models.generate_content(
        model=model, contents=json.dumps(summary, default=str),
        config=types.GenerateContentConfig(system_instruction=CHECKER_PROMPT, response_mime_type="application/json",
                                           response_json_schema=schema, thinking_config=_thinking(types, model))),
        "checker")
    try:
        verdict = _json_from_text(reply.text)
    except (ValueError, TypeError) as exc:
        raise LLMUnavailable(f"checker reply was not JSON: {exc}") from exc
    return bool(verdict.get("approved")), [str(p) for p in verdict.get("problems") or [] if str(p).strip()]


# ---------------------------------------------------------------------------------------------
# One planning request
# ---------------------------------------------------------------------------------------------

class PlanContext:
    def __init__(self, text, body_lat, body_lon, radius_km, form, pool):
        self.started = perf_counter()
        self.text, self.body_lat, self.body_lon, self.radius_km = text, body_lat, body_lon, radius_km
        self.form = {k: v for k, v in (form or {}).items() if v not in (None, "", [])}
        self.today = datetime.now(timezone_for(body_lat, body_lon)).date()  # the hiker's own date
        self.trace = Trace()
        self.tools = Tools(self.trace, pool)
        self.rules = parse_request(text, self.today)
        self.trails_by_id = {}
        self.llm_down = None  # reason string once any Gemini call failed; no more Gemini calls after that
        self.user_osm_id = None  # trail the user chose from "Other options" (no Gemini calls then)
        self.anywhere = set()  # rounded (lat, lon) of geocoded places: their trails use get_trails_anywhere
        self.area = None      # the resolved place's (lat, lon); the Gemini tools without a place look here
        self.resolved = None  # the last place name that resolved: {"lat", "lon", "name"}; never replaced by the default

    def elapsed(self):
        return perf_counter() - self.started

    # -- tool helpers ---------------------------------------------------------------------------
    def resolve(self, place):
        """A place name -> {"lat", "lon", "name"} or {"error", "name"}; None for no name. Home-area towns come from
        PLACES (no lookup, so Ypsilanti plans are unchanged); everything else from Nominatim."""
        name = clean_place(place)
        if name is None:
            return None
        here = local_place(name)
        if here:
            self.resolved = {"lat": here[0], "lon": here[1], "name": name}
            return dict(self.resolved)
        found = self.tools.call("geocode", geocode, name)
        if _failed(found):
            return {"error": LOOKUP_DOWN.format(place=name), "name": name}
        if not found.get("found"):
            return {"error": NOT_FOUND.format(place=name), "name": name}
        lat, lon = round(found["lat"], 4), round(found["lon"], 4)
        self.anywhere.add((lat, lon))
        self.resolved = {"lat": lat, "lon": lon, "name": name}
        return dict(self.resolved)

    def here(self):
        """Where location tools run: the resolved place, else (no place named) the request's own lat/lon."""
        return self.area or (self.body_lat, self.body_lon)

    def trails_future(self, lat, lon):
        lat, lon = round(lat, 4), round(lon, 4)
        if (lat, lon) in self.anywhere:
            return self.tools.submit("get_trails", _safe_trails_anywhere, lat, lon)
        return self.tools.submit("get_trails", _safe_trails, lat, lon, self.radius_km)

    def trails(self, lat, lon):
        result = self.trails_future(lat, lon).result()
        if not _failed(result):
            self.trails_by_id.update({t["osm_id"]: t for t in result["items"]})
        return result

    def elevation(self, trail):
        return self.tools.submit("get_elevation_gain", get_elevation_gain, trail["coords"], trail["osm_id"],
                                 label=trail["name"])

    def timing(self, trail, gain, day, start):
        lat, lon = trail["coords"][0]
        return self.tools.submit("compute_timing", compute_timing, trail["distance_km"], gain, lat, lon, day, start,
                                 label=trail["name"])

    def alerts(self, area):
        return self.tools.submit("get_nws_alerts", get_nws_alerts, round(area[0], 4), round(area[1], 4))

    def seasonal(self, area, day):
        return self.tools.submit("get_seasonal_hazards", get_seasonal_hazards, day, round(area[0], 4),
                                 round(area[1], 4))

    def hike_window(self, area, day, timing):
        """NWS hourly summary for start .. start + est_duration_min (unavailable without timing)."""
        if _failed(timing):
            return _Later(lambda: unavailable("NWS hourly", "No hike timing to look up"))
        return self.tools.submit("get_hike_window", get_hike_window, round(area[0], 4), round(area[1], 4), day,
                                 timing["start"], timing["est_duration_min"])

    def rank(self, items, constraints, lat, lon):
        def run(_key):
            return rank_trails(items, constraints, lat, lon)
        key = (constraints.get("distance_km"), sorted(constraints.get("prefs") or []), round(lat, 4), round(lon, 4),
               sorted((t["osm_id"], t.get("elevation_gain_m")) for t in items))
        return self.tools.call("rank_trails", run, key)

    # -- LLM function-call dispatch ----------------------------------------------------------------
    def dispatch(self, name, a):
        """Start the tool for one Gemini function call; returns a Future-like with .result()."""
        def num(key, default=None):
            try:
                return float(a.get(key, default))
            except (TypeError, ValueError):
                return default
        if name == "get_trails":  # a place name; never coordinates from the model
            found = self.resolve(a.get("place"))
            if found is not None and "error" in found:
                return _Later(lambda: {"error": found["error"]})
            if found is not None:
                self.area = (found["lat"], found["lon"])
            return self.trails_future(*self.here())
        lat, lon = self.here()
        if name == "rank_trails":
            constraints = {"distance_km": num("distance_km"), "prefs": [p for p in a.get("prefs") or [] if p in PREFS]}
            center = self.here()
            return _Later(lambda: self.rank(list(self.trails_by_id.values()), constraints, *center))
        if name == "get_weather":
            return self.tools.submit("get_weather", get_weather, round(lat, 4), round(lon, 4), str(a.get("date")))
        if name == "get_hazards":
            return self.tools.submit("get_hazards", get_hazards, round(lat, 4), round(lon, 4),
                                     num("radius_km", HAZARD_RADIUS_KM))
        if name == "get_climate":
            return self.tools.submit("get_climate", get_climate, round(lat, 4), round(lon, 4), str(a.get("date")))
        if name == "get_space_weather":
            return self.tools.submit("get_space_weather", get_space_weather)
        if name not in ("get_elevation_gain", "compute_timing"):
            return _Later(lambda: {"error": f"Unknown tool {name}"})

        def by_trail():  # resolved lazily so a get_trails call earlier in the same turn is seen
            trail = self.trails_by_id.get(int(num("osm_id", -1)))
            if trail is None:
                return {"error": f"Unknown osm_id {a.get('osm_id')}: call get_trails first and use its osm_id values"}
            if name == "get_elevation_gain":
                return self.elevation(trail).result()
            gain = self.elevation(trail).result().get("elevation_gain_m")
            return self.timing(trail, gain, str(a.get("date")), str(a.get("start_time"))).result()
        return _Later(by_trail)

    def for_llm(self, name, future):
        """Tool result trimmed for the model (no coordinates, at most a few list items)."""
        result = future.result()
        if isinstance(result, dict) and (_failed(result) or "error" in result):
            return result
        if name == "get_trails":
            self.trails_by_id.update({t["osm_id"]: t for t in result["items"]})
            center = list(self.here())
            items = sorted(result["items"], key=lambda t: haversine(center, t["coords"][0]))[:30]
            return {"count": len(result["items"]), "trails": [
                {"osm_id": t["osm_id"], "name": t["name"], "distance_km": t["distance_km"],
                 "distance_mi": round(_mi(t["distance_km"]), 1), "loop": _is_loop(t["coords"])} for t in items]}
        if name == "rank_trails":
            return [{"osm_id": t["osm_id"], "name": t["name"], "distance_km": t["distance_km"],
                     "distance_mi": round(_mi(t["distance_km"]), 1), "score": t["score"],
                     "score_parts": t["score_parts"]} for t in result[:8]]
        if name == "get_hazards":
            return {**result, "items": result["items"][:5]}
        return result


class _Later:
    """Future-like wrapper that computes its value on first .result()."""

    def __init__(self, fn):
        self.fn, self.done, self.value = fn, False, None

    def result(self):
        if not self.done:
            self.value, self.done = self.fn(), True
        return self.value


def _hazards_near_route(hazards, coords, limit_km):
    if _failed(hazards) or not coords:
        return []
    near = []
    for h in hazards["items"]:
        d = min(haversine([h["lat"], h["lon"]], p) for p in coords[:: max(1, len(coords) // 200)])
        if d <= limit_km:
            near.append((h, d))
    return near


def _hm(minutes):
    h, m = divmod(int(minutes), 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def make_plan(text, lat, lon, radius_km=SEARCH_RADIUS_KM, form=None, osm_id=None):
    """osm_id: the user picked this trail from "Other options"; it replaces the planner's pick (no Gemini calls)."""
    pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix="tool")
    try:
        ctx = PlanContext(text, lat, lon, radius_km, form, pool)
        ctx.user_osm_id = osm_id
        logger.info("Plan request: %r form=%s", text, ctx.form)
        return _plan(ctx)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)  # don't hold the response for unused prefetches


def _plan(ctx):
    rules, trace, today = ctx.rules, ctx.trace, ctx.today
    rules_constraints = merge_constraints(ctx.form, None, rules, today)
    # WHERE comes first, before any location tool: a home-area town (PLACES) or a place name the rules found
    # (Nominatim). The request's own lat/lon is used only when no place is named at all; when the rules find no
    # name but Gemini will run, nothing location-based runs until Gemini names the place (or says there is none).
    # Once a place resolves (ctx.resolved) it is never replaced by the default, even if Gemini fails or times out.
    named = rules.get("area_name") or rules.get("unknown_place")
    area_error = None
    found = ctx.resolve(named) if named else None
    if found and "error" in found:
        area_error = found["error"]
    if ctx.resolved is not None:
        rules_area = (ctx.resolved["lat"], ctx.resolved["lon"])
    elif named or (ctx.user_osm_id is None and gemini_configured()):
        rules_area = None  # a name that didn't resolve, or Gemini may still name the place
    else:
        rules_area = (ctx.body_lat, ctx.body_lon)  # the request names no place
    ctx.area = rules_area

    # Prefetch independent tools while the LLM thinks (identical LLM calls reuse these results).
    if rules_area is not None:
        ctx.trails_future(*rules_area)
        for name, fn in (("get_weather", get_weather), ("get_climate", get_climate)):
            ctx.tools.submit(name, fn, round(rules_area[0], 4), round(rules_area[1], 4), rules_constraints["date"])
        ctx.tools.submit("get_hazards", get_hazards, round(rules_area[0], 4), round(rules_area[1], 4), HAZARD_RADIUS_KM)
    space = ctx.tools.submit("get_space_weather", get_space_weather)
    prefetched = _prefetch_context(ctx, rules_area, rules_constraints, space)

    # ---- PLANNER, round 1 -------------------------------------------------------------------------
    planner, llm_answer, parsed_by = None, None, "rules"
    try:
        if ctx.user_osm_id is not None:
            raise LLMUnavailable("the user chose a trail")  # no Gemini call; the user's choice replaces the pick
        planner = Planner(ctx)
        llm_answer = planner.run(f"Request: {ctx.text}\nprefetched: {json.dumps(prefetched, default=str)}")
        parsed_by = "gemini"
        picked = ctx.trails_by_id.get(_as_int(llm_answer.get("chosen_osm_id")))
        trace.add("planner", "decide", f"Gemini proposed {picked['name']} (osm_id {picked['osm_id']})" if picked
                  else f"Gemini proposed osm_id {llm_answer.get('chosen_osm_id')}")
    except (LLMUnavailable, ValueError) as exc:
        planner = None
        if ctx.user_osm_id is None:
            logger.warning("LLM planner unavailable: %s", exc)
            trace.add("planner", "fallback", "LLM unavailable, used rule-based planner")

    llm_constraints = (llm_answer or {}).get("constraints") if isinstance((llm_answer or {}).get("constraints"), dict) else {}
    constraints = merge_constraints(ctx.form, llm_constraints, rules, today)
    # True when the hiker asked for a distance (form, text via Gemini, or text via rules), not the default.
    distance_given = any(_valid_km(src.get("distance_km")) for src in (ctx.form, llm_constraints, rules))
    llm_place = clean_place(llm_constraints.get("place")) if parsed_by == "gemini" else None
    if llm_place and llm_place.lower() in ("null", "none"):
        llm_place = None
    if llm_place and not (ctx.resolved and llm_place.lower() == ctx.resolved["name"].lower()):
        before = ctx.resolved
        found = ctx.resolve(llm_place)  # Gemini gives a name only; TrekChek looks it up
        if "error" in found:
            if before is not None:  # keep the place that did resolve
                ctx.resolved = before
                trace.add("planner", "decide", f"{found['error']}; keeping {before['name']}")
            else:
                area_error = found["error"]
    if ctx.resolved is not None:      # a named place: never the default
        area, place = (ctx.resolved["lat"], ctx.resolved["lon"]), ctx.resolved["name"]
    elif named or llm_place:          # a place was named but could not be found: no plan
        area, place = None, named or llm_place
        area_error = area_error or NOT_FOUND.format(place=place)
    else:                             # no place named at all: the request's own lat/lon
        area, place = (ctx.body_lat, ctx.body_lon), None
    ctx.area = area

    if area is None:  # the place name was not found (or the lookup is down): no plan
        trace.add("planner", "reject", area_error)
        return _response(ctx, constraints, parsed_by, None, [], area_error + ".", None, None, None,
                         space.result(), None, {"approved": False, "rounds": 0}, [area_error])

    trails = ctx.trails(*area)
    if _failed(trails) or not trails["items"]:
        where = place or "this area"
        message = TRAILS_DOWN.format(place=where) if _failed(trails) else NO_TRAILS.format(place=where)
        trace.add("planner", "reject", message if not _failed(trails) else f"{message} ({trails.get('error')})")
        # Sections that don't depend on a trail still report their own results.
        here = (round(area[0], 4), round(area[1], 4))
        weather = ctx.tools.submit("get_weather", get_weather, *here, constraints["date"])
        climate = ctx.tools.submit("get_climate", get_climate, *here, constraints["date"])
        hazards = ctx.tools.submit("get_hazards", get_hazards, *here, HAZARD_RADIUS_KM)
        extra = {"alerts": ctx.alerts(area).result(), "seasonal": ctx.seasonal(area, constraints["date"]).result(),
                 "hike_window": unavailable("NWS hourly", "No trail chosen, so there is no hike window"),
                 "conditions": []}
        extra["climate_anomaly"] = compare_climate(extra["hike_window"], climate.result())
        return _response(ctx, constraints, parsed_by, None, [], message + ".", None, weather.result(),
                         climate.result(), space.result(), hazards.result(), {"approved": False, "rounds": 0},
                         [message] + _advisories(extra), extra)

    # Rank, then add elevation for the top candidates and re-rank (elevation feeds the easy/hilly score).
    ranked = ctx.rank(trails["items"], constraints, *area)
    gains = {t["osm_id"]: ctx.elevation(t) for t in ranked[:3]}
    with_gain = [{**t, "elevation_gain_m": gains[t["osm_id"]].result().get("elevation_gain_m")} if t["osm_id"] in gains
                 else t for t in ranked]
    ranked = ctx.rank(with_gain, constraints, *area)

    user_trail = None
    if ctx.user_osm_id is not None:
        user_trail = next((t for t in ranked if t["osm_id"] == ctx.user_osm_id), None)
        if user_trail is None:
            trace.add("planner", "reject", f"Requested trail osm_id {ctx.user_osm_id} is not in the trail data for "
                                           "this area; planned as usual")
        else:
            trace.add("planner", "decide", f"User chose {user_trail['name']} from other options")

    # ---- choose, assemble, check: up to MAX_ROUNDS --------------------------------------------------
    rejected, attempts = set(), []
    answer = llm_answer
    asked_start = constraints["start"]  # before any checker-driven revision
    for round_no in range(1, MAX_ROUNDS + 1):
        chosen = user_trail or _choose(ctx, answer, ranked, rejected)
        if answer and isinstance(answer.get("constraints"), dict) and round_no > 1:
            constraints = merge_constraints({k: v for k, v in ctx.form.items() if k != "start"},
                                            answer["constraints"], {**rules, **constraints}, today)
        plan = _assemble(ctx, chosen, constraints, area)
        problems = _hard_checks(plan)
        approved = not problems
        for p in problems:
            trace.add("checker", "reject", p)

        llm_ok, llm_problems, llm_checked = True, [], False
        if parsed_by == "gemini" and ctx.llm_down:
            trace.add("checker", "fallback", "Gemini failed earlier in this request; used Python safety checks only")
        elif parsed_by == "gemini" and ctx.elapsed() <= LLM_START_DEADLINE_S:
            try:
                llm_ok, llm_problems = llm_check(ctx, _checker_view(plan, constraints))
                llm_checked = True
                for p in llm_problems:
                    if p not in problems:
                        trace.add("checker", "reject", p)
            except LLMUnavailable as exc:
                logger.warning("LLM checker unavailable: %s", exc)
                trace.add("checker", "fallback", "LLM checker unavailable, used Python safety checks only")
        elif parsed_by == "gemini":
            trace.add("checker", "fallback", f"Skipped the Gemini checker: more than {LLM_START_DEADLINE_S} s had "
                                             "passed, so /plan stays under 20 s. Python checks only.")
        approved = approved and llm_ok
        all_problems = problems + [p for p in llm_problems if p not in problems]
        attempts.append((plan, all_problems, approved, answer))

        if approved:
            trace.add("checker", "decide", ("Gemini and Python checks approved: " if llm_checked
                                            else "Python checks approved: ") + _approval_line(plan))
            break
        rejected.add(chosen["osm_id"])
        if round_no == MAX_ROUNDS:
            break
        if user_trail is not None and not _late_start_only(all_problems, plan["timing"]):
            break  # keep the user's trail; its problems become warnings (only a late start is fixed)
        # Send problems back to the planner for a different trail or start time.
        answer, constraints = _revise(ctx, planner, all_problems, plan, ranked, rejected, constraints)
        if answer is None and planner is not None:
            planner = None  # the LLM failed during revision; _revise already used the rules

    plan, problems, approved, final_answer = (attempts[-1] if attempts[-1][2]
                                              else min(attempts, key=lambda a: len(a[1])))
    explanation = _explanation(final_answer, plan, ranked, len(trails["items"]))
    trace.add("planner", "decide", f"Chose {plan['trail']['name']} ({plan['trail']['distance_km']:.1f} km, "
                                   f"score {plan['score']:.2f})")
    warnings = [] if approved else list(problems)
    moved = _start_moved_warning(asked_start, plan["constraints"]["start"], plan["timing"], approved)
    if moved:
        warnings.insert(0, moved)
    far = _distance_warning(plan["constraints"]["distance_km"], distance_given, trails["items"], plan["trail"])
    if far:
        warnings.insert(0, far)
    warnings += [w for w in _advisories(plan) if w not in warnings]
    now = datetime.now(timezone_for(*area))  # the trail's local time
    if plan["constraints"]["date"] == now.date().isoformat() and plan["constraints"]["start"] < now.strftime("%H:%M"):
        warnings.append(f"The {plan['constraints']['start']} start time has already passed today.")
    alternatives = [{"name": t["name"], "osm_id": t["osm_id"], "distance_km": t["distance_km"], "score": t["score"]}
                    for t in ranked if t["osm_id"] != plan["trail"]["osm_id"]][:2]
    return _response(ctx, plan["constraints"], parsed_by, plan["trail"], alternatives, explanation, plan["timing"],
                     plan["weather"], plan["climate"], space.result(), plan["hazards"],
                     {"approved": approved, "rounds": len(attempts)}, warnings,
                     {k: plan[k] for k in ("alerts", "hike_window", "seasonal", "conditions", "climate_anomaly")})


def _prefetch_context(ctx, area, constraints, space):
    """Run trails -> rank -> elevation -> timing for the rules guess, and summarize every prefetched
    result for the planner's first message, so it can usually decide in one LLM turn."""
    out = {"guessed_place": ctx.rules.get("area_name") or ctx.rules.get("unknown_place"),
           "guessed_constraints": constraints}
    if area is None:
        out["get_trails"] = {"note": "No place resolved yet. Call get_trails with the place NAME from the request "
                                     "(an empty place only if the request names none)."}
    if area is not None:
        trails = ctx.trails(*area)
        if _failed(trails):
            out["get_trails"] = trails
        elif trails["items"]:
            ranked = ctx.rank(trails["items"], constraints, *area)
            gains = {t["osm_id"]: ctx.elevation(t) for t in ranked[:3]}
            ranked = ctx.rank([{**t, "elevation_gain_m": gains[t["osm_id"]].result().get("elevation_gain_m")}
                               if t["osm_id"] in gains else t for t in ranked], constraints, *area)
            out["rank_trails"] = ctx.for_llm("rank_trails", _Later(lambda: ranked))
            for row, t in zip(out["rank_trails"], ranked):
                row["loop"], row["elevation_gain_m"] = _is_loop(t["coords"]), t.get("elevation_gain_m")
            top = ranked[0]
            out["compute_timing_for_top"] = ctx.timing(top, top.get("elevation_gain_m"), constraints["date"],
                                                       constraints["start"]).result()
            ctx.hike_window(area, constraints["date"], out["compute_timing_for_top"])  # warms the NWS hourly cache
        here = (round(area[0], 4), round(area[1], 4))
        ctx.alerts(area)  # not shown to the planner; started now so _assemble usually finds them done
        ctx.seasonal(area, constraints["date"])
        futures = {
            "get_weather": ctx.tools.submit("get_weather", get_weather, *here, constraints["date"]),
            "get_climate": ctx.tools.submit("get_climate", get_climate, *here, constraints["date"]),
            "get_hazards": ctx.tools.submit("get_hazards", get_hazards, *here, HAZARD_RADIUS_KM),
        }
    else:
        futures = {}
    futures["get_space_weather"] = space
    deadline = perf_counter() + PREFETCH_WAIT_S
    for name, future in futures.items():  # never hold the planner for a slow source
        try:
            result = future.result(timeout=max(0.0, deadline - perf_counter()))
        except FuturesTimeout:
            out[name] = "pending: still loading; call the tool if you need it"
            continue
        if name == "get_hazards" and not _failed(result):
            result = {**result, "items": result["items"][:5]}
        out[name] = result
    return out


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _choose(ctx, answer, ranked, rejected):
    """The planner's pick if it is one of the top GEMINI_TOP_N ranked trails (not yet rejected), else the top one."""
    candidates = [t for t in ranked if t["osm_id"] not in rejected] or ranked
    wanted = (answer or {}).get("chosen_osm_id")
    if wanted is not None:
        try:
            position = next((i for i, t in enumerate(candidates) if t["osm_id"] == int(wanted)), None)
        except (TypeError, ValueError):
            position = None
        if position is None:
            ctx.trace.add("planner", "reject", f"Planner picked osm_id {wanted}, which is not an available get_trails "
                                               f"result; using top-ranked {candidates[0]['name']}")
        elif position >= GEMINI_TOP_N:
            ctx.trace.add("planner", "reject", f"Gemini picked {candidates[position]['name']} (ranked #{position + 1}), "
                                               f"outside the top {GEMINI_TOP_N}; using top-ranked {candidates[0]['name']}")
        else:
            return candidates[position]
    return candidates[0]


def _assemble(ctx, trail, constraints, area):
    """Every number below comes from a tool result."""
    lat, lon = trail["coords"][0]
    gain_f = ctx.elevation(trail)
    weather = ctx.tools.submit("get_weather", get_weather, round(area[0], 4), round(area[1], 4), constraints["date"])
    climate = ctx.tools.submit("get_climate", get_climate, round(area[0], 4), round(area[1], 4), constraints["date"])
    hazards = ctx.tools.submit("get_hazards", get_hazards, round(area[0], 4), round(area[1], 4), HAZARD_RADIUS_KM)
    alerts, seasonal = ctx.alerts(area), ctx.seasonal(area, constraints["date"])
    gain = gain_f.result()
    gain_m = None if _failed(gain) else gain["elevation_gain_m"]
    gain_source = None if _failed(gain) else gain.get("source")
    timing = ctx.timing(trail, gain_m, constraints["date"], constraints["start"]).result()
    window = ctx.hike_window(area, constraints["date"], timing).result()
    anomaly = ctx.tools.call("compare_climate", compare_climate, window, climate.result())
    conditions = ctx.tools.call("derive_conditions", derive_conditions, timing, window)
    unusual = anomaly_condition(anomaly)  # colder or wetter only; a warning via _advisories, never a rejection
    if unusual:
        conditions = [*conditions, unusual]
    return {
        "trail": {"name": trail["name"], "osm_id": trail["osm_id"], "distance_km": trail["distance_km"],
                  "elevation_gain_m": gain_m, "coords": trail["coords"],
                  "elevation_source": gain_source},  # extra field (not in mock_plan.json): card label
        "score": trail.get("score", 0.0), "constraints": dict(constraints),
        "timing": timing, "weather": weather.result(), "climate": climate.result(), "hazards": hazards.result(),
        "alerts": alerts.result(), "hike_window": window, "seasonal": seasonal.result(), "conditions": conditions,
        "climate_anomaly": anomaly,
    }


def _hard_checks(plan):
    problems = []
    timing, weather = plan["timing"], plan["weather"]
    if not _failed(timing) and timing.get("warning"):
        problems.append(timing["warning"])
    for h, d in _hazards_near_route(plan["hazards"], plan["trail"]["coords"], HAZARD_NEAR_KM):
        problems.append(f"Active NASA EONET event {h['title']} ({h['type']}) is {d:.1f} km from the trail.")
    alerts, window = plan.get("alerts"), plan.get("hike_window")
    if alerts is not None and not _failed(alerts):
        for a in alerts["items"]:
            if a["severity"] in REJECT_SEVERITIES:
                problems.append(f"NWS {a['event']} ({a['severity']}) is in effect: {a['headline']}")
    if window is not None and not _failed(window):
        if window["max_precip_chance"] >= PRECIP_LIMIT:
            problems.append(f"NWS hourly forecasts up to a {window['max_precip_chance']}% chance of precipitation "
                            f"during the hike ({window['window']}).")
        thunder = [c for c in window.get("conditions") or [] if "thunder" in c.lower()]
        if thunder:
            problems.append(f"NWS hourly forecasts thunder during the hike ({window['window']}): {', '.join(thunder)}.")
    elif not _failed(weather) and (weather.get("precip_chance") or 0) >= PRECIP_LIMIT:
        # No hourly forecast for the hike window: fall back to the daily forecast.
        problems.append(f"NWS forecasts a {weather['precip_chance']}% chance of precipitation ({weather['summary']}).")
    return problems


def _advisories(plan):
    """Warnings that never reject a plan: Moderate NWS alerts, seasonal items and trail conditions."""
    out = []
    alerts, seasonal = plan.get("alerts"), plan.get("seasonal")
    if alerts is not None and not _failed(alerts):
        out += [f"NWS {a['event']} ({a['severity']}): {a['headline']}" for a in alerts["items"]
                if a["severity"] in WARN_SEVERITIES]
    if seasonal is not None and not _failed(seasonal):
        out += [f"{i['title']}: {i['advice']}." for i in seasonal["items"]]
    out += [f"{c['title']}: {c['advice']}." for c in plan.get("conditions") or []]
    return out


def _checker_view(plan, constraints):
    view = {k: v for k, v in plan.items() if k not in ("trail", "hazards")}
    view["trail"] = {k: v for k, v in plan["trail"].items() if k != "coords"}
    hz = plan["hazards"]
    view["hazards"] = hz if _failed(hz) else [
        {"title": h["title"], "type": h["type"], "km_from_trail": round(d, 1)}
        for h, d in _hazards_near_route(hz, plan["trail"]["coords"], HAZARD_RADIUS_KM)][:10]
    alerts = plan.get("alerts")
    if alerts is not None and not _failed(alerts):
        view["alerts"] = [{k: a[k] for k in ("event", "severity", "headline")} for a in alerts["items"][:5]]
    view["constraints"] = constraints
    return view


DISTANCE_TOLERANCE = 0.5  # a trail within ±50% of the requested distance counts as "near" it


def _distance_warning(target_km, distance_given, trails, chosen):
    """Warn when no trail in the area is within ±50% of the distance the hiker asked for."""
    if not distance_given or not target_km or not trails:
        return None
    closest = min(trails, key=lambda t: abs(t["distance_km"] - target_km))
    if abs(closest["distance_km"] - target_km) <= DISTANCE_TOLERANCE * target_km:
        return None
    text = (f"No trail near {target_km:.1f} km in this area; the closest is {closest['name']} "
            f"({closest['distance_km']:.1f} km)")
    if chosen and chosen.get("osm_id") != closest["osm_id"]:
        text += f"; this plan uses {chosen['name']} ({chosen['distance_km']:.1f} km)"
    return text + "."


def _start_moved_warning(asked, final, timing, approved):
    """Tell the hiker when the plan no longer starts at the time they asked for."""
    if not asked or not final or asked == final:
        return None
    sunset = None if _failed(timing) else timing.get("sunset")
    if approved and sunset:
        return (f"You asked to start at {asked}; moved to {final} so you finish at least 30 min "
                f"before sunset ({sunset}).")
    return f"You asked to start at {asked}; the plan starts at {final} instead" + (
        f" (sunset {sunset})." if sunset else ".")


def _approval_line(plan):
    t, w = plan["timing"], plan["weather"]
    bits = []
    if not _failed(t):
        bits.append(f"{t['start']} start is no later than the {t['latest_safe_start']} latest safe start")
    bits.append("NASA EONET hazards unavailable" if _failed(plan["hazards"])
                else f"no NASA EONET events within {HAZARD_NEAR_KM} km")
    window, alerts = plan.get("hike_window"), plan.get("alerts")
    if window is not None and not _failed(window):
        bits.append(f"{window['max_precip_chance']}% chance of precipitation during the hike")
    elif not _failed(w):
        bits.append(f"{w['precip_chance']}% chance of precipitation")
    if alerts is not None and not _failed(alerts):
        bits.append("no Severe or Extreme NWS alerts")
    return "; ".join(bits)


# A checker problem "about the start time" (the Gemini checker restates a late start in its own words).
_START_TIME_PROBLEM = re.compile(r"\b(start|sunset|dark|daylight|late)\b", re.IGNORECASE)


def _late_start_only(problems, timing):
    """True when the only problem is a late start: the timing check failed, and every other problem
    (e.g. the Gemini checker's wording of the same thing) is about the start time or sunset too."""
    if _failed(timing) or not timing.get("warning"):
        return False
    return all(p == timing["warning"] or _START_TIME_PROBLEM.search(p) for p in problems)


def _revise(ctx, planner, problems, plan, ranked, rejected, constraints):
    """Fix a rejected plan. Returns (answer, constraints).

    1. Only a late start: keep the trail and move the start to the latest safe start (Python, no LLM).
    2. Otherwise ask the Gemini planner for a different trail or start time (if available and in time).
    3. Otherwise the rule-based reviser tries the next-ranked trail.
    """
    timing = plan["timing"]
    if _late_start_only(problems, timing):
        start = timing["latest_safe_start"]
        rejected.discard(plan["trail"]["osm_id"])
        ctx.trace.add("planner", "decide", f"Planner moved the start from {timing['start']} to {start} "
                                           "to finish before sunset")
        return {"chosen_osm_id": plan["trail"]["osm_id"]}, {**constraints, "start": start}

    if planner is not None and not ctx.llm_down and ctx.elapsed() > LLM_START_DEADLINE_S:
        ctx.trace.add("planner", "fallback", f"Skipped the Gemini revision: more than {LLM_START_DEADLINE_S} s had "
                                             "passed, so /plan stays under 20 s; "
                                             "used the rule-based reviser")
    elif planner is not None and not ctx.llm_down:
        try:
            answer = planner.run(
                "The safety checker rejected your plan:\n- " + "\n- ".join(problems) +
                f"\nRejected osm_ids: {sorted(rejected)}. Choose a different trail or an earlier start time "
                "(use tools as needed) and answer with the same JSON.")
            ctx.trace.add("planner", "decide", f"Revised plan after checker feedback (osm_id {answer.get('chosen_osm_id')})")
            return answer, constraints
        except (LLMUnavailable, ValueError) as exc:
            logger.warning("LLM planner unavailable during revision: %s", exc)
            ctx.trace.add("planner", "fallback", "LLM unavailable, used rule-based planner")

    nxt = next((t for t in ranked if t["osm_id"] not in rejected), None)
    if nxt:
        ctx.trace.add("planner", "decide", f"Trying next-ranked trail {nxt['name']} ({nxt['distance_km']:.1f} km)")
        return {"chosen_osm_id": nxt["osm_id"]}, constraints
    return None, constraints


def _explanation(answer, plan, ranked, n_trails):
    trail, timing = plan["trail"], plan["timing"]
    km = trail["distance_km"]
    text = str((answer or {}).get("explanation") or "").strip()
    mentions_distance = any(s in text for s in (f"{km:.1f}", f"{_mi(km):.1f}", f"{km:.2f}"))
    if text and trail["name"].lower() in text.lower() and mentions_distance:
        return text
    rank = next((i + 1 for i, t in enumerate(ranked) if t["osm_id"] == trail["osm_id"]), None)
    parts = [f"{trail['name']} is {km:.1f} km ({_mi(km):.1f} mi)"
             f"{', a loop' if _is_loop(trail['coords']) else ''}"
             f"{f', ranked #{rank} of {n_trails} trails nearby' if rank else ''}."]
    if not _failed(timing):
        start = datetime.strptime(timing["start"], "%H:%M")
        finish = start + timedelta(minutes=timing["est_duration_min"])
        sunset = datetime.strptime(timing["sunset"], "%H:%M")
        margin = (sunset - finish).total_seconds() / 60
        if margin >= 0:
            parts.append(f"Starting at {timing['start']}, you should be back about {_hm(margin)} before sunset "
                         f"({timing['sunset']}).")
        else:
            parts.append(f"Starting at {timing['start']}, you would finish after sunset ({timing['sunset']}).")
    return " ".join(parts)


TIMING_KEYS = ("start", "est_duration_min", "sunset", "latest_safe_start", "turnaround_by")
WEATHER_KEYS = ("source", "summary", "precip_chance")
CLIMATE_KEYS = ("source", "years", "avg_high_c", "avg_low_c", "avg_precip_mm")
SPACE_KEYS = ("source", "kp_max", "aurora_chance", "checked_at")


def _section(result, keys, source=None, error=None):
    """Contract keys only; a failed or missing tool becomes {available: false, source, error}.
    A cached fallback after a failed live check also keeps "stale": true (alerts, hike_window)."""
    if result is None:
        return {"available": False, "source": source, "error": error}
    if _failed(result):
        return {"available": False, "source": result.get("source") or source, "error": result.get("error")}
    out = {k: result.get(k) for k in keys}
    if result.get("stale") is True:
        out["stale"] = True
    return out


ALERT_KEYS = ("source", "items", "checked_at")
WINDOW_KEYS = ("source", "window", "max_precip_chance", "min_temp_f", "max_temp_f", "max_wind_mph", "conditions",
               "summary", "checked_at")
SEASONAL_KEYS = ("source", "items", "checked_at")
ANOMALY_KEYS = ("source", "temp_diff_f", "rain_flag", "notable", "summary")


def _response(ctx, constraints, parsed_by, trail, alternatives, explanation, timing, weather, climate, space,
              hazards, checker, warnings, extra=None):
    """extra: the alerts, hike_window, seasonal, conditions and climate_anomaly results (None without a place)."""
    extra = extra or {}
    error = warnings[0] if warnings else None
    if hazards is None:
        hazards_out, hazards_at = _section(None, (), "NASA EONET", error), None
    elif _failed(hazards):
        hazards_out, hazards_at = _section(hazards, ()), None
    else:
        hazards_out, hazards_at = hazards["items"], hazards["checked_at"]
    if trail is not None and _failed(trail):
        trail = {"available": False, "source": trail.get("source"), "error": trail.get("error")}
    logger.info("Plan done in %.1f s: trail=%s parsed_by=%s approved=%s", ctx.elapsed(),
                (trail or {}).get("name"), parsed_by, checker.get("approved"))
    result = {
        "request": ctx.text,
        "constraints": {**{k: constraints.get(k) for k in ("distance_km", "date", "start", "prefs")},
                        "parsed_by": parsed_by},
        "trail": trail,
        "alternatives": alternatives,
        "explanation": explanation,
        "timing": _section(timing, TIMING_KEYS, "TrekChek timing", error),
        "weather": _section(weather, WEATHER_KEYS, "NWS", error),
        "climate": _section(climate, CLIMATE_KEYS, "NASA POWER", error),
        "space_weather": _section(space, SPACE_KEYS, "NASA DONKI", error),
        "hazards": hazards_out,
        "hazards_checked_at": hazards_at,
        "alerts": _section(extra.get("alerts"), ALERT_KEYS, "NWS", error),
        "hike_window": _section(extra.get("hike_window"), WINDOW_KEYS, "NWS hourly", error),
        "seasonal": _section(extra.get("seasonal"), SEASONAL_KEYS, "TrekChek seasonal calendar", error),
        "conditions": list(extra.get("conditions") or []),
        "climate_anomaly": _section(extra.get("climate_anomaly"), ANOMALY_KEYS, "NWS vs NASA POWER", error),
        "reports": [],
        "agent_trace": ctx.trace.items,
        "checker": checker,
        "narration": [],
        "warnings": warnings,
        "flood_or_burn_layer": None,
    }
    result["narration"] = generate_narration(result)
    return result
