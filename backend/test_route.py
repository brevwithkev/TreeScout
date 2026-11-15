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
from backend.tools import common, elevation, geocode, met, route, trails
from backend.tools.profile import climb_descent, moving_average, resample, stats

SEED = json.loads((trails.SEED_DIR / "grand_canyon_village.json").read_text(encoding="utf-8"))
BRIGHT_ANGEL = next(t for t in trails._parse_anywhere(SEED["payload"]) if t["osm_id"] == 2836803)
NOMINATIM_BANFF = {"Banff, Alberta": fixture("nominatim_banff_5.json")}
SRTM_BA = fixture("srtm_bright_angel_2836803.json")  # real OpenTopoData answer for its 100 sampled points
LINE = [[36.0, -112.0], [36.01, -112.0], [36.02, -112.0]]   # 2.2 km straight north


class RouteTests(unittest.TestCase):
    def test_loop_only_within_500_m(self):
        self.assertTrue(route.is_loop([[42.0, -83.0], [42.01, -83.0], [42.0044, -83.0]]))   # 489 m gap
        self.assertFalse(route.is_loop([[42.0, -83.0], [42.01, -83.0], [42.0046, -83.0]]))  # 511 m gap

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
        # A one-sample 50 m bump is mostly SRTM noise: the 3-point average flattens it to 16.7 m up and down.
        self.assertEqual(route.make_route({"coords": LINE, "distance_km": 2.224}, 10, bumpy)["climb_m"], 33)
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
        self.assertTrue(route.is_partial({"coords": LINE, "distance_km": 2.224}, 3.0))
        self.assertFalse(route.is_partial({"coords": LINE, "distance_km": 2.224}, 5.0))   # full out-and-back

    def test_complete_trails_beat_partial_turnarounds_unless_30_percent_further(self):
        from backend.tools.ranking import rank_trails
        long_line = {"name": "Long", "osm_id": 1, "distance_km": 10.0,
                     "coords": [[36.0, -112.0], [36.09, -112.0]]}        # partial: turned around at target/2
        def loop(km, osm_id):  # a square loop of about km, starting at the same point
            side = km / 4 / 111.0
            return {"name": f"Loop {km}", "osm_id": osm_id, "distance_km": km,
                    "coords": [[36.0, -112.0], [36.0 + side, -112.0], [36.0 + side, -112.0 + side * 1.24],
                               [36.0, -112.0 + side * 1.24], [36.0, -112.0]]}
        ranked = rank_trails([long_line, loop(6.0, 2)], {"distance_km": 8.0}, 36.0, -112.0)   # loop 25% off
        self.assertEqual(ranked[0]["name"], "Loop 6.0")
        ranked = rank_trails([long_line, loop(4.0, 3)], {"distance_km": 8.0}, 36.0, -112.0)   # loop 50% off
        self.assertEqual(ranked[0]["name"], "Long")
        self.assertTrue(ranked[0]["partial"])


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
        # Smoothed (50 m resampling, 3-point average, 10 m hysteresis): raw sums were 286 m up / 1612 m down.
        self.assertEqual((result["elevation_gain_m"], result["elevation_loss_m"]), (14, 1340))
        self.assertEqual((result["max_m"], result["min_m"]), (2084, 758))   # rim to river
        profile = result["profile"]
        net = profile[0][1] - profile[-1][1]
        self.assertAlmostEqual(result["elevation_loss_m"] - result["elevation_gain_m"], net, delta=2)  # consistent

    def test_full_out_and_back_to_the_river_from_a_1_pm_start(self):
        result = self.plan("A 30 km hike at the Grand Canyon starting at 1 PM tomorrow")
        trail, timing = result["trail"], result["timing"]
        self.assertEqual((trail["name"], trail["route"]), ("Bright Angel Trail", "out-and-back"))
        self.assertEqual(trail["distance_km"], round(2 * BRIGHT_ANGEL["distance_km"], 3))   # 25.1 km: to the river
        self.assertGreaterEqual(trail["elevation_gain_m"], trail["net_drop_m"])  # at least the net drop
        self.assertGreater(trail["net_drop_m"], 1250)                         # rim to river: about 1,330 m
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


