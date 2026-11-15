"""Offline checks for narration, the late check, /tts errors and the /plan narration step:
python -m unittest backend.test_narration"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from backend import agents, main, narration
from backend.test_agents import FAKE_TOOLS
from backend.tools.trails import haversine

KM_PER_DEG_LAT = 111.195
# A straight 5.0 km route due north: 51 points, 0.1 km apart (not a loop).
ROUTE = [[42.0 + i * 0.1 / KM_PER_DEG_LAT, -83.6] for i in range(51)]
TIMING = {"start": "10:00", "est_duration_min": 75, "sunset": "17:12", "latest_safe_start": "15:27",
          "turnaround_by": "16:04"}


def fake_plan(**overrides):
    plan = {"trail": {"name": "Fake Five", "osm_id": 1, "distance_km": 5.0, "elevation_gain_m": 20,
                      "coords": ROUTE},
            "timing": dict(TIMING), "weather": {"source": "NWS", "summary": "Sunny, 50F", "precip_chance": 0},
            "hazards": []}
    plan.update(overrides)
    return plan


class NarrationTests(unittest.TestCase):
    def test_route_uses_our_haversine_signature(self):
        self.assertAlmostEqual(haversine(ROUTE[0], ROUTE[-1]), 5.0, places=2)
        self.assertAlmostEqual(narration._cumulative_km(ROUTE)[-1], 5.0, places=2)

    def test_five_km_plan(self):
        points = narration.generate_narration(fake_plan())
        self.assertEqual([p["kind"] for p in points], ["trailhead", "km", "km", "halfway", "km", "km", "finish"])
        self.assertEqual([p["km"] for p in points], [0.0, 1.0, 2.0, 2.5, 3.0, 4.0, 5.0])
        indexes = [p["coord_index"] for p in points]
        self.assertEqual(indexes, sorted(indexes))
        self.assertEqual(indexes[0], 0)
        self.assertEqual(indexes[-1], len(ROUTE) - 1)
        for p in points[1:-1]:  # each marker lands within one point (0.1 km) of its distance
            self.assertLessEqual(abs(p["coord_index"] - round(p["km"] * 10)), 1, p)
        self.assertIn("Welcome to Fake Five, 5.0 kilometers total. Sunset is at 17:12; turn around by 16:04.",
                      points[0]["text"])
        self.assertIn("Sunny, 50F", points[0]["text"])
        self.assertEqual(points[1]["text"], "You've hiked 1 kilometer, 4 to go.")
        self.assertEqual(points[-1]["text"], "You've reached the end of Fake Five.")  # not a loop

    def test_loop_finish_and_hazard(self):
        loop = ROUTE + ROUTE[-2::-1]  # out and back: 10 km, ends at the start
        hazard = {"title": "Test fire", "type": "Wildfires", "lat": ROUTE[20][0], "lon": -83.59,
                  "date": "2026-11-13T00:00:00Z", "source": "NASA EONET"}  # ~0.8 km east of km 2
        points = narration.generate_narration(fake_plan(
            trail={"name": "Loop", "osm_id": 2, "distance_km": 10.0, "coords": loop}, hazards=[hazard]))
        self.assertEqual(points[-1]["text"], "You're back at the trailhead.")
        hazard_points = [p for p in points if p["kind"] == "hazard"]
        self.assertEqual(len(hazard_points), 1)
        self.assertIn("Caution: Test fire is 0.8 kilometers away, reported by NASA EONET on 2026-11-13.",
                      hazard_points[0]["text"])

    def test_unavailable_sections_do_not_break_it(self):
        unavailable = {"available": False, "source": "x", "error": "down"}
        points = narration.generate_narration(fake_plan(timing=unavailable, weather=unavailable, hazards=unavailable))
        self.assertEqual(points[0]["text"], "Welcome to Fake Five, 5.0 kilometers total.")
        self.assertEqual([p for p in points if p["kind"] == "halfway"][0]["text"], "Halfway there.")
        self.assertEqual(narration.generate_narration({"trail": None}), [])
        self.assertEqual(narration.generate_narration({"trail": unavailable}), [])


def rolling_hills(start):
    """6.8 km trail, sunset 17:12, planned 94 min (about 13.8 min/km)."""
    return fake_plan(trail={"name": "Rolling Hills", "osm_id": 3, "distance_km": 6.8, "coords": ROUTE},
                     timing={"start": start, "est_duration_min": 94, "sunset": "17:12"})


SLOWED_PACE = 2 * 94 / 6.8   # "Slow down" halves walking speed: about 27.6 min/km


class TopHazardTests(unittest.TestCase):
    DEER = {"title": "Firearm deer season", "advice": "Wear blaze orange", "source": "Michigan DNR season dates"}

    def trailhead(self, **overrides):
        return narration.generate_narration(fake_plan(**overrides))[0]["text"]

    def extra_words(self, **overrides):
        base = self.trailhead()
        text = self.trailhead(**overrides)
        self.assertTrue(text.startswith(base), text)
        return text[len(base):].strip()

    def test_deer_season(self):
        extra = self.extra_words(seasonal={"source": "x", "items": [self.DEER]})
        self.assertEqual(extra, "Deer season: wear orange.")

    def test_climate_anomaly_line(self):
        unusual = [{"title": "Unusual conditions", "advice": "a", "source": "NWS vs NASA POWER 2006-2025"}]
        def line(**anomaly):
            return self.extra_words(conditions=unusual, climate_anomaly={
                "source": "NWS vs NASA POWER 2006-2025", "notable": True, "summary": "s",
                "temp_diff_f": anomaly.get("diff", 0), "rain_flag": anomaly.get("rain", False)})
        self.assertEqual(line(diff=-9), "About 9F colder than normal: dress warm.")
        self.assertEqual(line(rain=True), "Rain likelier than normal for this week.")
        self.assertEqual(line(diff=-12, rain=True), "About 12F colder and wetter than normal: dress warm.")
        for text in (line(diff=-9), line(rain=True), line(diff=-12, rain=True)):
            self.assertLess(len(text.split()), 12)
        self.assertEqual(self.extra_words(conditions=unusual), "Unusual conditions.")  # no climate_anomaly section

    def test_priority_and_word_limit(self):
        severe = {"event": "Severe Thunderstorm Warning", "severity": "Severe", "headline": "h"}
        moderate = {"event": "Winter Weather Advisory", "severity": "Moderate", "headline": "h"}
        cases = [
            ({"alerts": {"source": "NWS", "items": [moderate, severe]}, "seasonal": {"source": "x", "items": [self.DEER]}},
             "Severe Thunderstorm Warning in effect."),
            ({"alerts": {"source": "NWS", "items": [moderate]}, "seasonal": {"source": "x", "items": [self.DEER]}},
             "Winter Weather Advisory in effect."),
            ({"hike_window": {"source": "NWS hourly", "max_precip_chance": 10, "conditions": ["Chance Thunderstorms"]},
              "seasonal": {"source": "x", "items": [self.DEER]}}, "Thunderstorms forecast: watch the sky."),
            ({"conditions": [{"title": "Freezing temperatures", "advice": "a", "source": "NWS hourly"}]},
             "Freezing temps: watch for icy boardwalks."),
            ({"conditions": [{"title": "Low light", "advice": "a", "source": "Computed"}]},
             "Low light at the finish: bring a headlamp."),
            ({"alerts": {"source": "NWS", "items": [{"event": "A very long made up alert name that goes on and on and "
                                                              "on forever", "severity": "Severe"}]}}, None),
        ]
        for overrides, expected in cases:
            with self.subTest(expected=expected):
                extra = self.extra_words(**overrides)
                if expected:
                    self.assertEqual(extra, expected)
                self.assertLess(len(extra.split()), 12, extra)

    def test_old_plans_and_unavailable_sections_add_nothing(self):
        self.assertEqual(self.extra_words(), "")
        self.assertEqual(self.extra_words(alerts={"available": False, "source": "NWS", "error": "x"},
                                          seasonal={"available": False, "source": "x", "error": "x"},
                                          hike_window={"available": False, "source": "x", "error": "x"}), "")


class LatePaceTests(unittest.TestCase):
    """late = (start + elapsed) + remaining_km * current pace + 30 min > sunset."""

    def test_slowed_morning_hike_is_not_late(self):
        # 10:30 start, slowed, 1 km in: now about 10:58, remaining 5.8 km x 27.6 = 160 min -> about 13:38,
        # plus 30 min = 14:08, well before 17:12.
        self.assertEqual(narration.check_late_pace(rolling_hills("10:30"), 1.0, SLOWED_PACE), {"late": False})

    def test_slowed_afternoon_hike_is_late(self):
        # 15:00 start, same trail and pace: about 18:08 finish, plus 30 min, is after 17:12.
        result = narration.check_late_pace(rolling_hills("15:00"), 1.0, SLOWED_PACE)
        self.assertTrue(result["late"])
        self.assertEqual(result["text"],
                         "You're behind schedule. Turn back now to reach the trailhead before sunset at 17:12.")

    def test_slowness_alone_is_not_late(self):
        # Even 4x slower than planned, a 09:00 start finishes long before sunset.
        self.assertEqual(narration.check_late_pace(rolling_hills("09:00"), 1.0, 4 * 94 / 6.8), {"late": False})

    def test_on_pace_but_finishing_inside_the_buffer_is_late(self):
        # 16:00 start at the planned pace: about 17:34 finish, after sunset -> late (pace is not a condition).
        self.assertTrue(narration.check_late_pace(rolling_hills("16:00"), 1.0, 94 / 6.8)["late"])

    def test_buffer_boundary(self):
        # Finish exactly 30 min before sunset is not late; one minute later is.
        plan = rolling_hills("10:00")
        plan["timing"]["sunset"] = "12:30"   # 1 km in after 30 min at 30 min/km: finish 10:00+30+5.8*30 = 13:24
        self.assertTrue(narration.check_late_pace(plan, 1.0, 30)["late"])
        plan["timing"]["sunset"] = "13:54"   # 13:24 + 30 = 13:54: exactly on the buffer
        self.assertEqual(narration.check_late_pace(plan, 1.0, 30), {"late": False})
        plan["timing"]["sunset"] = "13:53"
        self.assertTrue(narration.check_late_pace(plan, 1.0, 30)["late"])

    def test_missing_sunset_or_start_is_not_late(self):
        for missing in ("sunset", "start"):
            timing = {k: v for k, v in rolling_hills("15:00")["timing"].items() if k != missing}
            with self.subTest(missing=missing):
                self.assertEqual(narration.check_late_pace(fake_plan(timing=timing), 1.0, SLOWED_PACE),
                                 {"late": False})
        unavailable = {"available": False, "source": "x", "error": "down"}
        self.assertEqual(narration.check_late_pace(fake_plan(timing=unavailable), 1.0, 30), {"late": False})

    def test_current_pace_replaces_the_average(self):
        # 3:20 start, sunset 17:12, 94 min planned (13.8 min/km). 1 km in after 27.6 min (slowed): the average
        # projects 1 km late, but walking at 1.3x now (10.6 min/km) finishes 15:47.6 + 61.5 = 16:49 -> +30 = 17:19.
        plan = rolling_hills("15:20")
        self.assertTrue(narration.check_late_pace(plan, 1.0, SLOWED_PACE)["late"])                    # average
        self.assertTrue(narration.check_late_pace(plan, 1.0, SLOWED_PACE, 94 / 6.8 / 1.3)["late"])
        early = rolling_hills("14:30")
        self.assertTrue(narration.check_late_pace(early, 1.0, SLOWED_PACE)["late"])                   # average: late
        self.assertEqual(narration.check_late_pace(early, 1.0, SLOWED_PACE, 94 / 6.8 / 1.3), {"late": False})
        self.assertTrue(narration.check_late_pace(early, 1.0, SLOWED_PACE, SLOWED_PACE)["late"])      # still slow

    def test_current_pace_judges_from_the_first_metre(self):
        plan = rolling_hills("16:00")  # 16:00 + 94 + 30 > 17:12
        self.assertTrue(narration.check_late_pace(plan, 0.0, 0.0, 94 / 6.8)["late"])
        self.assertEqual(narration.check_late_pace(plan, 0.0, 0.0), {"late": False})  # average: too early

    def test_finishing_exactly_on_the_buffer_does_not_flicker(self):
        # Zero-slack plan (start = latest safe start): on pace with rounding noise is not late; 1 min more is.
        plan = rolling_hills("15:08")   # 15:08 + 94 = 16:42; +30 = 17:12 = sunset
        self.assertEqual(narration.check_late_pace(plan, 2.0, 2 * 94 / 6.8 + 0.1, 94 / 6.8), {"late": False})
        self.assertTrue(narration.check_late_pace(plan, 2.0, 2 * 94 / 6.8 + 1.0, 94 / 6.8)["late"])

    def test_too_early_to_judge(self):
        self.assertEqual(narration.check_late_pace(rolling_hills("15:00"), 0.1, 10), {"late": False})

    def test_endpoint_accepts_current_pace(self):
        body = main.LateNarrationRequest(plan=rolling_hills("14:30"), current_km=1.0, elapsed_min=SLOWED_PACE,
                                         current_pace_min_per_km=94 / 6.8 / 1.3)
        self.assertEqual(main.narrate_late(body), {"late": False})
        with self.assertRaises(Exception):
            main.LateNarrationRequest(plan={}, current_km=1, elapsed_min=1, current_pace_min_per_km=0)

    def test_endpoint(self):
        body = main.LateNarrationRequest(plan=rolling_hills("15:00"), current_km=1.0, elapsed_min=SLOWED_PACE)
        self.assertTrue(main.narrate_late(body)["late"])
        body = main.LateNarrationRequest(plan=rolling_hills("10:30"), current_km=1.0, elapsed_min=SLOWED_PACE)
        self.assertEqual(main.narrate_late(body), {"late": False})


class TtsTests(unittest.TestCase):
    def test_missing_key_returns_502_without_calling_elevenlabs(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(narration, "TTS_CACHE_DIR", Path(tmp)), \
                patch.dict("os.environ", {"ELEVENLABS_API_KEY": ""}), \
                patch.object(narration, "urlopen", side_effect=AssertionError("network must not be called")):
            with self.assertRaises(HTTPException) as caught:
                main.tts(main.TTSRequest(text="Hello from the trail."))
        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(caught.exception.detail, "ELEVENLABS_API_KEY is not set")

    def test_default_model_and_env_override(self):
        sent = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"ID3fake-mp3"

        def fake_urlopen(request, timeout):
            sent["url"], sent["body"] = request.full_url, request.data
            return FakeResponse()

        with tempfile.TemporaryDirectory() as tmp, patch.object(narration, "TTS_CACHE_DIR", Path(tmp)), \
                patch.object(narration, "urlopen", fake_urlopen):
            with patch.dict("os.environ", {"ELEVENLABS_API_KEY": "k", "ELEVENLABS_VOICE_ID": "voice1",
                                           "ELEVENLABS_MODEL": ""}):
                self.assertEqual(narration.synthesize_tts("Hi"), b"ID3fake-mp3")
                self.assertTrue(sent["url"].endswith("/text-to-speech/voice1"))
                self.assertIn(b'"model_id": "eleven_flash_v2_5"', sent["body"])
            with patch.dict("os.environ", {"ELEVENLABS_API_KEY": "k", "ELEVENLABS_VOICE_ID": "voice1",
                                           "ELEVENLABS_MODEL": "eleven_v3"}):
                narration.synthesize_tts("Hi")  # different model -> different cache key -> new request
                self.assertIn(b'"model_id": "eleven_v3"', sent["body"])


class PlanNarrationStepTests(unittest.TestCase):
    def test_plan_includes_narration(self):
        patches = [patch(f"backend.agents.{name}", fake) for name, fake in FAKE_TOOLS.items()]
        patches.append(patch.dict("os.environ", {"GEMINI_API_KEY": ""}))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        result = agents.make_plan("2 km walk near Ypsilanti tomorrow", 42.0, -83.0)
        kinds = [p["kind"] for p in result["narration"]]
        self.assertEqual(kinds[0], "trailhead")
        self.assertEqual(kinds[-1], "finish")
        self.assertIn(result["trail"]["name"], result["narration"][0]["text"])
        not_found = {"source": "OpenStreetMap Nominatim", "query": "Xanadu", "found": False, "checked_at": "t"}
        with patch("backend.agents.geocode", lambda name: not_found):  # offline: no place, no trail
            nowhere = agents.make_plan("A hike in Xanadu tomorrow", 42.0, -83.0)
        self.assertIsNone(nowhere["trail"])
        self.assertEqual(nowhere["narration"], [])


if __name__ == "__main__":
    unittest.main()
