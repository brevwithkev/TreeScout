"""Seasonal hazards from a small config table (no API), e.g. Michigan's firearm deer season."""

from backend.tools.common import now_iso, parse_date, unavailable

SOURCE = "TrekChek seasonal calendar"

# Each season: (month, day) start and end, inclusive, and the region it applies to.
# THESE DATES MUST BE VERIFIED EVERY YEAR against the Michigan DNR hunting digest before the season;
# the regular firearm deer season has been Nov 15-30 for years, but the DNR can change it.
SEASONS = [
    {
        "title": "Firearm deer season",
        "advice": "Wear blaze orange; hunters are common near state game areas",
        "source": "Michigan DNR season dates",
        "link": "https://www.michigan.gov/dnr/things-to-do/hunting/deer",
        "start": (11, 15), "end": (11, 30), "region": "michigan",
        "narration": "Deer season: wear orange.",  # short line for the trailhead narration
    },
]

# Coarse outlines of Michigan (lat, lon); inland lakes and some Great Lakes water count as Michigan, which is
# fine for "is this hike in Michigan". The southern edge follows the Indiana and Ohio borders and the eastern
# edge the Detroit and St. Clair rivers, so Toledo, Ohio and Windsor, Ontario are outside.
LOWER_PENINSULA = [
    (41.760, -86.830), (41.760, -84.806), (41.696, -84.806), (41.732, -83.454), (42.050, -83.150),
    (42.290, -83.100), (42.312, -83.074), (42.330, -83.030), (42.345, -82.960), (42.600, -82.700),
    (43.000, -82.420), (44.050, -82.500),
    (45.000, -83.300), (45.800, -84.400), (45.800, -85.000), (45.000, -86.000), (44.000, -86.600),
    (42.800, -86.300), (42.000, -86.650),
]
UPPER_PENINSULA = [
    (45.100, -87.600), (45.700, -87.900), (46.000, -88.200), (46.300, -89.900), (46.600, -90.400),
    (47.500, -88.000), (46.800, -85.000), (46.500, -84.400), (46.000, -83.500), (45.900, -84.700),
    (45.900, -86.000), (45.500, -87.000),
]


def _inside(lat, lon, polygon):
    """Ray casting point-in-polygon on (lat, lon) vertices."""
    inside = False
    for (lat1, lon1), (lat2, lon2) in zip(polygon, polygon[1:] + polygon[:1]):
        if (lat1 > lat) != (lat2 > lat):
            if lon < lon1 + (lat - lat1) * (lon2 - lon1) / (lat2 - lat1):
                inside = not inside
    return inside


def in_michigan(lat, lon):
    return _inside(float(lat), float(lon), LOWER_PENINSULA) or _inside(float(lat), float(lon), UPPER_PENINSULA)


def _in_season(day, season):
    return season["start"] <= (day.month, day.day) <= season["end"]


def get_seasonal_hazards(date, lat, lon):
    """Return {source, items: [{title, advice, source, link}], checked_at} for seasons that include date at
    this point, or an unavailable dict."""
    try:
        day = parse_date(date)
        regions = {"michigan"} if in_michigan(lat, lon) else set()
        items = [{k: s[k] for k in ("title", "advice", "source", "link")}
                 for s in SEASONS if s["region"] in regions and _in_season(day, s)]
        return {"source": SOURCE, "items": items, "checked_at": now_iso()}
    except Exception as exc:  # noqa: BLE001 - a tool must never raise
        return unavailable(SOURCE, f"{type(exc).__name__}: {exc}")


def narration_phrase(title):
    """The short trailhead phrase for a seasonal item, or None."""
    return next((s["narration"] for s in SEASONS if s["title"] == title), None)
