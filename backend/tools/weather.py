"""Daytime forecast for a date from the US National Weather Service (api.weather.gov)."""

from backend.tools.common import get_json, now_iso, parse_date, unavailable

SOURCE = "NWS"
TIMEOUT = 10


def get_weather(lat, lon, date):
    """Return {source, summary, temperature_f, precip_chance, period, checked_at} or an unavailable dict."""
    try:
        day = parse_date(date).isoformat()
        headers = {"Accept": "application/geo+json"}
        points = get_json(f"https://api.weather.gov/points/{float(lat):.4f},{float(lon):.4f}",
                          headers=headers, timeout=TIMEOUT)
        forecast_url = points["properties"]["forecast"]
        periods = get_json(forecast_url, headers=headers, timeout=TIMEOUT)["properties"]["periods"]

        same_day = [p for p in periods if str(p.get("startTime", ""))[:10] == day]
        if not same_day:
            return unavailable(SOURCE, f"No NWS forecast period for {day} (NWS forecasts cover about 7 days)")
        period = next((p for p in same_day if p.get("isDaytime")), same_day[0])

        temp = period.get("temperature")
        if period.get("temperatureUnit") == "C" and temp is not None:
            temp = round(temp * 9 / 5 + 32)
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
