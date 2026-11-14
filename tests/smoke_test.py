"""Check a running TreeScout server using only the standard library."""

import json
import unittest
from urllib.request import urlopen


class SmokeTests(unittest.TestCase):
    def test_health(self):
        with urlopen("http://localhost:8000/health", timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.load(response), {"ok": True})

    def test_frontend(self):
        for path, expected in [("/", b"TreeScout"), ("/app.js", b"checkHealth"), ("/styles.css", b":root")]:
            with self.subTest(path=path):
                with urlopen(f"http://localhost:8000{path}", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn(expected, response.read())


if __name__ == "__main__":
    unittest.main()
