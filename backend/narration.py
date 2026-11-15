"""Route narration, pace/sunset late check, and ElevenLabs TTS with a disk cache.

Adapted from a prototype: deterministic narration points (trailhead, every km, halfway, near hazards,
finish), a pace check for "turn back" warnings, and text-to-speech for POST /tts.
"""

import hashlib
import json
import logging
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend.tools.common import USER_AGENT
from backend.tools.trails import haversine  # haversine([lat, lon], [lat, lon]) -> km

logger = logging.getLogger("treescout.narration")

ROOT = Path(__file__).resolve().parent.parent
TTS_CACHE_DIR = ROOT / "cache" / "tts"
TTS_TIMEOUT_S = 15
DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"   # "Rachel", an ElevenLabs pre-made voice
DEFAULT_MODEL_ID = "eleven_flash_v2_5"      # ElevenLabs' lowest-latency TTS model
HAZARD_NARRATION_KM = 2.0
LOOP_MAX_GAP_KM = 0.2
LATE_BUFFER_MIN = 30           # must finish at least this long before sunset
TURN_BACK_CUE = "Turn back now."  # same text as frontend/app.js TURN_BACK_CUE (played instantly on TURN BACK)


def turn_back_text(sunset):
    """The spoken TURN BACK line (same text as frontend/app.js isLateLocally)."""
    return f"You're behind schedule. Turn back now to reach the trailhead before sunset at {sunset}."
LATE_MIN_KM = 0.25             # too early to judge pace before this


# ---------------------------------------------------------------------------------------------
# Narration
# ---------------------------------------------------------------------------------------------

def _cumulative_km(coords):
    cum = [0.0]
    for a, b in zip(coords, coords[1:]):
        cum.append(cum[-1] + haversine(a, b))
    return cum


def _index_at_km(cum, target_km):
    for idx, d in enumerate(cum):
        if d >= target_km:
            return idx
    return len(cum) - 1


def _section(plan, key):
    value = plan.get(key)
    return value if isinstance(value, dict) and value.get("available") is not False else {}


def generate_narration(plan):
    """Narration points from route geometry, timing, weather and hazards, sorted by coord_index.

    Each item: {"coord_index", "km", "kind", "text"}; kind is trailhead | km | halfway | hazard | finish.
    """
    trail = plan.get("trail")
    if not isinstance(trail, dict) or trail.get("available") is False:
        return []
    coords = trail.get("coords") or []
    if len(coords) < 2:
        return []

    name = trail.get("name") or "the trail"
    cum = _cumulative_km(coords)
    route_km = cum[-1]
    total_km = float(trail.get("distance_km") or 0.0) or route_km
    scale = route_km / total_km if total_km > 0 else 1.0  # trail km -> geometry km

    def index_at(trail_km):
        return _index_at_km(cum, trail_km * scale)

    timing = _section(plan, "timing")
    sunset, turnaround_by = timing.get("sunset"), timing.get("turnaround_by")
    summary = _section(plan, "weather").get("summary")

    welcome = f"Welcome to {name}, {total_km:.1f} kilometers total."
    if sunset and turnaround_by:
        welcome += f" Sunset is at {sunset}; turn around by {turnaround_by}."
    elif sunset:
        welcome += f" Sunset is at {sunset}."
    if summary:
        welcome += f" Weather: {summary}."
    points = [{"coord_index": 0, "km": 0.0, "kind": "trailhead", "text": welcome}]

    half_km = round(total_km / 2.0, 2)
    for k in range(1, int(total_km) + 1):
        if abs(k - total_km) < 0.25 or abs(k - half_km) < 0.2:
            continue  # the finish or halfway line covers it
        remaining = max(0.0, round(total_km - k, 1))
        points.append({"coord_index": index_at(k), "km": float(k), "kind": "km",
                       "text": f"You've hiked {k} kilometer{'s' if k != 1 else ''}, {remaining:g} to go."})

    if total_km >= 1.0:
        half_idx = index_at(half_km)
        if 0 < half_idx < len(coords) - 1:
            text = (f"Halfway. You're on schedule if it's before {turnaround_by}." if turnaround_by
                    else "Halfway there.")
            points.append({"coord_index": half_idx, "km": half_km, "kind": "halfway", "text": text})

    hazards = plan.get("hazards")
    for hazard in hazards if isinstance(hazards, list) else []:
        if not isinstance(hazard, dict):
            continue
        h_lat, h_lon = hazard.get("lat"), hazard.get("lon")
        if not isinstance(h_lat, (int, float)) or not isinstance(h_lon, (int, float)):
            continue
        where = [float(h_lat), float(h_lon)]
        best = min(range(len(coords)), key=lambda i: haversine(coords[i], where))
        near_km = haversine(coords[best], where)
        if near_km <= HAZARD_NARRATION_KM:
            title = hazard.get("title") or hazard.get("type") or "an active hazard"
            reported = f" on {str(hazard['date'])[:10]}" if hazard.get("date") else ""
            points.append({"coord_index": best, "km": round(cum[best] / scale, 2), "kind": "hazard",
                           "text": f"Caution: {title} is {near_km:.1f} kilometers away, reported by "
                                   f"{hazard.get('source') or 'NASA EONET'}{reported}."})

    is_loop = haversine(coords[0], coords[-1]) <= LOOP_MAX_GAP_KM
    points.append({"coord_index": len(coords) - 1, "km": round(total_km, 2), "kind": "finish",
                   "text": "You're back at the trailhead." if is_loop else f"You've reached the end of {name}."})

    points.sort(key=lambda item: (item["coord_index"], item["km"]))
    return points


