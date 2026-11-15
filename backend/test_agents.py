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
    "get_elevation_gain": lambda c, osm_id=None: {"source": "Open-Meteo", "elevation_gain_m": 12, "checked_at": "t"},
    "get_nws_alerts": lambda lat, lon: {"source": "NWS", "items": [], "checked_at": "t"},
    "get_hike_window": lambda lat, lon, d, start, minutes: {
        "source": "NWS hourly", "window": "10:00-10:30", "max_precip_chance": 10, "min_temp_f": 45, "max_temp_f": 50,
        "max_wind_mph": 5, "conditions": ["Sunny"], "summary": "During your hike: 10% rain, 45-50F, wind 5 mph",
        "checked_at": "t"},
    "get_seasonal_hazards": lambda d, lat, lon: {"source": "TreeScout seasonal calendar", "items": [], "checked_at": "t"},
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

    def test_planner_timeout_is_10_seconds_and_http_deadline_is_api_minimum(self):
        self.assertEqual(agents.LLM_CALL_TIMEOUT_S, 10)
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


class TimeBudgetTests(unittest.TestCase):
    """No Gemini call starts after LLM_START_DEADLINE_S, so /plan stays under 20 s."""

    setUp = FailFastTests.setUp            # fake tools
    run_plan = FailFastTests.run_plan      # fake Gemini client, timed make_plan

    ANSWER = json.dumps({"chosen_osm_id": 11, "explanation": "Real trail is 2.2 km (1.4 mi).",
                         "constraints": {"distance_km": 2, "start": "10:00"}})

    def test_budget_adds_up_to_under_20_seconds(self):
        self.assertEqual(agents.LLM_START_DEADLINE_S, 9)
        self.assertLess(agents.LLM_START_DEADLINE_S + agents.LLM_CALL_TIMEOUT_S, 20)

    def test_checker_is_skipped_when_the_start_deadline_has_passed(self):
        def slow_planner():
            time.sleep(0.3)  # finishes after the (patched) 0.2 s deadline
            return FakeReply(self.ANSWER)
        fake = FakeGemini(slow_planner, checker=lambda: FakeReply('{"approved": true, "problems": []}'))
        with patch.object(agents, "LLM_START_DEADLINE_S", 0.2):
            result, _ = self.run_plan(fake)
        self.assertEqual(result["constraints"]["parsed_by"], "gemini")
        self.assertEqual(fake.calls, {"planner": 1, "checker": 0})
        summaries = [e["summary"] for e in result["agent_trace"]]
        self.assertIn("Skipped the Gemini checker: more than 0.2 s had passed, so /plan stays under 20 s. "
                      "Python checks only.", summaries)
        self.assertTrue(result["checker"]["approved"])  # Python checks still ran

    def test_checker_runs_when_within_the_deadline(self):
        fake = FakeGemini(lambda: FakeReply(self.ANSWER),
                          checker=lambda: FakeReply('{"approved": true, "problems": []}'))
        result, _ = self.run_plan(fake)
        self.assertEqual(fake.calls, {"planner": 1, "checker": 1})
        self.assertFalse([e for e in result["agent_trace"] if "Skipped the Gemini" in e["summary"]])

    def test_planner_turn_is_not_started_after_the_deadline(self):
        fake = FakeGemini(lambda: FakeReply(self.ANSWER))
        with patch.object(agents, "LLM_START_DEADLINE_S", 0):
            result, _ = self.run_plan(fake)
        self.assertEqual(result["constraints"]["parsed_by"], "rules")
        self.assertEqual(fake.calls["planner"], 0)


FIVE_TRAILS = [  # same loop geometry, so ranking is decided by distance: top 3 are ids 1, 2, 3
    {"name": f"Trail {i}", "osm_id": i, "distance_km": km, "coords": COORDS}
    for i, km in ((1, 2.0), (2, 2.2), (3, 2.5), (4, 6.0), (5, 9.0))
]