class StrenuousTests(unittest.TestCase):
    def test_strenuous_warning(self):
        self.assertEqual(agents._strenuous({"elevation_gain_m": 1898}, {"est_duration_min": 491}),
                         "Strenuous: 1898 m of climbing, about 8.2 h; carry water and a headlamp")  # the formatter
        self.assertEqual(agents._strenuous({"elevation_gain_m": 200}, {"est_duration_min": 390}),
                         "Strenuous: 200 m of climbing, about 6.5 h; carry water and a headlamp")
        self.assertIsNone(agents._strenuous({"elevation_gain_m": 1000}, {"est_duration_min": 360}))  # not more than
        self.assertEqual(agents._strenuous({"elevation_gain_m": None}, {"est_duration_min": 400}),
                         "Strenuous: unknown climbing, about 6.7 h; carry water and a headlamp")
        self.assertIsNone(agents._strenuous(None, None))

    def test_bright_angel_is_strenuous_but_approved(self):
        result = BrightAngelTests.plan(self, "A 30 km hike at the Grand Canyon starting at 1 PM tomorrow")
        self.assertTrue(result["checker"]["approved"])
        self.assertIn(f"Strenuous: {result['trail']['elevation_gain_m']} m of climbing, about "
                      f"{result['timing']['est_duration_min'] / 60:.1f} h; carry water and a headlamp", result["warnings"])
        self.assertGreater(result["trail"]["elevation_gain_m"], 1000)
        easy = BrightAngelTests.plan(self, "A 2 km hike at the Grand Canyon tomorrow")
        self.assertFalse([w for w in easy["warnings"] if w.startswith("Strenuous")])


class PlaceTests(unittest.TestCase):
    def test_place_is_where_the_plan_is(self):
        with Offline(overpass=None, nominatim=GC_NOMINATIM):
            gc = agents.make_plan("A hike at the Grand Canyon tomorrow", 42.2411, -83.6130, form={"date": DATE})
        self.assertEqual(gc["place"], {"name": "Grand Canyon", "lat": 36.098, "lon": -112.0963,
                                       "display_name": "Grand Canyon, Coconino County, Arizona, United States",
                                       "country": "United States"})
        with Offline(overpass=None, nominatim={}):
            home = agents.make_plan("A 5 km walk tomorrow", 42.2411, -83.6130, form={"date": DATE})
        self.assertEqual(home["place"], {"name": None, "lat": 42.2411, "lon": -83.613, "display_name": None,
                                         "country": None})

    def test_place_without_a_trail_and_when_not_found(self):
        with Offline(overpass=None, nominatim={"Grand Canyon South Rim": fixture("nominatim_grand_canyon.json"),
                                               "Xyzzyville": []}):
            down = agents.make_plan("A hike near Grand Canyon South Rim tomorrow", 42.2411, -83.6130,
                                    form={"date": DATE})
            missing = agents.make_plan("A walk near Xyzzyville tomorrow", 42.2411, -83.6130, form={"date": DATE})
        self.assertIsNone(down["trail"])                                   # Overpass down, no seed nearby
        self.assertEqual(down["place"]["name"], "Grand Canyon South Rim")
        self.assertEqual((down["place"]["lat"], down["place"]["lon"], down["place"]["country"]),
                         (36.0255, -113.8321, "United States"))
        self.assertIsNone(missing["place"])

    def test_contract_example(self):
        contract = json.loads((Path(__file__).resolve().parents[1] / "mock_plan.json").read_text(encoding="utf-8"))
        self.assertEqual(set(contract["place"]), {"name", "lat", "lon", "display_name", "country"})


