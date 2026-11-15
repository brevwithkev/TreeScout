"""Total ascent along a route from the Open-Meteo elevation API."""

from threading import Lock
from urllib.error import HTTPError
import time

from backend.tools.common import get_json, now_iso, unavailable

SOURCE = "Open-Meteo Elevation"
URL = "https://api.open-meteo.com/v1/elevation"
TIMEOUT = 10
MAX_POINTS = 100  # Open-Meteo accepts up to 100 coordinates per request
_cache = {}
_lock = Lock()


def _sample(coords, n=MAX_POINTS):
    if len(coords) <= n:
        return list(coords)
    step = (len(coords) - 1) / (n - 1)
    return [coords[round(i * step)] for i in range(n)]


def _fetch(params):
    try:
        return get_json(URL, params=params, timeout=TIMEOUT)
    except HTTPError as exc:
        if exc.code != 429:
            raise
        time.sleep(1.5)  # Open-Meteo rate limit: one polite retry
        return get_json(URL, params=params, timeout=TIMEOUT)


def get_elevation_gain(coords):
    """Return {source, elevation_gain_m, points_sampled, checked_at} or an unavailable dict.

    elevation_gain_m is the sum of positive differences between consecutive sampled points.
    Successful results are cached in memory per sampled route (terrain does not change).
    """
    try:
        points = _sample([[float(p[0]), float(p[1])] for p in coords])
        if len(points) < 2:
            return unavailable(SOURCE, "Need at least two coordinates")
        params = {
            "latitude": ",".join(f"{p[0]:.5f}" for p in points),
            "longitude": ",".join(f"{p[1]:.5f}" for p in points),
        }
        key = (params["latitude"], params["longitude"])
        if key in _cache:
            return {**_cache[key], "checked_at": now_iso()}
        heights = _fetch(params)["elevation"]
        if len(heights) != len(points):
            return unavailable(SOURCE, f"Expected {len(points)} elevations, got {len(heights)}")
        gain = sum(max(0.0, b - a) for a, b in zip(heights, heights[1:]))
        result = {"source": SOURCE, "elevation_gain_m": round(gain), "points_sampled": len(points),
                  "checked_at": now_iso()}
        with _lock:
            if len(_cache) >= 512:
                _cache.pop(next(iter(_cache)))
            _cache[key] = result
        return result
    except Exception as exc:  # noqa: BLE001
        return unavailable(SOURCE, f"{type(exc).__name__}: {exc}")
