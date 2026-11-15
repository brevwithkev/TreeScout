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
from backend import demo  # noqa: E402

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
    """Planner + checker agents (or the rule-based fallback). Every field comes from live tools.

    DEMO_MODE=1: a saved recording whose slug matches the request text is returned instantly (form fields
    are ignored then). DEMO_RECORD=1: successful live plans (and their voice lines) are saved to demo_data/.
    """
    if demo.demo_mode():
        recorded = demo.load_recording(demo.slugify(body.request))
        if recorded is not None:
            return recorded
    form = {"distance_km": body.distance_km, "date": body.date, "start": body.start, "prefs": body.prefs}
    result = make_plan(body.request, body.lat, body.lon, radius_km=body.radius_km, form=form)
    if demo.demo_record():
        demo.save_recording(result)
    return result


@app.get("/demo/plans")
def demo_plans():
    """Saved recordings, newest first: [{slug, request, recorded_at}]."""
    return demo.list_recordings()


@app.get("/demo/plans/{slug}")
def demo_plan(slug: str):
    """One saved recording (lets the frontend load it with one click, whatever DEMO_MODE is)."""
    recorded = demo.load_recording(slug)
    if recorded is None:
        raise HTTPException(status_code=404, detail=f"No demo recording named {slug!r}")
    return recorded


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
    """MP3 speech for text via ElevenLabs (cached on disk). 502 if the key is missing or ElevenLabs fails.
    DEMO_MODE=1: saved demo audio (demo_data/tts/) or the local cache only; anything else gets a 502
    (the frontend shows captions instead)."""
    if demo.demo_mode():
        saved = demo.demo_audio(body.text)
        if saved is not None:
            return Response(content=saved, media_type="audio/mpeg")
    try:
        audio = synthesize_tts(body.text, cache_only=demo.demo_mode())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(content=audio, media_type="audio/mpeg")


app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