class GuardAndCoverageTests(unittest.TestCase):
    setUp = FailFastTests.setUp

    def plan_with(self, text, answer):
        from google.genai import types
        fake = FakeGemini(lambda: FakeReply(json.dumps(answer)),
                          checker=lambda: FakeReply('{"approved": true, "problems": []}'))
        with patch("backend.agents._safe_trails", lambda lat, lon, r: {"source": "OSM", "items": FIVE_TRAILS,
                                                                       "checked_at": "t"}), \
                patch("backend.agents._gemini", lambda: (fake, "gemini-test-flash", types)):
            return agents.make_plan(text, 42.0, -83.0)

    def test_pick_outside_top_3_is_replaced_by_top_ranked(self):
        result = self.plan_with("2 km walk near Ypsilanti tomorrow",
                                {"chosen_osm_id": 5, "explanation": "Trail 5 is 9.0 km (5.6 mi)."})
        self.assertEqual(result["constraints"]["parsed_by"], "gemini")
        self.assertEqual(result["trail"]["name"], "Trail 1")
        self.assertIn("Gemini picked Trail 5 (ranked #5), outside the top 3; using top-ranked Trail 1",
                      [e["summary"] for e in result["agent_trace"]])

    def test_pick_inside_top_3_is_kept(self):
        result = self.plan_with("2 km walk near Ypsilanti tomorrow",
                                {"chosen_osm_id": 2, "explanation": "Trail 2 is 2.2 km (1.4 mi)."})
        self.assertEqual(result["trail"]["name"], "Trail 2")
        self.assertFalse([e for e in result["agent_trace"] if "outside the top" in e["summary"]])

    def test_prompt_says_distance_matters_most(self):
        prompt = agents.PLANNER_PROMPT.format(top_n=agents.GEMINI_TOP_N, today="2026-11-15", weekday="Sunday",
                                              lat=42.0, lon=-83.0, form="{}")
        self.assertIn("Matching the requested distance matters most: pick from the top 3 of rank_trails", prompt)
        self.assertNotIn("Prefer a start time", prompt)

    def test_gemini_no_trail_for_unknown_place_is_outside_coverage(self):
        result = self.plan_with("A hike in Antarctica tomorrow",
                                {"chosen_osm_id": None, "constraints": {"distance_km": 8}, "explanation": "Out of area."})
        self.assertEqual(result["constraints"]["parsed_by"], "gemini")
        self.assertIsNone(result["trail"])
        self.assertEqual(result["warnings"], ["Outside our coverage area (southeast Michigan)"])
        self.assertIn("Antarctica is outside our coverage area (southeast Michigan)",
                      [e["summary"] for e in result["agent_trace"]])

    def test_gemini_no_trail_for_known_place_still_plans(self):
        result = self.plan_with("2 km walk near Ypsilanti tomorrow", {"chosen_osm_id": None, "explanation": "?"})
        self.assertEqual(result["trail"]["name"], "Trail 1")
        self.assertNotIn("Outside our coverage area (southeast Michigan)", result["warnings"])


class LateStartRevisionTests(unittest.TestCase):
    """Only a late start -> Python keeps the trail and moves the start; anything else -> Gemini revises."""

    setUp = FailFastTests.setUp

    def plan_with(self, text, checker_replies):
        from google.genai import types
        replies = iter(checker_replies)
        answer = json.dumps({"chosen_osm_id": 11, "explanation": "Real trail is 2.2 km (1.4 mi)."})
        fake = FakeGemini(lambda: FakeReply(answer), checker=lambda: FakeReply(json.dumps(next(replies))))
        with patch("backend.agents._gemini", lambda: (fake, "gemini-test-flash", types)):
            return agents.make_plan(text, 42.0, -83.0), fake

    def test_late_start_only_is_moved_in_python(self):
        result, fake = self.plan_with("2 km walk near Ypsilanti starting at 4:50 PM tomorrow", [
            {"approved": False, "problems": ["Starting at 16:50 is after the latest safe start."]},
            {"approved": True, "problems": []},
        ])
        timing = result["timing"]
        self.assertEqual(fake.calls, {"planner": 1, "checker": 2})  # no Gemini revision
        self.assertEqual(result["constraints"]["parsed_by"], "gemini")
        self.assertEqual(result["trail"]["name"], "Real trail")      # same trail
        self.assertEqual(timing["start"], timing["latest_safe_start"])
        self.assertIn(f"Planner moved the start from 16:50 to {timing['start']} to finish before sunset",
                      [e["summary"] for e in result["agent_trace"]])
        self.assertTrue(result["warnings"][0].startswith(f"You asked to start at 16:50; moved to {timing['start']}"))
        self.assertEqual(result["checker"], {"approved": True, "rounds": 2})

    def test_late_start_plus_another_problem_goes_to_gemini(self):
        result, fake = self.plan_with("2 km walk near Ypsilanti starting at 4:50 PM tomorrow", [
            {"approved": False, "problems": ["Thunderstorms are forecast for the afternoon."]},
            {"approved": True, "problems": []},
        ])
        self.assertEqual(fake.calls["planner"], 2)                    # Gemini was asked to revise
        self.assertFalse([e for e in result["agent_trace"] if e["summary"].startswith("Planner moved the start")])


