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


class LatePaceTests(unittest.TestCase):
    # Planned pace: 75 min / 5 km = 15 min/km.
    def test_on_pace_is_not_late(self):
        self.assertEqual(narration.check_late_pace(fake_plan(), current_km=2.0, elapsed_min=30), {"late": False})

    def test_slow_is_late(self):
        result = narration.check_late_pace(fake_plan(), current_km=1.0, elapsed_min=30)  # 30 min/km
        self.assertTrue(result["late"])
        self.assertEqual(result["text"],
                         "You're behind schedule. Turn back now to reach the trailhead before sunset at 17:12.")

    def test_missing_sunset_or_start_is_not_late(self):
        for missing in ("sunset", "start"):
            timing = {k: v for k, v in TIMING.items() if k != missing}
            with self.subTest(missing=missing):
                self.assertEqual(narration.check_late_pace(fake_plan(timing=timing), 1.0, 30), {"late": False})
        unavailable = {"available": False, "source": "x", "error": "down"}
        self.assertEqual(narration.check_late_pace(fake_plan(timing=unavailable), 1.0, 30), {"late": False})

    def test_too_early_to_judge(self):
        self.assertEqual(narration.check_late_pace(fake_plan(), current_km=0.1, elapsed_min=10), {"late": False})

    def test_endpoint(self):
        body = main.LateNarrationRequest(plan=fake_plan(), current_km=1.0, elapsed_min=30)
        self.assertTrue(main.narrate_late(body)["late"])


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
        outside = agents.make_plan("A hike in Antarctica tomorrow", 42.0, -83.0)
        self.assertEqual(outside["narration"], [])


if __name__ == "__main__":
    unittest.main()