# ---------------------------------------------------------------------------------------------
# Late check
# ---------------------------------------------------------------------------------------------

def _hhmm_to_min(value):
    try:
        hours, minutes = str(value).strip().split(":")[:2]
        return int(hours) * 60 + int(minutes)
    except (ValueError, AttributeError):
        return None


def check_late_pace(plan, current_km, elapsed_min):
    """{"late": true, "text": ...} only when finishing at the current pace, plus a 30 min buffer, would end
    after sunset:  (start + elapsed) + remaining_km * (elapsed / current_km) + 30 > sunset.
    Otherwise {"late": false}, including when start or sunset is missing and before LATE_MIN_KM.
    The frontend's fallback (frontend/app.js isLateLocally) uses the same rule."""
    trail = plan.get("trail") if isinstance(plan.get("trail"), dict) else {}
    timing = _section(plan, "timing")
    start_min, sunset_min = _hhmm_to_min(timing.get("start")), _hhmm_to_min(timing.get("sunset"))
    if start_min is None or sunset_min is None:
        return {"late": False}
    try:
        total_km = float(trail.get("distance_km") or 0.0)
        current_km, elapsed_min = float(current_km), float(elapsed_min)
    except (TypeError, ValueError):
        return {"late": False}
    if total_km <= 0 or elapsed_min <= 0 or current_km < LATE_MIN_KM:
        return {"late": False}  # too early: pace over a few metres is meaningless

    pace = elapsed_min / current_km                      # current minutes per km
    remaining_km = max(0.0, total_km - current_km)
    finish_min = start_min + elapsed_min + remaining_km * pace
    if finish_min + LATE_BUFFER_MIN > sunset_min:
        return {"late": True, "text": turn_back_text(timing["sunset"])}
    return {"late": False}


# ---------------------------------------------------------------------------------------------
# Text-to-speech (ElevenLabs)
# ---------------------------------------------------------------------------------------------

def synthesize_tts(text, cache_only=False):
    """MP3 bytes for text from ElevenLabs, cached in cache/tts/<sha256>.mp3.

    Raises ValueError for empty text, RuntimeError when the key is missing or ElevenLabs fails.
    Voice: ELEVENLABS_VOICE_ID; model: ELEVENLABS_MODEL (default eleven_flash_v2_5).
    cache_only=True (demo mode) never calls ElevenLabs: uncached text raises RuntimeError.
    """
    clean = (text or "").strip()
    if not clean:
        raise ValueError("Text is required for TTS")

    voice_id = (os.getenv("ELEVENLABS_VOICE_ID") or "").strip() or DEFAULT_VOICE_ID
    model_id = (os.getenv("ELEVENLABS_MODEL") or "").strip() or DEFAULT_MODEL_ID
    digest = hashlib.sha256(f"{model_id}:{voice_id}:{clean}".encode("utf-8")).hexdigest()
    cache_path = TTS_CACHE_DIR / f"{digest}.mp3"
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path.read_bytes()
    if cache_only:
        raise RuntimeError("Demo mode: no cached audio for this text")

    api_key = (os.getenv("ELEVENLABS_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set")

    request = Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        data=json.dumps({"text": clean, "model_id": model_id}).encode("utf-8"),
        headers={"xi-api-key": api_key, "Content-Type": "application/json", "Accept": "audio/mpeg",
                 "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urlopen(request, timeout=TTS_TIMEOUT_S) as response:
            audio = response.read()
    except HTTPError as exc:
        try:  # ElevenLabs explains failures in the body, e.g. {"detail": {"status": "quota_exceeded", ...}}
            detail = json.loads(exc.read().decode("utf-8")).get("detail")
            detail = detail.get("status") or detail.get("message") if isinstance(detail, dict) else detail
        except Exception:  # noqa: BLE001
            detail = exc.reason
        raise RuntimeError(f"ElevenLabs TTS failed: HTTP {exc.code} {detail}") from exc
    except (URLError, OSError) as exc:
        raise RuntimeError(f"ElevenLabs TTS request failed: {exc}") from exc
    if not audio:
        raise RuntimeError("ElevenLabs returned no audio")

    try:
        TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(audio)
    except OSError as exc:
        logger.warning("Could not cache TTS audio: %s", exc)
    return audio
