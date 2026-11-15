# TrekChek

**An AI agent team that plans your hike from live NASA data, checks the plan for safety, and talks you through the trail.**

Type or say a request like *"Easy 3 mile walk near Ypsilanti tomorrow morning."* A planner agent picks a real trail and works out the timing against sunset. A safety checker reviews the plan, and the map then simulates the hike with a spoken trail guide. If you fall behind, it tells you to turn back.

> Supported by the Michigan Space Grant Consortium, NASA grant 80NSSC20M0124.
> NASA does not endorse this project.

## Screenshots

_Screenshots coming soon (`docs/screenshots/`): plan view, hazards tab, satellite layer, hike simulation with the TURN BACK warning._

## How to run

Python 3.11 or newer. From the repository root, in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
Copy-Item .env.example .env      # then fill in your keys (see below)
uvicorn backend.main:app --reload
```

On macOS/Linux, activate with `source .venv/bin/activate` and copy with `cp .env.example .env`.

Open http://localhost:8000. `http://localhost:8000/health` returns `{"ok": true}`.

Keys in `.env` (all optional; anything missing falls back gracefully):

| Variable | Used for | Without it |
|---|---|---|
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Planner and safety-checker agents | Rule-based planner (`parsed_by: "rules"`) |
| `NASA_API_KEY` | NASA DONKI space weather | Space weather shows "Unavailable" |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL` | Voice guide (`/tts`); model defaults to `eleven_flash_v2_5` | Captions without audio |
| `DEMO_MODE`, `DEMO_RECORD` | Replay / record demo plans (see below) | `0` (off) |

### MongoDB Atlas hike history

Add `MONGODB_URI` (Atlas Connect → Drivers → Python connection string) and
`MONGODB_DB=trekchek` to the backend `.env`. Never put the URI in frontend code or commit it.
Use an Atlas database user with read/write access to `trekchek`, and add the backend
machine's public IP to the Atlas network access list. URL-encode special characters
in the database password when inserting it into the connection string.

```powershell
python -m pip install -r backend/requirements.txt
python -m backend.history  # real Atlas save/list/retrieve, then removes only its own test records
python -m uvicorn backend.main:app --reload
```

Generate a hike, click **Save current hike**, refresh the browser, then choose it
from **My saved hikes**. Saved plans are snapshots: their weather and warnings are
not refreshed, and the page labels them accordingly. History is separate from
`POST /plan`, so an Atlas failure cannot fail a planning request.

History is scoped to a random browser credential in localStorage, not a login.
Clearing browser storage loses access; another browser does not share this history.
Use HTTPS when deployed. This is a hackathon MVP; account login and write rate
limits are still needed for a public production rollout.

API: `POST /history` with `{plan}`, `GET /history?limit=20&before=<next_before>`,
and `GET /history/{id}`. All require the `X-History-Token` browser credential.
The UI lists the most recent 20 saves; the API supports pagination.
The credential is 32 cryptographically random bytes encoded as 64 lowercase hex
characters. Only its SHA-256 hash is stored in Atlas. Pagination uses an opaque
`next_before` cursor (time plus ID), so simultaneous saves are not skipped.
Snapshots are limited to 2 MB; connection and operation deadlines are bounded.
The compound owner/time/ID index is created lazily on the first history operation.

Offline regression tests (MongoDB is simulated, no Atlas account needed):

```powershell
python -m pip install -r backend/requirements-dev.txt
python -m unittest backend.test_history backend.test_weather_coverage
```

Full offline suite (fixtures and mongomock; no real Atlas/Gemini/ElevenLabs calls):

```powershell
$env:GEMINI_API_KEY = ' '; $env:DEMO_RECORD = '0'
python -m unittest discover -s backend -t . -p 'test_*.py'
node --check frontend/app.js
node --check frontend/history.js
```

Start a separate server on port **8011** with those environment variables and
`DEMO_MODE=0` (`python -m uvicorn backend.main:app --port 8011`). Do not use the
developer's port 8000 for tests. Then run:

```powershell
python -m unittest discover -s tests -p '*_test.py'
python tests/golden.py --url http://127.0.0.1:8011
python -m pip install -r backend/requirements-browser.txt
python tests/browser_history.py
# Optional real Chrome + Atlas test, with the network-enabled test server running:
python tests/browser_atlas.py
```

Browser regressions use real headless Chrome/Edge with mocked API responses and
blocked third-party map/audio requests; they are not an Atlas integration test.
Set `TREKCHEK_BROWSER_EXECUTABLE` to a browser path if neither is detected, or
install Playwright Chromium with `python -m playwright install chromium`.
`python -m backend.history` separately verifies actual Atlas operations without
printing the URI, credential, or browser token. An Atlas connection failure usually
requires checking the database user's permissions and the backend IP access list.

Forecast windows require continuous timestamp coverage, including both endpoints
and every interior interval. MET uses explicit `next_1_hours`, `next_6_hours`, or
`next_12_hours` aggregation periods; `instant` values are point samples, and the
final point does not extend coverage. When aggregated precipitation cannot be
assigned to the exact hike window without double counting or dividing a period,
the amount remains unknown. See [MET's interval semantics](https://api.met.no/doc/ForecastJSON).
Incomplete hourly data uses the existing unavailable section and is labeled as
not checked in the UI/briefing. Existing daily-weather fallback policy is preserved.
Briefing character limits apply only to optional model prose; all warnings and
checker rejection reasons are preserved ahead of it, including in no-trail plans.

### Demo recordings

Two switches, both off by default:

- **`DEMO_RECORD=1`** saves every successful `/plan` to `demo_data/plans/<slug>.json` (the slug is the request text lowercased, without punctuation, spaces as dashes) with a `demo_recorded_at` timestamp. It also saves the plan's voice lines (narration, the "Turn back now.", "You're back on schedule." and "Heading back to the trailhead." cues, and the turn-back sentence) to `demo_data/tts/`. Leave it off for normal use and testing, so demos aren't overwritten.
- **`DEMO_MODE=1`** replays recordings:
  - `/plan` returns a recording that matches the request text instantly, and plans live only when none matches.
  - `/tts` serves only saved audio (`demo_data/tts/`, then the local cache) and returns a clean 502 for anything else, so the page shows captions instead.
  - Recorded plans show a **"Recorded data from <time>"** badge.

`demo_data/` is committed, including the audio, which is named by its text alone. Demos therefore play with voice on any laptop, without an ElevenLabs key and whatever voice is configured there.

```powershell
# record (live planning; needs your API keys)
$env:DEMO_RECORD = "1"; uvicorn backend.main:app
# ...plan each demo request once in the browser, then stop the server