class OtherOptionsTests(unittest.TestCase):
    """POST /plan with osm_id: the user's trail replaces the pick; no Gemini calls; checks still run."""

    setUp = FailFastTests.setUp

    def plan(self, text, osm_id, hazards=None):
        from google.genai import types
        fake = FakeGemini(lambda: FakeReply('{"chosen_osm_id": 1, "explanation": "x"}'),
                          checker=lambda: FakeReply('{"approved": true, "problems": []}'))
        patches = [patch("backend.agents._safe_trails",
                         lambda lat, lon, r: {"source": "OSM", "items": FIVE_TRAILS, "checked_at": "t"}),
                   patch("backend.agents._gemini", lambda: (fake, "gemini-test-flash", types))]
        if hazards is not None:
            patches.append(patch("backend.agents.get_hazards", lambda lat, lon, r: {
                "source": "NASA EONET", "items": hazards, "radius_km": r, "checked_at": "t"}))
        for p in patches:
            p.start()
        try:
            return agents.make_plan(text, 42.0, -83.0, osm_id=osm_id), fake
        finally:
            for p in patches:
                p.stop()

    def test_user_choice_replaces_the_pick_without_gemini(self):
        result, fake = self.plan("2 km walk near Ypsilanti tomorrow", osm_id=4)  # ranked #4, outside the top 3
        self.assertEqual(result["trail"]["name"], "Trail 4")
        self.assertEqual(fake.calls, {"planner": 0, "checker": 0})
        self.assertEqual(result["constraints"]["parsed_by"], "rules")
        summaries = [e["summary"] for e in result["agent_trace"]]
        self.assertIn("User chose Trail 4 from other options", summaries)
        self.assertFalse([s for s in summaries if "LLM unavailable" in s or "outside the top" in s])
        self.assertTrue(result["timing"]["sunset"])                 # timing ran
        self.assertEqual(result["narration"][0]["kind"], "trailhead")  # narration ran
        self.assertNotIn(4, [a["osm_id"] for a in result["alternatives"]])

    def test_unknown_osm_id_plans_as_usual(self):
        result, fake = self.plan("2 km walk near Ypsilanti tomorrow", osm_id=999)
        self.assertEqual(result["trail"]["name"], "Trail 1")
        self.assertEqual(fake.calls["planner"], 0)
        self.assertIn("Requested trail osm_id 999 is not in the trail data for this area; planned as usual",
                      [e["summary"] for e in result["agent_trace"]])

    def test_late_start_on_the_chosen_trail_is_moved_not_switched(self):
        result, _ = self.plan("2 km walk near Ypsilanti starting at 4:55 PM tomorrow", osm_id=3)
        self.assertEqual(result["trail"]["name"], "Trail 3")
        self.assertEqual(result["timing"]["start"], result["timing"]["latest_safe_start"])
        self.assertTrue(result["warnings"][0].startswith("You asked to start at 16:55; moved to"))

    def test_other_problems_keep_the_chosen_trail_as_warnings(self):
        fire = {"title": "Test fire", "type": "Wildfires", "lat": COORDS[0][0], "lon": COORDS[0][1],
                "date": "2026-11-14", "distance_km": 0.0, "source": "NASA EONET"}
        result, _ = self.plan("2 km walk near Ypsilanti tomorrow", osm_id=2, hazards=[fire])
        self.assertEqual(result["trail"]["name"], "Trail 2")        # not switched to another trail
        self.assertFalse(result["checker"]["approved"])
        self.assertTrue(any("Test fire" in w for w in result["warnings"]))


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


SEVERE = {"event": "Tornado Warning", "severity": "Severe", "headline": "Tornado Warning until 3:00PM EST",
          "onset": None, "ends": None, "link": "https://api.weather.gov/alerts/x"}
