# TreeScout — Claude instructions

TreeScout is a hiking route planner: a FastAPI backend (Python 3.11+) plus a static frontend served at `/`. See README.md for setup.

## Ownership
- The user is the **frontend developer**. Only edit files in `frontend/` (`index.html`, `app.js`, `styles.css`, and any new frontend files).
- **Never edit anything in `backend/`.** A teammate owns it and works on it with Codex. If a frontend task needs a backend change, describe the change and let the user pass it to the teammate.

## API contract
- `mock_plan.json` is the API contract between backend and frontend. **Never change its shape** (no adding, removing, renaming or retyping fields).
- If the frontend needs a field that isn't in the contract, stop and tell the user so they can ask the teammate.
- Current endpoints:
  - `GET /health` returns `{"ok": true}`
  - `POST /plan` takes `{request: string, lat: number, lon: number, radius_km?: number (default 15)}` and returns an object shaped like `mock_plan.json`. It returns 404 when no trails are found and 502 when the upstream trail data fails; the error message is in `detail`.
  - `POST /tts` will be the only path to ElevenLabs. It does not exist in `backend/main.py` yet.
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
