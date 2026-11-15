"""Offline checks for elevation: fallback provider and the permanent disk cache.
python -m unittest backend.tools.test_elevation"""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from backend.tools import elevation

COORDS = [[42.24, -83.61], [42.25, -83.62], [42.26, -83.63]]
HEIGHTS = [220.0, 230.0, 225.0]  # gain = 10 m


class Provider:
    def __init__(self, heights=None, error=None):
        self.heights, self.error, self.calls = heights, error, 0

    def __call__(self, points):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.heights)


def rate_limited():
    body = io.BytesIO(json.dumps({"error": True, "reason": "Daily API request limit exceeded."}).encode())
    return HTTPError("https://api.open-meteo.com/v1/elevation", 429, "Too Many Requests", {}, body)


class ElevationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache_file = Path(tmp.name) / "elevation_cache.json"
        for p in (patch.object(elevation, "CACHE_FILE", self.cache_file), patch.object(elevation, "_cache", None)):
            p.start()
            self.addCleanup(p.stop)

    def use(self, meteo, open_elevation):
        p = patch.object(elevation, "PROVIDERS", [("Open-Meteo Elevation", meteo), ("Open-Elevation", open_elevation)])
        p.start()
        self.addCleanup(p.stop)

    def test_open_meteo_first(self):
        meteo, oe = Provider(HEIGHTS), Provider(HEIGHTS)
        self.use(meteo, oe)
        result = elevation.get_elevation_gain(COORDS, 101)
        self.assertEqual((result["elevation_gain_m"], result["source"]), (10, "Open-Meteo Elevation"))
        self.assertEqual((meteo.calls, oe.calls), (1, 0))

    def test_falls_back_to_open_elevation_when_rate_limited(self):
        meteo, oe = Provider(error=rate_limited()), Provider(HEIGHTS)
        self.use(meteo, oe)
        result = elevation.get_elevation_gain(COORDS, 101)
        self.assertEqual((result["elevation_gain_m"], result["source"]), (10, "Open-Elevation"))
        self.assertEqual((meteo.calls, oe.calls), (1, 1))  # no retry of the 429

    def test_cache_is_permanent_and_keyed_by_osm_id(self):
        meteo, oe = Provider(HEIGHTS), Provider(HEIGHTS)
        self.use(meteo, oe)
        elevation.get_elevation_gain(COORDS, 101)
        saved = json.loads(self.cache_file.read_text(encoding="utf-8"))
        self.assertEqual(list(saved.values()), [{"source": "Open-Meteo Elevation", "elevation_gain_m": 10,
                                                 "points_sampled": 3}])
        self.assertTrue(next(iter(saved)).startswith("101:"))
        elevation._cache = None                                # a fresh process reads the file
        again = elevation.get_elevation_gain(COORDS, 101)
        self.assertEqual((again["elevation_gain_m"], again["source"], meteo.calls), (10, "Open-Meteo Elevation", 1))
        elevation.get_elevation_gain(COORDS, 202)              # same geometry, other trail id: separate entry
        self.assertEqual(meteo.calls, 2)

    def test_both_fail_is_unavailable_and_not_cached(self):
        self.use(Provider(error=rate_limited()), Provider(error=TimeoutError("timed out")))
        result = elevation.get_elevation_gain(COORDS, 101)
        self.assertIs(result["available"], False)
        self.assertIn("Open-Meteo Elevation: HTTP 429 (Daily API request limit exceeded.)", result["error"])
        self.assertIn("Open-Elevation: TimeoutError: timed out", result["error"])
        self.assertFalse(self.cache_file.exists())

    def test_precompute_fills_the_cache_from_the_fixture(self):
        fixture = Path(self.cache_file.parent) / "fixture.json"
        fixture.write_text(json.dumps({"elements": [
            {"type": "way", "id": 7, "tags": {"name": "Long path"},
             "geometry": [{"lat": 42.0, "lon": -83.0}, {"lat": 42.02, "lon": -83.0}]}]}), encoding="utf-8")
        self.use(Provider(error=rate_limited()), Provider([200.0, 205.0]))
        with patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(elevation.precompute(fixture, pause_s=0), (1, 0))
        self.assertTrue(next(iter(json.loads(self.cache_file.read_text(encoding="utf-8")))).startswith("7:"))


if __name__ == "__main__":
    unittest.main()
