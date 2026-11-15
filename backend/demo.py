"""Demo mode: record /plan responses (and their voice lines) and replay them.

DEMO_RECORD=1 (default 0): every successful /plan response is saved to demo_data/plans/<slug>.json with
"demo_recorded_at" added (the live response is not changed), and its voice lines (narration, the
"Turn back now." cue and the plan's turn-back sentence) are saved to demo_data/tts/<sha256(text)>.mp3.
DEMO_MODE=1: /plan returns a recording whose slug matches the request text instantly, and /tts serves
demo_data/tts/ (then the local TTS cache) only. demo_data/ is committed so demos work on any machine.

Backfill audio for existing recordings:  python -m backend.demo audio
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from tempfile import NamedTemporaryFile
import json
import logging
import os
import re

logger = logging.getLogger("treescout.demo")

ROOT = Path(__file__).resolve().parent.parent
PLANS_DIR = ROOT / "demo_data" / "plans"
TTS_DIR = ROOT / "demo_data" / "tts"
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SLUG = 120


def _flag(name):
    return os.environ.get(name, "0").strip().lower() in ("1", "true", "yes", "on")


def demo_mode():
    """DEMO_MODE=1 (also true/yes/on) replays recordings; default off. Read per request."""
    return _flag("DEMO_MODE")


def demo_record():
    """DEMO_RECORD=1 (also true/yes/on) writes recordings to demo_data/; default off. Read per request."""
    return _flag("DEMO_RECORD")


def slugify(text):
    """'Easy 3 mile walk, near Ypsilanti!' -> 'easy-3-mile-walk-near-ypsilanti' (lowercase, punctuation
    removed, whitespace runs as single dashes)."""
    text = re.sub(r"[^\w\s]", "", str(text).lower().replace("_", " "))
    return re.sub(r"\s+", "-", text.strip())[:MAX_SLUG].strip("-")


def _path(slug):
    return PLANS_DIR / f"{slug}.json" if slug and SLUG_RE.match(slug) else None


def save_recording(plan):
    """Save a copy of a /plan response with demo_recorded_at, plus its voice lines. Never raises."""
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
    except OSError as exc:
        logger.warning("Could not save demo recording %s: %s", path.name, exc)
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        return None
    save_plan_audio(plan)
    return path


# ---------------------------------------------------------------------------------------------
# Demo voice audio: demo_data/tts/<sha256(text)>.mp3, keyed by text only so it plays on any laptop
# whatever ELEVENLABS_VOICE_ID / ELEVENLABS_MODEL are set to there.
# ---------------------------------------------------------------------------------------------

def audio_path(text):
    return TTS_DIR / f"{hashlib.sha256(str(text).strip().encode('utf-8')).hexdigest()}.mp3"


def demo_audio(text):
    """Saved demo audio bytes for text, or None."""
    path = audio_path(text)
    try:
        return path.read_bytes() if path.stat().st_size > 0 else None
    except OSError:
        return None


def plan_voice_lines(plan):
    """Every line the frontend may speak for this plan: narration, the turn-back cue, the turn-back text."""
    from backend.narration import TURN_BACK_CUE, turn_back_text  # local import: narration has no demo deps
    lines = [str(n["text"]).strip() for n in plan.get("narration") or [] if isinstance(n, dict) and n.get("text")]
    timing = plan.get("timing") if isinstance(plan.get("timing"), dict) else {}
    if lines:
        lines.append(TURN_BACK_CUE)
        if timing.get("sunset") and timing.get("available") is not False:
            lines.append(turn_back_text(timing["sunset"]))
    return list(dict.fromkeys(line for line in lines if line))


def save_plan_audio(plan, synthesize=None):
    """Save audio for the plan's voice lines that aren't saved yet (in parallel). Never raises.
    Returns {"saved": n, "already": n, "failed": n}."""
    if synthesize is None:
        from backend.narration import synthesize_tts as synthesize
    todo = [line for line in plan_voice_lines(plan) if demo_audio(line) is None]
    counts = {"saved": 0, "already": len(plan_voice_lines(plan)) - len(todo), "failed": 0}

    def save(line):
        try:
            audio = synthesize(line)
            path = audio_path(line)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(audio)
            tmp.replace(path)
            return True
        except Exception as exc:  # noqa: BLE001 - missing key, ElevenLabs error, disk error
            logger.warning("No demo audio for %r: %s", line[:60], exc)
            return False

    if todo:
        with ThreadPoolExecutor(max_workers=6, thread_name_prefix="demo-tts") as pool:
            for ok in pool.map(save, todo):
                counts["saved" if ok else "failed"] += 1
    logger.info("Demo audio for %r: %s", plan.get("request"), counts)
    return counts


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


def _main(argv):
    """python -m backend.demo audio  ->  save voice audio for every recording in demo_data/plans/."""
    if argv[1:] != ["audio"]:
        print("usage: python -m backend.demo audio")
        return 2
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    failed = 0
    for item in list_recordings():
        counts = save_plan_audio(load_recording(item["slug"]))
        failed += counts["failed"]
        print(f"{item['slug']}: {counts['saved']} saved, {counts['already']} already there, {counts['failed']} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv))
