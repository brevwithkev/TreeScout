"""Total ascent along a route: Open-Meteo, with Open-Elevation as a fallback, and a permanent disk cache.

Terrain doesn't change, so every successful result is kept forever in backend/data/elevation_cache.json
(committed), keyed by the trail's OSM id plus a hash of the sampled points. A cache hit never touches the
network, which also protects the Open-Meteo daily quota.

Fill the cache for every trail in the saved Ypsilanti snapshot:
    python -m backend.tools.elevation precompute
"""

from pathlib import Path
from threading import Lock
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import hashlib
import json
import logging
import os
import time

from backend.tools.common import USER_AGENT, get_json, now_iso, unavailable

logger = logging.getLogger("treescout.elevation")

SOURCE = "Open-Meteo Elevation"            # kept for callers that label the primary provider
OPEN_METEO = ("Open-Meteo Elevation", "https://api.open-meteo.com/v1/elevation")
OPEN_ELEVATION = ("Open-Elevation", "https://api.open-elevation.com/api/v1/lookup")
TIMEOUT = 10
MAX_POINTS = 100  # Open-Meteo accepts up to 100 coordinates per request
CACHE_FILE = Path(__file__).resolve().parents[1] / "data" / "elevation_cache.json"
PRECOMPUTE_PAUSE_S = 2.0  # between network lookups in precompute (public APIs rate-limit bursts)

_lock = Lock()
_cache = None  # loaded lazily from CACHE_FILE: {key: {"elevation_gain_m", "source", "points_sampled"}}


def _sample(coords, n=MAX_POINTS):
    if len(coords) <= n:
        return list(coords)
    step = (len(coords) - 1) / (n - 1)
    return [coords[round(i * step)] for i in range(n)]


def cache_key(osm_id, points):
    """'<osm_id>:<first 16 hex of sha256(sampled points)>' ('none:...' without an id)."""
    text = "|".join(f"{p[0]:.5f},{p[1]:.5f}" for p in points)
    return f"{osm_id if osm_id is not None else 'none'}:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"


def _load():
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _cache = {}
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable elevation cache %s: %s", CACHE_FILE, exc)
            _cache = {}
    return _cache


def _store(key, entry):
    """Add one result and rewrite the cache file atomically (sorted, so diffs stay small)."""
    with _lock:
        cache = _load()
        cache[key] = entry
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = CACHE_FILE.with_name(f"{CACHE_FILE.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")
            tmp.replace(CACHE_FILE)
        except OSError as exc:
            logger.warning("Could not save elevation cache: %s", exc)


def _open_meteo(points):
    data = get_json(OPEN_METEO[1], params={
        "latitude": ",".join(f"{p[0]:.5f}" for p in points),
        "longitude": ",".join(f"{p[1]:.5f}" for p in points),
    }, timeout=TIMEOUT)
    return data["elevation"]


def _open_elevation(points):
    body = json.dumps({"locations": [{"latitude": round(p[0], 5), "longitude": round(p[1], 5)} for p in points]})
    request = Request(OPEN_ELEVATION[1], data=body.encode("utf-8"), method="POST",
                      headers={"User-Agent": USER_AGENT, "Content-Type": "application/json", "Accept": "application/json"})
    with urlopen(request, timeout=TIMEOUT) as response:
        data = json.loads(response.read().decode("utf-8"))
    return [r["elevation"] for r in data["results"]]


PROVIDERS = [(OPEN_METEO[0], _open_meteo), (OPEN_ELEVATION[0], _open_elevation)]


def _describe(exc):
    if isinstance(exc, HTTPError):
        try:
            reason = json.loads(exc.read().decode("utf-8")).get("reason")
        except Exception:  # noqa: BLE001
            reason = None
        return f"HTTP {exc.code}" + (f" ({reason})" if reason else "")
    return f"{type(exc).__name__}: {exc}"


def get_elevation_gain(coords, osm_id=None):
    """Return {source, elevation_gain_m, points_sampled, checked_at} or an unavailable dict.

    elevation_gain_m is the sum of positive differences between consecutive sampled points (up to 100).
    Tries Open-Meteo, then Open-Elevation (no retries; a 429 moves straight to the fallback). Results are
    cached on disk forever; "source" names the provider that measured it.
    """
    try:
        points = _sample([[float(p[0]), float(p[1])] for p in coords])
        if len(points) < 2:
            return unavailable(SOURCE, "Need at least two coordinates")
        key = cache_key(osm_id, points)
        cached = _load().get(key)
        if cached:
            return {**cached, "checked_at": now_iso()}

        failures = []
        for name, fetch in PROVIDERS:
            try:
                heights = fetch(points)
            except Exception as exc:  # noqa: BLE001 - HTTP 429/5xx, timeouts, bad JSON
                failures.append(f"{name}: {_describe(exc)}")
                logger.warning("Elevation provider failed: %s", failures[-1])
                continue
            if len(heights) != len(points) or any(h is None for h in heights):
                failures.append(f"{name}: expected {len(points)} elevations, got {len(heights)}")
                continue
            gain = sum(max(0.0, b - a) for a, b in zip(heights, heights[1:]))
            entry = {"source": name, "elevation_gain_m": round(gain), "points_sampled": len(points)}
            _store(key, entry)
            return {**entry, "checked_at": now_iso()}
        return unavailable("Elevation (Open-Meteo, Open-Elevation)", "; ".join(failures))
    except Exception as exc:  # noqa: BLE001
        return unavailable(SOURCE, f"{type(exc).__name__}: {exc}")


def precompute(fixture=None, pause_s=PRECOMPUTE_PAUSE_S):
    """Compute elevation for every trail in the saved Ypsilanti snapshot (cached trails are skipped).
    Pauses between network lookups because the public providers rate-limit bursts. Returns (done, failed)."""
    from backend.tools.trails import YPSILANTI_FIXTURE, _parse_trails
    trails = _parse_trails(json.loads(Path(fixture or YPSILANTI_FIXTURE).read_text(encoding="utf-8")))
    done = failed = 0
    for trail in trails:
        points = _sample([[float(p[0]), float(p[1])] for p in trail["coords"]])
        needs_network = cache_key(trail["osm_id"], points) not in _load()
        result = get_elevation_gain(trail["coords"], trail["osm_id"])
        if needs_network and pause_s:
            time.sleep(pause_s)
        if result.get("available") is False:
            failed += 1
            print(f"  FAILED  {trail['name']} ({trail['osm_id']}): {result['error']}")
        else:
            done += 1
            print(f"  {result['elevation_gain_m']:>5} m  {result['source']:<22} {trail['name']} ({trail['osm_id']})")
    print(f"{done} of {len(trails)} trails cached in {CACHE_FILE}" + (f"; {failed} failed, run again later" if failed else ""))
    return done, failed


if __name__ == "__main__":
    import sys
    if sys.argv[1:] != ["precompute"]:
        print("usage: python -m backend.tools.elevation precompute")
        sys.exit(2)
    logging.basicConfig(level=logging.ERROR)
    sys.exit(1 if precompute()[1] else 0)
