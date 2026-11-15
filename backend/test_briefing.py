"""Deterministic briefing regressions; no Gemini or external services."""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend import agents, briefing
from backend.test_agents import FakeGemini, FakeReply
from backend.test_anywhere import Offline, plan as offline_plan


class QuantityTests(unittest.TestCase):
    def test_changed_sign_and_numeric_substrings_are_rejected(self):
        for source in ["-15F", "−15°F", "- 15 F", "115F"]:
            for claim in ["15F", "+15°F"]:
                with self.subTest(source=source, claim=claim):
                    self.assertFalse(briefing.validate(f"Expect {claim}. Dress warmly.", {"weather": source})[0])

    def test_valid_values_unicode_minus_and_ranges(self):
        for source, claim in [("-15F", "−15 °F"), ("−15°F", "-15F"), ("15F", "+15°F"),
                              ("-15--5F", "−15 to −5°F"), ("45-50F", "45 to 50 F"),
                              ("-15F to 5F", "-15 to 5F"), ("1,354 m", "1354 meters")]:
            with self.subTest(source=source, claim=claim):
                self.assertTrue(briefing.validate(f"Expect {claim}. Review the plan.", {"weather": source})[0])

    def test_decimal_formatting_does_not_lose_sign(self):
        for source, claim in [('−15,5°F', '-15.5F'), ('-15F', '-15.0°F')]:
            self.assertTrue(briefing.validate(f'Expect {claim}. Review the plan.', {'weather': source})[0])
        self.assertFalse(briefing.validate('Expect 15.5F. Review the plan.', {'weather': '−15,5°F'})[0])

    def test_units_and_unrelated_numbers_do_not_authorize_temperature(self):
        data = {"weather": "-15F", "distance_km": 15, "duration_min": 115}
        for claim in ["15F", "115F", "-15C", "15°C"]:
            self.assertFalse(briefing.validate(f"Expect {claim}. Review the plan.", data)[0])
        self.assertTrue(briefing.validate("Walk 15 km. Expect -15F.", data)[0])

    def test_numeric_temperature_fields_preserve_unit_and_sign(self):
        data = {"min_temp_f": -15, "max_temp_f": -5}
        self.assertTrue(briefing.validate("Expect -15 to -5°F. Review the plan.", data)[0])
        self.assertFalse(briefing.validate("Expect 15°F. Review the plan.", data)[0])

    def test_normal_or_difference_is_not_the_forecast_temperature(self):
        data = {'weather': 'Cold, -15F', 'climate': 'About 15F colder than normal',
                'warnings': ['Forecast -15F vs normal 15F'], 'temp_diff_f': 15}
        self.assertFalse(briefing.validate('Expect 15F. Review the warnings.', data)[0])
        self.assertTrue(briefing.validate('Expect -15F. About 15F colder than normal.', data)[0])
        self.assertTrue(briefing.validate('The normal is 15F. Expect -15F.', data)[0])

    def test_degree_word_formatting_is_equivalent(self):
        self.assertTrue(briefing.validate('Expect -15 degrees Fahrenheit. Review the plan.', {'min_temp_f': -15})[0])

    def test_malformed_empty_long_and_invented_output(self):
        for text in [None, {}, [], "", "{}", "*A hike.*", "No punctuation", "A. B. C.", "x" * 401,
                     "Walk 999 km. Enjoy."]:
            self.assertFalse(briefing.validate(text, {"distance_km": 3})[0], text)


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.ctx = SimpleNamespace(user_osm_id=None, llm_down=None, elapsed=lambda: 0, trace=agents.Trace())
        self.plan = {"trail": {"name": "Cold trail", "distance_km": 3},
                     "checker": {"approved": False, "problems": ["Checker rejected: active fire"]},
                     "weather": {"summary": "Cold, -15F"},
                     "hike_window": {"available": False, "error": "Missing middle hour"}}
        self.warnings = ["Extreme cold: -15F", "Thunderstorms during the hike", "Long warning " + "x" * 1500,
                         "Fourth warning", "Fifth warning"]

    def assert_critical(self, result):
        text = result["text"]
        for warning in self.warnings + self.plan["checker"]["problems"]:
            self.assertIn(warning, text)
        self.assertLess(text.index(self.warnings[-1]), text.index("Cold trail"))
        self.assertIn("full hike was not checked", text)
        self.assertGreater(len(text), briefing.MAX_CHARS)

    def test_every_fallback_trigger_is_deterministic_and_keeps_all_warnings(self):
        for output in [None, {}, "malformed", "Walk 999 km.", "A. B. C."]:
            with self.subTest(output=output), patch.object(agents, 'llm_briefing', return_value=output):
                result = agents.make_briefing(self.ctx, self.plan, self.warnings)
                self.assertEqual(result["source"], "template")
                self.assert_critical(result)
                self.assertEqual(result["text"], briefing.template(self.plan, self.warnings))
        for error in [agents.LLMUnavailable("timeout"), agents.LLMUnavailable("model failure"), RuntimeError("bad reply")]:
            with self.subTest(error=type(error)), patch.object(agents, 'llm_briefing', side_effect=error):
                self.assert_critical(agents.make_briefing(self.ctx, self.plan, self.warnings))

    def test_skip_paths_do_not_call_model(self):
        for field, value in [('user_osm_id', 42), ('llm_down', 'failed'), ('elapsed', lambda: 100)]:
            old = getattr(self.ctx, field)
            setattr(self.ctx, field, value)
            with patch.object(agents, 'llm_briefing', side_effect=AssertionError('must not call')):
                self.assert_critical(agents.make_briefing(self.ctx, self.plan, self.warnings))
            setattr(self.ctx, field, old)

    def test_successful_model_text_also_preserves_every_critical_warning(self):
        with patch.object(agents, 'llm_briefing', return_value="Cold trail is 3 km. Expect -15F."):
            result = agents.make_briefing(self.ctx, self.plan, self.warnings)
        self.assertEqual(result['source'], 'gemini')
        for warning in self.warnings + self.plan['checker']['problems']:
            self.assertIn(warning, result['text'])

    def test_empty_and_no_trail_plans(self):
        self.assertIn("No mapped trail", briefing.template({}, []))
        for trail in [None, {"available": False}]:
            text = briefing.template({"trail": trail}, self.warnings)
            for warning in self.warnings:
                self.assertIn(warning, text)
            self.assertNotIn("0.0 km", text)

    def test_no_trail_api_response_retains_warning_in_briefing(self):
        from backend.test_anywhere import NOMINATIM
        with Offline(nominatim=NOMINATIM):
            result = offline_plan("A hike in Antarctica tomorrow")
        self.assertIsNone(result['trail'])
        self.assertFalse(result['checker']['approved'])
        for warning in result['warnings']:
            self.assertIn(warning, result['briefing']['text'])

    def test_actual_briefing_timeout_does_not_wait_for_blocked_model(self):
        from google.genai import types
        entered, release = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            release.wait(5)
            return FakeReply("Cold trail is 3 km. Expect -15F.")
        fake = FakeGemini(lambda: None, checker=blocked)
        # Use a minimal SDK fake so the actual llm_briefing timeout path runs.
        fake.models = SimpleNamespace(generate_content=lambda **kwargs: blocked())
        try:
            with patch.object(agents, '_gemini', return_value=(fake, 'gemini-test', types)), \
                 patch.object(briefing, 'BRIEFING_TIMEOUT_S', 0.05):
                start = time.perf_counter()
                result = agents.make_briefing(self.ctx, self.plan, self.warnings)
                self.assertLess(time.perf_counter() - start, 1)
                self.assertTrue(entered.is_set())
                self.assertEqual(result['source'], 'template')
                self.assert_critical(result)
        finally:
            release.set()


if __name__ == '__main__':
    unittest.main()
