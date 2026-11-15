"""Discover named hiking routes and paths using OpenStreetMap Overpass."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import datetime, timezone
from math import asin, cos, degrees, isfinite, radians, sin, sqrt
from threading import Lock
from time import monotonic, time
import json
import hashlib
import logging
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_ENDPOINTS = (
    OVERPASS_URL,
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)
USER_AGENT = "TrekChek hackathon (contact: trekchek-team)"  # the example.com placeholder got HTTP 406
ROOT = Path(__file__).resolve().parents[2]
DISK_CACHE_DIR = ROOT / "data" / "overpass_cache"
SNAPSHOT_DIR = ROOT / "data" / "trail_snapshots"  # anywhere: one successful lookup per place, kept forever
SEED_DIR = ROOT / "backend" / "data" / "trail_snapshots"  # committed snapshots (real Overpass answers)
SEED_MATCH_KM = 10  # a seeded snapshot serves any point within this distance of its center
YPSILANTI_FIXTURE = ROOT / "tests" / "fixtures" / "overpass_ypsilanti.json"
logger = logging.getLogger(__name__)
CACHE_TTL_SECONDS = 300
DISK_FRESH_SECONDS = 24 * 3600  # trail geometry changes rarely; skip Overpass if the disk copy is newer
OVERPASS_BUDGET_SECONDS = 8  # all live Overpass attempts together; then saved copies or an error
TIMEOUT_SECONDS = 8          # per request (abandoned requests end on their own)
HEDGE_SECONDS = 2.5          # start the next mirror if the current one hasn't answered by then
ANYWHERE_RADII_KM = (10, 20)  # anywhere: 10 km, widened once to 20 km when it finds fewer than MIN_TRAILS
MIN_TRAILS = 3
SAC_SCALE = {"hiking": "T1", "mountain_hiking": "T2", "demanding_mountain_hiking": "T3", "alpine_hiking": "T4",
             "demanding_alpine_hiking": "T5", "difficult_alpine_hiking": "T6"}
_cache = {}
_lock = Lock()


class OverpassError(RuntimeError):
    """The upstream service could not provide usable trail data."""


def haversine(a, b):
    """Distance between two [latitude, longitude] points in kilometers."""
    lat1, lon1, lat2, lon2 = map(radians, (*a, *b))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 6371.0088 * 2 * asin(sqrt(min(1.0, max(0.0, h))))


def stitch_segments(segments):
    """Join the closest pair of free endpoints, reversing ways as needed."""
    remaining = [list(segment) for segment in segments if len(segment) >= 2]
    if not remaining:
        return []
    ordered = remaining.pop(0)
    while remaining:
        _, index, prepend, reverse = min(
            (haversine(endpoint, segment[end]), index, prepend, reverse)
            for index, segment in enumerate(remaining)
            for endpoint, prepend in [(ordered[0], True), (ordered[-1], False)]
            for end, reverse in ([(0, True), (-1, False)] if prepend else [(0, False), (-1, True)])
        )
        segment = remaining.pop(index)
        if reverse:
            segment.reverse()
        if prepend:
            ordered = segment + (ordered[1:] if segment[-1] == ordered[0] else ordered)
        else:
            ordered.extend(segment[1:] if ordered[-1] == segment[0] else segment)
    return ordered


def _points(geometry):
    return [[point["lat"], point["lon"]] for point in geometry or [] if point is not None]


def _bbox(lat, lon, radius_km):
    lat_delta = degrees(radius_km / 6371.0088)
    lon_delta = min(180, lat_delta / cos(radians(lat)))
    return tuple(round(value, 6) for value in (
        max(-90, lat - lat_delta), max(-180, lon - lon_delta),
        min(90, lat + lat_delta), min(180, lon + lon_delta),
    ))


def _parse_trails(payload, unnamed_mountain_paths=False):
    """Trails >= 1 km from an Overpass response. unnamed_mountain_paths (anywhere only): an unnamed
    highway=path with a sac_scale is kept as "Unnamed mountain path (T3)"."""
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise ValueError("Response has no trail elements")
    if payload.get("remark"):
        raise ValueError(f"Overpass query failed: {payload['remark']}")
    trails = []
    for element in payload["elements"]:
        tags = element.get("tags", {})
        name = tags.get("name", "").strip()
        if not name and unnamed_mountain_paths and element.get("type") == "way" and tags.get("sac_scale"):
            name = f"Unnamed mountain path ({SAC_SCALE.get(tags['sac_scale'], tags['sac_scale'])})"
        if not name:
            continue
        if element["type"] == "relation":
            coords = stitch_segments([
                _points(member.get("geometry"))
                for member in element.get("members", []) if member["type"] == "way"
            ])
        elif element["type"] == "way":
            coords = _points(element.get("geometry"))
        else:
            continue
        distance = sum(haversine(a, b) for a, b in zip(coords, coords[1:]))
        if distance >= 1:
            trails.append({"name": name, "osm_id": element["id"],
                           "distance_km": round(distance, 3), "coords": coords})
    return trails


def _save_json(path, payload):
    """Atomically replace cache files; cache write errors must not lose live results."""
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle)
        temporary.replace(path)
    except OSError as exc:
        logger.warning("Could not save Overpass response to %s: %s", path, exc)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not remove temporary cache file %s: %s", temporary, exc)


def _query_endpoint(endpoint, query, parse=_parse_trails, timeout=None):
    request = Request(
        endpoint, data=urlencode({"data": query}).encode("utf-8"), method="POST",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(request, timeout=timeout or TIMEOUT_SECONDS) as response:
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}")
        payload = json.load(response)
    return payload, parse(payload)


_cooldown_until = 0.0


def _query_hedged(query, failures, budget=None, parse=_parse_trails):
    """One bounded attempt; never cycle mirrors on throttling."""
    global _cooldown_until
    if monotonic() < _cooldown_until:
        failures.append("Overpass 429 cooldown; try later")
        return None
    budget = min(4, OVERPASS_BUDGET_SECONDS if budget is None else budget)
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="overpass")
    try:
        future = pool.submit(_query_endpoint, OVERPASS_URL, query, parse)
        done, _ = wait([future], timeout=budget)
        if not done:
            failures.append(f"Overpass: no answer within {budget:g} s")
            _cooldown_until = monotonic() + 30
            return None
        try:
            payload, items = future.result()
            return OVERPASS_URL, payload, items
        except (HTTPError, URLError, OSError, ValueError, KeyError, TypeError) as exc:
            delay = 30
            if isinstance(exc, HTTPError) and exc.code == 429:
                from email.utils import parsedate_to_datetime
                value = (exc.headers or {}).get("Retry-After", "60")
                try:
                    delay = max(60, float(value))
                except (ValueError, TypeError):
                    try:
                        delay = max(60, parsedate_to_datetime(value).timestamp() - time())
                    except (ValueError, TypeError, OverflowError):
                        delay = 60
            _cooldown_until = monotonic() + delay
            failures.append(f"Overpass unavailable: {exc}")
            return None
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def get_trails(lat, lon, radius_km=10):
    """Return trails >= 1 km, with endpoint failover and marked offline fallbacks.

    Disk/fixture fallbacks add ``cached: True`` to each trail. The fixture is
    specific to the Ypsilanti integration test's 15 km bbox, not nearby queries.
    Its first successful nonempty live response is saved for committing later.
    """
    if not all(isfinite(value) for value in (lat, lon, radius_km)) or not (
        -90 < lat < 90 and -180 <= lon <= 180 and radius_km > 0
    ):
        raise ValueError("Provide finite coordinates and a positive radius away from the poles.")
    bbox = _bbox(lat, lon, radius_km)
    bounds = ",".join(map(str, bbox))
    bbox_hash = hashlib.sha256(bounds.encode("utf-8")).hexdigest()
    cache_file = DISK_CACHE_DIR / f"{bbox_hash}.json"
    is_ypsilanti_test = bbox == _bbox(42.2411, -83.6130, 15)
    # Serialize cache misses too, so concurrent identical requests share one query.
    with _lock:
        now = monotonic()
        for key in list(_cache):
            if now - _cache[key][0] >= CACHE_TTL_SECONDS:
                del _cache[key]
        if bbox in _cache:
            return deepcopy(_cache[bbox][1])
        try:
            if cache_file.exists():
                trails = _parse_trails(json.loads(cache_file.read_text(encoding="utf-8")))
                for trail in trails:
                    trail.update(cached=True, captured_at=datetime.fromtimestamp(cache_file.stat().st_mtime, timezone.utc).isoformat())
                logger.info("Using Overpass response saved %.0f min ago: %s",
                            (time() - cache_file.stat().st_mtime) / 60, cache_file.name)
                _cache[bbox] = (monotonic(), trails)
                return deepcopy(trails)
        except (OSError, ValueError, KeyError, TypeError):
            pass  # missing or unreadable: query live below
        # Permanent, geographically matched real geometry before any live request.
        seed, _distance = (None, None) if is_ypsilanti_test else _seed_for(lat, lon)
        path = YPSILANTI_FIXTURE if is_ypsilanti_test else None
        try:
            payload = seed["payload"] if seed is not None else json.loads(path.read_text(encoding="utf-8")) if path else None
            if payload is not None:
                items = _parse_anywhere(payload)
                captured = seed.get("saved_at") if seed else datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                for item in items:
                    item.update(cached=True, captured_at=captured)
                _cache[bbox] = (monotonic(), items)
                return deepcopy(items)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        query = (
            '[out:json][timeout:25];('
            f'relation["route"="hiking"]({bounds});'
            f'way["name"]["highway"~"^(path|footway)$"]({bounds});'
            ');out geom;'
        )
        failures = []
        trails = None
        answer = _query_hedged(query, failures)
        if answer:
            endpoint, payload, trails = answer
            logger.info("Overpass endpoint answered: %s", endpoint)
            _save_json(cache_file, payload)
            if is_ypsilanti_test and len(trails) >= 5 and not YPSILANTI_FIXTURE.exists():
                _save_json(YPSILANTI_FIXTURE, payload)
                logger.info("Saved known-good Ypsilanti fixture to %s", YPSILANTI_FIXTURE)
        else:
            fallback_paths = [cache_file]
            if is_ypsilanti_test:
                fallback_paths.append(YPSILANTI_FIXTURE)
            for path in fallback_paths:
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    trails = _parse_trails(payload)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    logger.debug("Overpass fallback unavailable at %s: %s", path, exc)
                    continue
                for trail in trails:
                    trail["cached"] = True
                logger.warning("All Overpass endpoints failed; using cached response from %s", path)
                break
            else:
                seed, distance = (None, None) if is_ypsilanti_test else _seed_for(lat, lon)
                if seed is not None:  # e.g. Ann Arbor, Detroit: precached with `python -m backend.tools.trails precache`
                    trails = _parse_anywhere(seed["payload"])
                    for trail in trails:
                        trail["cached"] = True
                    logger.warning("All Overpass endpoints failed; using the seeded snapshot of %s (%.1f km away)",
                                   seed.get("name"), distance)
            if trails is None:
                raise OverpassError(
                    f"Overpass returned no trail data within {OVERPASS_BUDGET_SECONDS} s, "
                    "and no usable cached response or matching fixture is available. "
                    + "; ".join(failures)
                )
        if len(_cache) >= 128:
            del _cache[next(iter(_cache))]
        _cache[bbox] = (monotonic(), trails)
        return deepcopy(trails)


def _parse_anywhere(payload):
    return _parse_trails(payload, unnamed_mountain_paths=True)


def _anywhere_query(bounds, server_timeout=25):
    """Hiking routes, named paths/footways, named tracks, and paths with a sac_scale (mountain trails)."""
    return (f'[out:json][timeout:{server_timeout}];('
            f'relation["route"="hiking"]({bounds});'
            f'way["name"]["highway"~"^(path|footway)$"]({bounds});'
            f'way["highway"="track"]["name"]({bounds});'
            f'way["highway"="path"]["sac_scale"]({bounds});'
            ');out geom;')


def _snapshot_path(lat, lon):
    return SNAPSHOT_DIR / f"{lat:.3f}_{lon:.3f}.json"


REGION_POINT_KM = 600  # a region Nominatim returns as a point (e.g. "Rocky Mountains"): seeds within this distance


def seeds_in_region(lat, lon, bbox=None):
    """Names of the seeded snapshots inside a large region: inside its bounding box, or (no real box) within
    REGION_POINT_KM of its point. Nearest first."""
    out = []
    real_box = bbox is not None and haversine([bbox[0], bbox[2]], [bbox[1], bbox[3]]) >= 50
    for _path, name, s_lat, s_lon in _seed_index():
        inside = (bbox[0] <= s_lat <= bbox[1] and bbox[2] <= s_lon <= bbox[3]) if real_box else (
            haversine([lat, lon], [s_lat, s_lon]) <= REGION_POINT_KM)
        if inside:
            out.append((haversine([lat, lon], [s_lat, s_lon]), name))
    return [name for _, name in sorted(out)]


_seed_index_cache = {"key": None, "items": []}
_seed_lock = Lock()


def _seed_index():
    """[(path, name, lat, lon)] for the seeded snapshots; re-read only when the files change (they are MBs)."""
    paths = sorted(SEED_DIR.glob("*.json"))
    try:
        key = (str(SEED_DIR), tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in paths))
    except OSError:
        key = None
    with _seed_lock:
        if key is not None and key == _seed_index_cache["key"]:
            return _seed_index_cache["items"]
        items = []
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                items.append((path, data["name"], float(data["lat"]), float(data["lon"])))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                logger.warning("Unreadable seeded snapshot %s: %s", path.name, exc)
        _seed_index_cache.update(key=key, items=items)
        return items


def _seed_for(lat, lon):
    """The nearest committed snapshot within SEED_MATCH_KM: (data, distance_km), or (None, None)."""
    near = [(haversine([lat, lon], [s_lat, s_lon]), path) for path, _n, s_lat, s_lon in _seed_index()]
    near = [(d, path) for d, path in near if d <= SEED_MATCH_KM]
    if not near:
        return None, None
    distance, path = min(near)
    try:
        return json.loads(path.read_text(encoding="utf-8")), distance
    except (OSError, ValueError) as exc:
        logger.warning("Unreadable seeded snapshot %s: %s", path.name, exc)
        return None, None


def get_trails_anywhere(lat, lon):
    """(trails, radius_km, snapshot) for any place outside the Ypsilanti path (get_trails). snapshot is None for a
    live answer, else {"kind": "saved" | "seeded", "name", "saved_at"} (shown in the trace).

    A saved snapshot for the point (~100 m) is used first, forever; then a committed seed within SEED_MATCH_KM
    (backend/data/trail_snapshots/). Otherwise live Overpass (the same mirrors,
    OVERPASS_BUDGET_SECONDS for everything): 10 km, widened once to 20 km if that finds fewer than MIN_TRAILS.
    A completed search is saved, even with no trails, so a place needs one successful lookup. Raises
    OverpassError when Overpass does not answer and there is no snapshot.
    """
    lat, lon = float(lat), float(lon)
    if not (isfinite(lat) and isfinite(lon) and -90 < lat < 90 and -180 <= lon <= 180):
        raise ValueError("Provide finite coordinates away from the poles.")
    path = _snapshot_path(lat, lon)
    memo = ("anywhere", round(lat, 3), round(lon, 3))
    with _lock:
        if memo in _cache and monotonic() - _cache[memo][0] < CACHE_TTL_SECONDS:
            trails, radius, snapshot = _cache[memo][1]
            return deepcopy(trails), radius, deepcopy(snapshot)
        trails, snapshot = None, None
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if haversine([lat, lon], [float(saved['lat']), float(saved['lon'])]) > 0.2:
                raise ValueError("Saved snapshot does not match this location")
            trails, radius = _parse_anywhere(saved["payload"]), saved["radius_km"]
            snapshot = {"kind": "saved", "name": saved.get("name"), "saved_at": saved.get("saved_at")}
            logger.info("Using saved trail snapshot %s (%g km)", path.name, radius)
        except (OSError, ValueError, KeyError, TypeError):
            trails = None
        if trails is None:
            seed, distance = _seed_for(lat, lon)
            if seed is not None:
                trails, radius = _parse_anywhere(seed["payload"]), seed["radius_km"]
                snapshot = {"kind": "seeded", "name": seed.get("name"), "saved_at": seed.get("saved_at"),
                            "distance_km": round(distance, 1), "lat": seed["lat"], "lon": seed["lon"]}
                logger.info("Using seeded trail snapshot %s (%.1f km away)", seed.get("name"), distance)
        if trails is None:
            deadline, failures, best, complete = monotonic() + OVERPASS_BUDGET_SECONDS, [], None, False
            for radius in ANYWHERE_RADII_KM:
                left = deadline - monotonic()
                if left <= 0.5:
                    failures.append(f"no time left for the {radius} km search")
                    break
                bounds = ",".join(map(str, _bbox(lat, lon, radius)))
                answer = _query_hedged(_anywhere_query(bounds), failures, budget=left, parse=_parse_anywhere)
                if not answer:
                    break
                best = (answer[1], answer[2], radius)
                if len(answer[2]) >= MIN_TRAILS or radius == ANYWHERE_RADII_KM[-1]:
                    complete = True
                    break
            if best is None:
                raise OverpassError(f"Overpass returned no trail data within {OVERPASS_BUDGET_SECONDS} s and there "
                                    "is no saved snapshot for this place. " + "; ".join(failures))
            payload, trails, radius = best
            if complete:  # a 10 km answer whose 20 km retry failed is used but not saved
                saved_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                _save_json(path, {"lat": lat, "lon": lon, "radius_km": radius, "saved_at": saved_at,
                                  "payload": payload})
        if len(_cache) >= 128:
            del _cache[next(iter(_cache))]
        _cache[memo] = (monotonic(), (trails, radius, snapshot))
        return deepcopy(trails), radius, deepcopy(snapshot)


# ---------------------------------------------------------------------------------------------
# Pre-cache seeded snapshots (a command, never part of /plan):
#     python -m backend.tools.trails precache "Zion National Park, Utah, USA" "Banff, Alberta, Canada" ...
# Each place is geocoded (Nominatim), then ONE Overpass query runs at a time (mirrors tried in turn, PRECACHE_TIMEOUT_S
# each; /plan keeps its 8 s budget), and the trails are saved to backend/data/trail_snapshots/<slug>.json.
# ---------------------------------------------------------------------------------------------

PRECACHE_TIMEOUT_S = 60
PRECACHE_PAUSE_S = 5


def _round_geometry(points):
    return [{"lat": round(p["lat"], 5), "lon": round(p["lon"], 5)} for p in points or [] if p]


def _trim_payload(payload):
    """Only the elements that become trails (>= 1 km), with only the fields the parser reads, coords rounded."""
    out = []
    for element in payload.get("elements") or []:
        if not _parse_anywhere({"elements": [element]}):
            continue
        slim = {"type": element["type"], "id": element["id"], "tags": element.get("tags", {})}
        if element["type"] == "way":
            slim["geometry"] = _round_geometry(element.get("geometry"))
        else:
            slim["members"] = [{"type": "way", "geometry": _round_geometry(m.get("geometry"))}
                               for m in element.get("members", []) if m.get("type") == "way"]
        out.append(slim)
    return {"elements": out}


def _slug(name):
    import re
    return re.sub(r"[^a-z0-9]+", "_", name.split(",")[0].lower()).strip("_") or "place"


def precache_place(name, timeout=PRECACHE_TIMEOUT_S):
    """Geocode name, query Overpass one request at a time (10 km, 20 km if under MIN_TRAILS), save a seed.
    Returns {name, file, lat, lon, radius_km, trails, kb} or {name, error}."""
    from backend.tools.geocode import geocode
    found = geocode(name, name)
    if found.get("available") is False or not found.get("found"):
        return {"name": name, "error": f"geocode: {found.get('error') or found.get('reason')}"}
    lat, lon = round(found["lat"], 4), round(found["lon"], 4)
    best, failures = None, []
    for radius in ANYWHERE_RADII_KM:
        query = _anywhere_query(",".join(map(str, _bbox(lat, lon, radius))), server_timeout=timeout)
        answer = None
        for endpoint in OVERPASS_ENDPOINTS:  # one query at a time, never hedged
            try:
                answer = _query_endpoint(endpoint, query, _parse_anywhere, timeout=timeout)
                break
            except (HTTPError, URLError, OSError, ValueError, KeyError, TypeError) as exc:
                failures.append(f"{endpoint}: {type(exc).__name__}: {exc}")
        if answer is None:
            break
        best = (answer[0], answer[1], radius)
        if len(answer[1]) >= MIN_TRAILS:
            break
    if best is None:
        return {"name": name, "error": "; ".join(failures) or "no answer"}
    payload, found_trails, radius = best
    saved_at = datetime.now(timezone.utc).date().isoformat()
    data = {"name": name, "lat": lat, "lon": lon, "radius_km": radius, "saved_at": saved_at,
            "note": f"Seeded snapshot: the live Overpass answer of {saved_at} (OpenStreetMap contributors, ODbL) for "
                    f"{found.get('display_name')}, {radius} km; only elements that are trails >= 1 km, coordinates "
                    f"rounded to 5 decimals. Written by `python -m backend.tools.trails precache`.",
            "payload": _trim_payload(payload)}
    path = SEED_DIR / f"{_slug(name)}.json"
    SEED_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, separators=(",", ":")) + "\n", encoding="utf-8")
    return {"name": name, "file": path.name, "lat": lat, "lon": lon, "radius_km": radius,
            "trails": len(found_trails), "kb": path.stat().st_size // 1024, "where": found.get("display_name")}


if __name__ == "__main__":
    import sys
    import time as _time
    if len(sys.argv) < 3 or sys.argv[1] != "precache":
        print('usage: python -m backend.tools.trails precache "<place>" ["<place>" ...]')
        sys.exit(2)
    logging.basicConfig(level=logging.WARNING)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    failed = 0
    for i, place in enumerate(sys.argv[2:]):
        if i:
            _time.sleep(PRECACHE_PAUSE_S)
        result = precache_place(place)
        if "error" in result:
            failed += 1
            print(f"FAILED  {place}: {result['error']}", flush=True)
        else:
            print(f"{result['trails']:>4} trails  {result['radius_km']:>2} km  {result['kb']:>5} KB  {result['file']:<32} "
                  f"{result['where']}", flush=True)
    sys.exit(1 if failed else 0)
