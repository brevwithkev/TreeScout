"""Offline checks for the Phase H hazard tools (saved NWS fixture responses, no network):
python -m unittest backend.tools.test_hazard_tools"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.tools import alerts, common, conditions, seasonal, weather

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
ALERTS = json.loads((FIXTURES / "nws_alerts_ypsilanti.json").read_text(encoding="utf-8"))
HOURLY = json.loads((FIXTURES / "nws_hourly_ypsilanti.json").read_text(encoding="utf-8"))  # 2026-11-16
POINTS = {"properties": {"forecast": "https://api.weather.gov/gridpoints/DTX/47,28/forecast",
                         "forecastHourly": "https://api.weather.gov/gridpoints/DTX/47,28/forecast/hourly"}}
YPSI = (42.2411, -83.6130)


class Fetch:
    """Stands in for get_json_within: answers by URL and counts calls."""

    def __init__(self, routes):
        self.routes, self.urls = routes, []

    def __call__(self, seconds, url, params=None, headers=None):
        self.urls.append(url)
        for prefix, payload in self.routes.items():
            if url.startswith(prefix):
                if isinstance(payload, Exception):
                    raise payload
                return copy.deepcopy(payload)
        raise AssertionError(f"unexpected URL {url}")


class TempCache(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = patch.object(common, "CACHE_DIR", Path(tmp.name))
        p.start()
        self.addCleanup(p.stop)


class AlertsTests(TempCache):
    def test_parses_the_fixture_most_severe_first(self):
        fetch = Fetch({alerts.URL: ALERTS})
        with patch.object(alerts, "get_json_within", fetch):
            result = alerts.get_nws_alerts(*YPSI)
        self.assertEqual(result["source"], "NWS")
        self.assertIn("checked_at", result)
        self.assertEqual([a["event"] for a in result["items"]], ["Winter Weather Advisory", "Special Weather Statement"])
        first = result["items"][0]
        self.assertEqual(set(first), {"event", "severity", "headline", "onset", "ends", "link"})
        self.assertEqual((first["severity"], first["onset"], first["ends"]),
                         ("Moderate", "2026-11-16T07:00:00-05:00", "2026-11-16T19:00:00-05:00"))
        self.assertTrue(first["link"].startswith("https://api.weather.gov/alerts/"))
        self.assertEqual(result["items"][1]["ends"], "2026-11-16T10:00:00-05:00")  # no "ends": falls back to expires
        self.assertEqual(fetch.urls, [f"{alerts.URL}?point=42.2411,-83.6130"])  # literal comma

    def test_cached_for_ten_minutes(self):
        fetch = Fetch({alerts.URL: ALERTS})
        with patch.object(alerts, "get_json_within", fetch):
            alerts.get_nws_alerts(*YPSI)
            alerts.get_nws_alerts(42.2412, -83.6131)  # same ~1 km cell
        self.assertEqual(len(fetch.urls), 1)
        self.assertEqual(alerts.CACHE_SECONDS, 600)

    def test_failure_is_unavailable_and_not_cached(self):
        fetch = Fetch({alerts.URL: TimeoutError("no answer within 8 s")})
        with patch.object(alerts, "get_json_within", fetch):
            result = alerts.get_nws_alerts(*YPSI)
            alerts.get_nws_alerts(*YPSI)
        self.assertEqual((result["available"], result["source"]), (False, "NWS"))
        self.assertIn("TimeoutError", result["error"])
        self.assertEqual(len(fetch.urls), 2)


class HikeWindowTests(TempCache):
    def window(self, payload=HOURLY, start="10:00", minutes=90, date="2026-11-16"):
        fetch = Fetch({"https://api.weather.gov/points/": POINTS, POINTS["properties"]["forecastHourly"]: payload})
        with patch.object(weather, "get_json_within", fetch):
            return weather.get_hike_window(*YPSI, date, start, minutes), fetch

    def test_uses_only_the_overlapping_hours(self):
        result, _ = self.window()  # 10:00-11:30 overlaps the 10:00 and 11:00 periods: 42F and 46F, wind 3 and 5 mph
        self.assertEqual(result["source"], "NWS hourly")
        self.assertEqual((result["min_temp_f"], result["max_wind_mph"], result["max_precip_chance"]), (42, 5, 0))
        self.assertEqual(result["conditions"], ["Sunny"])
        self.assertEqual(result["summary"], "During your hike: 0% rain, 42-46F, wind 5 mph")
        self.assertEqual(result["window"], "10:00-11:30")
        self.assertIn("checked_at", result)

    def test_early_hike_is_freezing_with_unique_conditions(self):
        result, _ = self.window(start="04:00", minutes=180)  # 04:00-07:00: 30, 31, 31F
        self.assertEqual(result["min_temp_f"], 30)
        self.assertEqual(result["conditions"], ["Mostly Cloudy", "Mostly Sunny"])

    def test_rain_and_wind_ranges(self):
        payload = copy.deepcopy(HOURLY)
        period = next(p for p in payload["properties"]["periods"] if p["startTime"].startswith("2026-11-16T11"))
        period.update({"probabilityOfPrecipitation": {"value": 80}, "windSpeed": "10 to 20 mph",
                       "shortForecast": "Chance Showers And Thunderstorms"})
        result, _ = self.window(payload)
        self.assertEqual((result["max_precip_chance"], result["max_wind_mph"]), (80, 20))
        self.assertEqual(result["conditions"], ["Sunny", "Chance Showers And Thunderstorms"])

    def test_outside_the_forecast_is_unavailable(self):
        result, _ = self.window(date="2026-11-25")
        self.assertIs(result["available"], False)
        self.assertEqual(result["source"], "NWS hourly")

    def test_hourly_and_points_are_cached(self):
        _, fetch = self.window()
        with patch.object(weather, "get_json_within", fetch):
            weather.get_hike_window(*YPSI, "2026-11-16", "13:00", 60)  # another window, same location
        self.assertEqual(len(fetch.urls), 2)  # one /points, one hourly


class SeasonalTests(unittest.TestCase):
    def titles(self, date, lat, lon):
        result = seasonal.get_seasonal_hazards(date, lat, lon)
        self.assertEqual(result["source"], seasonal.SOURCE)
        return [i["title"] for i in result["items"]]

    def test_deer_season_in_michigan_on_nov_15(self):
        result = seasonal.get_seasonal_hazards("2026-11-15", *YPSI)
        self.assertEqual(result["items"], [{
            "title": "Firearm deer season", "advice": "Wear blaze orange; hunters are common near state game areas",
            "source": "Michigan DNR season dates", "link": "https://www.michigan.gov/dnr/things-to-do/hunting/deer"}])
        self.assertEqual(self.titles("2026-11-30", *YPSI), ["Firearm deer season"])

    def test_not_after_the_season(self):
        self.assertEqual(self.titles("2026-12-05", *YPSI), [])
        self.assertEqual(self.titles("2026-11-14", *YPSI), [])

    def test_not_in_ohio_or_ontario(self):
        self.assertEqual(self.titles("2026-11-15", 41.6528, -83.5379), [])   # Toledo, Ohio
        self.assertEqual(self.titles("2026-11-15", 39.9612, -82.9988), [])   # Columbus, Ohio
        self.assertEqual(self.titles("2026-11-15", 42.3149, -83.0364), [])   # Windsor, Ontario
        self.assertEqual(self.titles("2026-11-15", 41.6764, -86.2520), [])   # South Bend, Indiana

    def test_michigan_points(self):
        for lat, lon in (YPSI, (42.3314, -83.0458), (41.9164, -83.3977), (42.9634, -85.6681), (46.5436, -87.3954)):
            self.assertTrue(seasonal.in_michigan(lat, lon), (lat, lon))  # Ypsilanti, Detroit, Monroe, GR, Marquette


class ConditionsTests(unittest.TestCase):
    TIMING = {"source": "Computed", "start": "15:30", "est_duration_min": 60, "sunset": "17:10"}

    def test_low_light_when_finishing_within_45_min_of_sunset(self):
        found = conditions.derive_conditions(self.TIMING, None)  # finish 16:30, 40 min before sunset
        self.assertEqual([c["title"] for c in found], ["Low light"])
        self.assertEqual(set(found[0]), {"title", "advice", "source"})
        self.assertEqual(conditions.derive_conditions({**self.TIMING, "start": "15:20"}, None), [])  # 50 min

    def test_freezing(self):
        window = {"source": "NWS hourly", "min_temp_f": 32}
        found = conditions.derive_conditions({**self.TIMING, "start": "10:00"}, window)
        self.assertEqual(found, [{"title": "Freezing temperatures",
                                  "advice": "Down to 32F during your hike; watch for icy boardwalks",
                                  "source": "NWS hourly"}])
        self.assertEqual(conditions.derive_conditions(None, {**window, "min_temp_f": 33}), [])

    def test_unavailable_inputs(self):
        self.assertEqual(conditions.derive_conditions({"available": False}, {"available": False}), [])


if __name__ == "__main__":
    unittest.main()
