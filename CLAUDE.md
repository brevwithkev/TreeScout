# TreeScout — Claude instructions

TreeScout is a hiking route planner: a FastAPI backend (Python 3.11+) plus a static frontend served at `/`. See README.md for setup.

## Ownership
- The user owns `frontend/` and, by agreement with the teammate (Kenneth) on 2026-11-14, the **Phase 2 backend**: `backend/main.py` (`/plan`), `backend/agents.py`, `backend/tools/*` and their tests.
- `backend/narration.py` (narration, late check, ElevenLabs TTS) was created at the user's request on 2026-11-14 from a prototype; edit it only when the user asks.
- **Never create or edit `backend/voice.py`.** Kenneth owns it (with Codex). If it needs a change, describe it so the user can pass it on.
- Each task says which area it covers ("In frontend/ only", "In backend/ only"); stay inside it.

## API contract
- `mock_plan.json` is the API contract between backend and frontend. **Never change its shape** (no adding, removing, renaming or retyping fields).
- If the frontend needs a field that isn't in the contract, stop and tell the user so they can ask the teammate.
- `POST /plan` must never read `mock_plan.json` or emit mock values; only `GET /mock_plan.json` serves it. A failed data source is `{"available": false, "source": ..., "error": ...}`, never a mock fallback.
- Current endpoints:
  - `GET /health` returns `{"ok": true}`
  - `GET /mock_plan.json` serves the contract example (frontend "Use mock data" only).
  - `POST /plan` takes `{request, lat?, lon?, radius_km? (15), distance_km?, date? (YYYY-MM-DD), start? (HH:MM), prefs?, osm_id?}`; the optional form fields override the text. `osm_id` (a trail from "Other options") replaces the planner's pick with no Gemini calls; timing, Python checks and narration still run, only a late start is revised, and such requests are never replayed or recorded by demo mode. It runs `backend/agents.make_plan` (Gemini planner + checker, rule-based fallback) and always returns 200 in the `mock_plan.json` shape. Failures are per section (`available: false`). There is no coverage area (branch `anywhere`): a place that can't be found gives `trail: null` and the warning "Couldn't find <place>"; no trails gives `trail: null` and "No mapped trails found near <place>"; Overpass down with no snapshot gives `trail: null` and "Trail data unavailable right now; try again". Target under ~20 s.
  - Where (branch `anywhere`): Gemini (or the rules) extracts a place NAME only (`constraints.place`; the planner's tools take `place`, never coordinates). Home-area towns in `agents.PLACES` (Ypsilanti etc.) and requests without a place (the request's `lat`/`lon`) use the unchanged Ypsilanti path (`trails.get_trails`, 15 km, committed fixture). Any other name is looked up with Nominatim (`backend/tools/geocode.py`: limit 1, our User-Agent, max 1 request/s, disk cache forever) and its trails come from `trails.get_trails_anywhere`: saved snapshot (`data/trail_snapshots/`, kept forever) or live Overpass (same mirrors, 8 s total) at 10 km, widened once to 20 km if under 3 trails; it also includes named `highway=track` and `highway=path` + `sac_scale` ("Unnamed mountain path (T3)"). `tests/fixtures/ypsilanti_baseline.json` (`backend/test_anywhere.py`) pins the Ypsilanti plans from before the change.
  - Times are local to the trail: `common.timezone_for` (timezonefinder + zoneinfo) for timing/sunset (astral), the NWS hike window and "start time already passed"; the date words ("tomorrow") use the request's own lat/lon.
  - Tests: `python -m unittest backend.test_agents backend.test_narration backend.test_demo backend.tools.test_trails backend.tools.test_data_cache backend.tools.test_ranking backend.tools.test_elevation backend.tools.test_hazard_tools backend.test_anywhere` (offline), `python tests/test_tools.py` (live tools), and `tests/plan_test.py` (live, forbidden mock strings; needs a running server, `TREESCOUT_URL` to override `localhost:8000`).
  - `POST /tts` takes `{text}` (1-1000 chars) and returns `audio/mpeg`; it is the only path to ElevenLabs (`ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL`, default `eleven_flash_v2_5`). Audio is cached in `cache/tts/`. 502 with `detail` if the key is missing or ElevenLabs fails.
  - `POST /narrate/late` takes `{plan, current_km, elapsed_min, current_pace_min_per_km?}` and returns `{"late": false}` or `{"late": true, "text": ...}`. Late when now + remaining km x pace + 30 min > sunset (+0.5 min rounding slack); pace is `current_pace_min_per_km` when given (the frontend always sends the simulated pace), else the average so far (needs 0.25 km). `frontend/app.js isLateLocally` is the same rule.
  - Demo audio (`backend/demo.py plan_voice_lines`) covers narration, the fixed cues in `narration.FIXED_CUES` ("Turn back now.", "You're back on schedule.", "Heading back to the trailhead.") and the turn-back sentence; `python -m backend.demo audio` backfills.
  - `/plan` fills `narration` via `generate_narration`: items `{coord_index, km, kind, text}` sorted by `coord_index` (kind: trailhead, km, halfway, hazard, finish).
