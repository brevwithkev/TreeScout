"""Offline checks of Gemini fail-fast behavior (no network, no Gemini quota):
python -m unittest backend.test_agents"""

import json
import threading
import time
import unittest
from unittest.mock import patch

from backend import agents

COORDS = [[42.0, -83.0], [42.01, -83.0], [42.0, -83.0]]
FAKE_TOOLS = {
    "_safe_trails": lambda lat, lon, r: {"source": "OSM", "items": [
        {"name": "Real trail", "osm_id": 11, "distance_km": 2.2, "coords": COORDS},
        {"name": "Other trail", "osm_id": 12, "distance_km": 5.0, "coords": COORDS[:2]}], "checked_at": "t"},
    "get_weather": lambda lat, lon, d: {"source": "NWS", "summary": "Sunny, 50F", "precip_chance": 10, "checked_at": "t"},
    "get_climate": lambda lat, lon, d: {"source": "NASA POWER", "years": "2006-2025", "avg_high_c": 8.0,
                                        "avg_low_c": 0.0, "avg_precip_mm": 2.0, "checked_at": "t"},
    "get_hazards": lambda lat, lon, r: {"source": "NASA EONET", "items": [], "radius_km": r, "checked_at": "t"},
    "get_space_weather": lambda: {"source": "NASA DONKI", "kp_max": None, "storms": 0, "aurora_chance": "low",
                                  "window": "w", "checked_at": "t"},
    "get_elevation_gain": lambda c: {"source": "Open-Meteo", "elevation_gain_m": 12, "checked_at": "t"},
}


class FakeAPIError(Exception):
    def __init__(self, code, status):
        super().__init__(f"{code} {status}")
        self.code, self.status = code, status


class FakeReply:
    def __init__(self, text):
        self.text, self.function_calls = text, []


class FakeGemini:
    """Stands in for google.genai.Client: scripted planner/checker behavior, counts calls."""

    def __init__(self, planner, checker=None):
        self.planner, self.checker = planner, checker
        self.calls = {"planner": 0, "checker": 0}
        self.chats, self.models = self, self

    # client.chats.create(...).send_message(...)
    def create(self, **_):
        return self

    def send_message(self, _message):
        self.calls["planner"] += 1
        return self.planner()

    # client.models.generate_content(...)
    def generate_content(self, **_):
        self.calls["checker"] += 1
        return self.checker()


def _raise(code, status):
    def fn():
        raise FakeAPIError(code, status)
    return fn


class FailFastTests(unittest.TestCase):
    def setUp(self):
        for name, fake in FAKE_TOOLS.items():
            p = patch(f"backend.agents.{name}", fake)
            p.start()
            self.addCleanup(p.stop)

    def run_plan(self, fake):
        from google.genai import types
        with patch("backend.agents._gemini", lambda: (fake, "gemini-test-flash", types)):
            started = time.perf_counter()
            result = agents.make_plan("2 km walk near Ypsilanti tomorrow", 42.0, -83.0)
            return result, time.perf_counter() - started

    def test_planner_timeout_is_6_seconds_and_http_deadline_is_api_minimum(self):
        self.assertEqual(agents.LLM_CALL_TIMEOUT_S, 6)
        self.assertEqual(agents.LLM_HTTP_DEADLINE_MS, 10_000)

    def test_overloaded_planner_goes_straight_to_rules(self):
        for code, status in ((503, "UNAVAILABLE"), (504, "DEADLINE_EXCEEDED"), (429, "RESOURCE_EXHAUSTED")):
            with self.subTest(code=code):
                fake = FakeGemini(_raise(code, status), checker=lambda: FakeReply('{"approved": true, "problems": []}'))
                result, elapsed = self.run_plan(fake)
                self.assertEqual(result["constraints"]["parsed_by"], "rules")
                self.assertEqual(fake.calls, {"planner": 1, "checker": 0}, "no retry, no checker after a failure")
                self.assertLess(elapsed, 2)
                self.assertIn("LLM unavailable, used rule-based planner",
                              [e["summary"] for e in result["agent_trace"]])
                self.assertEqual(result["trail"]["name"], "Real trail")

    def test_slow_planner_is_abandoned_after_the_timeout(self):
        release = threading.Event()
        self.addCleanup(release.set)
        fake = FakeGemini(lambda: release.wait(30) and FakeReply("{}"))
        with patch.object(agents, "LLM_CALL_TIMEOUT_S", 0.5):
            result, elapsed = self.run_plan(fake)
        self.assertEqual(result["constraints"]["parsed_by"], "rules")
        self.assertLess(elapsed, 2.5, "must not wait for the slow model")
        self.assertEqual(fake.calls["planner"], 1)

    def test_checker_429_stops_further_gemini_calls(self):
        answer = json.dumps({"chosen_osm_id": 11, "explanation": "Real trail is 2.2 km (1.4 mi).",
                             "constraints": {"distance_km": 2, "start": "10:00"}})
        fake = FakeGemini(lambda: FakeReply(answer), checker=_raise(429, "RESOURCE_EXHAUSTED"))
        result, _ = self.run_plan(fake)
        self.assertEqual(result["constraints"]["parsed_by"], "gemini")
        self.assertEqual(fake.calls, {"planner": 1, "checker": 1})
        self.assertIn("LLM checker unavailable, used Python safety checks only",
                      [e["summary"] for e in result["agent_trace"]])
        self.assertTrue(result["checker"]["approved"])


