"""Hike timing: Naismith duration, sunset (astral), latest safe start and turnaround time."""

from datetime import datetime, timedelta

from astral import Observer
from astral.sun import sunset as astral_sunset

from backend.tools.common import now_iso, parse_date, timezone_for, unavailable

SOURCE = "Computed (astral sunset, Naismith's rule)"
SAFETY_MARGIN = timedelta(minutes=30)


def _hhmm(dt):
    return dt.strftime("%H:%M")


def compute_timing(distance_km, elevation_gain_m, lat, lon, date, start_time):
    """Return {source, start, est_duration_min, sunset, latest_safe_start, turnaround_by, warning, checked_at}.

    Times are local to the trailhead (timezonefinder). Naismith: hours = distance_km / 5 + elevation_gain_m / 600.
    latest_safe_start = sunset - duration - 30 min; turnaround_by = sunset - duration/2 - 30 min.
    """
    try:
        day = parse_date(date)
        gain = float(elevation_gain_m or 0)
        duration = timedelta(hours=float(distance_km) / 5 + gain / 600)
        tz = timezone_for(lat, lon)
        sun_down = astral_sunset(Observer(latitude=float(lat), longitude=float(lon)), date=day, tzinfo=tz)
        start = datetime.combine(day, datetime.strptime(str(start_time)[:5], "%H:%M").time(), tzinfo=tz)

        latest_safe = sun_down - duration - SAFETY_MARGIN
        turnaround = sun_down - duration / 2 - SAFETY_MARGIN
        warning = None
        if start > latest_safe:
            finish = start + duration
            warning = (f"Starting at {_hhmm(start)} is after the latest safe start of {_hhmm(latest_safe)}: "
                       f"you would finish around {_hhmm(finish)}, with sunset at {_hhmm(sun_down)}.")
        return {
            "source": SOURCE,
            "start": _hhmm(start),
            "est_duration_min": round(duration.total_seconds() / 60),
            "sunset": _hhmm(sun_down),
            "latest_safe_start": _hhmm(latest_safe),
            "turnaround_by": _hhmm(turnaround),
            "warning": warning,
            "elevation_known": elevation_gain_m is not None,
            "checked_at": now_iso(),
        }
    except Exception as exc:  # noqa: BLE001
        return unavailable(SOURCE, f"{type(exc).__name__}: {exc}")