- Demo mode (`backend/demo.py`): `DEMO_RECORD=1` writes `demo_data/plans/` and `demo_data/tts/` (committed); `DEMO_MODE=1` replays them. Keep `DEMO_RECORD` off when testing so committed demos aren't overwritten.
- Phase H hazard fields (added to `mock_plan.json` at the user's request on 2026-11-15), after `hazards_checked_at`:
  - `alerts`: `{source: "NWS", items: [{event, severity, headline, onset, ends, link}], checked_at}` (`backend/tools/alerts.py`, most severe first).
  - `hike_window`: `{source: "NWS hourly", window: "HH:MM-HH:MM", max_precip_chance, min_temp_f, max_temp_f, max_wind_mph, conditions: [shortForecast], summary, checked_at}` for the hours of the hike (`get_hike_window` in `backend/tools/weather.py`).
  - `seasonal`: `{source: "TreeScout seasonal calendar", items: [{title, advice, source, link}], checked_at}` from the table in `backend/tools/seasonal.py` (Michigan firearm deer season Nov 15-30; verify the dates every year).
  - `conditions`: list of `{title, advice, source}` derived without an API ("Low light" when the start is later than `timing.latest_safe_start`, i.e. finishing less than 30 min before sunset, the same buffer as the start-time move, so a start the planner moved is never flagged; "Freezing temperatures" when `min_temp_f <= 32`; `backend/tools/conditions.py`).
  - `climate_anomaly` (added 2026-11-15, after `conditions`): `{source: "NWS vs NASA POWER <years>", temp_diff_f, rain_flag, notable, summary}` (`backend/tools/anomaly.py`, no API). `temp_diff_f` = hike-window forecast minus the POWER normal for the same part of the day (window midpoint 10:00-17:59: forecast high vs normal high; 21:00-05:59: low vs low; otherwise the midpoints), whole F. `rain_flag` when the hike-window chance is >= 50% and the normal is under 2 mm/day. `notable` when |temp_diff_f| >= 8 or `rain_flag` (the card shows an "Unusual" badge). Only notably colder (`temp_diff_f <= -8`) or `rain_flag` adds an "Unusual conditions" item to `conditions` (and so `warnings`; notably warmer is card-only), never a rejection; the trailhead line then says e.g. "About 9F colder than normal: dress warm." or "Rain likelier than normal for this week." (`narration.anomaly_phrase`). Summary is e.g. "About 9F colder than normal for this week" or "Typical for this week". `{available: false, source, error}` when NWS hourly or POWER is unavailable.
  - NWS alerts and the NWS hourly forecast retry once after 1 s on a 5xx or timeout (`common.retry_once`: first attempt 6 s, retry 3 s, about 10 s worst case per source). If the live check still fails, the last successful result for that point (up to 6 h old, `common.STALE_MAX_S`) is used and `alerts` / `hike_window` carry an extra `"stale": true` with the original `checked_at` (the key is absent otherwise; not in `mock_plan.json`). The Hazards tab then shows "NWS alerts (cached from <time>)"; `available: false` only when there is no cached result.
  - Each of `alerts`, `hike_window`, `seasonal` can be `{available: false, source, error}`. Recordings made before Phase H don't have these keys; the frontend shows "Not checked in this recording".
  - Checker: Severe/Extreme alerts, `hike_window.max_precip_chance >= 70` and thunder in `hike_window.conditions` reject (the daily `weather.precip_chance` is only the fallback when the hike window is unavailable). Moderate alerts, seasonal items and conditions become `warnings`.
- `/plan` adds an optional `trail.elevation_source` (not in `mock_plan.json`; the provider shown on the Trail card). Elevation is cached permanently in `backend/data/elevation_cache.json` (committed); fill it with `python -m backend.tools.elevation precompute`.
- `trail.coords` are `[lat, lon]` pairs, which is the order Leaflet expects. `narration[].coord_index` indexes into `trail.coords`.

## Frontend stack
- Plain HTML, vanilla JavaScript and CSS. **No build step**: no npm, bundlers, frameworks or TypeScript.
- Leaflet is loaded from a CDN with a pinned version.
- FastAPI serves `frontend/` at `/` through `StaticFiles(html=True)`. Use relative URLs for API calls (`/plan`, `/tts`).

## Secrets
- **Never put API keys or secrets in frontend code.** All ElevenLabs calls go through the backend's `/tts` endpoint. Keys live only in the backend `.env`.

## Environment
- Windows with PowerShell. Use PowerShell syntax in commands and instructions, for example `.\.venv\Scripts\Activate.ps1` and `Copy-Item`.
- Run the app with `uvicorn backend.main:app --reload`, then open http://localhost:8000.
- Smoke tests (with the server running): `python -m unittest discover -s tests -p "*_test.py"`
- **Never test against port 8000**: that is the user's own server (often running with `--reload` and Gemini on). Start a test server on **port 8011** and point tests there (`python tests/golden.py --url http://127.0.0.1:8011`, `TREESCOUT_URL=http://127.0.0.1:8011`). Check the port is free first; if the server fails to bind, stop instead of testing whatever answers.
- To disable Gemini for a test server, set `GEMINI_API_KEY` to a **single space** (e.g. `$env:GEMINI_API_KEY = ' '`), not an empty string: on Windows an empty value deletes the variable and `load_dotenv` then reloads the real key from `.env`. Confirm the server log says "GEMINI_API_KEY or GEMINI_MODEL is not set". Keep `DEMO_RECORD=0`.
