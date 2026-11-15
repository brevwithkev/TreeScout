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
HEIGHTS = [200.0, 250.0, 300.0]  # a steady 100 m climb (above the 10 m smoothing threshold)


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
        self.assertEqual((result["elevation_gain_m"], result["source"]), (100, "Open-Meteo Elevation"))
        self.assertEqual((meteo.calls, oe.calls), (1, 0))

    def test_falls_back_to_open_elevation_when_rate_limited(self):
        meteo, oe = Provider(error=rate_limited()), Provider(HEIGHTS)
        self.use(meteo, oe)
        result = elevation.get_elevation_gain(COORDS, 101)
        self.assertEqual((result["elevation_gain_m"], result["source"]), (100, "Open-Elevation"))
        self.assertEqual((meteo.calls, oe.calls), (1, 1))  # no retry of the 429

    def test_cache_is_permanent_and_keyed_by_osm_id(self):
        meteo, oe = Provider(HEIGHTS), Provider(HEIGHTS)
        self.use(meteo, oe)
        elevation.get_elevation_gain(COORDS, 101)
        saved = json.loads(self.cache_file.read_text(encoding="utf-8"))
        entry = next(iter(saved.values()))
        self.assertEqual({k: entry[k] for k in ("source", "elevation_gain_m", "elevation_loss_m", "min_m", "max_m",
                                                "points_sampled")},
                         {"source": "Open-Meteo Elevation", "elevation_gain_m": 100, "elevation_loss_m": 0,
                          "min_m": 200, "max_m": 300, "points_sampled": 3})
        self.assertEqual([h for _km, h in entry["profile"]], HEIGHTS)
        self.assertEqual(entry["profile"][0][0], 0.0)
        self.assertAlmostEqual(entry["profile"][-1][0], 2.8, delta=0.1)   # km along the trail
        self.assertTrue(next(iter(saved)).startswith("101:"))
        elevation._cache = None                                # a fresh process reads the file
        again = elevation.get_elevation_gain(COORDS, 101)
        self.assertEqual((again["elevation_gain_m"], again["source"], meteo.calls), (100, "Open-Meteo Elevation", 1))
        elevation.get_elevation_gain(COORDS, 202)              # same geometry, other trail id: separate entry
        self.assertEqual(meteo.calls, 2)

    def test_srtm_is_the_first_provider(self):
        self.assertEqual([name for name, _ in elevation.PROVIDERS],
                         ["NASA SRTM via OpenTopoData", "Open-Meteo Elevation", "Open-Elevation"])
        sent = []

        def fake_get_json(url, params=None, headers=None, timeout=10):
            sent.append((url, params))
            return {"status": "OK", "results": [{"elevation": h} for h in HEIGHTS]}
        with patch.object(elevation, "get_json", fake_get_json), patch.object(elevation.time, "sleep") as sleep,                 patch.object(elevation, "SRTM_MIN_INTERVAL_S", 1.0):
            elevation._srtm_last[0] = 0.0
            self.assertEqual(elevation._srtm(COORDS), HEIGHTS)
            elevation._srtm(COORDS)
        self.assertEqual(sent[0][0], "https://api.opentopodata.org/v1/srtm30m")
        self.assertEqual(sent[0][1], {"locations": "42.24000,-83.61000|42.25000,-83.62000|42.26000,-83.63000"})
        self.assertEqual(sleep.call_count, 1)                              # at most 1 request per second
        self.assertGreater(sleep.call_args[0][0], 0.9)

    def test_srtm_error_status_moves_to_the_next_provider(self):
        with patch.object(elevation, "get_json", lambda *a, **k: {"status": "INVALID_REQUEST", "error": "x"}),                 patch.object(elevation, "SRTM_MIN_INTERVAL_S", 0):
            with self.assertRaises(ValueError):
                elevation._srtm(COORDS)

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
