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
                  patch.object(demo, "TTS_DIR", Path(self.tmp.name) / "demo_tts"),
                  patch.dict("os.environ", {"DEMO_RECORD": "1"}),
                  patch.object(narration, "TTS_CACHE_DIR", Path(self.tmp.name) / "tts"),
                  patch.object(narration, "urlopen", side_effect=AssertionError("no network in tests"))):
            p.start()
            self.addCleanup(p.stop)
        self.live_calls = []

        def make_plan(text, lat, lon, radius_km=15, form=None, osm_id=None):
            self.live_calls.append(text if osm_id is None else f"{text} #{osm_id}")
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

    def test_nothing_is_recorded_unless_demo_record(self):
        self.demo_env(False)
        with patch.dict("os.environ", {"DEMO_RECORD": "0"}):
            main.plan(main.PlanRequest(request="Not recorded"))
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(demo.demo_record())  # default off
            main.plan(main.PlanRequest(request="Also not recorded"))
        self.assertFalse(self.plans_dir.exists() and any(self.plans_dir.iterdir()))
        self.assertEqual(self.live_calls, ["Not recorded", "Also not recorded"])

    def test_recording_saves_voice_lines_to_demo_data(self):
        self.demo_env(False)
        plan = {"request": "Voice plan", "timing": {"sunset": "17:12"},
                "narration": [{"coord_index": 0, "text": "Welcome."}, {"coord_index": 5, "text": "Halfway."}]}
        spoken = []

        def fake_tts(text):
            spoken.append(text)
            return f"mp3:{text}".encode()

        with patch.object(main, "make_plan", lambda *a, **k: plan), patch.object(narration, "synthesize_tts", fake_tts):
            main.plan(main.PlanRequest(request="Voice plan"))
            expected = ["Welcome.", "Halfway.", "Turn back now.",
                        "You're behind schedule. Turn back now to reach the trailhead before sunset at 17:12."]
            self.assertEqual(sorted(spoken), sorted(expected))
            for line in expected:
                self.assertEqual(demo.demo_audio(line), f"mp3:{line}".encode())
            main.plan(main.PlanRequest(request="Voice plan"))  # recording again: audio already there
            self.assertEqual(len(spoken), 4)

    def test_demo_tts_serves_saved_audio_whatever_the_voice(self):
        demo.TTS_DIR.mkdir(parents=True)
        demo.audio_path("Welcome.").write_bytes(b"ID3demo")
        self.demo_env(True)
        with patch.dict("os.environ", {"ELEVENLABS_VOICE_ID": "another-voice", "ELEVENLABS_MODEL": "another-model"}):
            response = main.tts(main.TTSRequest(text="Welcome."))
        self.assertEqual((response.body, response.media_type), (b"ID3demo", "audio/mpeg"))
        self.demo_env(False)  # outside demo mode the saved demo audio is not used
        with patch.dict("os.environ", {"ELEVENLABS_API_KEY": ""}), self.assertRaises(HTTPException):
            main.tts(main.TTSRequest(text="Welcome."))

    def test_backfill_command(self):
        self.demo_env(False)
        plan = {"request": "Backfill me", "timing": {"sunset": "17:12"}, "narration": [{"coord_index": 0, "text": "Hi."}]}
        with patch.dict("os.environ", {"DEMO_RECORD": "1"}), patch.object(main, "make_plan", lambda *a, **k: plan),                 patch.object(narration, "synthesize_tts", side_effect=RuntimeError("no key")):
            main.plan(main.PlanRequest(request="Backfill me"))  # recorded, but audio failed
        self.assertIsNone(demo.demo_audio("Hi."))
        with patch.object(narration, "synthesize_tts", lambda text: b"ID3" + text.encode()),                 patch("dotenv.load_dotenv"):
            self.assertEqual(demo._main(["demo", "audio"]), 0)
        self.assertEqual(demo.demo_audio("Hi."), b"ID3Hi.")

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

    def test_chosen_alternative_is_neither_replayed_nor_recorded(self):
        self.demo_env(False)
        main.plan(main.PlanRequest(request="Easy walk"))                 # recorded
        saved = (self.plans_dir / "easy-walk.json").read_text(encoding="utf-8")
        self.demo_env(True)
        result = main.plan(main.PlanRequest(request="Easy walk", osm_id=42))
        self.assertNotIn("demo_recorded_at", result)                    # planned live, not replayed
        self.assertEqual(self.live_calls, ["Easy walk", "Easy walk #42"])
        self.assertEqual((self.plans_dir / "easy-walk.json").read_text(encoding="utf-8"), saved)  # not overwritten

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
