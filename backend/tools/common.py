"""Shared helpers for TreeScout data tools: HTTP JSON, timestamps, and failure results."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
import hashlib
import json
import logging
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

USER_AGENT = "TreeScout hackathon (contact: treescout-team)"
logger = logging.getLogger("treescout.tools")


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