MODERATE = {"event": "Winter Weather Advisory", "severity": "Moderate", "headline": "Winter Weather Advisory until 7PM",
            "onset": None, "ends": None, "link": "https://api.weather.gov/alerts/y"}
DEER = {"title": "Firearm deer season", "advice": "Wear blaze orange; hunters are common near state game areas",
        "source": "Michigan DNR season dates", "link": "https://www.michigan.gov/dnr/things-to-do/hunting/deer"}


class HazardCheckerTests(unittest.TestCase):
    """Phase H: NWS alerts, the NWS hourly hike window, seasonal items and conditions (rule-based planner)."""

    setUp = StartMovedTests.setUp

    def plan(self, text="2 km walk near Ypsilanti starting at 10 AM tomorrow", alerts=(), window=None, seasonal=(),
             daily_precip=10):
        fake_window = dict(FAKE_TOOLS["get_hike_window"](0, 0, "", "", 0), **(window or {}))
        patches = [
            patch("backend.agents.get_nws_alerts", lambda lat, lon: {"source": "NWS", "items": list(alerts),
                                                                     "checked_at": "t"}),
            patch("backend.agents.get_hike_window", lambda *a: fake_window),
            patch("backend.agents.get_seasonal_hazards", lambda d, lat, lon: {
                "source": "TreeScout seasonal calendar", "items": list(seasonal), "checked_at": "t"}),
            patch("backend.agents.get_weather", lambda lat, lon, d: {
                "source": "NWS", "summary": "Rain, 50F", "precip_chance": daily_precip, "checked_at": "t"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return agents.make_plan(text, 42.0, -83.0)

    def rejections(self, result):
        return [e["summary"] for e in result["agent_trace"] if e["agent"] == "checker" and e["step"] == "reject"]

    def test_new_fields_and_a_trace_line_per_tool(self):
        result = self.plan()
        self.assertEqual(result["alerts"], {"source": "NWS", "items": [], "checked_at": "t"})
        self.assertEqual(result["hike_window"]["summary"], "During your hike: 10% rain, 45-50F, wind 5 mph")
        self.assertEqual(result["seasonal"]["items"], [])
        self.assertEqual(result["conditions"], [])
        tools = {e.get("tool") for e in result["agent_trace"]}
        self.assertTrue({"get_nws_alerts", "get_hike_window", "get_seasonal_hazards", "derive_conditions"} <= tools)
        self.assertTrue(result["checker"]["approved"])

    def test_typical_climate_is_reported_without_a_warning(self):
        result = self.plan()  # 10:00-10:30, high 50F vs normal 46.4F
        self.assertEqual(result["climate_anomaly"], {"source": "NWS vs NASA POWER 2006-2025", "temp_diff_f": 4,
                                                     "rain_flag": False, "notable": False,
                                                     "summary": "Typical for this week"})
        self.assertIn("Compared NWS forecast with NASA POWER normals: Typical for this week",
                      [e["summary"] for e in result["agent_trace"]])
        self.assertFalse([w for w in result["warnings"] if w.startswith("Unusual conditions")])

    def test_unusual_cold_is_a_warning_not_a_rejection(self):
        result = self.plan(window={"min_temp_f": 33, "max_temp_f": 37})  # 37F vs normal high 46.4F: 9F colder
        self.assertTrue(result["checker"]["approved"])
        self.assertEqual(result["climate_anomaly"]["summary"], "About 9F colder than normal for this week")
        self.assertTrue(result["climate_anomaly"]["notable"])
        self.assertIn("Unusual conditions", [c["title"] for c in result["conditions"]])
        self.assertIn("Unusual conditions: About 9F colder than normal for this week (forecast high 37F vs normal "
                      "46F); dress for it.", result["warnings"])
        self.assertFalse(self.rejections(result))

    def test_missing_power_gives_an_unavailable_anomaly(self):
        with patch("backend.agents.get_climate", lambda lat, lon, d: {"available": False, "source": "NASA POWER",
                                                                      "error": "timed out"}):
            result = self.plan()
        self.assertEqual(result["climate_anomaly"], {"available": False, "source": "NWS vs NASA POWER",
                                                     "error": "NASA POWER normals unavailable (timed out)"})
        self.assertTrue(result["checker"]["approved"])

    def test_severe_alert_rejects(self):
        result = self.plan(alerts=[SEVERE])
        self.assertFalse(result["checker"]["approved"])
        self.assertIn("NWS Tornado Warning (Severe) is in effect: Tornado Warning until 3:00PM EST",
                      self.rejections(result))
        self.assertIn("NWS Tornado Warning (Severe) is in effect: Tornado Warning until 3:00PM EST", result["warnings"])

    def test_extreme_alert_rejects(self):
        self.assertFalse(self.plan(alerts=[{**SEVERE, "severity": "Extreme"}])["checker"]["approved"])

    def test_hike_window_rain_of_70_percent_rejects(self):
        result = self.plan(window={"max_precip_chance": 70})
        self.assertFalse(result["checker"]["approved"])
        self.assertIn("NWS hourly forecasts up to a 70% chance of precipitation during the hike (10:00-10:30).",
                      self.rejections(result))
        self.assertTrue(self.plan(window={"max_precip_chance": 69})["checker"]["approved"])

    def test_hike_window_rain_replaces_the_daily_chance(self):
        result = self.plan(daily_precip=90, window={"max_precip_chance": 10})  # rainy day, dry during the hike
        self.assertTrue(result["checker"]["approved"])

    def test_daily_chance_is_the_fallback_without_an_hourly_window(self):
        with patch("backend.agents.get_hike_window", lambda *a: {"available": False, "source": "NWS hourly",
                                                                 "error": "No NWS hourly forecast", "checked_at": "t"}):
            result = agents.make_plan("2 km walk near Ypsilanti starting at 10 AM tomorrow", 42.0, -83.0)
        self.assertTrue(result["checker"]["approved"])  # FAKE get_weather: 10%
        self.assertEqual(result["hike_window"], {"available": False, "source": "NWS hourly",
                                                 "error": "No NWS hourly forecast"})

    def test_thunder_rejects(self):
        result = self.plan(window={"conditions": ["Sunny", "Chance Showers And Thunderstorms"]})
        self.assertFalse(result["checker"]["approved"])
        self.assertTrue(any("thunder" in r for r in self.rejections(result)))

    def test_moderate_alert_seasonal_and_conditions_are_warnings_not_rejections(self):
        result = self.plan(alerts=[MODERATE], seasonal=[DEER], window={"min_temp_f": 30})
        self.assertTrue(result["checker"]["approved"])
        self.assertEqual(self.rejections(result), [])
        self.assertEqual(result["warnings"], [
            "NWS Winter Weather Advisory (Moderate): Winter Weather Advisory until 7PM",
            "Firearm deer season: Wear blaze orange; hunters are common near state game areas.",
            "Freezing temperatures: Down to 30F during your hike; watch for icy boardwalks.",
        ])
        self.assertEqual([c["title"] for c in result["conditions"]], ["Freezing temperatures"])

    def test_start_moved_by_the_planner_is_not_low_light(self):
        result = self.plan(text="2 km walk near Ypsilanti starting at 4:50 PM tomorrow")
        self.assertEqual(result["timing"]["start"], result["timing"]["latest_safe_start"])
        self.assertNotIn("Low light", [c["title"] for c in result["conditions"]])
        self.assertFalse([w for w in result["warnings"] if w.startswith("Low light")])
        self.assertTrue(result["warnings"][0].startswith("You asked to start at 16:50; moved to"))

    def test_late_start_that_cannot_be_fixed_is_low_light(self):
        late = lambda *a, **k: {"source": "Computed", "start": "16:50", "est_duration_min": 30, "sunset": "17:10",
                                "latest_safe_start": "16:10", "turnaround_by": "16:25", "checked_at": "t",
                                "warning": "Starting at 16:50 is after the latest safe start of 16:10."}
        with patch("backend.agents.compute_timing", late):
            result = self.plan(text="2 km walk near Ypsilanti starting at 4:50 PM tomorrow")
        self.assertIn("Low light", [c["title"] for c in result["conditions"]])

    def test_outside_coverage_marks_the_new_sections_unavailable(self):
        result = agents.make_plan("A hike in Antarctica tomorrow", 42.0, -83.0)
        for key in ("alerts", "hike_window", "seasonal"):
            self.assertIs(result[key]["available"], False, key)
        self.assertEqual(result["conditions"], [])
        self.assertEqual(result["warnings"], ["Outside our coverage area (southeast Michigan)"])


if __name__ == "__main__":
    unittest.main()
