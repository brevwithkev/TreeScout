"""AI trail briefing: one short Gemini call after the checker writes a 2-sentence plain-English forecast for the hike.

Every number in Gemini's text must appear in the plan data it was given; otherwise (or if Gemini is off, fails, times
out, or the request is past its Gemini budget) a template briefing built only from plan values is used. The briefing
never changes approval: it is text for the hiker.
"""

import json
import re

BRIEFING_TIMEOUT_S = 6
MAX_CHARS = 400
BRIEFING_PROMPT = """You are TrekChek's trail briefer. From the hike plan data (JSON), write exactly 2 short sentences in
plain English: what the hiker can expect on this hike (the route, the time it takes, the weather and daylight, and
the most important warning if there is one). Use only facts and numbers that appear in the data; do not convert
units, round, or add advice that the data does not support. Plain text only: no lists, no markdown, no JSON."""
# Parse complete quantities, not substrings. A range separator is distinct from
# the sign of either endpoint (45-50F, -15--5F and -15F to -5F all occur).
NUM = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+|,\d{1,2}(?!\d))?"
UNIT = r"(?:(?:°|degrees?)\s*)?(?:fahrenheit\b|celsius\b|[FC]\b)|kilomet(?:er|re)s?\b|km\b|minutes?\b|min\b|hours?\b|h\b|mph\b|mm\b|meters?\b|m\b|%"
QUANTITY = re.compile(rf"(?<![\w.])(?P<a>{NUM})\s*(?P<ua>{UNIT})?(?:\s*(?:to|through|[-–—])\s*(?P<b>{NUM})\s*(?P<ub>{UNIT})?)?", re.I)


def _unit(text):
    text = re.sub(r"degrees?", "", (text or "").lower()).replace("°", "").strip()
    for names, unit in [(("f", "fahrenheit"), "F"), (("c", "celsius"), "C"),
                        (("km", "kilometer", "kilometers", "kilometre", "kilometres"), "km"),
                        (("min", "minute", "minutes"), "min"), (("h", "hour", "hours"), "h"),
                        (("m", "meter", "meters"), "m")]:
        if text in names:
            return unit
    return text or None


def quantities(text):
    text = re.sub(r"([+−-])\s+(?=\d)", r"\1", str(text)).replace("−", "-")
    out = set()
    for match in QUANTITY.finditer(text):
        a, b = match['a'], match['b']
        ua, ub = _unit(match['ua']), _unit(match['ub'])
        before, after = text[max(0, match.start() - 45):match.start()], text[match.end():match.end() + 45]
        def context(unit):
            if unit in ("F", "C"):
                if re.match(r"\s*(?:colder|warmer|(?:above|below)\s+normal)\b", after, re.I):
                    return "difference_" + unit
                if re.search(r"\bnormal(?:\s+(?:high|low|temperature|of|is|about|:))*\s*$", before, re.I):
                    return "normal_" + unit
            return unit
        ua, ub = context(ua or ub), context(ub or ua)
        def value(token):
            grouped = re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", token)
            return float(token.replace(',', '') if grouped else token.replace(',', '.'))
        out.add((value(a), ua or ub))
        if b is not None:
            out.add((value(b), ub or ua))
    return out


def critical_warnings(plan, warnings):
    """Keep all warnings and rejection reasons verbatim, in stable order."""
    checker = plan.get("checker") or {}
    values = list(warnings or []) + list(plan.get("warnings") or [])
    for key in ("problems", "reasons"):
        reasons = checker.get(key) or []
        values.extend([reasons] if isinstance(reasons, str) else reasons)
    return list(dict.fromkeys(w for w in values if isinstance(w, str) and w.strip()))


def with_warnings(text, plan, warnings):
    # MAX_CHARS limits only model prose. Never truncate safety information.
    critical = critical_warnings(plan, warnings)
    window = plan.get("hike_window")
    if (not isinstance(window, dict) or window.get("available") is False) and "full hike was not checked" not in text:
        text = "Hike-window forecast unavailable; the full hike was not checked. " + text
    return ("Warnings: " + " | ".join(critical) + "\n\n" if critical else "") + text