class SeedElevationCacheTests(unittest.TestCase):
    def test_every_seeded_trail_has_cached_elevation(self):
        # Precomputed with `python -m backend.tools.elevation precompute seeds` (SRTM); a few Ann Arbor trails were
        # already cached from the Ypsilanti snapshot by an older provider.
        for path in trails.SEED_DIR.glob("*.json"):
            for t in trails._parse_anywhere(json.loads(path.read_text(encoding="utf-8"))["payload"]):
                entry = elevation.cached_entry(t["coords"], t["osm_id"])
                self.assertIsNotNone(entry, (path.name, t["name"]))
                self.assertIsNotNone(entry.get("elevation_gain_m"), (path.name, t["name"]))


class SmoothingTests(unittest.TestCase):
    def test_bright_angel_4_km_out(self):
        entry = elevation.cached_entry(BRIGHT_ANGEL["coords"], 2836803)
        r = route.make_route(BRIGHT_ANGEL, 8.0, {**entry, "available": True}, ref=[36.0545, -112.1406])
        self.assertEqual(r["turnaround_km"], 4.0)
        self.assertTrue(400 <= r["net_drop_m"] <= 700, r["net_drop_m"])      # rim (2,084 m) down about 500 m
        self.assertGreaterEqual(r["climb_m"], r["net_drop_m"])
        self.assertLess(r["climb_m"], r["net_drop_m"] + 150)                  # switchback noise is gone

    def test_switchback_noise_no_longer_inflates_the_descent(self):
        # OSM 1181890945 (Bright Angel, 7.9 km): the raw SRTM sum was 1,283 m down; the real drop is about 950 m.
        seed = trails._parse_anywhere(SEED["payload"])
        upper = next(t for t in seed if t["osm_id"] == 1181890945)
        entry = elevation.cached_entry(upper["coords"], upper["osm_id"])
        net = entry["profile"][0][1] - entry["profile"][-1][1]
        self.assertLess(entry["elevation_loss_m"], net + 60)
        self.assertGreater(entry["elevation_loss_m"], net - 1)

    def test_every_cached_profile_is_consistent(self):
        for key, entry in elevation._load().items():
            if not entry.get("profile"):
                continue
            with self.subTest(key=key):
                p = entry["profile"]
                net_rise = p[-1][1] - p[0][1]
                self.assertAlmostEqual(entry["elevation_gain_m"] - entry["elevation_loss_m"], net_rise, delta=2)
                self.assertEqual(stats(p), {k: entry[k] for k in ("elevation_gain_m", "elevation_loss_m",
                                                                  "min_m", "max_m")})

    def test_hysteresis_and_spacing(self):
        self.assertEqual(climb_descent([100, 105, 100, 106, 100]), (0.0, 0.0))   # wiggles under 10 m
        self.assertEqual(climb_descent([100, 120, 100]), (20.0, 20.0))
        self.assertEqual([k for k, _ in resample([[0, 1], [0.02, 1], [0.06, 1], [0.08, 1], [0.2, 1]])],
                         [0, 0.06, 0.2])
        self.assertEqual(moving_average([[0, 0], [1, 30], [2, 0]]), [[0, 0], [1, 10.0], [2, 0]])


