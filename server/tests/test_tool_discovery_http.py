"""HTTP gateway regressions with real HTTPX responses and synthetic credentials."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from server import config, db, keysvc, security
from server.routers import responses as R
from server.tests.test_tool_discovery import SEARCH


class DiscoveryHTTPTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = config.DB_PATH, config.USERS_FILE
        config.DB_PATH = Path(self.tmp.name) / 'test.db'
        config.USERS_FILE = Path(self.tmp.name) / 'users.json'
        db._conn = None
        db.connect()
        security.save_users({'secret': 'fixture', 'users': [], 'api_keys': []})
        self.token = keysvc.create_key('fixture')['key']
        from server.main import app
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE = self.original
        self.tmp.cleanup()

    def post(self, **body):
        return self.client.post('/v1/responses',
            headers={'Authorization': f'Bearer {self.token}'},
            json={'model': 'glm-5.2', 'input': 'Find the fixture.', **body})

    def test_hosted_search_returns_400_before_contacting_upstream(self):
        with mock.patch.object(config, 'http_client') as factory:
            result = self.post(tools=[{'type': 'tool_search'}])
        self.assertEqual(result.status_code, 400)
        factory.assert_not_called()

    def test_stream_completion_keeps_usage_from_separate_trailing_chunk(self):
        frames = [
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 's',
                'function': {'name': 'tool_search', 'arguments': '{"query":"fixture"}'}}]},
                'finish_reason': 'tool_calls'}]},
            {'choices': [], 'usage': {'prompt_tokens': 1234, 'completion_tokens': 20,
                'prompt_tokens_details': {'cached_tokens': 1000}}},
        ]
        raw = ''.join('data: ' + json.dumps(f) + '\n\n' for f in frames) + 'data: [DONE]\n\n'
        def handle(req):
            payload = json.loads(req.content)
            self.assertTrue(payload['stream_options']['include_usage'])
            return httpx.Response(200, content=raw, headers={'Content-Type': 'text/event-stream'})
        transport = httpx.MockTransport(handle)
        with mock.patch.object(config, 'http_client',
                               lambda *a, **kw: httpx.AsyncClient(transport=transport)):
            result = self.post(stream=True, tools=[SEARCH])
        self.assertEqual(result.status_code, 200)
        events = [json.loads(line[5:]) for line in result.text.splitlines()
                  if line.startswith('data:')]
        terminal = events[-1]
        self.assertEqual(terminal['type'], 'response.completed')
        self.assertEqual(terminal['response']['usage']['input_tokens'], 1234)
        self.assertEqual(terminal['response']['usage']['input_tokens_details']['cached_tokens'], 1000)
        self.assertEqual(terminal['response']['output'][0]['type'], 'tool_search_call')

    def test_real_httpx_stream_error_keeps_error_detail(self):
        transport = httpx.MockTransport(lambda req: httpx.Response(503,
            json={'error': {'message': 'fixture route unavailable'}}))
        with mock.patch.object(config, 'http_client',
                               lambda *a, **kw: httpx.AsyncClient(transport=transport)):
            result = self.post(stream=True)
        self.assertEqual(result.status_code, 503)
        self.assertIn('fixture route unavailable', result.text)

    def test_bad_json_search_returns_502_on_nonstreaming_endpoint(self):
        transport = httpx.MockTransport(lambda req: httpx.Response(200, json={
            'choices': [{'finish_reason': 'tool_calls', 'message': {'tool_calls': [
                {'id': 's', 'function': {'name': 'tool_search', 'arguments': '{'}}]}}]}))
        with mock.patch.object(config, 'http_client',
                               lambda *a, **kw: httpx.AsyncClient(transport=transport)):
            result = self.post(tools=[SEARCH])
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.json()['error']['code'], 'invalid_tool_search_arguments')


if __name__ == '__main__':
    unittest.main()
