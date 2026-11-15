"""Shared helpers for TreeScout data tools: HTTP JSON, timestamps, and failure results."""

from datetime import datetime, timezone
import json
import logging
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


def parse_date(value):
    """Accept a date, datetime, or 'YYYY-MM-DD' string; return a datetime.date."""
    if hasattr(value, "year") and hasattr(value, "day"):
        return value if not isinstance(value, datetime) else value.date()
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
