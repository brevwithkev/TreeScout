"""Offline checks for the NASA POWER / EONET time cap and disk cache:
python -m unittest backend.tools.test_data_cache"""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.tools import climate, common, hazards

LAT, LON = 42.2411, -83.6130


def power_payload(*_args, **_kwargs):
    days = {f"20251{d}": 8.0 for d in range(112, 119)}  # 2025-11-12 .. 2025-11-18 (one window)
    return {"properties": {"parameter": {"T2M_MAX": days, "T2M_MIN": {k: 0.0 for k in days},
                                         "PRECTOTCORR": {k: 2.0 for k in days}}}}


def eonet_payload(*_args, **_kwargs):
    return {"events": [{"title": "Test fire", "categories": [{"title": "Wildfires"}], "sources": [{"url": "u"}],
                        "geometry": [{"date": "2026-11-13T00:00:00Z", "type": "Point", "coordinates": [-83.60, 42.25]}]}]}


class CountingFetch:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.calls = payload, error, 0

    def __call__(self, seconds, url, params=None, headers=None):
        self.calls += 1
        if self.error:
            raise self.error
        return self.payload()


class CacheTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = patch.object(common, "CACHE_DIR", Path(tmp.name))
        p.start()
        self.addCleanup(p.stop)
        self.cache_dir = Path(tmp.name)

    def test_limits(self):
        self.assertEqual((climate.TIMEOUT, hazards.TIMEOUT), (8, 8))
        self.assertEqual((climate.CACHE_SECONDS, hazards.CACHE_SECONDS), (6 * 3600, 6 * 3600))

    def test_power_cached_per_location_and_week(self):
        fetch = CountingFetch(power_payload)
        with patch.object(climate, "get_json_within", fetch):
            first = climate.get_climate(LAT, LON, "2026-11-15")      # Sunday, ISO week 46
            again = climate.get_climate(LAT + 0.001, LON, "2026-11-10")  # same ~1 km cell, same week
            self.assertEqual(fetch.calls, 1)
            self.assertEqual(first, again)                            # incl. the original checked_at
            self.assertEqual(first["avg_high_c"], 8.0)
            climate.get_climate(LAT, LON, "2026-11-16")               # next ISO week
            climate.get_climate(42.30, LON, "2026-11-15")             # different place
            self.assertEqual(fetch.calls, 3)

    def test_eonet_cached_per_location(self):
        fetch = CountingFetch(eonet_payload)
        with patch.object(hazards, "get_json_within", fetch):
            first = hazards.get_hazards(LAT, LON, 100)
            self.assertEqual(hazards.get_hazards(LAT, LON, 100), first)
            self.assertEqual(fetch.calls, 1)
            self.assertEqual(first["items"][0]["title"], "Test fire")
            hazards.get_hazards(LAT, LON, 50)                         # different radius
            hazards.get_hazards(42.50, -83.90, 100)                   # different place
            self.assertEqual(fetch.calls, 3)

    def test_entries_expire_after_6_hours(self):
        fetch = CountingFetch(eonet_payload)
        with patch.object(hazards, "get_json_within", fetch):
            hazards.get_hazards(LAT, LON, 100)
            old = time.time() - 6 * 3600 - 1
            for path in (self.cache_dir / "eonet").glob("*.json"):
                os.utime(path, (old, old))
            hazards.get_hazards(LAT, LON, 100)
            self.assertEqual(fetch.calls, 2)

    def test_failures_are_not_cached(self):
        failing = CountingFetch(error=OSError("boom"))
        with patch.object(climate, "get_json_within", failing):
            self.assertIs(climate.get_climate(LAT, LON, "2026-11-15")["available"], False)
        working = CountingFetch(power_payload)
        with patch.object(climate, "get_json_within", working):
            self.assertEqual(climate.get_climate(LAT, LON, "2026-11-15")["avg_high_c"], 8.0)
            self.assertEqual(working.calls, 1)


class TimeCapTests(unittest.TestCase):
    """A server that never answers: the tools give up at their deadline with available:false."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.release = threading.Event()
        self.addCleanup(self.release.set)

        def hang(*_args, **_kwargs):
            self.release.wait(30)
            raise OSError("released")

        for p in (patch.object(common, "CACHE_DIR", Path(tmp.name)), patch.object(common, "get_json", hang),
                  patch.object(climate, "TIMEOUT", 0.3), patch.object(hazards, "TIMEOUT", 0.3)):
            p.start()
            self.addCleanup(p.stop)

    def test_power_gives_up(self):
        started = time.perf_counter()
        result = climate.get_climate(LAT, LON, "2026-11-15")
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertEqual((result["available"], result["source"], result["error"]),
                         (False, "NASA POWER", "TimeoutError: no answer within 0.3 s"))

    def test_eonet_gives_up(self):
        started = time.perf_counter()
        result = hazards.get_hazards(LAT, LON, 100)
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertEqual((result["available"], result["source"], result["error"]),
                         (False, "NASA EONET", "TimeoutError: no answer within 0.3 s"))


if __name__ == "__main__":
    unittest.main()
