"""Explicit REAL Atlas + Chrome generate/save/refresh/reopen integration check.

Requires the separately started test server on 8011, Gemini disabled and
DEMO_RECORD=0. Uses a fresh browser owner and deletes only that owner's labeled
integration record. Does not run as part of the offline suite.
"""
import hashlib
import os
from pathlib import Path
import sys
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, expect

BASE = os.getenv('TREESCOUT_URL', 'http://127.0.0.1:8011').rstrip('/')
if urlparse(BASE).port != 8011:
    raise RuntimeError('Integration checks must use the isolated server on 8011')


def run():
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    from backend.history import collection
    owner_hash = None
    coll = collection()
    try:
        with sync_playwright() as p:
            executable = os.getenv('TREKCHEK_BROWSER_EXECUTABLE')
            if not executable:
                executable = next((x for x in ['C:/Program Files/Google/Chrome/Application/chrome.exe',
                                              'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'] if Path(x).exists()), None)
            browser = p.chromium.launch(headless=True, **({'executable_path': executable} if executable else {}))
            try:
                context = browser.new_context()
                page = context.new_page()
                page.goto(BASE)
                page.wait_for_selector('#saved-hikes')
                token = page.evaluate("localStorage.getItem('trekchek-history-token')")
                assert token and len(token) == 64
                owner_hash = hashlib.sha256(token.encode()).hexdigest()
                page.locator('#use-mock').uncheck()
                page.locator('#request').fill('ATLAS BROWSER INTEGRATION CHECK: Easy 3 mile walk near Ypsilanti tomorrow morning')
                with page.expect_response(lambda r: urlparse(r.url).path == '/plan', timeout=90000) as response:
                    page.locator('#plan-btn').click()
                generated = response.value.json()
                assert response.value.status == 200 and generated.get('trail')
                assert generated['constraints']['parsed_by'] == 'rules', 'Run test server with Gemini disabled'
                expect(page.locator('#save-hike')).to_be_enabled()
                with page.expect_response(lambda r: urlparse(r.url).path == '/history' and r.request.method == 'POST') as saved_response:
                    page.locator('#save-hike').click()
                assert saved_response.value.status == 201
                saved = saved_response.value.json()
                expect(page.locator('#saved-hikes option')).to_have_count(2)
                page.reload()
                expect(page.locator('#saved-hikes option')).to_have_count(2)
                with page.expect_response(lambda r: urlparse(r.url).path == '/history/' + saved['id']) as reopened_response:
                    page.locator('#saved-hikes').select_option(saved['id'])
                reopened = reopened_response.value.json()
                assert reopened['plan'] == generated, 'Snapshot changed during persistence'
                assert reopened['saved_at'] == saved['saved_at']
                expect(page.locator('#snapshot-notice')).to_contain_text('Weather and warnings have not been refreshed')
                expect(page.locator('#mode-badges')).to_contain_text('Saved snapshot from')
                assert 'Live' not in page.locator('#mode-badges').inner_text()
                print('PASS: real browser generate/save/refresh/reopen; complete Atlas snapshot unchanged.')
                print('Leaflet loaded:', page.evaluate("typeof L !== 'undefined'"))
            finally:
                browser.close()
    finally:
        if owner_hash:
            # Fresh context means this owner is unique to this invocation.
            coll.delete_many({'owner': owner_hash, 'request': {'$regex': '^ATLAS BROWSER INTEGRATION CHECK:'}})
            assert coll.count_documents({'owner': owner_hash}) == 0
            print('Temporary browser integration records removed.')


if __name__ == '__main__':
    try:
        run()
    except Exception:
        # Never expose driver exception text, request headers or credentials.
        raise SystemExit('Browser/Atlas integration failed. Check the isolated server and Atlas setup; no credentials were printed.')
