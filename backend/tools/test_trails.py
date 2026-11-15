"""Offline checks: python -m unittest backend.tools.test_trails."""

import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from backend.main import ROOT, PlanRequest, plan
from backend.tools import trails


class _Response(io.StringIO):
    """Fake urlopen() response; trails.py checks .status."""
    status = 200


class TrailTests(unittest.TestCase):
    def setUp(self):
        trails._cache.clear()
        # Keep test responses out of the real data/overpass_cache (a fresh disk copy skips Overpass).
        self._tmp = tempfile.TemporaryDirectory()
        patcher = patch.object(trails, "DISK_CACHE_DIR", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        # main.plan() records every successful plan; keep test plans out of the real demo_data/.
        patcher = patch("backend.demo.PLANS_DIR", Path(self._tmp.name) / "plans")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_stitch_reverses_and_prepends_segments(self):
        a, b, c, d = [0, 0], [0, 1], [0, 2], [0, 3]
        self.assertEqual(trails.stitch_segments([[b, c], [d, c], [b, a]]), [a, b, c, d])
        self.assertAlmostEqual(trails.haversine(a, b), 111.195, places=3)

    @patch.object(trails, "urlopen")
    def test_filter_distance_geometry_and_cache(self, fetch):
        points = [{"lat": 42, "lon": -83}, {"lat": 42.02, "lon": -83}]
        elements = [
            {"type": "way", "id": 1, "tags": {"name": "Named path"}, "geometry": points},
            {"type": "way", "id": 2, "tags": {}, "geometry": points},
            {"type": "way", "id": 3, "tags": {"name": "Short"}, "geometry": points[:1]},
            {"type": "relation", "id": 4, "tags": {"name": "Hiking route"}, "members": [
                {"type": "way", "geometry": points},
                {"type": "way", "geometry": [{"lat": 42.04, "lon": -83}, points[-1]]},
            ]},
        ]
        fetch.return_value = _Response(json.dumps({"elements": elements}))
        result = trails.get_trails(42, -83, 15)
        self.assertEqual([item["name"] for item in result], ["Named path", "Hiking route"])
        self.assertAlmostEqual(result[1]["distance_km"], 4.448, places=3)
        self.assertEqual(result[1]["coords"], [[42, -83], [42.02, -83], [42.04, -83]])
        result[0]["name"] = "Changed"
        self.assertEqual(trails.get_trails(42, -83, 15)[0]["name"], "Named path")
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.kwargs["timeout"], trails.TIMEOUT_SECONDS)

    @patch.object(trails, "urlopen")
    def test_upstream_failure_raises_and_is_not_cached(self, fetch):
        fetch.side_effect = HTTPError(trails.OVERPASS_URL, 429, "Rate limited", {}, None)
        with self.assertRaises(trails.OverpassError) as caught:
            trails.get_trails(42, -83, 15)
        self.assertIn("429", str(caught.exception))
        self.assertFalse(trails._cache)

    def test_hanging_mirrors_give_up_within_the_budget(self):
        self.assertEqual(trails.OVERPASS_BUDGET_SECONDS, 8)
        release = threading.Event()
        self.addCleanup(release.set)

        def hang(*_args, **_kwargs):
            release.wait(30)
            raise OSError("released")

        with patch.object(trails, "urlopen", hang), patch.object(trails, "OVERPASS_BUDGET_SECONDS", 1), \
                patch.object(trails, "HEDGE_SECONDS", 0.3):
            started = time.perf_counter()
            with self.assertRaises(trails.OverpassError) as caught:
                trails.get_trails(42, -83, 15)
            elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.5)
        self.assertIn("no answer within 1 s", str(caught.exception))

    def test_plan_builds_contract_from_tools_without_mock_values(self):
        """POST /plan, offline: fake tools, no Gemini key -> rule-based planner."""
        coords = [[42.0, -83.0], [42.01, -83.0], [42.0, -83.0]]
        fakes = {
            "_safe_trails": lambda lat, lon, r: {"source": "OSM", "items": [
                {"name": "Real trail", "osm_id": 11, "distance_km": 2.2, "coords": coords},
                {"name": "Other trail", "osm_id": 12, "distance_km": 5.0, "coords": coords[:2]}],
                "checked_at": "2026-11-14T00:00:00Z"},
            "get_weather": lambda lat, lon, d: {"source": "NWS", "summary": "Sunny, 50F", "precip_chance": 10,
                                                "temperature_f": 50, "checked_at": "x"},
            "get_climate": lambda lat, lon, d: {"available": False, "source": "NASA POWER", "error": "boom",
                                                "checked_at": "x"},
            "get_hazards": lambda lat, lon, r: {"source": "NASA EONET", "items": [], "radius_km": r, "checked_at": "t"},
            "get_space_weather": lambda: {"source": "NASA DONKI", "kp_max": None, "storms": 0,
                                          "aurora_chance": "low", "window": "w", "checked_at": "t"},
            "get_elevation_gain": lambda c, osm_id=None: {"source": "Open-Meteo", "elevation_gain_m": 12,
                                                          "checked_at": "t"},
            "get_nws_alerts": lambda lat, lon: {"source": "NWS", "items": [], "checked_at": "t"},
            "get_hike_window": lambda lat, lon, d, start, minutes: {"available": False, "source": "NWS hourly",
                                                                    "error": "boom", "checked_at": "t"},
            "get_seasonal_hazards": lambda d, lat, lon: {"source": "TrekChek seasonal calendar", "items": [],
                                                         "checked_at": "t"},
        }
        patches = [patch(f"backend.agents.{name}", fake) for name, fake in fakes.items()]
        patches.append(patch.dict("os.environ", {"GEMINI_API_KEY": ""}))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        result = plan(PlanRequest(request="2 km walk near Ypsilanti tomorrow", lat=42, lon=-83))
        expected = json.loads((ROOT / "mock_plan.json").read_text(encoding="utf-8"))
        self.assertEqual(list(result), list(expected))
        # Contract trail keys plus the optional elevation_source (card label).
        self.assertEqual(result["trail"].keys(), expected["trail"].keys() | {"elevation_source"})
        self.assertEqual(result["trail"]["elevation_source"], "Open-Meteo")
        self.assertEqual(result["trail"]["name"], "Real trail")
        self.assertEqual(result["trail"]["elevation_gain_m"], 12)
        self.assertEqual(result["constraints"]["parsed_by"], "rules")
        self.assertEqual(result["climate"], {"available": False, "source": "NASA POWER", "error": "boom"})
        self.assertEqual([a["name"] for a in result["alternatives"]], ["Other trail"])
        self.assertTrue(result["checker"]["approved"])
        text = json.dumps(result)
        for mock_value in ("Example Loop", "Acorn Nature Trail", "River Bend Path", "Found 14 named trails"):
            self.assertNotIn(mock_value, text)


if __name__ == "__main__":
    unittest.main()