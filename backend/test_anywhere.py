"""Offline checks for planning anywhere (saved Nominatim/Overpass fixtures, no network, Gemini off):
python -m unittest backend.test_anywhere

The Ypsilanti plans are pinned in tests/fixtures/ypsilanti_baseline.json, written by
`python -m backend.test_anywhere baseline`: first on the code before "anywhere" (same location and trails), then
regenerated on 2026-11-15 when linear trails became out-and-back routes (ranked by the distance walked).
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
        with Offline(overpass=None, nominatim=NOMINATIM):   # the South Rim cliff is far from any seeded snapshot
            result = plan("5 km hike near Grand Canyon South Rim tomorrow")
        self.assertIsNone(result["trail"])
        self.assertEqual(result["warnings"],
                         ["Trail data for Grand Canyon South Rim is unavailable right now; try again"])

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
        self.assertLess(trails.haversine(result["trail"]["coords"][0], [42.2411, -83.6130]), 15)

    def test_gemini_tool_calls_use_place_names_not_coordinates(self):
        with Offline(overpass=saved_overpass(), nominatim=NOMINATIM):
            ctx = agents.PlanContext("hike near banff or xyzzyville", 42.2411, -83.6130, 15, {}, agents.ThreadPoolExecutor(2))
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
        self.assertEqual(params, {"q": "Banff, Alberta", "format": "json", "limit": 5, "addressdetails": 1})
        self.assertEqual(headers["User-Agent"], common.USER_AGENT)
        again = geocode.geocode("banff, alberta")                              # cached forever, case-insensitive
        self.assertEqual((again["lat"], again["lon"], again["country"]), (found["lat"], found["lon"], found["country"]))
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


GC_VILLAGE = (36.0545, -112.1406)   # Grand Canyon Village (Nominatim place=village)
GC_FAMOUS = ("Bright Angel", "South Kaibab")


def gc_overpass():
    """Overpass answered from the saved Grand Canyon fixtures (village and canyon points, 10 km)."""
    answers = {}
    for nominatim, slug in (("nominatim_grand_canyon_village_5.json", "gc_village"),
                            ("nominatim_grand_canyon_5.json", "gc_valley")):
        hit = geocode.pick_best(fixture(nominatim))
        box = ",".join(map(str, trails._bbox(round(float(hit["lat"]), 4), round(float(hit["lon"]), 4), 10)))
        answers[box] = fixture(f"overpass_{slug}_10km.json")

    def answer(query):
        for box, payload in answers.items():
            if f"({box});" in query:
                return payload
        raise AssertionError(f"unexpected Overpass query {query[:120]}")
    return answer


GC_NOMINATIM = {"Grand Canyon Village, Arizona, USA": fixture("nominatim_grand_canyon_village_5.json"),
                "Grand Canyon": fixture("nominatim_grand_canyon_5.json")}


class GeocodeQualityTests(unittest.TestCase):
    def test_prefers_the_village_over_a_census_boundary(self):
        hits = fixture("nominatim_grand_canyon_village_5.json")
        self.assertEqual([(h["class"], h["type"]) for h in hits], [("boundary", "census"), ("place", "village")])
        best = geocode.pick_best(hits)
        self.assertEqual((best["class"], best["type"]), ("place", "village"))

    def test_settlement_boundary_counts_as_a_town(self):
        best = geocode.pick_best(fixture("nominatim_banff_5.json"))
        self.assertEqual((best["class"], best.get("addresstype")), ("boundary", "town"))
        self.assertEqual(geocode.rank(best), 0)

    def test_natural_features_rank_last(self):
        cliff = {"class": "natural", "type": "cliff"}
        trailhead = {"class": "highway", "type": "trailhead"}
        park = {"class": "boundary", "type": "national_park"}
        other = {"class": "tourism", "type": "viewpoint"}
        self.assertIs(geocode.pick_best([cliff, other, park]), park)
        self.assertIs(geocode.pick_best([cliff, trailhead]), trailhead)
        self.assertIs(geocode.pick_best([cliff, other]), other)
        self.assertIs(geocode.pick_best([cliff]), cliff)   # only natural features: the first one

    def test_only_natural_results_keep_nominatims_first(self):
        hits = fixture("nominatim_grand_canyon_5.json")   # "Grand Canyon": five canyons/gorges worldwide
        self.assertTrue(all(h["class"] == "natural" for h in hits))
        best = geocode.pick_best(hits)
        self.assertEqual(best["display_name"], "Grand Canyon, Coconino County, Arizona, United States")

    def test_prompt_asks_for_the_most_specific_hikeable_place(self):
        prompt = agents.PLANNER_PROMPT.format(top_n=3, today="2026-11-15", weekday="Sunday", lat=42.0, lon=-83.0,
                                              form="{}")
        self.assertIn("most specific hikeable place for the request, with region and country", prompt)
        self.assertIn('"Grand Canyon Village, Arizona, USA"', prompt)
        self.assertIn('"Banff, Alberta, Canada"', prompt)

    def gemini_plan(self, text, place):
        from google.genai import types
        from backend.test_agents import FakeGemini, FakeReply
        answer = {"chosen_osm_id": None, "explanation": "?", "constraints": {"distance_km": 8, "place": place}}
        fake = FakeGemini(lambda: FakeReply(json.dumps(answer)),
                          checker=lambda: FakeReply('{"approved": true, "problems": []}'))
        with patch("backend.agents._gemini", lambda: (fake, "gemini-test-flash", types)):
            return agents.make_plan(text, 42.2411, -83.6130, form={"date": DATE})

    def test_grand_canyon_resolves_to_the_village_and_finds_the_famous_trails(self):
        with Offline(overpass=gc_overpass(), nominatim=GC_NOMINATIM) as off:
            result = self.gemini_plan("An 8 km hike at the Grand Canyon tomorrow",
                                      "Grand Canyon Village, Arizona, USA")
            self.assertEqual(result["constraints"]["parsed_by"], "gemini")
            self.assertEqual(off.places, ["Grand Canyon", "Grand Canyon Village, Arizona, USA"])  # rules, then Gemini
            self.assertIn("Nominatim: Grand Canyon Village, Arizona, USA -> 36.0545, -112.1406 "
                          "(Grand Canyon Village, Coconino County, Arizona, United States)", summaries(result))
            self.assertLess(trails.haversine(result["trail"]["coords"][0], GC_VILLAGE), 10)
            found, _radius, snapshot = trails.get_trails_anywhere(*GC_VILLAGE)
        names = {t["name"] for t in found}
        self.assertTrue(any(f in n for n in names for f in GC_FAMOUS), sorted(names))
        self.assertEqual(snapshot["kind"], "seeded")
        self.assertEqual(off.queries, [])  # the committed seed answers before live Overpass

    def test_grand_canyon_without_gemini(self):
        # The rules pass the name as written; Nominatim returns only natural features for "Grand Canyon", so the
        # canyon itself is used (about 6 km from the village). Its 10 km search still has the famous trails.
        with Offline(overpass=gc_overpass(), nominatim=GC_NOMINATIM):
            result = plan("8 km hike near Grand Canyon tomorrow")
            self.assertLess(trails.haversine(result["trail"]["coords"][0], GC_VILLAGE), 15)
            hit = geocode.pick_best(GC_NOMINATIM["Grand Canyon"])
            found, _radius, _snap = trails.get_trails_anywhere(round(float(hit["lat"]), 4), round(float(hit["lon"]), 4))
        self.assertLess(trails.haversine((float(hit["lat"]), float(hit["lon"])), GC_VILLAGE), 8)
        self.assertTrue(any(f in t["name"] for t in found for f in GC_FAMOUS))


class TzdataTests(unittest.TestCase):
    """zoneinfo reads only the tzdata package (common.py resets TZPATH), so every machine agrees."""

    def offset(self, zone, when):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime.fromisoformat(when).replace(tzinfo=ZoneInfo(zone)).utcoffset().total_seconds() / 3600

    def test_zoneinfo_uses_the_tzdata_package(self):
        import importlib.metadata
        import zoneinfo
        self.assertEqual(zoneinfo.TZPATH, ())
        version = tuple(int(x) for x in importlib.metadata.version("tzdata").split(".")[:2])
        self.assertGreaterEqual(version, (2026, 6))

    def test_phoenix_is_utc_minus_7_with_no_dst(self):
        self.assertEqual(self.offset("America/Phoenix", "2026-11-15T12:00"), -7)
        self.assertEqual(self.offset("America/Phoenix", "2026-07-01T12:00"), -7)

    def test_edmonton_follows_tzdata(self):
        # tzdata 2026.6 has Alberta on UTC-06:00 all year (no change back to UTC-07:00 in November).
        self.assertEqual(self.offset("America/Edmonton", "2026-11-15T12:00"), -6)
        self.assertEqual(self.offset("America/Edmonton", "2026-07-01T12:00"), -6)
        self.assertEqual(self.offset("America/Detroit", "2026-11-15T12:00"), -5)   # Detroit still changes
        self.assertEqual(self.offset("America/Detroit", "2026-07-01T12:00"), -4)

    def test_banff_sunset_is_the_utc_sunset_in_local_time(self):
        from datetime import date, timedelta, timezone
        from astral import Observer
        from astral.sun import sunset
        with Offline(overpass=saved_overpass(), nominatim=NOMINATIM):
            result = plan("Easy 6 km hike near Banff, Alberta tomorrow")
        lat, lon = result["trail"]["coords"][0]
        utc = sunset(Observer(latitude=lat, longitude=lon), date=date.fromisoformat(DATE), tzinfo=timezone.utc)
        self.assertEqual(result["timing"]["sunset"], (utc - timedelta(hours=6)).strftime("%H:%M"))
        self.assertEqual(utc.strftime("%H"), "23")   # about 23:47 UTC: 16:47 by the sun's clock, 17:47 at UTC-6


# Hand-written Nominatim answer (the shape of a real one) for a place with no seeded snapshot.
MOAB = [{"place_id": 1, "lat": "38.5732", "lon": "-109.5498", "class": "place", "type": "city", "addresstype": "city",
         "display_name": "Moab, Grand County, Utah, United States", "importance": 0.5}]
# Anything from the default (Ypsilanti) location or Michigan that must never appear for a place elsewhere.
HOME_LEAKS = ("Ypsilanti", "Michigan", "Rolling Hills", "Yellow Trail", "deer", "Deer", "America/Detroit")


def no_home_data(test, result):
    text = json.dumps(result)
    for leak in HOME_LEAKS:
        test.assertNotIn(leak, text)
    for lat, lon in [p for s in ("trail",) if result.get(s) for p in result[s].get("coords", [])]:
        test.assertGreater(trails.haversine([lat, lon], [42.2411, -83.6130]), 100)


class FakeCall:
    def __init__(self, name, args):
        self.name, self.args, self.id = name, args, f"call-{name}"


class ResolvedPlaceIsKeptTests(unittest.TestCase):
    """Regression: a named place whose trails time out must never fall back to the default Ypsilanti location."""

    def gemini_then_failure(self, text, place):
        """Gemini calls get_trails(place), then its next turn fails (as when Overpass made it miss the deadline)."""
        from google.genai import types
        from backend.test_agents import FakeAPIError, FakeGemini

        class Reply:
            text, function_calls = "", [FakeCall("get_trails", {"place": place})]
        turns = iter([lambda: Reply(), lambda: (_ for _ in ()).throw(FakeAPIError(503, "UNAVAILABLE"))])
        fake = FakeGemini(lambda: next(turns)())
        with patch("backend.agents._gemini", lambda: (fake, "gemini-test-flash", types)), \
                patch("backend.agents.gemini_configured", lambda: True):
            return agents.make_plan(text, 42.2411, -83.6130, form={"date": DATE})

    def assert_unavailable_for(self, result, place):
        self.assertIsNone(result["trail"])
        self.assertEqual(result["warnings"][0], f"Trail data for {place} is unavailable right now; try again")
        self.assertFalse(result["checker"]["approved"])
        self.assertEqual(result["narration"], [])
        no_home_data(self, result)

    def test_overpass_timeout_after_gemini_resolved_the_place(self):
        with Offline(overpass=None, nominatim={"Moab, Utah, USA": MOAB}) as off:
            result = self.gemini_then_failure("I want to take a hike at the canyon near moab tomorrow",
                                              "Moab, Utah, USA")
        self.assertEqual(result["constraints"]["parsed_by"], "rules")              # Gemini failed: rules fallback
        self.assertIn("LLM unavailable, used rule-based planner", summaries(result))
        self.assertEqual(off.places, ["Moab, Utah, USA"])
        self.assert_unavailable_for(result, "Moab, Utah, USA")
        self.assertTrue(all("(42." not in q for q in off.queries))               # no Overpass query for Ypsilanti

    def test_overpass_timeout_for_a_place_the_rules_found(self):
        with Offline(overpass=None, nominatim={"Moab, Utah": MOAB}) as off:
            result = plan("I want to take a hike at Moab, Utah tomorrow")
        self.assertEqual(off.places, ["Moab, Utah"])
        self.assert_unavailable_for(result, "Moab, Utah")

    def test_nothing_location_based_runs_before_gemini_names_the_place(self):
        with Offline(overpass=None, nominatim={"Moab, Utah, USA": MOAB}) as off:
            result = self.gemini_then_failure("I want to take a hike at the canyon near moab tomorrow",
                                              "Moab, Utah, USA")
        tools = [e.get("tool") for e in result["agent_trace"] if e.get("tool")]
        first_location_tool = next(i for i, t in enumerate(tools) if t not in ("get_space_weather",))
        self.assertEqual(tools[first_location_tool], "geocode")
        self.assertEqual(len(off.queries), 1)

    def test_no_place_named_still_uses_the_request_location(self):
        with Offline(overpass=None, nominatim={}):
            result = plan("A 5 km walk tomorrow")
        ypsi = {t["osm_id"] for t in trails._parse_trails(json.loads(trails.YPSILANTI_FIXTURE.read_text("utf-8")))}
        self.assertIn(result["trail"]["osm_id"], ypsi)                           # the committed Ypsilanti snapshot

    def test_i_want_to_take_a_hike_with_gemini_inventing_a_town(self):
        """Gemini names Ann Arbor for a request that names no place: ignored, the Ypsilanti default is used."""
        with Offline(overpass=None, nominatim={}) as off:   # any Nominatim query fails the test
            result = GeocodeQualityTests.gemini_plan(None, "I want to take a hike", "Ann Arbor, Michigan, USA")
        self.assertEqual(off.places, [])
        ypsi = {t["osm_id"] for t in trails._parse_trails(json.loads(trails.YPSILANTI_FIXTURE.read_text("utf-8")))}
        self.assertIn(result["trail"]["osm_id"], ypsi)
        self.assertIn("Gemini named Ann Arbor, Michigan, USA, which the request doesn't name; using the hiker's "
                      "location", summaries(result))

    def test_mentioned(self):
        self.assertTrue(agents.mentioned("Grand Canyon Village, Arizona, USA", "a hike at the grand canyon"))
        self.assertTrue(agents.mentioned("Great Smoky Mountains National Park", "the Smokies this weekend"))
        self.assertTrue(agents.mentioned("Banff, Alberta, Canada", "hike near Banff"))
        self.assertFalse(agents.mentioned("Ann Arbor, Michigan, USA", "I want to take a hike"))
        self.assertFalse(agents.mentioned("Ypsilanti, Michigan", "a 5 km walk tomorrow"))


class SeededSnapshotTests(unittest.TestCase):
    def test_grand_canyon_with_overpass_down_uses_the_seeded_snapshot(self):
        with Offline(overpass=None, nominatim=GC_NOMINATIM) as off:
            result = plan("I want to take a hike at the Grand Canyon tomorrow")
        self.assertEqual(off.places, ["Grand Canyon"])
        self.assertEqual(off.queries, [])                                          # Overpass never asked
        self.assertTrue(any("seeded snapshot of Grand Canyon Village, Arizona, USA, OSM data from 2026-11-15" in s
                            for s in summaries(result)), summaries(result))
        names = [result["trail"]["name"]] + [a["name"] for a in result["alternatives"]]
        self.assertTrue(any(f in n for n in names for f in GC_FAMOUS), names)
        lat, lon = result["trail"]["coords"][0]
        self.assertEqual(result["timing"]["sunset"], local_sunset(lat, lon, "America/Phoenix").strftime("%H:%M"))
        self.assertEqual(str(common.timezone_for(lat, lon)), "America/Phoenix")
        no_home_data(self, result)

    def test_gemini_village_name_uses_the_seed_too(self):
        with Offline(overpass=None, nominatim=GC_NOMINATIM):
            result = GeocodeQualityTests.gemini_plan(None, "An 8 km hike at the Grand Canyon tomorrow",
                                                     "Grand Canyon Village, Arizona, USA")
        self.assertEqual(result["constraints"]["parsed_by"], "gemini")
        self.assertIsNotNone(result["trail"])
        self.assertLess(trails.haversine(result["trail"]["coords"][0], GC_VILLAGE), 10)
        no_home_data(self, result)

    def test_banff_seed_and_seed_files(self):
        found, radius, snapshot = trails.get_trails_anywhere(51.1751, -115.5721)
        self.assertEqual((radius, snapshot["kind"], snapshot["name"]), (10, "seeded", "Banff, Alberta, Canada"))
        self.assertGreaterEqual(len(found), 10)
        far, _ = trails._seed_for(36.0255, -113.8321)   # Grand Canyon South Rim cliff: 180 km from the village seed
        self.assertIsNone(far)
        for path in trails.SEED_DIR.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(data) >= {"name", "lat", "lon", "radius_km", "saved_at", "payload"}, True, path)


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
