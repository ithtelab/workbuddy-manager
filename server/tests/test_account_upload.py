"""JSON account import accepts supported exports and isolates bad files."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import config, security
from server.routers import accounts


class AccountUploadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.auth_dir = Path(self.tmp.name) / 'auths'
        self.auth_dir.mkdir()
        self.group = {'id': None, 'name': '默认分组', 'auth_dir': str(self.auth_dir),
                      'is_default': True, 'base_url': 'http://127.0.0.1'}
        self.config_patch = mock.patch.object(config, 'AUTH_DIR', self.auth_dir)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.group_patch = mock.patch.object(accounts.upstreamsvc, 'default_upstream',
                                             return_value=self.group)
        self.group_patch.start()
        self.addCleanup(self.group_patch.stop)
        self.reload_patch = mock.patch.object(accounts.reload, 'request_reload_or_restart')
        self.reload = self.reload_patch.start()
        self.addCleanup(self.reload_patch.stop)
        app = FastAPI()
        app.include_router(accounts.router)
        app.dependency_overrides[security.require_session_admin] = lambda: {
            'username': 'test', 'role': 'admin'}
        self.client = TestClient(app)

    def test_imports_canonical_and_flat_exports(self) -> None:
        canonical = {
            'account': {'uid': 'canonical-1', 'nickname': 'Canonical'},
            'auth': {'accessToken': 'at-1', 'refreshToken': 'rt-1',
                     'expiresAt': 4102444800, 'domain': 'www.codebuddy.cn', 'realm': 'cn'},
            'device_token': 'device-1',
        }
        flat = [{'uid': 'flat-1', 'access_token': 'at-2', 'refresh_token': 'rt-2',
                 'expires_at': 4102444800, 'nickname': 'Flat'}]
        response = self.client.post(
            '/api/accounts/upload',
            files=[
                ('files', ('canonical.json', json.dumps(canonical), 'application/json')),
                ('files', ('flat.json', json.dumps(flat), 'application/json')),
            ],
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body['uploaded']), 2)
        self.assertEqual(body['failed'], [])
        saved = json.loads((self.auth_dir / 'workbuddy-canonical-1.json').read_text())
        self.assertEqual(saved['auth']['accessToken'], 'at-1')
        self.assertEqual(saved['device_token'], 'device-1')
        self.assertEqual(json.loads((self.auth_dir / 'workbuddy-flat-1.json').read_text())
                         ['account']['nickname'], 'Flat')
        self.assertEqual(self.reload.call_count, 2)

    def test_bad_file_does_not_block_valid_file(self) -> None:
        response = self.client.post(
            '/api/accounts/upload',
            files=[
                ('files', ('bad.json', '{"account": {}}', 'application/json')),
                ('files', ('good.json', json.dumps({
                    'uid': 'good-1', 'access_token': 'at', 'expires_at': 4102444800,
                }), 'application/json')),
            ],
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([item['uid'] for item in body['uploaded']], ['good-1'])
        self.assertEqual(body['failed'][0]['file'], 'bad.json')
        self.assertTrue((self.auth_dir / 'workbuddy-good-1.json').exists())

    def test_duplicate_uid_is_reported_as_update(self) -> None:
        self.client.post('/api/accounts/upload', files=[
            ('files', ('first.json', json.dumps({
                'uid': 'same-1', 'access_token': 'old', 'expires_at': 4102444800,
            }), 'application/json')),
        ])
        response = self.client.post('/api/accounts/upload', files=[
            ('files', ('second.json', json.dumps({
                'uid': 'same-1', 'access_token': 'new', 'expires_at': 4102444800,
            }), 'application/json')),
        ])
        self.assertTrue(response.json()['uploaded'][0]['updated'])
        self.assertEqual(json.loads((self.auth_dir / 'workbuddy-same-1.json').read_text())
                         ['auth']['accessToken'], 'new')

    def test_imports_multiple_accounts_from_one_json_array(self) -> None:
        response = self.client.post('/api/accounts/upload', files=[
            ('files', ('accounts.json', json.dumps([
                {'uid': 'array-1', 'access_token': 'at-1', 'expires_at': 4102444800},
                {'uid': 'array-2', 'access_token': 'at-2', 'expires_at': 4102444800},
            ]), 'application/json')),
        ])
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['uid'] for item in response.json()['uploaded']], ['array-1', 'array-2'])

    def test_imports_flat_export_without_expiry(self) -> None:
        response = self.client.post('/api/accounts/upload', files=[
            ('files', ('flat-no-expiry.json', json.dumps({
                'uid': 'no-expiry', 'access_token': 'at', 'refresh_token': 'rt',
                'created_at': 4102444800,
            }), 'application/json')),
        ])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['uploaded'][0]['uid'], 'no-expiry')


if __name__ == '__main__':
    unittest.main()
