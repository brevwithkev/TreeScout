"""Real Chrome UI tests with mocked HTTP APIs (NOT a real Atlas integration).

Start the test server on 8011; run python tests/browser_history.py.
Third-party map/audio services are blocked to keep the suite deterministic.
"""
import copy
import json
import os
from pathlib import Path
import time
import unittest
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
BASE = os.getenv('TREESCOUT_URL', 'http://127.0.0.1:8011').rstrip('/')
if urlparse(BASE).port != 8011:
    raise RuntimeError('Browser checks must use the isolated server on port 8011')


class HistoryBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        executable = os.getenv('TREKCHEK_BROWSER_EXECUTABLE')
        if not executable:
            executable = next((p for p in ['C:/Program Files/Google/Chrome/Application/chrome.exe',
                               'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'] if Path(p).exists()), None)
        cls.browser = cls.playwright.chromium.launch(headless=True, **({'executable_path': executable} if executable else {}))

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda exc: self.errors.append(str(exc)))
        self.records = {}
        self.tokens = []
        self.pending = []
        self.delay_id = None
        self.fail_save = False
        self.fail_load = False
        self.fail_list = False
        self.hold_save = False
        self.plan = json.loads((ROOT / 'mock_plan.json').read_text(encoding='utf-8'))
        self.plan['request'] = 'Browser regression hike'
        self.plan['trail']['name'] = 'Cold regression trail'
        self.plan['warnings'] = ['Extreme cold: -15F', 'Do not hike during thunderstorms']
        self.plan['hike_window'] = {'available': False, 'source': 'NWS hourly', 'error': 'Missing full coverage'}
        self.plan['briefing'] = {'text': 'Extreme cold: -15F. Do not hike during thunderstorms.', 'source': 'template'}
        self.page.route('**/*', self.route)
        self.page.goto(BASE)
        expect(self.page.locator('#history-status')).to_contain_text('No saved hikes yet')

    def route(self, route):
        path = urlparse(route.request.url).path
        if not route.request.url.startswith(BASE + '/'):
            route.abort()
            return
        if path == '/history' or path.startswith('/history/'):
            self.tokens.append(route.request.headers.get('x-history-token'))
            if route.request.method == 'POST':
                if self.hold_save:
                    self.pending.append(route)
                elif self.fail_save:
                    route.fulfill(status=503, json={'detail': 'History save unavailable'})
                else:
                    self.records['a' * 32] = {'plan': route.request.post_data_json['plan'], 'saved_at': '2026-11-15T12:00:00+00:00'}
                    route.fulfill(status=201, json={'id': 'a' * 32, 'saved_at': '2026-11-15T12:00:00+00:00'})
            elif path == '/history':
                if self.fail_list:
                    route.fulfill(status=503, json={'detail': 'History unavailable; plan still usable'})
                else:
                    route.fulfill(json={'items': [{'id': key, 'saved_at': value['saved_at'], 'trail_name': value['plan']['trail']['name'],
                                                  'request': value['plan']['request']} for key, value in self.records.items()], 'next_before': None})
            elif path.rsplit('/', 1)[-1] == self.delay_id:
                self.pending.append(route)
            elif self.fail_load:
                route.fulfill(status=503, json={'detail': 'History load unavailable'})
            else:
                route.fulfill(json=self.records[path.rsplit('/', 1)[-1]])
        elif path == '/plan':
            route.fulfill(json=self.plan)
        elif path == '/health':
            route.fulfill(json={'ok': True})
        elif path == '/demo/plans':
            route.fulfill(json=[])
        elif path == '/tts':
            route.fulfill(status=502, json={'detail': 'Audio disabled in browser tests'})
        else:
            route.continue_()

    def generate(self):
        self.page.locator('#use-mock').uncheck()
        self.page.locator('#request').fill(self.plan['request'])
        self.page.locator('#plan-btn').click()
        expect(self.page.locator('#save-hike')).to_be_enabled()
        expect(self.page.locator('#results')).to_contain_text(self.plan['trail']['name'])

    def wait_pending(self):
        deadline = time.monotonic() + 5
        while not self.pending and time.monotonic() < deadline:
            self.page.wait_for_timeout(10)
        self.assertTrue(self.pending, 'Expected a deferred API response')

    def seed_list(self):
        for key, name in [('a' * 32, 'Older trail'), ('b' * 32, 'Newer trail')]:
            plan = copy.deepcopy(self.plan)
            plan['trail']['name'] = name
            self.records[key] = {'plan': plan, 'saved_at': '2026-11-15T12:00:00+00:00'}
        self.page.locator('#refresh-hikes').click()
        expect(self.page.locator('#saved-hikes option')).to_have_count(3)

    def test_save_refresh_reopen_preserves_plan_token_and_snapshot_label(self):
        self.generate()
        self.page.locator('#save-hike').click()
        expect(self.page.locator('#history-status')).to_contain_text('Saved to Atlas')
        self.assertEqual(self.records['a' * 32]['plan'], self.plan)
        token = self.page.evaluate("localStorage.getItem('trekchek-history-token')")
        self.assertRegex(token, '^[a-f0-9]{64}$')
        self.page.reload()
        expect(self.page.locator('#saved-hikes option')).to_have_count(2)
        self.page.locator('#saved-hikes').select_option('a' * 32)
        expect(self.page.locator('#snapshot-notice')).to_contain_text('Weather and warnings have not been refreshed')
        expect(self.page.locator('#mode-badges')).to_contain_text('Saved snapshot from')
        self.assertNotIn('Live', self.page.locator('#mode-badges').inner_text())
        expect(self.page.locator('#results')).to_contain_text('Extreme cold: -15F')
        self.page.locator('#refresh-hikes').click()
        expect(self.page.locator('#snapshot-notice')).to_be_visible()
        self.assertTrue(all(t == token for t in self.tokens))
        self.assertEqual(self.errors, [])

    def test_failed_save_preserves_plan_and_can_retry(self):
        self.generate()
        self.fail_save = True
        self.page.locator('#save-hike').click()
        expect(self.page.locator('#history-status')).to_contain_text('History save unavailable')
        expect(self.page.locator('#save-hike')).to_be_enabled()
        expect(self.page.locator('#results')).to_contain_text('Cold regression trail')
        self.fail_save = False
        self.page.locator('#save-hike').click()
        expect(self.page.locator('#history-status')).to_contain_text('Saved to Atlas')

    def test_empty_and_unavailable_history_does_not_break_planning(self):
        self.fail_list = True
        self.page.locator('#refresh-hikes').click()
        expect(self.page.locator('#history-status')).to_contain_text('History unavailable')
        self.generate()
        expect(self.page.locator('#save-hike')).to_be_enabled()

    def test_failed_load_clears_stale_save_and_reports_error(self):
        self.generate()
        self.seed_list()
        self.fail_load = True
        self.page.locator('#saved-hikes').select_option('a' * 32)
        expect(self.page.locator('#history-status')).to_contain_text('History load unavailable')
        expect(self.page.locator('#save-hike')).to_be_disabled()
        expect(self.page.locator('#plan-btn')).to_be_enabled()

    def test_rapid_switching_ignores_old_response(self):
        self.seed_list()
        self.delay_id = 'a' * 32
        self.page.locator('#saved-hikes').select_option('a' * 32)
        self.wait_pending()
        self.page.locator('#saved-hikes').select_option('b' * 32)
        expect(self.page.locator('#history-status')).to_contain_text('Saved hike reopened')
        self.pending.pop().fulfill(json=self.records['a' * 32])
        self.page.wait_for_timeout(100)
        expect(self.page.locator('#results')).to_contain_text('Newer trail')
        self.assertNotIn('Older trail', self.page.locator('#results').inner_text())
        self.assertEqual(self.errors, [])

    def test_generation_supersedes_pending_history_load(self):
        self.seed_list()
        self.delay_id = 'a' * 32
        self.page.locator('#saved-hikes').select_option('a' * 32)
        self.wait_pending()
        self.page.locator('#use-mock').uncheck()
        self.page.locator('#request').fill('A new generated hike')
        self.page.locator('#plan-form').dispatch_event('submit')
        expect(self.page.locator('#save-hike')).to_be_enabled()
        self.pending.pop().fulfill(json=self.records['a' * 32])
        self.page.wait_for_timeout(100)
        expect(self.page.locator('#results')).to_contain_text('Cold regression trail')
        expect(self.page.locator('#snapshot-notice')).to_be_hidden()

    def test_old_save_completion_does_not_replace_new_load_status(self):
        self.generate()
        self.seed_list()
        self.hold_save = True
        self.page.locator('#save-hike').click()
        self.wait_pending()
        self.page.locator('#saved-hikes').select_option('b' * 32)
        expect(self.page.locator('#history-status')).to_contain_text('Saved hike reopened')
        self.pending.pop().fulfill(status=201, json={'id': 'c' * 32, 'saved_at': '2026-11-15T12:00:00+00:00'})
        self.page.wait_for_timeout(100)
        expect(self.page.locator('#history-status')).to_contain_text('Saved hike reopened')
        expect(self.page.locator('#results')).to_contain_text('Newer trail')
        expect(self.page.locator('#snapshot-notice')).to_be_visible()


if __name__ == '__main__':
    unittest.main(verbosity=2)
