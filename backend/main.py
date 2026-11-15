"""TreeScout application and static frontend."""

from pathlib import Path
import logging

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from backend.agents import make_plan  # noqa: E402  (after load_dotenv so agents see the keys)
from backend.narration import check_late_pace, synthesize_tts  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DEFAULT_LAT = 42.2411
DEFAULT_LON = -83.6130

app = FastAPI(title="TreeScout")


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/mock_plan.json")
def mock_plan() -> FileResponse:
    """The API contract example for frontend work. Never read by POST /plan."""
    return FileResponse(ROOT / "mock_plan.json", media_type="application/json")


class PlanRequest(BaseModel):
    request: str = Field(min_length=1)
    lat: float = Field(default=DEFAULT_LAT, gt=-90, lt=90, allow_inf_nan=False)
    lon: float = Field(default=DEFAULT_LON, ge=-180, le=180, allow_inf_nan=False)
    radius_km: float = Field(default=15, gt=0, le=50, allow_inf_nan=False)
    # Explicit form fields; when present they override what the planner reads from the text.
    distance_km: float | None = Field(default=None, gt=0, le=100, allow_inf_nan=False)
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    start: str | None = Field(default=None, pattern=r"^\d{1,2}:\d{2}$")
    prefs: list[str] | None = None


@app.post("/plan")
def plan(body: PlanRequest):
    """Planner + checker agents (or the rule-based fallback). Every field comes from live tools."""
    form = {"distance_km": body.distance_km, "date": body.date, "start": body.start, "prefs": body.prefs}
    return make_plan(body.request, body.lat, body.lon, radius_km=body.radius_km, form=form)


class LateNarrationRequest(BaseModel):
    plan: dict
    current_km: float = Field(ge=0, allow_inf_nan=False)
    elapsed_min: float = Field(ge=0, allow_inf_nan=False)


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)  # cap protects ElevenLabs credits


@app.post("/narrate/late")
def narrate_late(body: LateNarrationRequest):
    """{"late": true, "text": ...} when the hiker should turn back; otherwise {"late": false}."""
    return check_late_pace(body.plan, body.current_km, body.elapsed_min)


@app.post("/tts")
def tts(body: TTSRequest) -> Response:
    """MP3 speech for text via ElevenLabs (cached on disk). 502 if the key is missing or ElevenLabs fails."""
    try:
        audio = synthesize_tts(body.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(content=audio, media_type="audio/mpeg")


app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
