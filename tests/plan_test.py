"""Live check that POST /plan never returns mock_plan.json placeholders.

Needs a running server: python -m unittest discover -s tests -p "*_test.py"
Uses TREESCOUT_URL (default http://127.0.0.1:8011, the test server). Never point it at port 8000: that is the
developer's own server (Gemini on).
"""

import json
import os
import unittest
from urllib.request import Request, urlopen

BASE = os.environ.get("TREESCOUT_URL", "http://127.0.0.1:8011").rstrip("/")
FORBIDDEN = ("Acorn Nature Trail", "River Bend Path", "Example Loop", "Found 14 named trails")
REQUESTS = [
    {"request": "Easy 3 mile walk near Ypsilanti tomorrow morning"},
    # What the frontend sends: text plus explicit form fields.
    {"request": "5 mile shaded hike near Ann Arbor tomorrow", "lat": 42.2411, "lon": -83.6130,
     "distance_km": 8, "start": "10:00", "prefs": ["shaded"]},
    {"request": "A hike in Antarctica tomorrow"},
]


def post_plan(body):
    request = Request(f"{BASE}/plan", data=json.dumps(body).encode("utf-8"), method="POST",
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=90) as response:
        return response.status, response.read().decode("utf-8")


class PlanHasNoMockValues(unittest.TestCase):
    def test_plan_output_contains_no_mock_values(self):
        with urlopen(f"{BASE}/mock_plan.json", timeout=10) as response:
            contract = json.load(response)
        for body in REQUESTS:
            with self.subTest(request=body["request"]):
                status, text = post_plan(body)
                self.assertEqual(status, 200)
                for value in FORBIDDEN:
                    self.assertNotIn(value, text, f"mock value {value!r} leaked into /plan output")
                plan = json.loads(text)
                self.assertEqual(list(plan), list(contract), "top-level keys must match mock_plan.json")
                self.assertIn(plan["constraints"]["parsed_by"], ("gemini", "rules"))
                self.assertTrue(plan["agent_trace"], "agent_trace must not be empty")

    def test_outside_coverage(self):
        _, text = post_plan({"request": "A hike in Antarctica tomorrow"})
        plan = json.loads(text)
        self.assertIsNone(plan["trail"])
        self.assertTrue(any(w.startswith(("No mapped trails found near", "Trail data unavailable"))
                            for w in plan["warnings"]), plan["warnings"])


if __name__ == "__main__":
    unittest.main()
