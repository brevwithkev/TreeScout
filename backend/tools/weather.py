"""Forecasts from the US National Weather Service (api.weather.gov): the daytime forecast for a date, and
the hourly forecast for the hours of the hike."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import logging
import re
import time

from backend.tools import common
from backend.tools.coverage import covers, utc
from backend.tools.common import (cache_get, cache_put, get_json, get_json_within, now_iso, parse_date, retry_once,
                                  timezone_for, unavailable)

logger = logging.getLogger("treescout.tools")

SOURCE = "NWS"
HOURLY_SOURCE = "NWS hourly"
TIMEOUT = 10
HOURLY_TIMEOUT = 8            # hard cap for /points when called from get_weather
# Hourly forecast: common.FIRST_TIMEOUT_S (6 s) for /points + hourly together, then one retry with 3 s.
CACHE_SECONDS = 10 * 60       # points metadata and hourly periods, per location (~1 km)
TZ = ZoneInfo("America/Detroit")  # default for hike_window(); get_hike_window passes the point's own zone
HEADERS = {"Accept": "application/geo+json"}


def _points(lat, lon, timeout=HOURLY_TIMEOUT):
    """The /points metadata (forecast URLs) for a location, cached for 10 min."""
    key = {"lat": round(float(lat), 2), "lon": round(float(lon), 2)}
    cached = cache_get("nws_points", key, CACHE_SECONDS)
    if cached is not None:
        return cached
    props = get_json_within(timeout, f"https://api.weather.gov/points/{float(lat):.4f},{float(lon):.4f}",
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


def _cached_hourly(key, max_age_s):
    cached = cache_get("nws_hourly", key, max_age_s)
    return cached if isinstance(cached, dict) and isinstance(cached.get("periods"), list) else None  # skips old lists


def hourly_periods(lat, lon):
    """{periods, checked_at[, stale]}: the NWS hourly forecast for a location (about 6.5 days), cached 10 min.

    A 5xx or timeout (on /points or the hourly forecast) is retried once after 1 s. If it still fails, the last
    successful forecast for the location (up to 6 h old) comes back with stale: true and its original checked_at.
    Raises only when there is no such cached forecast.
    """
    key = {"lat": round(float(lat), 2), "lon": round(float(lon), 2)}
    cached = _cached_hourly(key, CACHE_SECONDS)
    if cached is not None:
        return cached

    def fetch(timeout):  # /points (usually cached) and the hourly forecast share one time budget
        deadline = time.monotonic() + timeout
        url = _points(lat, lon, timeout)["forecastHourly"]
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError(f"no answer within {timeout:g} s")
        return get_json_within(left, url, headers=HEADERS)["properties"]["periods"]
    try:
        result = {"periods": retry_once(fetch, "NWS hourly"), "checked_at": now_iso()}
    except Exception as exc:  # noqa: BLE001 - fall back to the last good forecast, else re-raise
        stale = _cached_hourly(key, common.STALE_MAX_S)
        if stale is None:
            raise
        logger.warning("NWS hourly failed (%s: %s); using the cached forecast from %s", type(exc).__name__, exc,
                       stale.get("checked_at"))
        return {**stale, "stale": True}
    cache_put("nws_hourly", key, result)
    return result


def hike_window(periods, date, start, est_duration_min, tz=TZ):
    """Summarize the hourly periods that overlap [start, start + duration) on date (local time in tz).
    Returns the result dict without source/checked_at, or None when no period overlaps."""
    begin = utc(datetime.combine(parse_date(date), datetime.strptime(str(start)[:5], "%H:%M").time(), tzinfo=tz))
    end = begin + timedelta(minutes=max(1, int(est_duration_min)))
    overlap = []
    for p in periods:
        p_start, p_end = utc(datetime.fromisoformat(p["startTime"])), utc(datetime.fromisoformat(p["endTime"]))
        if p_start < end and p_end > begin:
            overlap.append(p)
    if not overlap:
        return None
    # An overlap is not full coverage: reject a missing prefix, suffix, or interior gap.
    if not covers(begin, end, [(datetime.fromisoformat(p["startTime"]), datetime.fromisoformat(p["endTime"]))
                               for p in overlap]):
        return None
    overlap.sort(key=lambda p: utc(datetime.fromisoformat(p["startTime"])))
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
        "window": f"{begin.astimezone(tz):%H:%M}-{end.astimezone(tz):%H:%M}",
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
        hourly = hourly_periods(lat, lon)
        summary = hike_window(hourly["periods"], date, start, est_duration_min, timezone_for(lat, lon))
        if summary is None:
            return unavailable(HOURLY_SOURCE, f"No complete NWS hourly forecast for {date} {start} "
                                              "(the hourly forecast covers about 6 days)")
        if hourly.get("stale"):  # live check failed: the last good forecast, with its own checked_at
            return {"source": HOURLY_SOURCE, **summary, "checked_at": hourly["checked_at"], "stale": True}
        return {"source": HOURLY_SOURCE, **summary, "checked_at": now_iso()}
    except Exception as exc:  # noqa: BLE001 - a tool must never raise
        return unavailable(HOURLY_SOURCE, f"{type(exc).__name__}: {exc}")
