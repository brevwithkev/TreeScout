"""Offline checks for planning anywhere (saved Nominatim/Overpass fixtures, no network, Gemini off):
python -m unittest backend.test_anywhere

The Ypsilanti plans must stay exactly as they were before "anywhere": tests/fixtures/ypsilanti_baseline.json was
written by `python -m backend.test_anywhere baseline` on the code before the change.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import agents
from backend.test_agents import FAKE_TOOLS
from backend.tools import common, geocode, trails

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
BASELINE = FIXTURES / "ypsilanti_baseline.json"
DATE = "2026-11-20"
YPSI_PROMPTS = [
    "Easy 3 mile walk near Ypsilanti this Saturday morning",
    "4 miles with hills near Ypsilanti starting at 3:30 PM tomorrow",
    "Dog-friendly loop under 5 km near Ypsilanti",
    "10 miles with hills near Ypsilanti starting at 2 PM tomorrow",
    "A 5 km walk tomorrow",  # no place: the request's own lat/lon (Ypsilanti)
]
# Network tools replaced by fakes; trails come from the real code with saved Overpass responses.
OFFLINE_TOOLS = {k: v for k, v in FAKE_TOOLS.items() if k != "_safe_trails"}


def fake_gain(coords, osm_id=None):
    return {"source": "test", "elevation_gain_m": (int(osm_id or 0) % 47) + 5, "checked_at": "t"}


class Offline:
    """Context manager: Gemini off, fake weather/NASA tools, temp caches, Overpass answered from fixtures."""

    def __init__(self, overpass=None, nominatim=None):
        self.overpass = overpass    # callable(query) -> payload or None (None: Overpass down)
        self.nominatim = nominatim  # {query: response list}; any other query fails the test (no network)
        self.queries, self.places = [], []

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.patches = [patch.dict("os.environ", {"GEMINI_API_KEY": ""}),
                        patch.object(common, "CACHE_DIR", tmp / "tool_cache"),
                        patch.object(trails, "DISK_CACHE_DIR", tmp / "overpass_cache"),
                        patch("backend.agents.get_elevation_gain", fake_gain)]
        self.patches += [patch(f"backend.agents.{name}", fake) for name, fake in OFFLINE_TOOLS.items()]
        self.patches.append(patch.object(trails, "_query_hedged", self._hedged))
        self.patches.append(patch.object(trails, "SNAPSHOT_DIR", tmp / "trail_snapshots"))
        self.patches.append(patch.object(geocode, "get_json_within", self._nominatim))
        self.patches.append(patch.object(geocode, "MIN_INTERVAL_S", 0))
        trails._cache.clear()
        for p in self.patches:
            p.start()
        return self

    def _hedged(self, query, failures, budget=None, parse=None):
        self.queries.append(query)
        payload = self.overpass(query) if self.overpass else None
        if payload is None:
            failures.append("test: Overpass down")
            return None
        return "test", payload, (parse or trails._parse_trails)(payload)

    def _nominatim(self, seconds, url, params=None, headers=None):
        self.places.append(params["q"])
        if self.nominatim is None or params["q"] not in self.nominatim:
            raise AssertionError(f"unexpected Nominatim query {params['q']!r}")
        return json.loads(json.dumps(self.nominatim[params["q"]]))

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()
        trails._cache.clear()
        self.tmp.cleanup()


def summary(plan):
    """The parts of a plan that must not change for Ypsilanti."""
    trail = plan.get("trail") or {}
    return {
        "trail": {k: trail.get(k) for k in ("name", "osm_id", "distance_km", "elevation_gain_m")},
        "coords": len(trail.get("coords") or []),
        "alternatives": plan.get("alternatives"),
        "constraints": plan.get("constraints"),
        "timing": plan.get("timing"),
        "explanation": plan.get("explanation"),
        "warnings": plan.get("warnings"),
        "narration": [n["text"] for n in plan.get("narration") or []],
        "checker": plan.get("checker"),
    }


def ypsilanti_plans():
    """Ypsilanti trails come from the committed fixture (Overpass down, empty disk cache)."""
    out = {}
    with Offline(overpass=None):
        for prompt in YPSI_PROMPTS:
            out[prompt] = summary(agents.make_plan(prompt, 42.2411, -83.6130, form={"date": DATE}))
    return out


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


NOMINATIM = {"Grand Canyon South Rim": fixture("nominatim_grand_canyon.json"),
             "Banff, Alberta": fixture("nominatim_banff.json"),
             "Antarctica": fixture("nominatim_antarctica.json"),
             "Xyzzyville": []}
CENTERS = {slug: (float(NOMINATIM[q][0]["lat"]), float(NOMINATIM[q][0]["lon"]))
           for slug, q in (("grand_canyon", "Grand Canyon South Rim"), ("banff", "Banff, Alberta"),
                           ("antarctica", "Antarctica"))}


def bounds(slug, radius):
    lat, lon = CENTERS[slug]
    return ",".join(map(str, trails._bbox(round(lat, 4), round(lon, 4), radius)))


def saved_overpass(overrides=None):
    """Overpass answered from the saved fixtures, by the query's bounding box (10 or 20 km)."""
    answers = {}
    for slug, radii in (("grand_canyon", (10, 20)), ("banff", (10,)), ("antarctica", (10, 20))):
        for r in radii:
            answers[bounds(slug, r)] = fixture(f"overpass_{slug}_{r}km.json")
    answers.update(overrides or {})

    def answer(query):
        for box, payload in answers.items():
            if f"({box});" in query:
                return payload
        raise AssertionError(f"unexpected Overpass query {query[:120]}")
    return answer


