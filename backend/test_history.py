"""Offline HTTP-level history tests; no Atlas credentials or external calls."""
import copy
from datetime import datetime, timezone
import os
import unittest
from unittest.mock import patch

import mongomock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pymongo.errors import ServerSelectionTimeoutError, ConnectionFailure, OperationFailure, NetworkTimeout

from backend import history


class HistoryTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(history.router)
        self.client = TestClient(app)
        self.db = mongomock.MongoClient().trekchek.hike_history
        self.patch = patch.object(history, "collection", return_value=self.db)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.headers = {"X-History-Token": "a" * 64}
        self.plan = {"request": "A cold hike", "trail": {"name": "Real trail", "coords": [[42, -83], [42.01, -83]]},
                     "warnings": ["Extreme cold: -15F"], "hike_window": {"min_temp_f": -15},
                     "checker": {"approved": False}}

    def save(self):
        response = self.client.post("/history", json={"plan": self.plan}, headers=self.headers)
        self.assertEqual(response.status_code, 201)
        return response.json()["id"]

    def test_round_trip_preserves_plan_warnings_and_sign(self):
        original = copy.deepcopy(self.plan)
        identifier = self.save()
        loaded = self.client.get(f"/history/{identifier}", headers=self.headers).json()
        self.assertEqual(loaded["plan"], original)
        self.assertIn("saved_at", loaded)
        rows = self.client.get("/history", headers=self.headers).json()["items"]
        self.assertEqual(rows[0]["id"], identifier)
        self.assertNotIn("plan", rows[0])
        self.assertNotIn("owner", rows[0])

    def test_another_browser_cannot_list_or_read(self):
        identifier = self.save()
        other = {"X-History-Token": "b" * 64}
        self.assertEqual(self.client.get("/history", headers=other).json()["items"], [])
        self.assertEqual(self.client.get(f"/history/{identifier}", headers=other).status_code, 404)

    def test_missing_or_short_token_rejected(self):
        self.assertEqual(self.client.get("/history").status_code, 422)
        self.assertEqual(self.client.get("/history", headers={"X-History-Token": "short"}).status_code, 422)

    def test_pagination_returns_every_saved_hike_once(self):
        identifiers = {self.save() for _ in range(3)}
        first = self.client.get("/history?limit=2", headers=self.headers).json()
        second = self.client.get("/history", params={"limit": 2, "before": first["next_before"]}, headers=self.headers).json()
        self.assertEqual({x["id"] for x in first["items"] + second["items"]}, identifiers)
        self.assertIsNone(second["next_before"])
        self.assertEqual(self.client.get("/history?limit=1000", headers=self.headers).status_code, 422)

    def test_missing_trail_rejected(self):
        self.plan["trail"] = None
        self.assertEqual(self.client.post("/history", json={"plan": self.plan}, headers=self.headers).status_code, 422)

    def test_oversized_snapshot_rejected(self):
        self.plan["request"] = "x" * 2_000_001
        self.assertEqual(self.client.post("/history", json={"plan": self.plan}, headers=self.headers).status_code, 413)

    def test_database_errors_are_sanitized(self):
        with patch.object(self.db, "insert_one", side_effect=ServerSelectionTimeoutError("secret-host")):
            response = self.client.post("/history", json={"plan": self.plan}, headers=self.headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret-host", response.text)

    def test_no_configuration_returns_503(self):
        self.patch.stop()
        with patch.dict(os.environ, {"MONGODB_URI": ""}):
            self.assertEqual(self.client.get("/history", headers=self.headers).status_code, 503)

    def test_indexes_support_owner_and_time(self):
        history.setup_indexes()
        self.assertIn([("owner", 1), ("saved_at", -1), ("_id", -1)],
                      [index["key"] for index in self.db.index_information().values()])

    def test_history_failure_does_not_break_plan_endpoint(self):
        from backend.main import app
        client = TestClient(app)
        with patch("backend.main.make_plan", return_value=self.plan), patch("backend.main.demo.demo_mode", return_value=False), patch("backend.main.demo.demo_record", return_value=False):
            response = client.post("/plan", json={"request": "A cold hike"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), self.plan)

    def test_independent_clients_share_persisted_history_and_only_hash_is_stored(self):
        identifier = self.save()
        other_request_client = TestClient(self.client.app)
        response = other_request_client.get(f'/history/{identifier}', headers=self.headers)
        self.assertEqual(response.json()['plan'], self.plan)
        row = self.db.find_one({'_id': identifier})
        self.assertNotEqual(row['owner'], self.headers['X-History-Token'])
        self.assertEqual(len(row['owner']), 64)
        self.assertEqual(datetime.fromisoformat(row['saved_at']).utcoffset().total_seconds(), 0)
        self.assertNotIn(self.headers['X-History-Token'], str(row))

    def test_malformed_credentials_are_not_echoed(self):
        for token in ['a' * 32, 'g' * 64, ' ' * 64, 'A' * 64, 'bearer-secret-credential']:
            for path in ['/history', '/history/' + 'a' * 32]:
                response = self.client.get(path, headers={'X-History-Token': token})
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(token, response.text)

    def test_invalid_bodies(self):
        for body in [{}, {'plan': None}, {'plan': []}, {'plan': {}}, {'plan': self.plan, 'extra': 1},
                     {'plan': {**self.plan, 'request': ''}}, {'plan': {**self.plan, 'trail': {'coords': []}}}]:
            response = self.client.post('/history', json=body, headers=self.headers)
            self.assertEqual(response.status_code, 422, body)
        response = self.client.post('/history', content='{bad json', headers=self.headers)
        self.assertEqual(response.status_code, 422)

    def test_invalid_coordinates_and_warnings_are_rejected(self):
        for coords in [[None, None], [['42', -83], [42, -83]], [[91, 0], [42, -83]],
                       [[42, -181], [42, -83]], [[True, 0], [42, -83]], [[0, 0, 0], [42, -83]]]:
            body = {'plan': {**self.plan, 'trail': {'coords': coords}}}
            self.assertEqual(self.client.post('/history', json=body, headers=self.headers).status_code, 422)
        for warnings in [None, {}, 'a warning', [42]]:
            body = {'plan': {**self.plan, 'warnings': warnings}}
            self.assertEqual(self.client.post('/history', json=body, headers=self.headers).status_code, 422)

    def test_nonfinite_snapshot_is_rejected_without_crashing(self):
        import json
        for value in [float('nan'), float('inf'), -float('inf')]:
            body = json.dumps({'plan': {**self.plan, 'untrusted': value}})
            response = self.client.post('/history', content=body,
                                        headers={**self.headers, 'Content-Type': 'application/json'})
            self.assertEqual(response.status_code, 422)

    def test_body_limit_including_streamed_request(self):
        def chunks():
            yield b'{"plan": {"request": "'
            for _ in range(21):
                yield b'x' * 100_000
        response = self.client.post('/history', content=chunks(), headers=self.headers)
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.db.count_documents({}), 0)

    def test_nonexistent_and_malformed_ids(self):
        for identifier in ['a' * 32, 'not-an-id', 'x' * 200]:
            self.assertEqual(self.client.get('/history/' + identifier, headers=self.headers).status_code, 404)

    def test_pagination_empty_boundaries_invalid_cursor_and_timestamp_ties(self):
        self.assertEqual(self.client.get('/history', headers=self.headers).json(), {'items': [], 'next_before': None})
        with patch.object(history, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 11, 15, tzinfo=timezone.utc)
            ids = {self.save() for _ in range(5)}
        seen, cursor = [], None
        for _ in range(6):
            params = {'limit': 1}
            if cursor:
                params['before'] = cursor
            page = self.client.get('/history', params=params, headers=self.headers).json()
            seen += [item['id'] for item in page['items']]
            cursor = page['next_before']
            if cursor is None:
                break
        self.assertEqual(set(seen), ids)
        self.assertEqual(len(seen), 5)
        for limit in ['0', '-1', '51', 'oops']:
            self.assertEqual(self.client.get('/history', params={'limit': limit}, headers=self.headers).status_code, 422)
        for cursor in ['not-a-cursor', 'e30', 'WzEsMl0', 'W251bGwsbnVsbF0']:
            self.assertEqual(self.client.get('/history', params={'before': cursor}, headers=self.headers).status_code, 422)

    def test_all_database_operation_failures_are_sanitized(self):
        for error in [ServerSelectionTimeoutError, ConnectionFailure, OperationFailure, NetworkTimeout]:
            for method, operation in [('insert_one', lambda: self.client.post('/history', json={'plan': self.plan}, headers=self.headers)),
                                      ('find', lambda: self.client.get('/history', headers=self.headers)),
                                      ('find_one', lambda: self.client.get('/history/' + 'a' * 32, headers=self.headers))]:
                with self.subTest(error=error, method=method), patch.object(self.db, method, side_effect=error('secret://credential')):
                    response = operation()
                    self.assertEqual(response.status_code, 503)
                    self.assertNotIn('secret', response.text)

    def test_real_collection_factory_reuses_client_creates_index_and_bounds_timeouts(self):
        self.patch.stop()
        client = mongomock.MongoClient()
        with patch.dict(os.environ, {'MONGODB_URI': 'mongodb://test-only', 'MONGODB_DB': ''}), \
             patch.object(history, '_client', None), patch.object(history, '_indexed', set()), \
             patch.object(history, 'MongoClient', return_value=client) as factory:
            first, second = history.collection(), history.collection()
            self.assertIs(first, second)
            factory.assert_called_once()
            options = factory.call_args.kwargs
            for name in ['serverSelectionTimeoutMS', 'connectTimeoutMS', 'socketTimeoutMS', 'timeoutMS']:
                self.assertGreater(options[name], 0)
                self.assertLessEqual(options[name], 3000)
            self.assertEqual(first.database.name, 'trekchek')
            self.assertIn([('owner', 1), ('saved_at', -1), ('_id', -1)],
                          [entry['key'] for entry in first.index_information().values()])

    def test_connection_and_index_failures_are_sanitized_and_retryable(self):
        self.patch.stop()
        for error in [ValueError('secret-uri'), ServerSelectionTimeoutError('secret-uri')]:
            with patch.dict(os.environ, {'MONGODB_URI': 'mongodb://test-only'}), \
                 patch.object(history, '_client', None), patch.object(history, 'MongoClient', side_effect=error):
                response = self.client.get('/history', headers=self.headers)
                self.assertEqual(response.status_code, 503)
                self.assertNotIn('secret-uri', response.text)
        client = mongomock.MongoClient()
        with patch.dict(os.environ, {'MONGODB_URI': 'mongodb://test-only', 'MONGODB_DB': 'trekchek'}), \
             patch.object(history, '_client', client), patch.object(history, '_indexed', set()), \
             patch.object(client.trekchek.hike_history, 'create_index', side_effect=OperationFailure('secret-index')):
            response = self.client.get('/history', headers=self.headers)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('secret-index', response.text)
            self.assertFalse(history._indexed)

    def test_simulated_integration_command_cleans_only_its_own_records(self):
        identifier = self.save()
        history.verify_round_trip()
        self.assertEqual([r['_id'] for r in self.db.find({})], [identifier])
