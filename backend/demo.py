"""Demo mode: record successful /plan responses and replay them.

Every successful /plan response is saved to demo_data/plans/<slug>.json with "demo_recorded_at" added
(the live response is not changed). With DEMO_MODE=1, /plan returns a recording whose slug matches the
request text instantly, and /tts serves cached audio only. demo_data/ is committed so demos work on any
machine.
"""

from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
import json
import logging
import os
import re

logger = logging.getLogger("treescout.demo")

ROOT = Path(__file__).resolve().parent.parent
PLANS_DIR = ROOT / "demo_data" / "plans"
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SLUG = 120


def demo_mode():
    """DEMO_MODE=1 (also true/yes/on) turns demo mode on; default off. Read per request."""
    return os.environ.get("DEMO_MODE", "0").strip().lower() in ("1", "true", "yes", "on")


def slugify(text):
    """'Easy 3 mile walk, near Ypsilanti!' -> 'easy-3-mile-walk-near-ypsilanti' (lowercase, punctuation
    removed, whitespace runs as single dashes)."""
    text = re.sub(r"[^\w\s]", "", str(text).lower().replace("_", " "))
    return re.sub(r"\s+", "-", text.strip())[:MAX_SLUG].strip("-")


def _path(slug):
    return PLANS_DIR / f"{slug}.json" if slug and SLUG_RE.match(slug) else None


def save_recording(plan):
    """Save a copy of a /plan response with demo_recorded_at. Never raises (recording is best-effort)."""
    path = _path(slugify(plan.get("request") or ""))
    if path is None:
        return None
    record = {**plan, "demo_recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()
              .replace("+00:00", "Z")}
    temporary = None
    try:
        PLANS_DIR.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=PLANS_DIR, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(record, handle, ensure_ascii=False, indent=1)
        temporary.replace(path)
        logger.info("Recorded demo plan %s", path.name)
        return path
    except OSError as exc:
        logger.warning("Could not save demo recording %s: %s", path.name, exc)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        return None


def load_recording(slug):
    """The saved plan for a slug, or None."""
    path = _path(slug)
    if path is None or not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Unreadable demo recording %s: %s", path.name, exc)
        return None


def list_recordings():
    """[{slug, request, recorded_at}] for every saved plan, newest first."""
    items = []
    for path in PLANS_DIR.glob("*.json") if PLANS_DIR.is_dir() else []:
        plan = load_recording(path.stem)
        if isinstance(plan, dict):
            items.append({"slug": path.stem, "request": plan.get("request"),
                          "recorded_at": plan.get("demo_recorded_at")})
    items.sort(key=lambda item: item["recorded_at"] or "", reverse=True)
    return items
