"""Open natural-hazard events near a point from NASA EONET v3."""

from backend.tools.common import cache_get, cache_put, get_json_within, now_iso, unavailable
from backend.tools.trails import haversine

SOURCE = "NASA EONET"
URL = "https://eonet.gsfc.nasa.gov/api/v3/events"
TIMEOUT = 8                 # hard cap for the whole request; then available:false
CACHE_SECONDS = 6 * 3600    # results cached on disk per location (~1 km) and radius


def _point(geometry):
    """[lat, lon] for an EONET geometry: the point itself, or the centroid of a polygon's outer ring."""
    coords = geometry.get("coordinates")
    if geometry.get("type") == "Point":
        return [float(coords[1]), float(coords[0])]
    ring = coords[0] if coords and isinstance(coords[0][0], (list, tuple)) else coords
    return [sum(p[1] for p in ring) / len(ring), sum(p[0] for p in ring) / len(ring)]


def get_hazards(lat, lon, radius_km=100):
    """Return {source, items: [...], radius_km, checked_at} sorted by distance, or an unavailable dict.

    Successful results are cached for 6 h per location (rounded to 0.01 deg) and radius; a cached result
    keeps its original checked_at (so the UI shows when EONET was actually checked).
    """
    try:
        key = {"lat": round(float(lat), 2), "lon": round(float(lon), 2), "radius_km": float(radius_km)}
        cached = cache_get("eonet", key, CACHE_SECONDS)
        if cached is not None:
            return cached
        events = get_json_within(TIMEOUT, URL, params={"status": "open"})["events"]
        items = []
        for event in events:
            geometries = [g for g in event.get("geometry") or [] if g.get("coordinates")]
            if not geometries:
                continue
            latest = max(geometries, key=lambda g: g.get("date") or "")
            try:
                where = _point(latest)
            except (TypeError, ValueError, IndexError, ZeroDivisionError):
                continue
            distance = haversine([lat, lon], where)
            if distance > radius_km:
                continue
            categories = event.get("categories") or [{}]
            sources = event.get("sources") or [{}]
            items.append({
                "title": event.get("title") or "Untitled event",
                "type": categories[0].get("title") or categories[0].get("id") or "Event",
                "date": latest.get("date"),
                "lat": round(where[0], 5),
                "lon": round(where[1], 5),
                "distance_km": round(distance, 1),
                "source": SOURCE,
                "link": sources[0].get("url") or event.get("link"),
            })
        items.sort(key=lambda item: item["distance_km"])
        result = {"source": SOURCE, "items": items, "radius_km": radius_km, "checked_at": now_iso()}
        cache_put("eonet", key, result)
        return result
    except Exception as exc:  # noqa: BLE001
        return unavailable(SOURCE, f"{type(exc).__name__}: {exc}")