class GeocodeSanityTests(unittest.TestCase):
    def test_never_a_street_cafe_or_station(self):
        hits = fixture("nominatim3_national_rocky_mountain.json")   # a Manila street, AT shelters, a cafe
        best = geocode.pick_best(hits, "Hike in National Rocky Mountain tomorrow", "National Rocky Mountain")
        self.assertEqual((best["class"], best["type"]), ("amenity", "shelter"))
        self.assertIsNone(geocode.pick_best(fixture("nominatim3_main_street.json"), "a hike on Main Street", "Main Street"))
        street = geocode.pick_best(fixture("nominatim3_main_street.json"), "start at 315 Main Street", "315 Main Street")
        self.assertIsNotNone(street)                                             # looks like an address: allowed

    def test_parks_and_reserves_first(self):
        best = geocode.pick_best(fixture("nominatim3_rocky_mountain_np.json"), "", "Rocky Mountain National Park")
        self.assertEqual((best["class"], best["type"]), ("leisure", "nature_reserve"))
        self.assertEqual(geocode.country_of(best), ("United States", "us"))
        self.assertEqual(geocode.rank({"class": "natural", "type": "peak"}), 0)
        self.assertEqual(geocode.rank({"class": "natural", "type": "cliff"}), 2)

    def test_country_named_in_the_request(self):
        canyon = fixture("nominatim_grand_canyon_5.json")              # the Arizona canyon first
        self.assertEqual(geocode.pick_best(canyon, "hike the Grand Canyon", "Grand Canyon")["display_name"],
                         "Grand Canyon, Coconino County, Arizona, United States")
        self.assertEqual(geocode.pick_best(canyon, "hike the Grand Canyon in Australia", "Grand Canyon")["display_name"],
                         "Grand Canyon, Blackheath, New South Wales, 2785, Australia")
        self.assertIsNone(geocode.pick_best(canyon, "hike the Grand Canyon in Peru", "Grand Canyon"))
        self.assertEqual(geocode.countries_named("a hike in New Mexico", "Santa Fe, New Mexico"), set())
        self.assertEqual(geocode.countries_named("", "Banff, Alberta, Canada"), {"ca"})

    def plan(self, text, nominatim):
        with Offline(overpass=None, nominatim=nominatim):
            return agents.make_plan(text, 42.2411, -83.6130, form={"date": DATE})

    def test_messages(self):
        streets = self.plan("A hike near Main Street tomorrow", {"Main Street": fixture("nominatim3_main_street.json")})
        self.assertEqual(streets["warnings"], ["Couldn't find Main Street as a hiking area; try a park or town name"])
        self.assertIsNone(streets["trail"])
        peru = self.plan("A hike near Grand Canyon, Peru tomorrow",
                         {"Grand Canyon, Peru": fixture("nominatim_grand_canyon_5.json")})
        self.assertEqual(peru["warnings"], ["Couldn't find Grand Canyon, Peru as a hiking area; try a park or town name"])
        empty = self.plan("A hike near Banff, Alberta, USA tomorrow",
                          {"Banff, Alberta, USA": fixture("nominatim3_banff_alberta_usa.json")})
        self.assertEqual(empty["warnings"], ["Couldn't find Banff, Alberta, USA"])

    def test_rules_normalize_national_park_names(self):
        for text in ("Hike in National Rocky Mountain tomorrow", "a hike near rocky mountain national park",
                     "8 km at Rocky Mountain National Park this Saturday", "national park rocky mountain hike"):
            with self.subTest(text=text):
                self.assertEqual(agents.parse_request(text, date(2026, 11, 15)).get("unknown_place"),
                                 "Rocky Mountain National Park")
        result = self.plan("Hike in National Rocky Mountain tomorrow",
                           {"Rocky Mountain National Park": fixture("nominatim3_rocky_mountain_np.json")})
        self.assertEqual(result["place"]["display_name"],
                         "Rocky Mountain National Park, Larimer County, Colorado, United States")
        self.assertEqual(result["place"]["country"], "United States")


