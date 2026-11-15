"""Offline checks for out-and-back routes, SRTM elevation and the elevation-unknown checker rule:
python -m unittest backend.test_route"""

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import sunset

from backend import agents
from backend.narration import TURNAROUND_TEXT
from backend.test_anywhere import DATE, GC_NOMINATIM, Offline, fixture, summaries
from backend.tools import elevation, route, trails

SEED = json.loads((trails.SEED_DIR / "grand_canyon_village.json").read_text(encoding="utf-8"))
BRIGHT_ANGEL = next(t for t in trails._parse_anywhere(SEED["payload"]) if t["osm_id"] == 2836803)
SRTM_BA = fixture("srtm_bright_angel_2836803.json")  # real OpenTopoData answer for its 100 sampled points
LINE = [[36.0, -112.0], [36.01, -112.0], [36.02, -112.0]]   # 2.2 km straight north


class RouteTests(unittest.TestCase):
    def test_loop_only_within_200_m(self):
        self.assertTrue(route.is_loop([[42.0, -83.0], [42.01, -83.0], [42.0015, -83.0]]))   # 167 m gap
        self.assertFalse(route.is_loop([[42.0, -83.0], [42.01, -83.0], [42.0025, -83.0]]))  # 278 m gap

    def test_full_out_and_back(self):
        r = route.make_route({"coords": LINE, "distance_km": 2.224}, target_km=10)
        self.assertEqual((r["kind"], r["route_km"], r["turnaround_km"]), ("out-and-back", 4.448, 2.224))
        self.assertEqual(r["coords"], LINE + LINE[-2::-1])          # out, then back the same way
        self.assertEqual(r["coords"][r["turnaround_index"]], LINE[-1])

    def test_turns_around_at_half_the_requested_distance(self):
        r = route.make_route({"coords": LINE, "distance_km": 2.224}, target_km=2.0)
        self.assertEqual((r["route_km"], r["turnaround_km"]), (2.0, 1.0))
        turn = r["coords"][r["turnaround_index"]]
        self.assertAlmostEqual(trails.haversine(LINE[0], turn), 1.0, delta=0.01)
        self.assertEqual(r["coords"][-1], LINE[0])

    def test_climb_counts_both_directions(self):
        # Down 600 m into a canyon over 2.2 km: the walk out has no climb, the walk back climbs 600 m.
        down = {"elevation_gain_m": 0, "elevation_loss_m": 600, "profile": [[0, 2000], [1.112, 1700], [2.224, 1400]]}
        self.assertEqual(route.make_route({"coords": LINE, "distance_km": 2.224}, 10, down)["climb_m"], 600)
        self.assertEqual(route.make_route({"coords": LINE, "distance_km": 2.224}, 2.224, down)["climb_m"], 300)
        bumpy = {"elevation_gain_m": 50, "elevation_loss_m": 50, "profile": [[0, 100], [1.0, 150], [2.224, 100]]}
        self.assertEqual(route.make_route({"coords": LINE, "distance_km": 2.224}, 10, bumpy)["climb_m"], 100)
        loop = [[42.0, -83.0], [42.01, -83.0], [42.0, -83.0]]
        self.assertEqual(route.make_route({"coords": loop, "distance_km": 2.2}, 10, bumpy)["climb_m"], 50)
        self.assertIsNone(route.make_route({"coords": LINE, "distance_km": 2.224}, 10, None)["climb_m"])

    def test_starts_at_the_end_nearer_the_place(self):
        up = {"elevation_gain_m": 600, "elevation_loss_m": 0, "profile": [[0, 1400], [2.224, 2000]]}
        r = route.make_route({"coords": LINE, "distance_km": 2.224}, 2.224, up, ref=[36.021, -112.0])
        self.assertEqual(r["coords"][0], LINE[-1])                     # walked from the north end
        self.assertEqual(r["climb_m"], 300)                            # descends 300 m out, climbs it back

    def test_ranks_by_the_distance_walked(self):
        self.assertEqual(route.route_km({"coords": LINE, "distance_km": 2.224}), 4.448)
        self.assertEqual(route.route_km({"coords": LINE, "distance_km": 2.224}, 3.0), 3.0)


