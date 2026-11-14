"""TreeScout application and static frontend."""

from pathlib import Path
import json

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.tools.trails import OverpassError, get_trails

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

app = FastAPI(title="TreeScout")


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}


class PlanRequest(BaseModel):
    request: str = Field(min_length=1)
    lat: float = Field(gt=-90, lt=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    radius_km: float = Field(default=15, gt=0, allow_inf_nan=False)


@app.post("/plan")
def plan(body: PlanRequest):
    try:
        trails = get_trails(body.lat, body.lon, body.radius_km)
    except OverpassError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not trails:
        raise HTTPException(status_code=404, detail="No named trails at least 1 km long found in this area.")
    result = json.loads((ROOT / "mock_plan.json").read_text(encoding="utf-8"))
    result["request"] = body.request
    result["trail"].update(trails[0])
    return result


app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
