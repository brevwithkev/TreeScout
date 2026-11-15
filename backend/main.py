"""TreeScout application and static frontend."""

from pathlib import Path
import logging

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from backend.agents import make_plan  # noqa: E402  (after load_dotenv so agents see the keys)

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


app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
