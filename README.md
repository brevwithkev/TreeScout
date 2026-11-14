# TreeScout

TreeScout is a hiking route planner designed to combine NASA data, an AI planning agent, and a voice trail guide. This initial scaffold provides a Python 3.11+ FastAPI backend, a plain HTML/CSS/JavaScript frontend served at `/` with no build step, and `mock_plan.json` as the shared backend/frontend API contract. External data integrations, route planning, and voice generation are not implemented yet.

## Run locally

From the repository root (Python 3.11 or newer required):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
Copy-Item .env.example .env
uvicorn backend.main:app --reload
```

On macOS/Linux, activate with `source .venv/bin/activate` and copy with `cp .env.example .env`. If `.env` already exists, keep your existing settings. Fill in the environment variables as integrations are added; no API keys are required to start this scaffold. Environment variables are loaded using python-dotenv, and existing process environment values take precedence.

Open http://localhost:8000. The health endpoint at http://localhost:8000/health returns `{"ok": true}`.

With the server running, run the smoke checks in another activated terminal:

```powershell
python -m unittest discover -s tests -p "*_test.py"
```