class UsOnlyTests(unittest.TestCase):
    def test_banff_shows_official_alerts_as_us_only(self):
        with Offline(overpass=None, nominatim=NOMINATIM_BANFF):
            result = agents.make_plan("Easy 6 km hike near Banff, Alberta tomorrow", 42.2411, -83.6130,
                                      form={"date": DATE})
        self.assertIsNotNone(result["trail"])
        self.assertEqual(result["alerts"], {"available": False, "source": "NWS",
                                            "error": "Official alerts: available for US locations only"})
        self.assertEqual(result["weather"]["source"], "MET Norway")                 # no NWS coverage: MET Norway
        self.assertEqual(result["hike_window"]["source"], "MET Norway")
        self.assertEqual(result["place"]["country"], "Canada")
        tools = {e.get("tool") for e in result["agent_trace"]}
        self.assertFalse({"get_nws_alerts", "get_weather", "get_hike_window"} & tools)   # NWS was never asked
        self.assertTrue({"get_met_weather", "get_met_hike_window"} <= tools)

    def test_us_places_still_ask_nws(self):
        with Offline(overpass=None, nominatim=GC_NOMINATIM):
            result = agents.make_plan("A hike at the Grand Canyon tomorrow", 42.2411, -83.6130, form={"date": DATE})
        self.assertEqual(result["alerts"]["source"], "NWS")
        self.assertNotIn("error", result["alerts"])


class LargeRegionTests(unittest.TestCase):
    SEEDS = {"rmnp.json": ("Rocky Mountain National Park, Colorado, USA", 40.3428, -105.6836),
             "banff.json": ("Banff, Alberta, Canada", 51.1751, -115.5721),
             "acadia.json": ("Acadia National Park, Maine, USA", 44.3386, -68.2733)}

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for file, (name, lat, lon) in self.SEEDS.items():
            (Path(tmp.name) / file).write_text(json.dumps({"name": name, "lat": lat, "lon": lon, "radius_km": 10,
                                                           "payload": {"elements": []}}), encoding="utf-8")
        p = patch.object(trails, "SEED_DIR", Path(tmp.name))
        p.start()
        self.addCleanup(p.stop)

    def plan(self, text, nominatim):
        with Offline(overpass=None, nominatim=nominatim) as off:
            result = agents.make_plan(text, 42.2411, -83.6130, form={"date": DATE})
        self.assertEqual(off.queries, [])                                      # no trail search in a region
        self.assertIsNone(result["trail"])
        return result

    def test_mountain_range_suggests_seeds_inside(self):
        result = self.plan("A hike in the Rocky Mountains tomorrow",
                           {"Rocky Mountains": fixture("nominatim3_rocky_mountains.json")})
        self.assertEqual(result["warnings"], ["Rocky Mountains is a large region; try a specific park or town "
                                              "(e.g. Rocky Mountain National Park, Colorado, USA)"])

    def test_state_suggests_seeds_inside_its_box(self):
        hits = fixture("nominatim3_colorado.json")                    # the state first, then Colorado County, TX ...
        self.assertTrue(geocode.is_large_region(hits[0]))
        result = self.plan("A hike in Colorado tomorrow", {"Colorado": hits})
        self.assertEqual(result["warnings"], ["Colorado is a large region; try a specific park or town "
                                              "(e.g. Rocky Mountain National Park, Colorado, USA)"])

    def test_no_seed_inside_uses_the_default_example(self):
        result = self.plan("A hike in Antarctica tomorrow", {"Antarctica": fixture("nominatim_antarctica.json")})
        self.assertEqual(result["warnings"], ["Antarctica is a large region; try a specific park or town "
                                              "(e.g. Rocky Mountain National Park)"])

    def test_what_counts_as_large(self):
        self.assertTrue(geocode.is_large_region({"class": "natural", "type": "mountain_range"}))
        self.assertTrue(geocode.is_large_region({"class": "boundary", "type": "administrative", "addresstype": "country"}))
        self.assertTrue(geocode.is_large_region({"class": "waterway", "type": "river",
                                                 "boundingbox": ["31.8", "40.5", "-115.2", "-105.8"]}))  # 1,250 km
        self.assertFalse(geocode.is_large_region({"class": "leisure", "type": "nature_reserve",          # a big park
                                                  "boundingbox": ["35.0", "38.0", "-118.0", "-115.0"]}))
        self.assertFalse(geocode.is_large_region(fixture("nominatim3_rocky_mountain_np.json")[0]))
        self.assertFalse(geocode.is_large_region(fixture("nominatim_grand_canyon_5.json")[0]))   # a point valley
        self.assertFalse(geocode.is_large_region(fixture("nominatim_banff_5.json")[0]))          # a town


