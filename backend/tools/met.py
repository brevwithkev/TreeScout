"""Forecasts outside NWS coverage from MET Norway Locationforecast 2.0 compact
(https://api.met.no/weatherapi/locationforecast/2.0/documentation): the daytime summary for a date and the hike window.

MET's terms: an identifying User-Agent, cached answers (we keep 30 min per ~1 km point), at most 4 decimals in
lat/lon. The compact format has precipitation amounts but no probability of precipitation, so
precip_chance / max_precip_chance are None (unknown) and the amount in mm is reported instead.
"""

from datetime import datetime, timedelta
from backend.tools.coverage import covers, utc

from backend.tools.common import (USER_AGENT, cache_get, cache_put, get_json_within, now_iso, parse_date,
                                  timezone_for, unavailable)

SOURCE = "MET Norway"
URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
TIMEOUT = 8
CACHE_SECONDS = 30 * 60
SYMBOLS = {  # MET symbol codes (without _day/_night/_polartwilight) -> short words like NWS shortForecast
    "clearsky": "Clear", "fair": "Mostly clear", "partlycloudy": "Partly cloudy", "cloudy": "Cloudy", "fog": "Fog",
    "lightrain": "Light rain", "rain": "Rain", "heavyrain": "Heavy rain", "lightrainshowers": "Light rain showers",
    "rainshowers": "Rain showers", "heavyrainshowers": "Heavy rain showers", "lightsleet": "Light sleet",
    "sleet": "Sleet", "heavysleet": "Heavy sleet", "lightsnow": "Light snow", "snow": "Snow",
    "heavysnow": "Heavy snow", "lightsnowshowers": "Light snow showers", "snowshowers": "Snow showers",
    "heavysnowshowers": "Heavy snow showers", "lightsleetshowers": "Light sleet showers",
    "sleetshowers": "Sleet showers", "heavysleetshowers": "Heavy sleet showers",
}


def words(symbol):
    """"partlycloudy_day" -> "Partly cloudy"; any thunder symbol -> "... and thunder" (the checker looks for it)."""
    code = str(symbol or "").split("_")[0]
    thunder = code.endswith("andthunder")
    base = code[: -len("andthunder")] if thunder else code
    text = SYMBOLS.get(base, base.replace("showers", " showers").capitalize() or "Unknown")
    return f"{text} and thunder" if thunder else text


def _f(c):
    return round(c * 9 / 5 + 32)


def _mph(ms):
    return round(ms * 2.23694)


