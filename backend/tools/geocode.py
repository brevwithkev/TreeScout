"""Place name -> coordinates with OpenStreetMap Nominatim (https://nominatim.org/release-docs/latest/api/Search/).

Nominatim's usage policy: an identifying User-Agent, at most 1 request per second, and cache results. Results
(found or not found) are cached on disk forever; failures are not cached.

One request asks for RESULTS candidates; the best is picked by kind (pick_best): trailheads, parks, protected areas
and settlements first, then anything else, natural features (cliffs, valleys, peaks) last; Nominatim's own order
breaks ties.
"""

from threading import Lock
import time

from backend.tools.common import USER_AGENT, cache_get, cache_put, get_json_within, now_iso, unavailable

SOURCE = "OpenStreetMap Nominatim"
URL = "https://nominatim.openstreetmap.org/search"
TIMEOUT = 6
MIN_INTERVAL_S = 1.0          # Nominatim: max 1 request/second
RESULTS = 5
CACHE_VERSION = 2             # 2: best of RESULTS candidates (1 was the first of limit=1)
PREFERRED = {                 # (class, type) -> rank; lower is better. Unlisted: 1, class natural: 2.
    ("highway", "trailhead"): 0, ("boundary", "national_park"): 0, ("boundary", "protected_area"): 0,
    ("leisure", "park"): 0, ("leisure", "nature_reserve"): 0,
    ("place", "village"): 0, ("place", "town"): 0, ("place", "city"): 0, ("place", "hamlet"): 0,
}
SETTLEMENTS = {"village", "town", "city", "hamlet", "municipality"}  # boundary=administrative with these addresstypes
FOREVER_S = 100 * 365 * 86400
_lock = Lock()
_last_request = [0.0]


def clean_place(place):
    """Collapse whitespace; None for an empty name."""
    name = " ".join(str(place or "").split()).strip(" ,.")[:200]
    return name or None


def rank(hit):
    """0 trailhead/park/protected area/settlement, 1 other, 2 natural feature."""
    kind = (hit.get("class"), hit.get("type"))
    if kind in PREFERRED:
        return PREFERRED[kind]
    if kind == ("boundary", "administrative") and hit.get("addresstype") in SETTLEMENTS:
        return 0
    return 2 if hit.get("class") == "natural" else 1


def pick_best(hits):
    """The best-ranked candidate; Nominatim's order (importance) breaks ties."""
    return min(enumerate(hits), key=lambda pair: (rank(pair[1]), pair[0]))[1]


def geocode(place):
    """{source, query, found: true, lat, lon, display_name, checked_at} or {..., found: false} for a place name,
    or an unavailable dict when Nominatim can't be reached. Never raises."""
    name = clean_place(place)
    if name is None:
        return unavailable(SOURCE, "No place name")
    key = {"q": name.lower(), "v": CACHE_VERSION}
    cached = cache_get("nominatim", key, FOREVER_S)
    if cached is not None:
        return cached
    try:
        with _lock:  # one request at a time, at least MIN_INTERVAL_S apart
            wait = _last_request[0] + MIN_INTERVAL_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                data = get_json_within(TIMEOUT, URL, params={"q": name, "format": "json", "limit": RESULTS},
                                       headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
            finally:
                _last_request[0] = time.monotonic()
        if not isinstance(data, list):
            raise ValueError("unexpected Nominatim response")
        if data:
            hit = pick_best(data)
            result = {"source": SOURCE, "query": name, "found": True, "lat": float(hit["lat"]),
                      "lon": float(hit["lon"]), "display_name": hit.get("display_name") or name,
                      "kind": f"{hit.get('class')}={hit.get('type')}", "checked_at": now_iso()}
        else:
            result = {"source": SOURCE, "query": name, "found": False, "checked_at": now_iso()}
    except Exception as exc:  # noqa: BLE001 - a tool must never raise
        return unavailable(SOURCE, f"{type(exc).__name__}: {exc}")
    cache_put("nominatim", key, result)
    return result
