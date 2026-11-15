"""Elevation along a trail: NASA SRTM via OpenTopoData first, then Open-Meteo, then Open-Elevation, with a
permanent disk cache.

Terrain doesn't change, so every successful result is kept forever in backend/data/elevation_cache.json
(committed), keyed by the trail's OSM id plus a hash of the sampled points. A cache hit never touches the
network, which also protects the providers' daily quotas. New results keep the sampled profile
([[km along the trail, m], ...]) so an out-and-back route can count the climb back up (backend/tools/route.py).

Fill the cache for every trail in the saved Ypsilanti snapshot, or in the seeded snapshots:
    python -m backend.tools.elevation precompute
    python -m backend.tools.elevation precompute seeds
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
from backend.tools.profile import stats
from backend.tools.trails import haversine

logger = logging.getLogger("treescout.elevation")

SRTM = ("NASA SRTM via OpenTopoData", "https://api.opentopodata.org/v1/srtm30m")
SOURCE = SRTM[0]                            # the primary provider
SRTM_MIN_INTERVAL_S = 1.0                   # OpenTopoData public API: max 1 request/second, 100 points/request
OPEN_METEO = ("Open-Meteo Elevation", "https://api.open-meteo.com/v1/elevation")
OPEN_ELEVATION = ("Open-Elevation", "https://api.open-elevation.com/api/v1/lookup")
TIMEOUT = 10
MAX_POINTS = 100  # OpenTopoData and Open-Meteo accept up to 100 coordinates per request
CACHE_FILE = Path(__file__).resolve().parents[1] / "data" / "elevation_cache.json"
PRECOMPUTE_PAUSE_S = 2.0  # between network lookups in precompute (public APIs rate-limit bursts)

_lock = Lock()
_srtm_lock = Lock()
_srtm_last = [0.0]
_cache = None  # loaded lazily from CACHE_FILE: {key: {"elevation_gain_m", "source", "points_sampled"}}


def _sample_indices(count, n=MAX_POINTS):
    if count <= n:
        return list(range(count))
    step = (count - 1) / (n - 1)
    return [round(i * step) for i in range(n)]


def _sample(coords, n=MAX_POINTS):
    return [coords[i] for i in _sample_indices(len(coords), n)]


def _sample_km(coords, n=MAX_POINTS):
    """Distance along the full trail (km) at each sampled point."""
    cum = [0.0]
    for a, b in zip(coords, coords[1:]):
        cum.append(cum[-1] + haversine(a, b))
    return [cum[i] for i in _sample_indices(len(coords), n)]


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


def _srtm(points):
    """NASA SRTM 30 m heights from OpenTopoData (our User-Agent; at most 1 request per second)."""
    with _srtm_lock:
        wait = _srtm_last[0] + SRTM_MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            data = get_json(SRTM[1], params={"locations": "|".join(f"{p[0]:.5f},{p[1]:.5f}" for p in points)},
                            timeout=TIMEOUT)
        finally:
            _srtm_last[0] = time.monotonic()
    if data.get("status") != "OK":
        raise ValueError(f"status {data.get('status')}: {data.get('error')}")
    return [r.get("elevation") for r in data["results"]]


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


PROVIDERS = [(SRTM[0], _srtm), (OPEN_METEO[0], _open_meteo), (OPEN_ELEVATION[0], _open_elevation)]


def _describe(exc):
    if isinstance(exc, HTTPError):
        try:
            reason = json.loads(exc.read().decode("utf-8")).get("reason")
        except Exception:  # noqa: BLE001
            reason = None
        return f"HTTP {exc.code}" + (f" ({reason})" if reason else "")
    return f"{type(exc).__name__}: {exc}"


def cached_entry(coords, osm_id=None):
    """The cached result for a trail, or None (never touches the network)."""
    try:
        return _load().get(cache_key(osm_id, _sample([[float(p[0]), float(p[1])] for p in coords])))
    except (TypeError, ValueError, IndexError):
        return None


def get_elevation_gain(coords, osm_id=None):
    """Return {source, elevation_gain_m, elevation_loss_m, min_m, max_m, profile, points_sampled, checked_at} or
    an unavailable dict (entries cached before profiles existed have only source/elevation_gain_m/points_sampled).

    elevation_gain_m / elevation_loss_m are the sums of the rises / drops between consecutive sampled points (up to
    100); profile is [[km along the trail, m], ...]. Tries NASA SRTM via OpenTopoData, then Open-Meteo, then
    Open-Elevation (no retries; any failure moves to the next). Cached on disk forever; "source" names the provider.
    """
    try:
        full = [[float(p[0]), float(p[1])] for p in coords]
        points = _sample(full)
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
            profile = [[round(k, 3), round(h, 1)] for k, h in zip(_sample_km(full), heights)]
            # Smoothed totals (backend/tools/profile.py): 50 m resampling, 3-point average, 10 m hysteresis.
            entry = {"source": name, **stats(profile), "points_sampled": len(points), "profile": profile}
            _store(key, entry)
            return {**entry, "checked_at": now_iso()}
        return unavailable("Elevation (NASA SRTM via OpenTopoData, Open-Meteo, Open-Elevation)", "; ".join(failures))
    except Exception as exc:  # noqa: BLE001
        return unavailable(SOURCE, f"{type(exc).__name__}: {exc}")


def recompute_from_profiles():
    """Recompute gain/loss/min/max of every cached entry that has a stored profile (no network). Returns
    (recomputed, without_profile)."""
    with _lock:
        cache = _load()
        done = skipped = 0
        for key, entry in cache.items():
            if entry.get("profile"):
                entry.update(stats(entry["profile"]))
                done += 1
            else:
                skipped += 1
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_name(f"{CACHE_FILE.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(CACHE_FILE)
    return done, skipped


def precompute(fixture=None, pause_s=PRECOMPUTE_PAUSE_S, seeds=False):
    """Compute elevation for every trail in the saved Ypsilanti snapshot, or (seeds=True) in every seeded snapshot
    (backend/data/trail_snapshots/); cached trails are skipped. Pauses between network lookups because the public
    providers rate-limit bursts. Returns (done, failed)."""
    from backend.tools.trails import SEED_DIR, YPSILANTI_FIXTURE, _parse_anywhere, _parse_trails
    if seeds:
        trails = [t for path in sorted(SEED_DIR.glob("*.json"))
                  for t in _parse_anywhere(json.loads(path.read_text(encoding="utf-8"))["payload"])]
    else:
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
    if sys.argv[1:] == ["recompute"]:
        print("recomputed %d entries from their stored profiles; %d have no profile" % recompute_from_profiles())
        sys.exit(0)
    if sys.argv[1:] not in (["precompute"], ["precompute", "seeds"]):
        print("usage: python -m backend.tools.elevation precompute [seeds] | recompute")
        sys.exit(2)
    logging.basicConfig(level=logging.ERROR)
    sys.exit(1 if precompute(seeds=sys.argv[2:] == ["seeds"])[1] else 0)
