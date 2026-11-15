"""Typical conditions for the same calendar week over the last 20 years, from NASA POWER."""

from datetime import date as Date, timedelta

from backend.tools.common import cache_get, cache_put, get_json_within, now_iso, parse_date, unavailable

SOURCE = "NASA POWER"
URL = "https://power.larc.nasa.gov/api/temporal/daily/point"
TIMEOUT = 8                 # hard cap for the whole request; then available:false
CACHE_SECONDS = 6 * 3600    # results cached on disk per location (~1 km) and ISO week of the hike date
YEARS = 20
HALF_WINDOW_DAYS = 3


def _same_day(year, day):
    try:
        return day.replace(year=year)
    except ValueError:  # Feb 29 in a non-leap year
        return Date(year, 2, 28)


def get_climate(lat, lon, date):
    """Return {source, years, avg_high_c, avg_low_c, avg_precip_mm, days, checked_at} or an unavailable dict.

    Successful results are cached for 6 h per location (rounded to 0.01 deg) and ISO week of the date;
    a cached result keeps its original checked_at.
    """
    try:
        day = parse_date(date)
        iso = day.isocalendar()
        key = {"lat": round(float(lat), 2), "lon": round(float(lon), 2), "week": f"{iso[0]}-W{iso[1]:02d}"}
        cached = cache_get("power", key, CACHE_SECONDS)
        if cached is not None:
            return cached
        years = range(day.year - YEARS, day.year)
        windows = [(_same_day(y, day) - timedelta(days=HALF_WINDOW_DAYS),
                    _same_day(y, day) + timedelta(days=HALF_WINDOW_DAYS)) for y in years]
        wanted = {(start + timedelta(days=i)).strftime("%Y%m%d")
                  for start, end in windows for i in range((end - start).days + 1)}

        # One request spanning all 20 windows is much faster than 20 requests.
        data = get_json_within(TIMEOUT, URL, params={
            "parameters": "T2M_MAX,T2M_MIN,PRECTOTCORR", "community": "RE", "format": "JSON",
            "latitude": f"{float(lat):.4f}", "longitude": f"{float(lon):.4f}",
            "start": windows[0][0].strftime("%Y%m%d"), "end": windows[-1][1].strftime("%Y%m%d"),
        })
        params = data["properties"]["parameter"]

        def mean(name):
            values = [v for k, v in params[name].items() if k in wanted and v is not None and v > -998]
            return (round(sum(values) / len(values), 1), len(values)) if values else (None, 0)

        high, n_high = mean("T2M_MAX")
        low, _ = mean("T2M_MIN")
        precip, _ = mean("PRECTOTCORR")
        if high is None and low is None:
            return unavailable(SOURCE, "NASA POWER returned no valid values for this week")
        result = {
            "source": SOURCE,
            "years": f"{years[0]}-{years[-1]}",
            "avg_high_c": high,
            "avg_low_c": low,
            "avg_precip_mm": precip,
            "days": n_high,
            "checked_at": now_iso(),
        }
        cache_put("power", key, result)
        return result
    except Exception as exc:  # noqa: BLE001
        return unavailable(SOURCE, f"{type(exc).__name__}: {exc}")
