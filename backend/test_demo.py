"""Offline checks for demo mode: python -m unittest backend.test_demo"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from backend import demo, main, narration


def fake_plan(request):
    return {"request": request, "trail": {"name": "Live trail"}, "warnings": []}


class DemoModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plans_dir = Path(self.tmp.name) / "plans"
        for p in (patch.object(demo, "PLANS_DIR", self.plans_dir),
                  patch.object(narration, "TTS_CACHE_DIR", Path(self.tmp.name) / "tts"),
                  patch.object(narration, "urlopen", side_effect=AssertionError("no network in tests"))):
            p.start()
            self.addCleanup(p.stop)
        self.live_calls = []

        def make_plan(text, lat, lon, radius_km=15, form=None):
            self.live_calls.append(text)
            return fake_plan(text)

        p = patch.object(main, "make_plan", make_plan)
        p.start()
        self.addCleanup(p.stop)

    def demo_env(self, on):
        p = patch.dict("os.environ", {"DEMO_MODE": "1" if on else "0"})
        p.start()
        self.addCleanup(p.stop)

    def test_slugify(self):
        self.assertEqual(demo.slugify("Easy 3 mile walk near Ypsilanti tomorrow morning"),
                         "easy-3-mile-walk-near-ypsilanti-tomorrow-morning")
        self.assertEqual(demo.slugify("4 miles with hills near Ypsilanti starting at 3:30 PM tomorrow"),
                         "4-miles-with-hills-near-ypsilanti-starting-at-330-pm-tomorrow")
        self.assertEqual(demo.slugify("  A hike in   Antarctica, tomorrow!  "), "a-hike-in-antarctica-tomorrow")
        self.assertEqual(demo.slugify("Dog-friendly loop"), "dogfriendly-loop")

    def test_demo_mode_flag(self):
        for value, expected in (("1", True), ("true", True), ("0", False), ("", False)):
            with self.subTest(value=value), patch.dict("os.environ", {"DEMO_MODE": value}):
                self.assertEqual(demo.demo_mode(), expected)
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(demo.demo_mode())  # default off

    def test_live_plan_is_recorded_with_timestamp_on_the_copy_only(self):
        self.demo_env(False)
        result = main.plan(main.PlanRequest(request="Easy walk, near Ypsilanti!"))
        self.assertNotIn("demo_recorded_at", result)
        saved = json.loads((self.plans_dir / "easy-walk-near-ypsilanti.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["trail"], {"name": "Live trail"})
        self.assertRegex(saved["demo_recorded_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_demo_mode_replays_matching_recording_without_planning(self):
        self.demo_env(False)
        main.plan(main.PlanRequest(request="Easy walk near Ypsilanti"))
        self.demo_env(True)
        replay = main.plan(main.PlanRequest(request="easy walk near ypsilanti"))  # same slug
        self.assertEqual(self.live_calls, ["Easy walk near Ypsilanti"])         # no second live call
        self.assertIn("demo_recorded_at", replay)

    def test_demo_mode_falls_back_to_live_when_nothing_matches(self):
        self.demo_env(True)
        result = main.plan(main.PlanRequest(request="Something never recorded"))
        self.assertEqual(self.live_calls, ["Something never recorded"])
        self.assertNotIn("demo_recorded_at", result)

    def test_demo_endpoints(self):
        self.demo_env(False)
        main.plan(main.PlanRequest(request="First plan"))
        main.plan(main.PlanRequest(request="Second plan"))
        listing = main.demo_plans()
        self.assertEqual({item["slug"] for item in listing}, {"first-plan", "second-plan"})
        self.assertEqual({item["request"] for item in listing}, {"First plan", "Second plan"})
        self.assertTrue(all(item["recorded_at"] for item in listing))
        self.assertEqual(main.demo_plan("first-plan")["request"], "First plan")
        for bad in ("missing", "../../.env", "First Plan"):
            with self.subTest(slug=bad), self.assertRaises(HTTPException) as caught:
                main.demo_plan(bad)
            self.assertEqual(caught.exception.status_code, 404)

    def test_tts_in_demo_mode_serves_cache_only(self):
        self.demo_env(True)
        with self.assertRaises(HTTPException) as caught:
            main.tts(main.TTSRequest(text="Never cached"))
        self.assertEqual(caught.exception.status_code, 502)
        self.assertEqual(caught.exception.detail, "Demo mode: no cached audio for this text")

        with patch.dict("os.environ", {"ELEVENLABS_VOICE_ID": "v", "ELEVENLABS_MODEL": "m"}):
            import hashlib
            narration.TTS_CACHE_DIR.mkdir(parents=True)
            digest = hashlib.sha256(b"m:v:Cached line").hexdigest()
            (narration.TTS_CACHE_DIR / f"{digest}.mp3").write_bytes(b"ID3cached")
            response = main.tts(main.TTSRequest(text="Cached line"))
        self.assertEqual(response.body, b"ID3cached")
        self.assertEqual(response.media_type, "audio/mpeg")


if __name__ == "__main__":
    unittest.main()
