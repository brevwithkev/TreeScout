"""Forecasts from the US National Weather Service (api.weather.gov): the daytime forecast for a date, and
the hourly forecast for the hours of the hike."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import re

from backend.tools.common import cache_get, cache_put, get_json, get_json_within, now_iso, parse_date, unavailable

SOURCE = "NWS"
HOURLY_SOURCE = "NWS hourly"
TIMEOUT = 10
HOURLY_TIMEOUT = 8            # hard cap per request for the hourly forecast
CACHE_SECONDS = 10 * 60       # points metadata and hourly periods, per location (~1 km)
TZ = ZoneInfo("America/Detroit")
HEADERS = {"Accept": "application/geo+json"}


def _points(lat, lon):
    """The /points metadata (forecast URLs) for a location, cached for 10 min."""
    key = {"lat": round(float(lat), 2), "lon": round(float(lon), 2)}
    cached = cache_get("nws_points", key, CACHE_SECONDS)
    if cached is not None:
        return cached
    props = get_json_within(HOURLY_TIMEOUT, f"https://api.weather.gov/points/{float(lat):.4f},{float(lon):.4f}",
                            headers=HEADERS)["properties"]
    points = {"forecast": props["forecast"], "forecastHourly": props["forecastHourly"]}
    cache_put("nws_points", key, points)
    return points


def get_weather(lat, lon, date):
    """Return {source, summary, temperature_f, precip_chance, period, checked_at} or an unavailable dict."""
    try:
        day = parse_date(date).isoformat()
        forecast_url = _points(lat, lon)["forecast"]
        periods = get_json(forecast_url, headers=HEADERS, timeout=TIMEOUT)["properties"]["periods"]

        same_day = [p for p in periods if str(p.get("startTime", ""))[:10] == day]
        if not same_day:
            return unavailable(SOURCE, f"No NWS forecast period for {day} (NWS forecasts cover about 7 days)")
        period = next((p for p in same_day if p.get("isDaytime")), same_day[0])

        temp = _fahrenheit(period)
        precip = (period.get("probabilityOfPrecipitation") or {}).get("value")
        precip = 0 if precip is None else int(precip)  # NWS sends null when no precipitation is expected
        short = period.get("shortForecast") or "Forecast"
        return {
            "source": SOURCE,
            "summary": f"{short}, {temp}F" if temp is not None else short,
            "temperature_f": temp,
            "precip_chance": precip,
            "period": period.get("name"),
            "checked_at": now_iso(),
        }
    except Exception as exc:  # noqa: BLE001 - a tool must never raise
        return unavailable(SOURCE, f"{type(exc).__name__}: {exc}")


def _fahrenheit(period):
    temp = period.get("temperature")
    if period.get("temperatureUnit") == "C" and temp is not None:
        temp = round(temp * 9 / 5 + 32)
    return temp


def _wind_mph(text):
    """'7 mph' -> 7, '5 to 10 mph' -> 10, '' -> None."""
    numbers = [int(n) for n in re.findall(r"\d+", str(text or ""))]
    return max(numbers) if numbers else None


def hourly_periods(lat, lon):
    """The NWS hourly forecast periods for a location (about 6.5 days), cached for 10 min."""
    key = {"lat": round(float(lat), 2), "lon": round(float(lon), 2)}
    cached = cache_get("nws_hourly", key, CACHE_SECONDS)
    if cached is not None:
        return cached
    url = _points(lat, lon)["forecastHourly"]
    periods = get_json_within(HOURLY_TIMEOUT, url, headers=HEADERS)["properties"]["periods"]
    cache_put("nws_hourly", key, periods)
    return periods


def hike_window(periods, date, start, est_duration_min):
    """Summarize the hourly periods that overlap [start, start + duration) on date (America/Detroit).
    Returns the result dict without source/checked_at, or None when no period overlaps."""
    begin = datetime.combine(parse_date(date), datetime.strptime(str(start)[:5], "%H:%M").time(), tzinfo=TZ)
    end = begin + timedelta(minutes=max(1, int(est_duration_min)))
    overlap = []
    for p in periods:
        p_start, p_end = datetime.fromisoformat(p["startTime"]), datetime.fromisoformat(p["endTime"])
        if p_start < end and p_end > begin:
            overlap.append(p)
    if not overlap:
        return None
    precip = max(int((p.get("probabilityOfPrecipitation") or {}).get("value") or 0) for p in overlap)
    temps = [t for t in (_fahrenheit(p) for p in overlap) if t is not None]
    winds = [w for w in (_wind_mph(p.get("windSpeed")) for p in overlap) if w is not None]
    conditions = list(dict.fromkeys(p.get("shortForecast") for p in overlap if p.get("shortForecast")))
    low, high = (min(temps), max(temps)) if temps else (None, None)
    wind = max(winds) if winds else None
    bits = [f"{precip}% rain"]
    if temps:
        bits.append(f"{low}F" if low == high else f"{low}-{high}F")
    if wind is not None:
        bits.append(f"wind {wind} mph")
    return {
        "window": f"{begin:%H:%M}-{end:%H:%M}",
        "max_precip_chance": precip,
        "min_temp_f": low,
        "max_temp_f": high,
        "max_wind_mph": wind,
        "conditions": conditions,
        "summary": "During your hike: " + ", ".join(bits),
    }


def get_hike_window(lat, lon, date, start, est_duration_min):
    """Return {source: "NWS hourly", window, max_precip_chance, min_temp_f, max_temp_f, max_wind_mph,
    conditions: [unique shortForecast], summary, checked_at} for the hours of the hike, or an unavailable dict."""
    try:
        summary = hike_window(hourly_periods(lat, lon), date, start, est_duration_min)
        if summary is None:
            return unavailable(HOURLY_SOURCE, f"No NWS hourly forecast for {date} {start} "
                                              "(the hourly forecast covers about 6 days)")
        return {"source": HOURLY_SOURCE, **summary, "checked_at": now_iso()}
    except Exception as exc:  # noqa: BLE001 - a tool must never raise
        return unavailable(HOURLY_SOURCE, f"{type(exc).__name__}: {exc}")
