"""Typical conditions for the same calendar week over the last 20 years, from NASA POWER."""

from datetime import date as Date, timedelta

from backend.tools.common import get_json, now_iso, parse_date, unavailable

SOURCE = "NASA POWER"
URL = "https://power.larc.nasa.gov/api/temporal/daily/point"
TIMEOUT = 25
YEARS = 20
HALF_WINDOW_DAYS = 3


def _same_day(year, day):
    try:
        return day.replace(year=year)
    except ValueError:  # Feb 29 in a non-leap year
        return Date(year, 2, 28)


def get_climate(lat, lon, date):
    """Return {source, years, avg_high_c, avg_low_c, avg_precip_mm, days, checked_at} or an unavailable dict."""
    try:
        day = parse_date(date)
        years = range(day.year - YEARS, day.year)
        windows = [(_same_day(y, day) - timedelta(days=HALF_WINDOW_DAYS),
                    _same_day(y, day) + timedelta(days=HALF_WINDOW_DAYS)) for y in years]
        wanted = {(start + timedelta(days=i)).strftime("%Y%m%d")
                  for start, end in windows for i in range((end - start).days + 1)}

        # One request spanning all 20 windows is much faster than 20 requests.
        data = get_json(URL, params={
            "parameters": "T2M_MAX,T2M_MIN,PRECTOTCORR", "community": "RE", "format": "JSON",
            "latitude": f"{float(lat):.4f}", "longitude": f"{float(lon):.4f}",
            "start": windows[0][0].strftime("%Y%m%d"), "end": windows[-1][1].strftime("%Y%m%d"),
        }, timeout=TIMEOUT)
        params = data["properties"]["parameter"]

        def mean(name):
            values = [v for k, v in params[name].items() if k in wanted and v is not None and v > -998]
            return (round(sum(values) / len(values), 1), len(values)) if values else (None, 0)

        high, n_high = mean("T2M_MAX")
        low, _ = mean("T2M_MIN")
        precip, _ = mean("PRECTOTCORR")
        if high is None and low is None:
            return unavailable(SOURCE, "NASA POWER returned no valid values for this week")
        return {
            "source": SOURCE,
            "years": f"{years[0]}-{years[-1]}",
            "avg_high_c": high,
            "avg_low_c": low,
            "avg_precip_mm": precip,
            "days": n_high,
            "checked_at": now_iso(),
        }
    except Exception as exc:  # noqa: BLE001
        return unavailable(SOURCE, f"{type(exc).__name__}: {exc}")