# present
$env:DEMO_RECORD = "0"; $env:DEMO_MODE = "1"; uvicorn backend.main:app
```

To save voice audio for recordings that don't have it yet, run `python -m backend.demo audio`.

The **Demo plans** dropdown (shown whenever recordings exist) loads one with a single click. `GET /demo/plans` lists them.

### Elevation cache

Elevation gain is cached permanently in `backend/data/elevation_cache.json` (committed), by trail OSM id and route. To fill it for every trail in the saved Ypsilanti snapshot, run:

```powershell
python -m backend.tools.elevation precompute
```

It skips cached trails and pauses between lookups. If a provider is rate-limited (Open-Meteo has a daily limit), run it again later and commit the updated file.

### Tests

```powershell
python -m unittest backend.test_agents backend.test_narration backend.test_demo backend.tools.test_trails backend.tools.test_data_cache backend.tools.test_ranking backend.tools.test_elevation  # offline
python tests/test_tools.py                                  # live data tools, Ypsilanti, tomorrow
python tests/golden.py --url http://127.0.0.1:8011         # golden prompts against a test server (default URL)
python -m unittest discover -s tests -p "*_test.py"         # smoke + no-mock-values checks (server running)
```

To keep Gemini quota for demos, run golden prompts with `GEMINI_API_KEY` set to a single space (an empty variable on Windows lets `.env` reload the real key).

## Architecture

```
Browser: Leaflet map + panel (frontend/, plain HTML/JS/CSS, no build step)
   │  POST /plan                 POST /narrate/late          POST /tts
   ▼
