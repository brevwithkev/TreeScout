"""Check a running TrekChek server using only the standard library.

Uses TREESCOUT_URL (default http://127.0.0.1:8011, the test server). Never point it at port 8000: that is the
developer's own server.
"""

import json
import os
import unittest
from urllib.request import urlopen

BASE = os.environ.get("TREESCOUT_URL", "http://127.0.0.1:8011").rstrip("/")


class SmokeTests(unittest.TestCase):
    def test_health(self):
        with urlopen(f"{BASE}/health", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response), {"ok": True})

    def test_frontend(self):
        for path, expected in [("/", b"TrekChek"), ("/app.js", b"checkHealth"), ("/styles.css", b":root")]:
            with self.subTest(path=path):
                with urlopen(f"{BASE}{path}", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn(expected, response.read())


if __name__ == "__main__":
    unittest.main()
