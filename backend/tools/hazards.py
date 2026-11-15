"""Open natural-hazard events near a point from NASA EONET v3."""

from backend.tools.common import get_json, now_iso, unavailable
from backend.tools.trails import haversine

SOURCE = "NASA EONET"
URL = "https://eonet.gsfc.nasa.gov/api/v3/events"
TIMEOUT = 15


def _point(geometry):
    """[lat, lon] for an EONET geometry: the point itself, or the centroid of a polygon's outer ring."""
    coords = geometry.get("coordinates")
    if geometry.get("type") == "Point":
        return [float(coords[1]), float(coords[0])]
    ring = coords[0] if coords and isinstance(coords[0][0], (list, tuple)) else coords
    return [sum(p[1] for p in ring) / len(ring), sum(p[0] for p in ring) / len(ring)]


def get_hazards(lat, lon, radius_km=100):
    """Return {source, items: [...], radius_km, checked_at} sorted by distance, or an unavailable dict."""
    try:
        events = get_json(URL, params={"status": "open"}, timeout=TIMEOUT)["events"]
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
        return {"source": SOURCE, "items": items, "radius_km": radius_km, "checked_at": now_iso()}
    except Exception as exc:  # noqa: BLE001
        return unavailable(SOURCE, f"{type(exc).__name__}: {exc}")
