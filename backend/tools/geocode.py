"""Place name -> coordinates with OpenStreetMap Nominatim (https://nominatim.org/release-docs/latest/api/Search/).

Nominatim's usage policy: an identifying User-Agent, at most 1 request per second, and cache results. The raw
candidates (found or none) are cached on disk forever; failures are not cached.

One request asks for RESULTS candidates (with address details); pick_best chooses a hiking area:
- never a street, house, building, transport stop, shop or residential area, unless the request looks like an
  address (looks_like_address);
- national parks, protected areas, nature reserves, parks, peaks, trailheads and towns first, then anything else,
  other natural features (cliffs, valleys) last; Nominatim's own order breaks ties;
- not when the request names a country and the result is in another one.
If nothing qualifies: {"found": false, "reason": "not_hiking_area"} ("Couldn't find <place> as a hiking area").
If Nominatim's own best match (or the pick) is a large region (a mountain range, continent, country, state, province
or region, or anything else over LARGE_KM across that isn't a park, protected area, peak, trailhead or town):
{"found": false, "reason": "large_region", "bbox": [s, n, w, e] or None, "lat", "lon"}; no trails are searched.
"""

from threading import Lock
import re
import time

from backend.tools.common import USER_AGENT, cache_get, cache_put, get_json_within, now_iso, unavailable

SOURCE = "OpenStreetMap Nominatim"
URL = "https://nominatim.openstreetmap.org/search"
TIMEOUT = 6
MIN_INTERVAL_S = 1.0          # Nominatim: max 1 request/second
RESULTS = 5
CACHE_VERSION = 4             # 4: + bounding boxes; 3: candidates with address details; 2: best of 5; 1: limit=1
LARGE_KM = 200                # a bounding box wider than this (diagonal) is a region, not a hiking area
LARGE_TYPES = {"continent", "country", "state", "province", "region", "state_district", "archipelago", "territory"}
PREFERRED = {                 # (class, type) -> rank 0; other kept results 1, other natural features 2
    ("highway", "trailhead"), ("boundary", "national_park"), ("boundary", "protected_area"),
    ("leisure", "park"), ("leisure", "nature_reserve"), ("natural", "peak"), ("natural", "volcano"),
    ("place", "village"), ("place", "town"), ("place", "city"), ("place", "hamlet"), ("place", "municipality"),
}
SETTLEMENTS = {"village", "town", "city", "hamlet", "municipality"}  # boundary=administrative with these addresstypes
EXCLUDED_CLASSES = {"building", "railway", "public_transport", "aeroway", "shop", "office", "craft", "man_made"}
EXCLUDED_ADDRESSTYPES = {"road", "house", "house_number", "building", "residential", "street"}
EXCLUDED_TYPES = {("place", "house"), ("place", "houses"), ("place", "plot"), ("landuse", "residential"),
                  ("landuse", "commercial"), ("landuse", "industrial"), ("landuse", "retail")}
AMENITIES_KEPT = {"shelter"}  # e.g. trail shelters; other amenities (cafes, bus stations) are not hiking areas
ADDRESS = re.compile(r"\b\d+\s+\w+(?:\s+\w+)*\s+(?:street|st|avenue|ave|road|rd|boulevard|blvd|drive|dr|lane|ln|"
                     r"way|court|ct|highway|hwy|place|pl)\b", re.IGNORECASE)
COUNTRIES = {  # names a request may use -> ISO code (Nominatim address.country_code)
    "united states": "us", "united states of america": "us", "usa": "us", "u.s.": "us", "u.s.a.": "us",
    "america": "us", "canada": "ca", "mexico": "mx", "united kingdom": "gb", "uk": "gb", "england": "gb",
    "scotland": "gb", "wales": "gb", "ireland": "ie", "france": "fr", "germany": "de", "italy": "it", "spain": "es",
    "portugal": "pt", "switzerland": "ch", "austria": "at", "norway": "no", "sweden": "se", "finland": "fi",
    "iceland": "is", "new zealand": "nz", "australia": "au", "japan": "jp", "nepal": "np", "peru": "pe",
    "chile": "cl", "argentina": "ar", "costa rica": "cr", "south africa": "za", "philippines": "ph",
}
US_CODES = {"us", "pr", "gu", "vi", "as", "mp"}  # NWS covers the US and its territories
FOREVER_S = 100 * 365 * 86400
_lock = Lock()
_last_request = [0.0]


def clean_place(place):
    """Collapse whitespace; None for an empty name."""
    name = " ".join(str(place or "").split()).strip(" ,.")[:200]
    return name or None


def looks_like_address(text):
    return bool(ADDRESS.search(str(text or "")))


def countries_named(*texts):
    """ISO codes of the countries the texts name ("Banff, Alberta, Canada" -> {"ca"})."""
    found = set()
    for text in texts:
        low = f" {str(text or '').lower()} "
        for name, code in COUNTRIES.items():  # "New Mexico" / "New England" are US regions, not countries
            if re.search(rf"(?<!new )(?<![a-z]){re.escape(name)}(?![a-z])", low):
                found.add(code)
    return found


