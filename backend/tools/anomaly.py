"""Climate anomaly: the NWS hourly forecast for the hike window compared with NASA POWER 20-year normals (no API)."""

from backend.tools.common import now_iso, unavailable

SOURCE_PREFIX = "NWS vs NASA POWER"
NOTABLE_TEMP_F = 8        # |forecast - normal| >= 8F is notable
RAIN_CHANCE = 50          # rain flag: hike-window chance >= 50% ...
DRY_WEEK_MM = 2.0         # ... in a week that normally gets under 2 mm/day
# Which normal matches the time of day (by the middle of the hike window):
DAY_HOURS = (10, 18)      # 10:00-17:59: forecast high vs normal high
NIGHT_HOURS = (21, 6)     # 21:00-05:59: forecast low vs normal low; otherwise (morning/evening) the midpoints


def _failed(result):
    return not isinstance(result, dict) or result.get("available") is False


def _f(c):
    return c * 9 / 5 + 32


def _mid_hour(window):
    """Middle of "HH:MM-HH:MM" in hours (None if it can't be read)."""
    try:
        a, b = (int(t[:2]) * 60 + int(t[3:5]) for t in str(window).split("-"))
        return ((a + b) / 2 if b >= a else ((a + b + 1440) / 2) % 1440) / 60
    except (TypeError, ValueError):
        return None


def compare_climate(hike_window, climate):
    """{source, temp_diff_f, rain_flag, notable, summary, basis, checked_at} or {available: false, source, error}.

    temp_diff_f = forecast minus normal (whole F) for the same part of the day; rain_flag when the hike window has a
    >= 50% rain chance in a week that normally gets under 2 mm/day; notable when |temp_diff_f| >= 8 or rain_flag.
    """
    years = None if _failed(climate) else climate.get("years")
    source = f"{SOURCE_PREFIX} {years}" if years else SOURCE_PREFIX
    if _failed(hike_window):
        why = (hike_window or {}).get("error") if isinstance(hike_window, dict) else None
        return unavailable(source, f"NWS hourly forecast unavailable{f' ({why})' if why else ''}")
    if _failed(climate):
        why = (climate or {}).get("error") if isinstance(climate, dict) else None
        return unavailable(source, f"NASA POWER normals unavailable{f' ({why})' if why else ''}")
    try:
        hi_c, lo_c = climate.get("avg_high_c"), climate.get("avg_low_c")
        hi_f, lo_f = hike_window.get("max_temp_f"), hike_window.get("min_temp_f")
        if None in (hi_c, lo_c, hi_f, lo_f):
            return unavailable(source, "Missing temperatures in the NWS forecast or NASA POWER normals")
        hour = _mid_hour(hike_window.get("window"))
        if hour is not None and DAY_HOURS[0] <= hour < DAY_HOURS[1]:
            forecast, normal, what = hi_f, _f(hi_c), "high"
        elif hour is not None and (hour >= NIGHT_HOURS[0] or hour < NIGHT_HOURS[1]):
            forecast, normal, what = lo_f, _f(lo_c), "low"
        else:
            forecast, normal, what = (hi_f + lo_f) / 2, _f((hi_c + lo_c) / 2), "average"
        diff = int(round(forecast - normal))
        chance, precip = hike_window.get("max_precip_chance"), climate.get("avg_precip_mm")
        rain_flag = bool(chance is not None and precip is not None and chance >= RAIN_CHANCE and precip < DRY_WEEK_MM)

        bits = []
        if abs(diff) >= NOTABLE_TEMP_F:
            bits.append(f"About {abs(diff)}F {'colder' if diff < 0 else 'warmer'} than normal for this week")
        if rain_flag:
            bits.append(f"{chance}% chance of rain in a week that is usually dry ({precip} mm/day)")
        notable = bool(bits)
        return {
            "source": source,
            "temp_diff_f": diff,
            "rain_flag": rain_flag,
            "notable": notable,
            "summary": "; ".join(bits) if notable else "Typical for this week",
            # not in the contract: what was compared, for the trail-conditions advice
            "basis": f"forecast {what} {round(forecast)}F vs normal {round(normal)}F",
            "checked_at": now_iso(),
        }
    except Exception as exc:  # noqa: BLE001
        return unavailable(source, f"{type(exc).__name__}: {exc}")


def anomaly_condition(anomaly):
    """The trail-conditions item for a notable anomaly, else None."""
    if _failed(anomaly) or not anomaly.get("notable"):
        return None
    return {"title": "Unusual conditions", "advice": f"{anomaly['summary']} ({anomaly['basis']}); dress for it",
            "source": anomaly["source"]}
