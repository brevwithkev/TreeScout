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
  - `POST /plan` takes `{request, lat?, lon?, radius_km? (15), distance_km?, date? (YYYY-MM-DD), start? (HH:MM), prefs?}`; the optional form fields override the text. It runs `backend/agents.make_plan` (Gemini planner + checker, rule-based fallback) and always returns 200 in the `mock_plan.json` shape. Failures are per section (`available: false`); no trails or outside southeast Michigan gives `trail: null` (outside) or an unavailable trail plus a `warnings` entry. Target under ~20 s.
  - Tests: `python -m unittest backend.test_agents backend.test_narration backend.test_demo backend.tools.test_trails backend.tools.test_data_cache` (offline), `python tests/test_tools.py` (live tools), and `tests/plan_test.py` (live, forbidden mock strings; needs a running server, `TREESCOUT_URL` to override `localhost:8000`).
  - `POST /tts` takes `{text}` (1-1000 chars) and returns `audio/mpeg`; it is the only path to ElevenLabs (`ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL`, default `eleven_flash_v2_5`). Audio is cached in `cache/tts/`. 502 with `detail` if the key is missing or ElevenLabs fails.
  - `POST /narrate/late` takes `{plan, current_km, elapsed_min}` and returns `{"late": false}` or `{"late": true, "text": ...}`.
  - `/plan` fills `narration` via `generate_narration`: items `{coord_index, km, kind, text}` sorted by `coord_index` (kind: trailhead, km, halfway, hazard, finish).
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