def plan(text, **offline):
    return agents.make_plan(text, 42.2411, -83.6130, form={"date": DATE})


def summaries(result):
    return [e["summary"] for e in result["agent_trace"]]


def local_sunset(lat, lon, tz):
    from datetime import date
    from zoneinfo import ZoneInfo
    from astral import Observer
    from astral.sun import sunset
    return sunset(Observer(latitude=lat, longitude=lon), date=date.fromisoformat(DATE), tzinfo=ZoneInfo(tz))


class AnywhereTests(unittest.TestCase):
    def test_grand_canyon(self):
        with Offline(overpass=saved_overpass(), nominatim=NOMINATIM) as off:
            result = plan("5 km hike near Grand Canyon South Rim tomorrow")
            self.assertEqual(off.places, ["Grand Canyon South Rim"])
            self.assertIn("Nominatim: Grand Canyon South Rim -> 36.0255, -113.8321 "
                          "(Grand Canyon South Rim, Mohave County, Arizona, United States)", summaries(result))
            self.assertIn("within 10 km", " ".join(summaries(result)))      # 4 trails at 10 km: no widening
            self.assertEqual(len(off.queries), 1)
            trail = result["trail"]
            self.assertIsNotNone(trail)
            self.assertLess(trails.haversine(trail["coords"][0], CENTERS["grand_canyon"]), 15)
            # Arizona (America/Phoenix, no DST): sunset is Phoenix local time, not Detroit's.
            expected = local_sunset(*trail["coords"][0], "America/Phoenix").strftime("%H:%M")  # at the trailhead
            self.assertEqual(result["timing"]["sunset"], expected)
            self.assertEqual(result["seasonal"]["items"], [])                  # deer season is Michigan only
            again = plan("5 km hike near Grand Canyon South Rim tomorrow")    # snapshot + cached place: no network
            self.assertEqual(len(off.queries), 1)
            self.assertEqual(off.places, ["Grand Canyon South Rim"])
            self.assertEqual(again["trail"]["osm_id"], trail["osm_id"])

    def test_banff_with_a_comma_and_local_time(self):
        with Offline(overpass=saved_overpass(), nominatim=NOMINATIM) as off:
            result = plan("Easy 6 km hike near Banff, Alberta tomorrow")
        self.assertEqual(off.places, ["Banff, Alberta"])
        self.assertIsNotNone(result["trail"])
        self.assertLess(trails.haversine(result["trail"]["coords"][0], CENTERS["banff"]), 15)
        expected = local_sunset(*result["trail"]["coords"][0], "America/Edmonton").strftime("%H:%M")
        self.assertEqual(result["timing"]["sunset"], expected)
        self.assertNotEqual(expected, local_sunset(*CENTERS["banff"], "America/Detroit").strftime("%H:%M"))
        self.assertEqual(result["seasonal"]["items"], [])

    def test_antarctica_has_no_trails(self):
        with Offline(overpass=saved_overpass(), nominatim=NOMINATIM) as off:
            result = plan("A hike in Antarctica tomorrow")
            self.assertEqual(len(off.queries), 2)                              # 10 km found 0: widened to 20 km
            self.assertIn(f"({bounds('antarctica', 20)});", off.queries[1])
            self.assertIsNone(result["trail"])
            self.assertEqual(result["warnings"], ["No mapped trails found near Antarctica"])
            self.assertEqual(result["narration"], [])
            plan("A hike in Antarctica tomorrow")                              # the empty answer was saved too
            self.assertEqual(len(off.queries), 2)

    def test_widens_to_20_km_once_when_under_3_trails(self):
        sparse = fixture("overpass_grand_canyon_10km.json")
        sparse = {**sparse, "elements": sparse["elements"][:2]}
        with Offline(overpass=saved_overpass({bounds("grand_canyon", 10): sparse}), nominatim=NOMINATIM) as off:
            result = plan("5 km hike near Grand Canyon South Rim tomorrow")
        self.assertEqual(len(off.queries), 2)
        self.assertIn("within 20 km", " ".join(summaries(result)))
        self.assertIsNotNone(result["trail"])

    def test_overpass_down_without_a_snapshot(self):
        with Offline(overpass=None, nominatim=NOMINATIM):
            result = plan("Easy 6 km hike near Banff, Alberta tomorrow")
        self.assertIsNone(result["trail"])
        self.assertEqual(result["warnings"], ["Trail data unavailable right now; try again"])

    def test_place_not_found(self):
        with Offline(overpass=saved_overpass(), nominatim=NOMINATIM) as off:
            result = plan("A walk near Xyzzyville tomorrow")
        self.assertEqual(off.queries, [])
        self.assertIsNone(result["trail"])
        self.assertEqual(result["warnings"], ["Couldn't find Xyzzyville"])

    def test_ypsilanti_needs_no_lookup(self):
        with Offline(overpass=None, nominatim={}) as off:   # any Nominatim query would fail the test
            result = plan("Easy 3 mile walk near Ypsilanti this Saturday morning")
        self.assertEqual(off.places, [])
        self.assertEqual(result["trail"]["name"], "Yellow Trail (Central Loop)")

    def test_gemini_tool_calls_use_place_names_not_coordinates(self):
        with Offline(overpass=saved_overpass(), nominatim=NOMINATIM):
            ctx = agents.PlanContext("hike", 42.2411, -83.6130, 15, {}, agents.ThreadPoolExecutor(2))
            future = ctx.dispatch("get_trails", {"place": "Banff, Alberta", "lat": 0.0, "lon": 0.0})
            found = ctx.for_llm("get_trails", future)
            self.assertGreater(found["count"], 3)
            self.assertEqual(ctx.area, (round(CENTERS["banff"][0], 4), round(CENTERS["banff"][1], 4)))
            missing = ctx.dispatch("get_trails", {"place": "Xyzzyville"}).result()
            self.assertEqual(missing, {"error": "Couldn't find Xyzzyville"})
        names = [d.name for d in agents._declarations(__import__("google.genai", fromlist=["types"]).types)
                 [0].function_declarations]
        self.assertIn("get_trails", names)
        for name, _desc, props, _req in agents.PLANNER_TOOLS:
            self.assertFalse({"lat", "lon", "center_lat", "center_lon", "area_lat", "area_lon"} & set(props), name)


class GeocodeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.calls = []
        geocode._last_request[0] = 0.0  # no earlier request in this test

        def fetch(seconds, url, params=None, headers=None):
            self.calls.append((url, params, headers))
            return [] if params["q"] == "Nowhere" else fixture("nominatim_banff.json")
        for p in (patch.object(common, "CACHE_DIR", Path(tmp.name)), patch.object(geocode, "get_json_within", fetch),
                  patch.object(geocode, "MIN_INTERVAL_S", 0)):
            p.start()
            self.addCleanup(p.stop)

    def test_request_cache_and_not_found(self):
        found = geocode.geocode("  Banff,  Alberta ")
        self.assertEqual((found["found"], found["lat"], found["lon"]), (True, 51.175076, -115.5720773))
        url, params, headers = self.calls[0]
        self.assertEqual(url, "https://nominatim.openstreetmap.org/search")
        self.assertEqual(params, {"q": "Banff, Alberta", "format": "json", "limit": 1})
        self.assertEqual(headers["User-Agent"], common.USER_AGENT)
        self.assertEqual(geocode.geocode("banff, alberta"), found)            # cached forever, case-insensitive
        self.assertEqual(len(self.calls), 1)
        self.assertIs(geocode.geocode("Nowhere")["found"], False)
        self.assertEqual(len(self.calls), 2)

    def test_at_most_one_request_per_second(self):
        with patch.object(geocode.time, "sleep") as sleep, patch.object(geocode, "MIN_INTERVAL_S", 1.0):
            geocode.geocode("Banff, Alberta")
            geocode.geocode("Nowhere")
        self.assertEqual(sleep.call_count, 1)
        self.assertGreater(sleep.call_args[0][0], 0.9)

    def test_failure_is_unavailable_and_not_cached(self):
        with patch.object(geocode, "get_json_within", side_effect=TimeoutError("no answer within 6 s")):
            self.assertIs(geocode.geocode("Banff, Alberta")["available"], False)
        self.assertTrue(geocode.geocode("Banff, Alberta")["found"])


