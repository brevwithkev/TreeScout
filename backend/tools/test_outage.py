"""Offline outage checks: no live Overpass traffic or invented geometry."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from backend.tools import trails
from backend import agents


class OutageTests(unittest.TestCase):
    def setUp(self):
        trails._cache.clear()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for p in (patch.object(trails, 'DISK_CACHE_DIR', Path(tmp.name) / 'overpass'),
                  patch.object(trails, 'SNAPSHOT_DIR', Path(tmp.name) / 'snapshots'),
                  patch.object(trails, '_cooldown_until', 0)):
            p.start()
            self.addCleanup(p.stop)

    def test_seed_geometry_works_without_calling_rate_limited_overpass(self):
        with patch.object(trails, 'urlopen', side_effect=HTTPError('url', 429, 'limited', {}, None)) as fetch:
            items, radius, snapshot = trails.get_trails_anywhere(37.2982, -113.0263)
        self.assertTrue(items)
        self.assertTrue(all(t['coords'] and t['osm_id'] for t in items))
        self.assertEqual(snapshot['kind'], 'seeded')
        self.assertIn('Zion', snapshot['name'])
        fetch.assert_not_called()

    def test_home_fixture_is_used_before_live_request_and_has_capture_time(self):
        with patch.object(trails, 'urlopen', side_effect=AssertionError('No live call')):
            items = trails.get_trails(42.2411, -83.6130, 15)
        self.assertGreater(len(items), 5)
        self.assertTrue(items[0]['captured_at'])
        summary = agents._summarize('get_trails', {'items': items}, [42.2411, -83.6130, 15])
        self.assertIn('closures not checked', summary)

    def test_429_respects_retry_after_and_never_cycles_mirrors(self):
        error = HTTPError('url', 429, 'limited', {'Retry-After': '180'}, None)
        with patch.object(trails, 'urlopen', side_effect=error) as fetch, patch.object(trails, 'monotonic', return_value=100):
            for _ in range(2):
                with self.assertRaises(trails.OverpassError):
                    trails.get_trails(0, 0, 10)
            self.assertEqual(fetch.call_count, 1)
            self.assertGreaterEqual(trails._cooldown_until, 280)

    def test_http_date_retry_after(self):
        error = HTTPError('url', 429, 'limited', {'Retry-After': 'Wed, 21 Oct 2037 07:28:00 GMT'}, None)
        with patch.object(trails, 'urlopen', side_effect=error), patch.object(trails, 'monotonic', return_value=100):
            failures = []
            self.assertIsNone(trails._query_hedged('query', failures))
            self.assertGreater(trails._cooldown_until, 160)

    def test_unrelated_seed_is_rejected_and_no_cache_is_unavailable(self):
        with patch.object(trails, 'urlopen', side_effect=HTTPError('url', 429, 'limited', {}, None)) as fetch:
            result = agents._safe_trails_anywhere(0, 0)
        self.assertFalse(result['available'])
        self.assertIn('no saved snapshot', result['error'])
        self.assertNotIn('items', result)
        self.assertEqual(fetch.call_count, 1)

    def test_expired_bbox_cache_is_still_reused_without_overpass(self):
        lat, lon, radius = 42, -83, 15
        import hashlib
        bounds = ','.join(map(str, trails._bbox(lat, lon, radius)))
        path = trails.DISK_CACHE_DIR / (hashlib.sha256(bounds.encode()).hexdigest() + '.json')
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'elements': [{'type': 'way', 'id': 1, 'tags': {'name': 'Real OSM path'},
                                                'geometry': [{'lat': 42, 'lon': -83}, {'lat': 42.02, 'lon': -83}]}]}))
        os.utime(path, (1, 1))
        with patch.object(trails, 'urlopen', side_effect=AssertionError('No live call')):
            items = trails.get_trails(lat, lon, radius)
        self.assertEqual(items[0]['name'], 'Real OSM path')
        self.assertTrue(items[0]['captured_at'].startswith('1970'))