def briefing_data(plan, warnings):
    """The plan values the briefing may use (also what its numbers are checked against)."""
    trail = plan.get("trail") or {}
    timing = plan.get("timing") if isinstance(plan.get("timing"), dict) and plan["timing"].get("available") is not False else {}
    def ok(key):
        v = plan.get(key)
        return v if isinstance(v, dict) and v.get("available") is not False else {}
    alerts = [a.get("event") for a in ok("alerts").get("items") or []]
    return {
        "trail": {"name": trail.get("name"), "route": trail.get("route"), "distance_km": trail.get("distance_km"),
                  "climb_m": trail.get("elevation_gain_m")},
        "timing": {k: timing.get(k) for k in ("start", "est_duration_min", "sunset", "turnaround_by")},
        "weather": ok("weather").get("summary"),
        "during_the_hike": ok("hike_window").get("summary"),
        "alerts": alerts,
        "conditions": [c.get("title") for c in plan.get("conditions") or [] if isinstance(c, dict)],
        "climate": ok("climate_anomaly").get("summary"),
        "warnings": critical_warnings(plan, warnings),
        "temperatures": {k: ok("hike_window").get(k) for k in ("min_temp_f", "max_temp_f")},
        "temperature_f": ok("weather").get("temperature_f"),
        "forecast_coverage": ("Hike-window forecast unavailable; the full hike was not checked."
                              if not ok("hike_window") else None),
    }


def _allowed(data):
    out = set()
    def visit(value, key=""):
        if isinstance(value, dict):
            for k, v in value.items():
                visit(v, k)
        elif isinstance(value, list):
            for v in value:
                visit(v, key)
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
            out.update(quantities(value))
            unit = ("F" if key.endswith("_f") else "C" if key.endswith("_c") else
                    "km" if key.endswith("_km") else "m" if key.endswith("_m") else
                    "min" if key.endswith("_min") else None)
            if unit and isinstance(value, (int, float)):
                if key.startswith("temp_diff_"):
                    unit = "difference_" + unit
                elif key.startswith("avg_") and unit in ("F", "C"):
                    unit = "normal_" + unit
                out.add((float(value), unit))
    visit(data)
    return out


def validate(text, data):
    """(ok, reason): 1-2 plain sentences whose every number appears in data."""
    if not isinstance(text, str):
        return False, "reply must be text"
    text = " ".join(text.split())
    if not text:
        return False, "empty reply"
    if len(text) > MAX_CHARS or any(ch in text for ch in "{}[]*#`"):
        return False, "not 2 plain sentences"
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
    if not 1 <= len(sentences) <= 2 or not text.endswith((".", "!")):
        return False, f"{len(sentences)} sentences"
    allowed = _allowed(data)
    unknown = quantities(text) - allowed
    if unknown:
        return False, "signed quantities or units not in the plan"
    return True, None


def template(plan, warnings):
    """Critical warnings first, followed by deterministic prose from plan values."""
    trail = plan.get("trail") or {}
    data = briefing_data(plan, warnings)
    timing, climb = data["timing"], trail.get("elevation_gain_m")
    shape = "out-and-back" if trail.get("route") == "out-and-back" else "loop" if trail.get("route") == "loop" else "route"
    if not trail or trail.get("available") is False:
        return with_warnings("No mapped trail is available. Generate a new plan before hiking.", plan, warnings)
    first = f"{trail.get('name') or 'This trail'} is a {float(trail.get('distance_km') or 0):.1f} km {shape}"
    if timing.get("est_duration_min"):
        first += f" taking about {timing['est_duration_min']} min"
    if climb is not None:
        first += f" with {climb} m of climbing"
    first += f", and sunset is at {timing['sunset']}." if timing.get("sunset") else "."
    weather = data["during_the_hike"] or (f"The forecast is {data['weather']}" if data["weather"] else None)
    second = f"{weather.rstrip('.')}." if weather else "No forecast is available for this hike."
    if data["alerts"]:
        second = second[:-1] + f"; {data['alerts'][0]} in effect."
    if data["forecast_coverage"]:
        second = data["forecast_coverage"] + (f" Daily forecast: {data['weather']}." if data["weather"] else "")
    return with_warnings(f"{first} {second}", plan, warnings)
