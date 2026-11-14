"""Offline checks: python -m unittest backend.tools.test_trails."""

import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from fastapi import HTTPException

from backend.main import PlanRequest, plan
from backend.tools import trails


class TrailTests(unittest.TestCase):
    def setUp(self):
        trails._cache.clear()

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
        fetch.return_value = io.StringIO(json.dumps({"elements": elements}))
        result = trails.get_trails(42, -83, 15)
        self.assertEqual([item["name"] for item in result], ["Named path", "Hiking route"])
        self.assertAlmostEqual(result[1]["distance_km"], 4.448, places=3)
        self.assertEqual(result[1]["coords"], [[42, -83], [42.02, -83], [42.04, -83]])
        result[0]["name"] = "Changed"
        self.assertEqual(trails.get_trails(42, -83, 15)[0]["name"], "Named path")
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.kwargs["timeout"], 15)

    @patch.object(trails, "urlopen")
    def test_upstream_failure_becomes_502_and_is_not_cached(self, fetch):
        fetch.side_effect = HTTPError(trails.OVERPASS_URL, 429, "Rate limited", {}, None)
        with self.assertRaises(HTTPException) as caught:
            plan(PlanRequest(request="A hike", lat=42, lon=-83))
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn("429", caught.exception.detail)
        self.assertFalse(trails._cache)

    @patch("backend.main.get_trails")
    def test_plan_contract_and_default_radius(self, discover):
        discover.return_value = [{"name": "Real trail", "osm_id": 1, "distance_km": 2,
                                  "coords": [[42, -83], [42.02, -83]]}]
        result = plan(PlanRequest(request="A hike", lat=42, lon=-83))
        discover.assert_called_once_with(42, -83, 15)
        from backend.main import ROOT
        expected = json.loads((ROOT / "mock_plan.json").read_text())
        self.assertEqual(result.keys(), expected.keys())
        self.assertEqual(result["trail"].keys(), expected["trail"].keys())
        self.assertEqual(result["request"], "A hike")
        self.assertEqual(result["trail"]["name"], "Real trail")


if __name__ == "__main__":
    unittest.main()