MET_BANFF = fixture("met_norway_banff.json")   # real MET Norway compact answer for Banff, 2026-11-15T15:00Z


class MetNorwayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.calls = []

        def fetch(seconds, url, params=None, headers=None):
            self.calls.append((url, params, headers))
            return json.loads(json.dumps(MET_BANFF))
        for p in (patch.object(common, "CACHE_DIR", Path(tmp.name)), patch.object(met, "get_json_within", fetch)):
            p.start()
            self.addCleanup(p.stop)

    def test_request_and_30_min_cache(self):
        met.get_met_weather(51.17507, -115.57208, "2026-11-16")
        met.get_met_hike_window(51.17507, -115.57208, "2026-11-16", "10:00", 120)
        self.assertEqual(len(self.calls), 1)                                  # one request, then the cache
        url, params, headers = self.calls[0]
        self.assertEqual(url, "https://api.met.no/weatherapi/locationforecast/2.0/compact")
        self.assertEqual(params, {"lat": "51.1751", "lon": "-115.5721"})       # at most 4 decimals
        self.assertEqual(headers["User-Agent"], common.USER_AGENT)
        self.assertEqual(met.CACHE_SECONDS, 1800)

    def test_daily_summary(self):
        w = met.get_met_weather(51.1751, -115.5721, "2026-11-16")
        self.assertEqual((w["source"], w["precip_chance"]), ("MET Norway", None))   # compact: amounts only
        self.assertRegex(w["summary"], r"^[A-Z][a-z ]+(and thunder)?, -?\d+F$")
        self.assertIsInstance(w["precip_mm"], float)

    def test_hike_window_in_local_time(self):
        hw = met.get_met_hike_window(51.1751, -115.5721, "2026-11-16", "10:00", 120)
        self.assertEqual((hw["source"], hw["window"], hw["max_precip_chance"]), ("MET Norway", "10:00-12:00", None))
        # 10:00-12:00 in Banff (UTC-6) is 16:00-18:00 UTC: the hourly steps 16:00 and 17:00 UTC
        steps = {s["time"]: s for s in MET_BANFF["properties"]["timeseries"]}
        temps = [steps[t]["data"]["instant"]["details"]["air_temperature"]
                 for t in ("2026-11-16T16:00:00Z", "2026-11-16T17:00:00Z")]
        self.assertEqual((hw["min_temp_f"], hw["max_temp_f"]), (round(min(temps) * 9 / 5 + 32),
                                                                round(max(temps) * 9 / 5 + 32)))
        self.assertTrue(hw["summary"].startswith("During your hike: "))
        self.assertTrue(hw["conditions"])

    def test_beyond_the_forecast_is_unavailable(self):
        self.assertIs(met.get_met_hike_window(51.1751, -115.5721, "2026-12-30", "10:00", 60)["available"], False)

    def test_failure_is_unavailable(self):
        with patch.object(met, "get_json_within", side_effect=TimeoutError("no answer within 8 s")):
            result = met.get_met_weather(1.0, 1.0, "2026-11-16")
        self.assertEqual((result["available"], result["source"]), (False, "MET Norway"))

    def test_symbols(self):
        self.assertEqual(met.words("partlycloudy_day"), "Partly cloudy")
        self.assertEqual(met.words("lightssnowshowersandthunder_day".replace("ss", "s")), "Light snow showers and thunder")
        self.assertEqual(met.words("heavyrainandthunder"), "Heavy rain and thunder")

    def test_thunder_from_met_rejects_like_nws(self):
        plan = {"timing": {"available": False}, "weather": {"available": False}, "hazards": {"items": []},
                "trail": {"coords": [[51.0, -115.0]]},
                "hike_window": {"source": "MET Norway", "window": "10:00-12:00", "max_precip_chance": None,
                                "conditions": ["Rain showers and thunder"]}}
        problems = agents._hard_checks(plan)
        self.assertEqual(problems, ["MET Norway forecasts thunder during the hike (10:00-12:00): "
                                    "Rain showers and thunder."])