def timeseries(lat, lon):
    """[(start UTC, hours, entry)] from MET, cached 30 min per point (~1 km). Raises on failure."""
    lat, lon = round(float(lat), 4), round(float(lon), 4)
    key = {"lat": round(lat, 2), "lon": round(lon, 2)}
    data = cache_get("met_norway", key, CACHE_SECONDS)
    if data is None:
        data = get_json_within(TIMEOUT, URL, params={"lat": f"{lat:.4f}", "lon": f"{lon:.4f}"},
                               headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        data = {"timeseries": data["properties"]["timeseries"], "checked_at": now_iso()}
        cache_put("met_norway", key, data)
    out = []
    for step in data["timeseries"]:
        start = utc(datetime.fromisoformat(step["time"].replace("Z", "+00:00")))
        d = step.get("data") or {}
        # MET instant is a sample at a point, not an hour of forecast. Only an
        # explicit next_X_hours object defines an interval; the final point has none.
        hours = next((h for h in (1, 6, 12) if f"next_{h}_hours" in d), 0)
        span = f"next_{hours}_hours" if hours else None
        out.append((start, hours, {
            "temp_c": (d.get("instant") or {}).get("details", {}).get("air_temperature"),
            "wind_ms": (d.get("instant") or {}).get("details", {}).get("wind_speed"),
            "symbol": ((d.get(span) or {}).get("summary") or {}).get("symbol_code") if span else None,
            "precip_mm": ((d.get(span) or {}).get("details") or {}).get("precipitation_amount") if span else None,
        }))
    return sorted(out, key=lambda step: step[0]), data.get("checked_at")


def _summarize(steps):
    temps = [s["temp_c"] for _, _, s in steps if s["temp_c"] is not None]
    winds = [s["wind_ms"] for _, _, s in steps if s["wind_ms"] is not None]
    conditions = list(dict.fromkeys(words(s["symbol"]) for _, _, s in steps if s["symbol"]))
    amounts = [s["precip_mm"] for _, _, s in steps]
    precip = round(float(sum(amounts)), 1) if all(v is not None for v in amounts) else None
    return temps, winds, conditions, precip


def _window(steps, begin, end):
    """Use MET's aggregate intervals, never infer coverage from point spacing."""
    window = sorted((s for s in steps if s[1] > 0 and s[0] < end and
                     s[0] + timedelta(hours=s[1]) > begin), key=lambda s: s[0])
    if not covers(begin, end, [(s[0], s[0] + timedelta(hours=s[1])) for s in window]):
        return None
    return window


def _exact_amount(window, begin, end, amount):
    # Aggregated rain cannot be divided into subhours or summed twice over
    # overlapping periods. Unknown is preferable to an invented hike total.
    cursor = begin
    for start, hours, _ in window:
        if start != cursor:
            return None
        cursor = start + timedelta(hours=hours)
    return amount if cursor == end else None


def _instant_values(steps, begin, end):
    """Temperature/wind are point samples, not values for the aggregate period."""
    samples = [s for start, _, s in steps if begin <= start < end]
    return ([s['temp_c'] for s in samples if s['temp_c'] is not None],
            [s['wind_ms'] for s in samples if s['wind_ms'] is not None])


def get_met_weather(lat, lon, date):
    """{source: "MET Norway", summary, precip_chance: None, precip_mm, temperature_f, period, checked_at}: the
    daytime (06:00-18:00 local) forecast for date, or an unavailable dict."""
    try:
        tz = timezone_for(lat, lon)
        day = parse_date(date)
        begin = utc(datetime.combine(day, datetime.min.time(), tz) + timedelta(hours=6))
        end = begin + timedelta(hours=12)
        steps, checked = timeseries(lat, lon)
        daytime = _window(steps, begin, end)
        if daytime is None:
            return unavailable(SOURCE, f"No complete MET Norway daytime forecast for {day}")
        _, _, conditions, precip = _summarize(daytime)
        temps, _winds = _instant_values(steps, begin, end)
        precip = _exact_amount(daytime, begin, end, precip)
        hi = _f(max(temps)) if temps else None
        summary = f"{conditions[0] if conditions else 'Forecast'}, {hi}F" if hi is not None else (conditions[0] if conditions else "Forecast")
        return {"source": SOURCE, "summary": summary, "precip_chance": None, "precip_mm": precip,
                "temperature_f": hi, "period": f"{day} daytime", "checked_at": now_iso()}
    except Exception as exc:  # noqa: BLE001 - a tool must never raise
        return unavailable(SOURCE, f"{type(exc).__name__}: {exc}")


def get_met_hike_window(lat, lon, date, start, est_duration_min):
    """The hike-window section (same keys as weather.get_hike_window) from MET Norway; max_precip_chance is None
    (compact has no probability) and max_precip_mm gives the amount. Unavailable dict on failure."""
    try:
        tz = timezone_for(lat, lon)
        begin = utc(datetime.combine(parse_date(date), datetime.strptime(str(start)[:5], "%H:%M").time(), tz))
        end = begin + timedelta(minutes=max(1, int(est_duration_min)))
        steps, _checked = timeseries(lat, lon)
        window = _window(steps, begin, end)
        if window is None:
            return unavailable(SOURCE, f"No complete MET Norway forecast for {date} {start}; the full hike was not checked")
        _, _, conditions, precip = _summarize(window)
        temps, winds = _instant_values(steps, begin, end)
        precip = _exact_amount(window, begin, end, precip)
        lo, hi = (_f(min(temps)), _f(max(temps))) if temps else (None, None)
        wind = _mph(max(winds)) if winds else None
        temp_text = "" if lo is None else (f", {lo}F" if lo == hi else f", {lo}-{hi}F")
        return {
            "source": SOURCE,
            "window": f"{begin.astimezone(tz):%H:%M}-{end.astimezone(tz):%H:%M}",
            "max_precip_chance": None,
            "max_precip_mm": precip,
            "min_temp_f": lo, "max_temp_f": hi, "max_wind_mph": wind,
            "conditions": conditions,
            "summary": (f"During your hike: {', '.join(conditions) or 'Forecast'}; "
                        + (f"{precip:g} mm precipitation" if precip is not None else "precipitation amount unknown for this exact window")
                        + (f"; sampled temperatures{temp_text}" if temps else "; temperature samples unavailable")
                        + (f", sampled wind {wind} mph" if wind is not None else "")),
            "checked_at": now_iso(),
        }
    except Exception as exc:  # noqa: BLE001 - a tool must never raise
        return unavailable(SOURCE, f"{type(exc).__name__}: {exc}")