class StartMovedTests(unittest.TestCase):
    def setUp(self):
        for name, fake in FAKE_TOOLS.items():
            p = patch(f"backend.agents.{name}", fake)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict("os.environ", {"GEMINI_API_KEY": ""})  # rule-based planner, no Gemini
        p.start()
        self.addCleanup(p.stop)

    def test_late_start_is_moved_with_a_warning(self):
        result = agents.make_plan("2 km walk near Ypsilanti starting at 4:50 PM tomorrow", 42.0, -83.0)
        self.assertTrue(result["checker"]["approved"])
        self.assertEqual(result["checker"]["rounds"], 2)
        moved_to = result["timing"]["start"]
        self.assertNotEqual(moved_to, "16:50")
        self.assertLessEqual(moved_to, result["timing"]["latest_safe_start"])
        self.assertEqual(result["warnings"][0],
                         f"You asked to start at 16:50; moved to {moved_to} so you finish at least 30 min "
                         f"before sunset ({result['timing']['sunset']}).")
        approval = [e["summary"] for e in result["agent_trace"] if e["agent"] == "checker" and e["step"] == "decide"]
        self.assertIn(f"{moved_to} start is no later than the", approval[0])

    def test_no_warning_when_start_is_kept(self):
        result = agents.make_plan("2 km walk near Ypsilanti starting at 10 AM tomorrow", 42.0, -83.0)
        self.assertEqual(result["timing"]["start"], "10:00")
        self.assertEqual(result["warnings"], [])


class DistanceWarningTests(unittest.TestCase):
    """FAKE_TOOLS has Real trail (2.2 km, a loop) and Other trail (5.0 km)."""

    def setUp(self):
        for name, fake in FAKE_TOOLS.items():
            p = patch(f"backend.agents.{name}", fake)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict("os.environ", {"GEMINI_API_KEY": ""})
        p.start()
        self.addCleanup(p.stop)

    def test_warns_when_no_trail_is_within_half_the_distance(self):
        result = agents.make_plan("20 km walk near Ypsilanti tomorrow", 42.0, -83.0)
        far = [w for w in result["warnings"] if w.startswith("No trail near")]
        self.assertEqual(len(far), 1, result["warnings"])
        self.assertTrue(far[0].startswith("No trail near 20.0 km in this area; the closest is Other trail (5.0 km)"))
        if result["trail"]["name"] != "Other trail":
            self.assertIn(f"; this plan uses {result['trail']['name']}", far[0])

    def test_no_warning_when_a_trail_is_close_enough(self):
        result = agents.make_plan("2 km walk near Ypsilanti tomorrow", 42.0, -83.0)
        self.assertFalse([w for w in result["warnings"] if w.startswith("No trail near")])

    def test_no_warning_for_the_default_distance(self):
        result = agents.make_plan("A walk near Ypsilanti tomorrow", 42.0, -83.0)  # default 8 km, not asked
        self.assertFalse([w for w in result["warnings"] if w.startswith("No trail near")])


if __name__ == "__main__":
    unittest.main()
