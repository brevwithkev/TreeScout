"""Offline checks for the Phase H hazard tools (saved NWS fixture responses, no network):
python -m unittest backend.tools.test_hazard_tools"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.tools import alerts, anomaly, common, conditions, seasonal, weather

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
    TIMING = {"source": "Computed", "start": "15:45", "est_duration_min": 60, "sunset": "17:10",
              "latest_safe_start": "15:40"}

    def test_low_light_uses_the_30_min_start_buffer(self):
        found = conditions.derive_conditions(self.TIMING, None)  # finish 16:45, 25 min before sunset
        self.assertEqual([c["title"] for c in found], ["Low light"])
        self.assertEqual(set(found[0]), {"title", "advice", "source"})
        self.assertIn("within 30 min of sunset", found[0]["advice"])
        at_safe = {**self.TIMING, "start": "15:40"}           # exactly the latest safe start: 30 min margin
        self.assertEqual(conditions.derive_conditions(at_safe, None), [])
        self.assertEqual(conditions.derive_conditions({**self.TIMING, "start": "15:20"}, None), [])  # 50 min

    def test_start_at_latest_safe_start_is_not_low_light_despite_rounding(self):
        # 93.6 min hike, sunset 17:11:40 -> latest safe 15:08 (floored), est 94 min: finish 16:42 looks 29 min early.
        timing = {"start": "15:08", "est_duration_min": 94, "sunset": "17:11", "latest_safe_start": "15:08"}
        self.assertEqual(conditions.derive_conditions(timing, None), [])

    def test_margin_fallback_without_latest_safe_start(self):
        timing = {k: v for k, v in self.TIMING.items() if k != "latest_safe_start"}
        self.assertEqual([c["title"] for c in conditions.derive_conditions(timing, None)], ["Low light"])

    def test_freezing(self):
        window = {"source": "NWS hourly", "min_temp_f": 32}
        found = conditions.derive_conditions({**self.TIMING, "start": "10:00"}, window)
        self.assertEqual(found, [{"title": "Freezing temperatures",
                                  "advice": "Down to 32F during your hike; watch for icy boardwalks",
                                  "source": "NWS hourly"}])
        self.assertEqual(conditions.derive_conditions(None, {**window, "min_temp_f": 33}), [])

    def test_unavailable_inputs(self):
        self.assertEqual(conditions.derive_conditions({"available": False}, {"available": False}), [])


# Fixture values: NASA POWER 2006-2025 normals for mid-November in Ypsilanti (rounded) and NWS hike windows.
POWER = {"source": "NASA POWER", "years": "2006-2025", "avg_high_c": 9.4, "avg_low_c": 0.6, "avg_precip_mm": 2.3,
         "days": 140, "checked_at": "t"}        # normal high 48.9F, low 33.1F
COLD = {"source": "NWS hourly", "window": "10:00-12:00", "max_precip_chance": 10, "min_temp_f": 36, "max_temp_f": 40,
        "max_wind_mph": 5, "conditions": ["Cloudy"], "summary": "During your hike: 10% rain, 36-40F, wind 5 mph",
        "checked_at": "t"}


class ClimateAnomalyTests(unittest.TestCase):
    def test_9f_colder_is_notable(self):
        found = anomaly.compare_climate(COLD, POWER)    # daytime: forecast high 40F vs normal high 48.9F
        self.assertEqual({k: found[k] for k in ("source", "temp_diff_f", "rain_flag", "notable", "summary")},
                         {"source": "NWS vs NASA POWER 2006-2025", "temp_diff_f": -9, "rain_flag": False,
                          "notable": True, "summary": "About 9F colder than normal for this week"})
        self.assertEqual(anomaly.anomaly_condition(found), {
            "title": "Unusual conditions",
            "advice": "About 9F colder than normal for this week (forecast high 40F vs normal 49F); dress for it",
            "source": "NWS vs NASA POWER 2006-2025"})

    def test_3f_warmer_is_typical(self):
        found = anomaly.compare_climate({**COLD, "max_temp_f": 52}, POWER)
        self.assertEqual((found["temp_diff_f"], found["notable"], found["summary"]), (3, False, "Typical for this week"))
        self.assertIsNone(anomaly.anomaly_condition(found))

    def test_rain_in_a_dry_week(self):
        wet = {**COLD, "max_temp_f": 49, "max_precip_chance": 60}
        found = anomaly.compare_climate(wet, {**POWER, "avg_precip_mm": 1.4})
        self.assertTrue(found["rain_flag"] and found["notable"])
        self.assertEqual(found["summary"], "60% chance of rain in a week that is usually dry (1.4 mm/day)")
        self.assertFalse(anomaly.compare_climate(wet, POWER)["rain_flag"])            # 2.3 mm/day: not a dry week
        self.assertFalse(anomaly.compare_climate({**wet, "max_precip_chance": 49},
                                                 {**POWER, "avg_precip_mm": 1.4})["rain_flag"])

    def test_same_part_of_the_day(self):
        night = anomaly.compare_climate({**COLD, "window": "22:00-23:00", "min_temp_f": 25}, POWER)
        self.assertEqual(night["temp_diff_f"], -8)                                    # low 25F vs normal low 33.1F
        morning = anomaly.compare_climate({**COLD, "window": "07:00-08:30", "min_temp_f": 36, "max_temp_f": 40}, POWER)
        self.assertEqual(morning["temp_diff_f"], -3)                                  # 38F vs normal average 41F

    def test_missing_power_is_unavailable(self):
        found = anomaly.compare_climate(COLD, {"available": False, "source": "NASA POWER", "error": "timed out"})
        self.assertEqual(found["available"], False)
        self.assertEqual(found["source"], "NWS vs NASA POWER")
        self.assertIn("NASA POWER normals unavailable (timed out)", found["error"])
        self.assertIsNone(anomaly.anomaly_condition(found))

    def test_missing_nws_or_values_is_unavailable(self):
        found = anomaly.compare_climate({"available": False, "source": "NWS hourly", "error": "502"}, POWER)
        self.assertEqual((found["available"], found["error"]), (False, "NWS hourly forecast unavailable (502)"))
        self.assertEqual(anomaly.compare_climate(None, None)["available"], False)
        self.assertEqual(anomaly.compare_climate({**COLD, "max_temp_f": None}, POWER)["available"], False)
        self.assertEqual(anomaly.compare_climate(COLD, {**POWER, "avg_high_c": "x"})["available"], False)  # no crash


if __name__ == "__main__":
    unittest.main()