class PrecacheTests(unittest.TestCase):
    """python -m backend.tools.trails precache: one Overpass query at a time (60 s), trimmed seeded snapshot."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.seed_dir = Path(tmp.name)
        p = patch.object(trails, "SEED_DIR", self.seed_dir)
        p.start()
        self.addCleanup(p.stop)

    def test_widens_trims_and_saves(self):
        gc10 = fixture("overpass_gc_village_10km.json")
        sparse = {**gc10, "elements": gc10["elements"][:2] + [{"type": "way", "id": 9, "tags": {"name": "Tiny"},
                                                              "geometry": [{"lat": 36.0, "lon": -112.0},
                                                                           {"lat": 36.0001, "lon": -112.0}]}]}
        calls = []

        def query(endpoint, q, parse, timeout=None):
            calls.append((endpoint, timeout, "[timeout:60]" in q))
            if len(calls) == 1:
                raise TimeoutError("busy")                      # first mirror fails: the next one is tried
            return (sparse, parse(sparse)) if len(calls) == 2 else (gc10, parse(gc10))
        found = {"source": "OpenStreetMap Nominatim", "found": True, "lat": 36.0545, "lon": -112.1406,
                 "display_name": "Grand Canyon Village, Coconino County, Arizona, United States"}
        with patch("backend.tools.geocode.geocode", lambda name, text="": found),                 patch.object(trails, "_query_endpoint", query):
            result = trails.precache_place("Grand Canyon Village, Arizona, USA")
        self.assertEqual([c[1] for c in calls], [60, 60, 60])        # one at a time, 60 s each
        self.assertTrue(all(c[2] for c in calls))                   # and a 60 s server-side timeout
        self.assertEqual((result["radius_km"], result["file"]), (20, "grand_canyon_village.json"))
        data = json.loads((self.seed_dir / "grand_canyon_village.json").read_text(encoding="utf-8"))
        self.assertEqual(set(data) >= {"name", "lat", "lon", "radius_km", "saved_at", "note", "payload"}, True)
        self.assertEqual(len(data["payload"]["elements"]), len(trails._parse_anywhere(gc10)))  # only real trails
        self.assertNotIn("Tiny", json.dumps(data))
        trails._cache.clear()
        got, radius, snap = trails.get_trails_anywhere(36.0545, -112.1406)
        self.assertEqual((radius, snap["kind"], len(got)), (20, "seeded", len(trails._parse_anywhere(gc10))))

    def test_home_towns_fall_back_to_a_seed_when_overpass_is_down(self):
        (self.seed_dir / "ann_arbor.json").write_text(json.dumps(
            {"name": "Ann Arbor, Michigan, USA", "lat": 42.2808, "lon": -83.743, "radius_km": 10, "saved_at": "x",
             "payload": {"elements": [{"type": "way", "id": 5, "tags": {"name": "Arb Loop"},
                                       "geometry": [{"lat": 42.28, "lon": -83.74}, {"lat": 42.29, "lon": -83.74}]}]}}),
            encoding="utf-8")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        trails._cache.clear()
        with patch.object(trails, "DISK_CACHE_DIR", Path(tmp.name)),                 patch.object(trails, "_query_hedged", lambda q, failures, *a, **k: None):
            found = trails.get_trails(42.2808, -83.743, 15)            # Ann Arbor: home path, Overpass down
            self.assertEqual([(t["name"], t["cached"]) for t in found], [("Arb Loop", True)])
            trails._cache.clear()
            with self.assertRaises(trails.OverpassError):
                trails.get_trails(45.0, -90.0, 15)                       # nothing seeded near: still an error
        trails._cache.clear()


if __name__ == "__main__":
    unittest.main()