def country_of(hit):
    """(country name, ISO code) of a Nominatim hit: address details, else the last part of display_name."""
    address = hit.get("address") or {}
    name = address.get("country") or str(hit.get("display_name") or "").split(",")[-1].strip() or None
    code = (address.get("country_code") or COUNTRIES.get(str(name or "").lower()) or "").lower() or None
    return name, code


def excluded(hit):
    """Streets, houses, buildings, transport, shops and residential areas: never a hiking area."""
    cls, typ = hit.get("class"), hit.get("type")
    if cls == "highway":
        return typ != "trailhead"
    if cls in EXCLUDED_CLASSES or (cls, typ) in EXCLUDED_TYPES or hit.get("addresstype") in EXCLUDED_ADDRESSTYPES:
        return True
    return cls == "amenity" and typ not in AMENITIES_KEPT


def _bbox_km(hit):
    from backend.tools.trails import haversine  # local: trails does not import geocode
    try:
        s, n, w, e = (float(v) for v in hit["boundingbox"])
    except (KeyError, TypeError, ValueError):
        return None
    return haversine([s, w], [n, e])


def is_large_region(hit):
    """A mountain range, continent, country, state, province or region, or a non-park area over LARGE_KM across."""
    if (hit.get("class"), hit.get("type")) == ("natural", "mountain_range"):
        return True
    if hit.get("type") in LARGE_TYPES or hit.get("addresstype") in LARGE_TYPES:
        return True
    if rank(hit) == 0:  # parks, protected areas, peaks, trailheads, towns: big boxes are fine (e.g. national parks)
        return False
    size = _bbox_km(hit)
    return size is not None and size > LARGE_KM


def rank(hit):
    """0 park/protected area/reserve/peak/trailhead/settlement, 1 other, 2 other natural feature."""
    kind = (hit.get("class"), hit.get("type"))
    if kind in PREFERRED:
        return 0
    if kind == ("boundary", "administrative") and hit.get("addresstype") in SETTLEMENTS:
        return 0
    return 2 if hit.get("class") == "natural" else 1


def pick_best(hits, request_text="", place=""):
    """The best hiking-area candidate, or None when none qualifies (only streets etc., or the wrong country)."""
    address = looks_like_address(request_text) or looks_like_address(place)
    named = countries_named(request_text, place)
    ok = [(i, h) for i, h in enumerate(hits) if address or not excluded(h)]
    if named:
        ok = [(i, h) for i, h in ok if country_of(h)[1] in named]
    if not ok:
        return None
    return min(ok, key=lambda pair: (rank(pair[1]), pair[0]))[1]


def _trim(hit):
    keep = ("lat", "lon", "class", "type", "addresstype", "display_name", "importance", "boundingbox")
    out = {k: hit.get(k) for k in keep if k in hit}
    address = hit.get("address") or {}
    if address:
        out["address"] = {k: address[k] for k in ("country", "country_code") if k in address}
    return out


def geocode(place, request_text=""):
    """{source, query, found: true, lat, lon, display_name, country, country_code, kind, checked_at} for the best
    hiking area named place; {..., found: false, reason: "no_match" | "not_hiking_area"} when there is none; or an
    unavailable dict when Nominatim can't be reached. Never raises."""
    name = clean_place(place)
    if name is None:
        return unavailable(SOURCE, "No place name")
    key = {"q": name.lower(), "v": CACHE_VERSION}
    cached = cache_get("nominatim", key, FOREVER_S)
    if cached is None:
        try:
            with _lock:  # one request at a time, at least MIN_INTERVAL_S apart
                wait = _last_request[0] + MIN_INTERVAL_S - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                try:
                    data = get_json_within(TIMEOUT, URL, params={"q": name, "format": "json", "limit": RESULTS,
                                                                 "addressdetails": 1},
                                           headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
                finally:
                    _last_request[0] = time.monotonic()
            if not isinstance(data, list):
                raise ValueError("unexpected Nominatim response")
        except Exception as exc:  # noqa: BLE001 - a tool must never raise
            return unavailable(SOURCE, f"{type(exc).__name__}: {exc}")
        cached = {"query": name, "hits": [_trim(h) for h in data if isinstance(h, dict)], "checked_at": now_iso()}
        cache_put("nominatim", key, cached)
    hits = cached.get("hits") or []
    base = {"source": SOURCE, "query": name, "checked_at": cached.get("checked_at") or now_iso()}
    if not hits:
        return {**base, "found": False, "reason": "no_match"}
    hit = pick_best(hits, request_text, name)
    top = hits[0]  # Nominatim's own best match: "Colorado" is the state, even if a small town is also called that
    region = top if is_large_region(top) and not looks_like_address(request_text) else (
        hit if hit is not None and is_large_region(hit) else None)
    if region is not None:
        bbox = region.get("boundingbox")
        return {**base, "found": False, "reason": "large_region", "region": region.get("display_name") or name,
                "lat": float(region["lat"]), "lon": float(region["lon"]),
                "bbox": [float(v) for v in bbox] if bbox else None}
    if hit is None:
        return {**base, "found": False, "reason": "not_hiking_area"}
    country, code = country_of(hit)
    return {**base, "found": True, "lat": float(hit["lat"]), "lon": float(hit["lon"]),
            "display_name": hit.get("display_name") or name, "country": country, "country_code": code,
            "kind": f"{hit.get('class')}={hit.get('type')}"}
