"""Active NWS watches, warnings and advisories for a point (api.weather.gov/alerts/active)."""

from backend.tools.common import cache_get, cache_put, get_json_within, now_iso, unavailable

SOURCE = "NWS"
URL = "https://api.weather.gov/alerts/active"
TIMEOUT = 8                 # hard cap for the whole request; then available:false
CACHE_SECONDS = 10 * 60     # alerts change quickly, so only 10 min per location (~1 km)
SEVERITY_RANK = {"Extreme": 4, "Severe": 3, "Moderate": 2, "Minor": 1, "Unknown": 0}


def parse_alerts(data):
    """NWS alerts GeoJSON -> [{event, severity, headline, onset, ends, link}], most severe first."""
    items = []
    for feature in (data or {}).get("features") or []:
        props = feature.get("properties") or {}
        severity = props.get("severity") if props.get("severity") in SEVERITY_RANK else "Unknown"
        items.append({
            "event": props.get("event") or "Weather alert",
            "severity": severity,
            "headline": props.get("headline") or props.get("event") or "",
            "onset": props.get("onset") or props.get("effective"),
            "ends": props.get("ends") or props.get("expires"),
            "link": props.get("@id") or feature.get("id"),
        })
    items.sort(key=lambda a: -SEVERITY_RANK[a["severity"]])
    return items


def get_nws_alerts(lat, lon):
    """Return {source: "NWS", items: [...], checked_at} or an unavailable dict. Cached on disk for 10 min."""
    try:
        lat, lon = float(lat), float(lon)
        key = {"lat": round(lat, 2), "lon": round(lon, 2)}
        cached = cache_get("nws_alerts", key, CACHE_SECONDS)
        if cached is not None:
            return cached
        # The point is put in the URL as-is: NWS wants a literal comma, not %2C.
        data = get_json_within(TIMEOUT, f"{URL}?point={lat:.4f},{lon:.4f}", headers={"Accept": "application/geo+json"})
        result = {"source": SOURCE, "items": parse_alerts(data), "checked_at": now_iso()}
        cache_put("nws_alerts", key, result)
        return result
    except Exception as exc:  # noqa: BLE001 - a tool must never raise
        return unavailable(SOURCE, f"{type(exc).__name__}: {exc}")