class MountainPathAndTimezoneTests(unittest.TestCase):
    WAY = {"type": "way", "id": 7, "tags": {"highway": "path", "sac_scale": "demanding_mountain_hiking"},
           "geometry": [{"lat": 51.0, "lon": -115.0}, {"lat": 51.01, "lon": -115.0}]}

    def test_unnamed_sac_scale_path_is_kept_anywhere_only(self):
        self.assertEqual([t["name"] for t in trails._parse_anywhere({"elements": [self.WAY]})],
                         ["Unnamed mountain path (T3)"])
        self.assertEqual(trails._parse_trails({"elements": [self.WAY]}), [])   # the Ypsilanti path is unchanged

    def test_query_includes_tracks_and_sac_scale_paths(self):
        query = trails._anywhere_query("1,2,3,4")
        self.assertIn('way["highway"="track"]["name"](1,2,3,4);', query)
        self.assertIn('way["highway"="path"]["sac_scale"](1,2,3,4);', query)

    def test_timezones(self):
        self.assertEqual(str(common.timezone_for(42.2411, -83.6130)), "America/Detroit")
        self.assertEqual(str(common.timezone_for(*CENTERS["grand_canyon"])), "America/Phoenix")
        self.assertEqual(str(common.timezone_for(*CENTERS["banff"])), "America/Edmonton")
        self.assertTrue(str(common.timezone_for(*CENTERS["antarctica"])))     # some zone, never a crash
        self.assertEqual(str(common.timezone_for("x", None)), "America/Detroit")


class YpsilantiUnchangedTests(unittest.TestCase):
    def test_same_plans_as_before(self):
        expected = json.loads(BASELINE.read_text(encoding="utf-8"))
        actual = ypsilanti_plans()
        for prompt in YPSI_PROMPTS:
            self.assertEqual(actual[prompt], expected[prompt], prompt)


if __name__ == "__main__":
    if sys.argv[1:] == ["baseline"]:
        BASELINE.write_text(json.dumps(ypsilanti_plans(), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {BASELINE}")
    else:
        unittest.main()