FastAPI (backend/main.py)
   ├─ Planner agent (Gemini function calling)  ──┐   rule-based fallback if Gemini is unavailable
   ├─ Safety checker (Gemini + Python checks) ◄──┘   up to 2 rounds
   │      │
   │      ▼  tools (backend/tools/), run concurrently
   │   trails (OpenStreetMap/Overpass) · rank_trails · elevation (Open-Meteo) · timing (sunset + Naismith)
   │   weather (NWS) · hazards (NASA EONET) · climate (NASA POWER) · space weather (NASA DONKI)
   │
   ├─ Narration (backend/narration.py): trailhead, every km, halfway, nearby hazards, finish
   └─ /tts → ElevenLabs (cached on disk) → spoken guide in the browser
```

Every number in a plan comes from a tool; the language model only chooses and explains. A failed source shows up as `{"available": false, "source": ..., "error": ...}`, never as made-up data. The response always has the shape of `mock_plan.json` (the frontend/backend contract).

## How trails are picked

**1. Find and rank.** Named hiking routes and paths of at least 1 km within 15 km of the requested place come from OpenStreetMap. `rank_trails` scores each from 0 to 1:

| Part | Weight | How |
|---|---|---|
| Distance closeness | 0.75 | `1 − |trail km − requested km| / requested km` |
| Loop | 0.1 | Start and end within 200 m |
| Distance from center | 0.1 | Trailhead distance from the requested place, out to 15 km |
| Elevation preference | 0.05 | For "easy" or "hilly": climb per km compared with 30 m/km; a neutral 0.5 when a trail's elevation is unknown |

Distance dominates: loop, center and elevation together are worth at most 0.25. A trail whose distance score is more than 1/3 higher, i.e. much closer to the requested distance, therefore always ranks first; a loop bonus can't outweigh it. The top three get real elevation gain and are re-ranked. Gemini may only pick among the top 3, and a pick outside them is replaced by the top-ranked trail.

**2. Plan.** The Gemini planner sees the ranked trails and the prefetched weather, hazards, climate and space weather. It can call any tool, then picks a trail and writes the explanation. Python checks that the chosen trail really came from the trail search.

**3. Check.** The plan fails the hard checks if:
- the start is later than the **latest safe start** (sunset minus the Naismith estimate minus 30 min);
- an active NASA EONET event is **within 5 km** of the route;
- NWS gives a **precipitation chance of 70% or more**.

A Gemini safety checker reviews the same data. On a rejection the planner gets the problems back and picks another trail or an earlier start (**max 2 rounds**). Unresolved problems become warnings. If the plan moves your requested start time, the first warning says so (e.g. *"You asked to start at 15:30; moved to 15:00 so you finish at least 30 min before sunset (17:12)."*). There is also a warning when no trail is within 50% of the requested distance. A place that can't be found, or has no mapped trails, gets a warning and no trail.

**4. Fallback.** If Gemini is missing, rate-limited, overloaded or slower than 10 s per call, the request goes straight to the rule-based planner, with no retries. That planner parses miles/km, today/tomorrow/weekdays, times like "2 PM", and easy/hilly/shaded/dog. It picks the top-ranked trail, runs the same Python checks and marks the plan `parsed_by: "rules"`.

**On the trail.** The hike bar keeps two ideas apart. *Playback* (Normal / Fast / Very fast: the whole hike in about 60 / 20 / 6 s; `?simSeconds=N` sets Normal) only changes how fast the demo plays. *Hiker pace vs plan* (a 0.5x-1.5x slider plus "Slow down" 0.6x and "Speed up" 1.3x) changes how far the hiker gets per simulated minute, so at 0.5x the clock runs twice as long per km. The browser asks `/narrate/late` every 3 s and whenever the pace changes, sending the pace it is simulating. It is late when *current time + remaining distance at the current pace + 30 min* is after sunset; the browser uses the same rule if the endpoint is unavailable. Going late shows TURN BACK (with "Head back now: back at the trailhead about 16:52, 19 min before sunset.") and speaks it once; speeding up enough clears it at the next check and shows and says "You're back on schedule." "Head back" walks the hiker back along the route to the trailhead and shows the arrival time against sunset. Pause and Reset put the pace back to 1x and clear the banners.

## Data sources

| Dataset | Provider | What we use it for | Dates / place | Link |
|---|---|---|---|---|
| EONET v3 (open events) | NASA | Active natural hazards: map markers, Hazards tab, 5 km safety check | Events open at request time, within 100 km of the search area (cached 6 h per location) | https://eonet.gsfc.nasa.gov/ |
| POWER daily point data (T2M_MAX, T2M_MIN, PRECTOTCORR) | NASA Langley | "Typical conditions this week": average high, low, precipitation | Same calendar week (±3 days) in each of the 20 years before the hike date, at the search-area point (cached 6 h per location and week) | https://power.larc.nasa.gov/ |
| DONKI geomagnetic storms (GST) | NASA CCMC | Max Kp and aurora chance tonight | Last 3 days through tomorrow, global | https://kauai.ccmc.gsfc.nasa.gov/DONKI/ |
| GIBS: MODIS Terra Corrected Reflectance, true color | NASA | Satellite base-map layer | Yesterday's imagery (local date), map view | https://nasa-gibs.github.io/gibs-api-docs/ |
| Forecast API | NOAA National Weather Service | Daytime forecast and precipitation chance for the hike day | Hike date, search area | https://www.weather.gov/documentation/services-web-api |
| OpenStreetMap via Overpass | OpenStreetMap contributors | Trail names and geometry; dark base map tiles | Saved southeast Michigan snapshot (Ypsilanti, 15 km radius; OSM data as of Nov 14, 2026); live Overpass elsewhere in southeast Michigan | https://www.openstreetmap.org/copyright |
| Elevation API | Open-Meteo (Copernicus DEM) | Elevation gain from up to 100 points along each candidate trail | Per trail; cached permanently in `backend/data/elevation_cache.json` | https://open-meteo.com/en/docs/elevation-api |
| Open-Elevation API | Open-Elevation (SRTM) | Fallback when Open-Meteo fails or is rate-limited; the Trail card names the provider | Per trail | https://open-elevation.com/ |
| Text to speech | ElevenLabs | Spoken trail guide and turn-back warning | Narration lines per plan | https://elevenlabs.io/docs |

## Limits

- **NASA EONET** covers large, reported events (wildfires, severe storms, floods); it does not know about trail closures, fallen trees or local flooding.
- **NASA POWER** values are ~0.5° grid averages, not trail-specific measurements.
- **MODIS** imagery has 250 m pixels: good for clouds, snow and smoke, not trail detail.
- **Naismith's rule** (5 km/h plus 1 h per 600 m of climb) is an estimate; real pace varies with terrain, weather and the hiker.
- **Trails anywhere OpenStreetMap has them** (branch `anywhere`). Places are looked up with OSM Nominatim; saved trail data covers the Ypsilanti area, and anywhere else needs one successful live Overpass lookup (the public servers are often slow or unavailable; TrekChek gives up after 8 s), which is then saved. NWS weather and alerts cover the US only; elsewhere those sections say unavailable.
- NASA POWER and EONET requests give up after 8 s (the section shows "Unavailable"), and results are cached on disk for 6 h in `data/tool_cache/`, so a repeat plan can show hazards up to 6 h old (the Hazards tab shows when EONET was checked).
- Gemini quota and latency vary; plans then come from the rule-based planner.
- **TrekChek is a planning aid, not a safety guarantee.** Check local conditions, carry a light, and turn back early.

## AI tools used

- **Gemini API**: planner and safety-checker agents.
- **ElevenLabs**: voice trail guide.
- **Codex**, **Claude Code**: coding assistants used to build the app.
- **Argon**: narration prototype that `backend/narration.py` was adapted from.

## Credits

Supported by the Michigan Space Grant Consortium, NASA grant 80NSSC20M0124. NASA does not endorse this project.
Map data © OpenStreetMap contributors. Imagery: NASA GIBS / MODIS Terra.
