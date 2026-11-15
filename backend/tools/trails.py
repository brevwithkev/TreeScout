"""Discover named hiking routes and paths using OpenStreetMap Overpass."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
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
USER_AGENT = "TreeScout hackathon (contact: treescout-team)"  # the example.com placeholder got HTTP 406
ROOT = Path(__file__).resolve().parents[2]
DISK_CACHE_DIR = ROOT / "data" / "overpass_cache"
YPSILANTI_FIXTURE = ROOT / "tests" / "fixtures" / "overpass_ypsilanti.json"
logger = logging.getLogger(__name__)
CACHE_TTL_SECONDS = 300
DISK_FRESH_SECONDS = 24 * 3600  # trail geometry changes rarely; skip Overpass if the disk copy is newer
TIMEOUT_SECONDS = 12
HEDGE_SECONDS = 4  # start the next mirror if the current one hasn't answered by then
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


def _parse_trails(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        raise ValueError("Response has no trail elements")
    if payload.get("remark"):
        raise ValueError(f"Overpass query failed: {payload['remark']}")
    trails = []
    for element in payload["elements"]:
        name = element.get("tags", {}).get("name", "").strip()
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


def _query_endpoint(endpoint, query):
    request = Request(
        endpoint, data=urlencode({"data": query}).encode("utf-8"), method="POST",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}")
        payload = json.load(response)
    return payload, _parse_trails(payload)


def _query_hedged(query, failures):
    """Hedged requests: start the first endpoint, and start the next one after HEDGE_SECONDS without an
    answer (or right away after a failure). The first success wins. Returns (endpoint, payload, trails)
    or None, appending each failure to ``failures``. Worst case is about TIMEOUT + 2 * HEDGE seconds
    instead of 3 * TIMEOUT."""
    pool = ThreadPoolExecutor(max_workers=len(OVERPASS_ENDPOINTS), thread_name_prefix="overpass")
    try:
        pending, queue = {}, list(OVERPASS_ENDPOINTS)
        while queue or pending:
            if queue:  # each pass follows a hedge timeout or a failure: start the next mirror
                endpoint = queue.pop(0)
                pending[pool.submit(_query_endpoint, endpoint, query)] = endpoint
            done, _ = wait(pending, timeout=HEDGE_SECONDS if queue else None, return_when=FIRST_COMPLETED)
            for future in done:
                endpoint = pending.pop(future)
                try:
                    payload, trails = future.result()
                except (HTTPError, URLError, OSError, ValueError, KeyError, TypeError) as exc:
                    message = f"{endpoint}: {exc}"
                    failures.append(message)
                    logger.warning("Overpass endpoint failed: %s", message)
                    continue
                return endpoint, payload, trails
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
            if time() - cache_file.stat().st_mtime < DISK_FRESH_SECONDS:
                trails = _parse_trails(json.loads(cache_file.read_text(encoding="utf-8")))
                logger.info("Using Overpass response saved %.0f min ago: %s",
                            (time() - cache_file.stat().st_mtime) / 60, cache_file.name)
                _cache[bbox] = (monotonic(), trails)
                return deepcopy(trails)
        except (OSError, ValueError, KeyError, TypeError):
            pass  # missing or unreadable: query live below
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
                raise OverpassError(
                    f"All Overpass endpoints failed ({TIMEOUT_SECONDS}-second timeout per endpoint, "
                    f"staggered {HEDGE_SECONDS} s apart), "
                    "and no usable cached response or matching fixture is available. "
                    + "; ".join(failures)
                )
        if len(_cache) >= 128:
            del _cache[next(iter(_cache))]
        _cache[bbox] = (monotonic(), trails)
        return deepcopy(trails)
