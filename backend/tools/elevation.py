"""Total ascent along a route from the Open-Meteo elevation API."""

from backend.tools.common import get_json, now_iso, unavailable

SOURCE = "Open-Meteo Elevation"
URL = "https://api.open-meteo.com/v1/elevation"
TIMEOUT = 10
MAX_POINTS = 100  # Open-Meteo accepts up to 100 coordinates per request


def _sample(coords, n=MAX_POINTS):
    if len(coords) <= n:
        return list(coords)
    step = (len(coords) - 1) / (n - 1)
    return [coords[round(i * step)] for i in range(n)]


def get_elevation_gain(coords):
    """Return {source, elevation_gain_m, points_sampled, checked_at} or an unavailable dict.

    elevation_gain_m is the sum of positive differences between consecutive sampled points.
    """
    try:
        points = _sample([[float(p[0]), float(p[1])] for p in coords])
        if len(points) < 2:
            return unavailable(SOURCE, "Need at least two coordinates")
        data = get_json(URL, params={
            "latitude": ",".join(f"{p[0]:.5f}" for p in points),
            "longitude": ",".join(f"{p[1]:.5f}" for p in points),
        }, timeout=TIMEOUT)
        heights = data["elevation"]
        if len(heights) != len(points):
            return unavailable(SOURCE, f"Expected {len(points)} elevations, got {len(heights)}")
        gain = sum(max(0.0, b - a) for a, b in zip(heights, heights[1:]))
        return {"source": SOURCE, "elevation_gain_m": round(gain), "points_sampled": len(points),
                "checked_at": now_iso()}
    except Exception as exc:  # noqa: BLE001
        return unavailable(SOURCE, f"{type(exc).__name__}: {exc}")