class BrightAngelTests(unittest.TestCase):
    """Bright Angel Trail (OSM 2836803) from the seeded snapshot, with its real NASA SRTM profile."""

    def plan(self, text, osm_id=2836803):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        wanted = SRTM_BA["params"]["locations"]

        def srtm(points):  # the only provider: the fixture for Bright Angel, unknown for anything else
            if "|".join(f"{p[0]:.5f},{p[1]:.5f}" for p in points) != wanted:
                raise ValueError("no fixture for this trail")
            return [r["elevation"] for r in SRTM_BA["response"]["results"]]
        with Offline(overpass=None, nominatim=GC_NOMINATIM), \
                patch("backend.agents.get_elevation_gain", elevation.get_elevation_gain), \
                patch.object(elevation, "CACHE_FILE", Path(tmp.name) / "elevation_cache.json"), \
                patch.object(elevation, "_cache", None), \
                patch.object(elevation, "PROVIDERS", [("NASA SRTM via OpenTopoData", srtm)]):
            return agents.make_plan(text, 42.2411, -83.6130, form={"date": DATE}, osm_id=osm_id)

    def test_srtm_profile(self):
        with patch.object(elevation, "get_json", lambda *a, **k: SRTM_BA["response"]), \
                patch.object(elevation, "_cache", {}), patch.object(elevation, "_store", lambda *a: None):
            result = elevation.get_elevation_gain(BRIGHT_ANGEL["coords"], 2836803)
        self.assertEqual(result["source"], "NASA SRTM via OpenTopoData")
        self.assertEqual((result["elevation_gain_m"], result["elevation_loss_m"]), (286, 1612))
        self.assertEqual((result["max_m"], result["min_m"]), (2084, 758))   # rim to river

    def test_full_out_and_back_to_the_river_from_a_1_pm_start(self):
        result = self.plan("A 30 km hike at the Grand Canyon starting at 1 PM tomorrow")
        trail, timing = result["trail"], result["timing"]
        self.assertEqual((trail["name"], trail["route"]), ("Bright Angel Trail", "out-and-back"))
        self.assertEqual(trail["distance_km"], round(2 * BRIGHT_ANGEL["distance_km"], 3))   # 25.1 km: to the river
        self.assertGreater(trail["elevation_gain_m"], 1800)                  # 1612 m back up + 286 m of rises
        self.assertTrue(trail["coords"][0][0] < 36.06)                         # starts at the rim (village) end
        self.assertGreaterEqual(timing["est_duration_min"], 7 * 60)          # Naismith: 25.1/5 + 1898/600 h
        self.assert_sensible_sunset(result, "13:00")

    def test_partial_out_and_back_turns_around_at_half_the_distance(self):
        result = self.plan("A 15 km hike at the Grand Canyon starting at 1 PM tomorrow")
        trail, timing = result["trail"], result["timing"]
        self.assertEqual((trail["distance_km"], trail["turnaround_km"]), (15.0, 7.5))
        self.assertGreater(trail["elevation_gain_m"], 900)                   # about 1 km down to Havasupai Gardens
        self.assertGreaterEqual(timing["est_duration_min"], 4 * 60)          # several hours, not 3 h of flat walking
        self.assertIn(TURNAROUND_TEXT, [n["text"] for n in result["narration"]])
        turn = [n for n in result["narration"] if n["text"] == TURNAROUND_TEXT][0]
        self.assertEqual(turn["coord_index"], trail["turnaround_index"])
        self.assert_sensible_sunset(result, "13:00")

    def assert_sensible_sunset(self, result, asked):
        """A 1 PM start can't finish 30 min before sunset: the start moves earlier and the plan is approved."""
        timing = result["timing"]
        lat, lon = result["trail"]["coords"][0]
        sun = sunset(Observer(latitude=lat, longitude=lon), date=date.fromisoformat(DATE),
                     tzinfo=ZoneInfo("America/Phoenix"))
        self.assertEqual(timing["sunset"], sun.strftime("%H:%M"))
        self.assertLess(timing["start"], asked)
        self.assertEqual(timing["start"], timing["latest_safe_start"])
        start = datetime.combine(sun.date(), datetime.strptime(timing["start"], "%H:%M").time(), sun.tzinfo)
        self.assertLessEqual(start + timedelta(minutes=timing["est_duration_min"] + 30), sun + timedelta(minutes=1))
        self.assertTrue(result["checker"]["approved"])
        self.assertTrue(any(w.startswith(f"You asked to start at {asked}; moved to {timing['start']}")
                            for w in result["warnings"]), result["warnings"])


class ElevationUnknownTests(unittest.TestCase):
    def unknown(self, *a, **k):
        return {"available": False, "source": "Elevation", "error": "all providers failed"}

    def test_mountainous_terrain_is_not_approved_without_elevation(self):
        with Offline(overpass=None, nominatim=GC_NOMINATIM), patch("backend.agents.get_elevation_gain", self.unknown):
            result = agents.make_plan("A 10 km hike at the Grand Canyon tomorrow", 42.2411, -83.6130,
                                      form={"date": DATE})
        self.assertFalse(result["checker"]["approved"])
        self.assertIsNone(result["trail"]["elevation_gain_m"])
        self.assertTrue(any("in mountainous terrain (nearby trails span" in w for w in result["warnings"]),
                        result["warnings"])
        self.assertIn(agents.ELEVATION_UNKNOWN, result["warnings"])

    def test_flat_terrain_is_approved_with_a_warning(self):
        with Offline(overpass=None, nominatim={}), patch("backend.agents.get_elevation_gain", self.unknown):
            result = agents.make_plan("Easy 3 mile walk near Ypsilanti this Saturday morning", 42.2411, -83.6130,
                                      form={"date": DATE})
        self.assertTrue(result["checker"]["approved"])
        self.assertIn(agents.ELEVATION_UNKNOWN, result["warnings"])


class SeedElevationCacheTests(unittest.TestCase):
    def test_every_seeded_trail_has_a_cached_srtm_profile(self):
        for path in trails.SEED_DIR.glob("*.json"):
            for t in trails._parse_anywhere(json.loads(path.read_text(encoding="utf-8"))["payload"]):
                entry = elevation.cached_entry(t["coords"], t["osm_id"])
                self.assertIsNotNone(entry, (path.name, t["name"]))
                self.assertEqual(entry["source"], "NASA SRTM via OpenTopoData")
                self.assertTrue(entry["profile"])


if __name__ == "__main__":
    unittest.main()
