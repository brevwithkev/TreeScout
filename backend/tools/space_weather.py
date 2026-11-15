"""Geomagnetic storm activity (aurora chance) from NASA DONKI GST."""

from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
import json
import os

from backend.tools.common import get_json, now_iso, unavailable

SOURCE = "NASA DONKI"
URL = "https://api.nasa.gov/DONKI/GST"
TIMEOUT = 15


def _aurora_chance(kp):
    if kp is None or kp < 5:
        return "low"
    return "possible" if kp < 7 else "likely"


def get_space_weather():
    """Return {source, kp_max, storms, aurora_chance, window, checked_at} or an unavailable dict.

    DONKI GST only lists geomagnetic storms (Kp >= 5). No storms in the window means kp_max is
    null (below storm level) and aurora_chance is "low".
    """
    try:
        key = os.environ.get("NASA_API_KEY") or ""
        if not key:
            return unavailable(SOURCE, "NASA_API_KEY is not set")
        today = datetime.now(timezone.utc).date()
        start, end = today - timedelta(days=3), today + timedelta(days=1)
        events = get_json(URL, params={"startDate": start.isoformat(), "endDate": end.isoformat(),
                                       "api_key": key}, timeout=TIMEOUT) or []
        kps = [float(k["kpIndex"]) for e in events for k in (e.get("allKpIndex") or [])
               if k.get("kpIndex") is not None]
        kp_max = max(kps) if kps else None
        return {
            "source": SOURCE,
            "kp_max": kp_max,
            "storms": len(events),
            "aurora_chance": _aurora_chance(kp_max),
            "window": f"{start.isoformat()} to {end.isoformat()}",
            "checked_at": now_iso(),
        }
    except HTTPError as exc:
        try:  # api.nasa.gov explains failures in the body, e.g. {"error": {"code": "API_KEY_INVALID"}}
            code = json.loads(exc.read().decode("utf-8"))["error"]["code"]
        except Exception:  # noqa: BLE001
            code = exc.reason
        return unavailable(SOURCE, f"HTTP {exc.code}: {code}")
    except Exception as exc:  # noqa: BLE001
        # Never echo the URL: it contains the API key.
        message = str(exc).replace(os.environ.get("NASA_API_KEY") or "\0", "***")
        return unavailable(SOURCE, f"{type(exc).__name__}: {message}")
