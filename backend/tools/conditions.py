"""Trail conditions derived from other tool results (no API): low light near sunset, freezing temperatures."""

from datetime import datetime, timedelta

from backend.tools.timing import SAFETY_MARGIN

# "Low light" uses the same 30 min buffer as the start-time move (timing.latest_safe_start = sunset - duration - 30),
# so a plan whose start the planner just moved to the latest safe start is not flagged.
LOW_LIGHT_MIN = int(SAFETY_MARGIN.total_seconds() // 60)
FREEZING_F = 32


def _available(result):
    return isinstance(result, dict) and result.get("available") is not False


def derive_conditions(timing, hike_window):
    """[{title, advice, source}] from the timing and the NWS hourly hike window (either may be unavailable)."""
    out = []
    if _available(timing) and timing.get("start") and timing.get("sunset") and timing.get("est_duration_min"):
        start = datetime.strptime(timing["start"], "%H:%M")
        finish = start + timedelta(minutes=int(timing["est_duration_min"]))
        margin = (datetime.strptime(timing["sunset"], "%H:%M") - finish).total_seconds() / 60
        safe = timing.get("latest_safe_start")
        # Same test as the timing check (start after the latest safe start); the margin is only a fallback, because
        # HH:MM rounding can make a start exactly at latest_safe_start look like 29 min before sunset.
        late = timing["start"] > safe if safe else margin < LOW_LIGHT_MIN
        if late:
            out.append({"title": "Low light",
                        "advice": f"You finish around {finish:%H:%M}, within {LOW_LIGHT_MIN} min of sunset "
                                  f"({timing['sunset']}); bring a headlamp",
                        "source": timing.get("source") or "Computed (astral sunset)"})
    if _available(hike_window) and hike_window.get("min_temp_f") is not None and hike_window["min_temp_f"] <= FREEZING_F:
        out.append({"title": "Freezing temperatures",
                    "advice": f"Down to {hike_window['min_temp_f']}F during your hike; watch for icy boardwalks",
                    "source": hike_window.get("source") or "NWS hourly"})
    return out
