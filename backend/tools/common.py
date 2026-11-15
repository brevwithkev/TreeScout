"""Shared helpers for TrekChek data tools: HTTP JSON, timestamps, and failure results."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo
import zoneinfo
from tempfile import NamedTemporaryFile
import hashlib
import json
import logging
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

USER_AGENT = "TrekChek hackathon (contact: trekchek-team)"
logger = logging.getLogger("treescout.tools")


DEFAULT_TZ = "America/Detroit"
# Time zones come from the tzdata package (backend/requirements.txt), never the OS zone files, so every machine
# computes the same local times (Windows has none; Linux/macOS copies vary by age).
zoneinfo.reset_tzpath(to=())
_tz_finder = None
_tz_lock = Lock()


@lru_cache(maxsize=4096)
def _tz_name(lat, lon):
    global _tz_finder
    with _tz_lock:  # TimezoneFinder loads its data once; not documented as thread-safe
        if _tz_finder is None:
            from timezonefinder import TimezoneFinder
            _tz_finder = TimezoneFinder()
        name = _tz_finder.timezone_at(lat=lat, lng=lon)
    if not name:  # open ocean in old data: the nautical zone for the longitude
        offset = -round(lon / 15)
        name = "Etc/GMT" if offset == 0 else f"Etc/GMT{offset:+d}"
    return name


def timezone_for(lat, lon):
    """ZoneInfo for a point (timezonefinder); America/Detroit if the lookup fails. Never raises."""
    try:
        return ZoneInfo(_tz_name(round(float(lat), 4), round(float(lon), 4)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("No time zone for %s, %s (%s); using %s", lat, lon, exc, DEFAULT_TZ)
        return ZoneInfo(DEFAULT_TZ)


def now_iso():
    """Current UTC time as ISO 8601 with a Z suffix, e.g. 2026-11-14T20:14:00Z."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def unavailable(source, error):
    """The standard result for a failed data source. Never raise from a tool; return this."""
    logger.warning("%s unavailable: %s", source, error)
    return {"available": False, "source": source, "error": str(error), "checked_at": now_iso()}


def get_json(url, params=None, headers=None, timeout=10):
    """GET a URL and decode JSON. Raises on HTTP/network/decode errors (callers catch)."""
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})})
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body) if body.strip() else None


def get_json_within(seconds, url, params=None, headers=None):
    """get_json with a hard limit on the whole call (connect + server time + download).

    urlopen's timeout applies to each socket operation, so a slow server can take longer in total.
    Here we stop waiting after `seconds` and raise TimeoutError; the abandoned request finishes (or
    hits its own socket timeout) in the background and is ignored.
    """
    future = _DEADLINE_POOL.submit(get_json, url, params, headers, seconds)
    try:
        return future.result(timeout=seconds)
    except FuturesTimeout:
        raise TimeoutError(f"no answer within {seconds:g} s") from None


_DEADLINE_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool-http")

RETRY_DELAY_S = 1.0          # wait before the single retry of a transient failure
FIRST_TIMEOUT_S = 6.0        # retried sources: the first attempt gets 6 s ...
RETRY_TIMEOUT_S = 3.0        # ... the retry 3 s, so about 6 + 1 + 3 = 10 s worst case per source
STALE_MAX_S = 6 * 3600       # a failed live check may fall back to a cached success up to this old


def transient(exc):
    """True for errors worth one retry: HTTP 5xx and timeouts."""
    if isinstance(exc, HTTPError):
        return exc.code >= 500
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    return isinstance(exc, URLError) and isinstance(exc.reason, (TimeoutError, socket.timeout))


def retry_once(fn, label):
    """fn(timeout_s) with FIRST_TIMEOUT_S; on a transient error (5xx, timeout) wait RETRY_DELAY_S and call
    fn(RETRY_TIMEOUT_S) once more. Other errors raise. fn must keep the whole attempt within timeout_s."""
    try:
        return fn(FIRST_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - re-raised unless transient
        if not transient(exc):
            raise
        logger.warning("%s: %s: %s; retrying once in %g s", label, type(exc).__name__, exc, RETRY_DELAY_S)
        time.sleep(RETRY_DELAY_S)
        return fn(RETRY_TIMEOUT_S)


# ---------------------------------------------------------------------------------------------
# Small JSON disk cache for slow data sources (NASA POWER, NASA EONET)
# ---------------------------------------------------------------------------------------------

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "tool_cache"  # data/ is gitignored


def _cache_path(kind, key):
    digest = hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:32]
    return CACHE_DIR / kind / f"{digest}.json"


def cache_get(kind, key, max_age_s):
    """A cached result for key if it was saved less than max_age_s ago, else None. Never raises."""
    path = _cache_path(kind, key)
    try:
        if time.time() - path.stat().st_mtime >= max_age_s:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cache_put(kind, key, result):
    """Save a successful result (atomically). Cache problems are logged, never raised."""
    path = _cache_path(kind, key)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(result, handle)
        temporary.replace(path)
    except OSError as exc:
        logger.warning("Could not cache %s result: %s", kind, exc)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def parse_date(value):
    """Accept a date, datetime, or 'YYYY-MM-DD' string; return a datetime.date."""
    if hasattr(value, "year") and hasattr(value, "day"):
        return value if not isinstance(value, datetime) else value.date()
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
