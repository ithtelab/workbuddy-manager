"""数据面转发链路在 qoder 上游上的端到端测试（走真实路由与依赖注入）。

覆盖（架构文档 B05 验收标准）：
  * global 密钥 + qoder(global) 上游调 `qmodel`：转发到 bridge、鉴权头形状正确；
  * 版本不匹配（cn 密钥 + qoder(global) 上游）：503 且**不发请求**（不回落）；
  * `/v1/models` 与 `/v1/chat/completions` 都走该上游地址（anthropic/responses
    兼容层复用同一 `_upstream_for`/`_upstream_headers` 链路，由既有测试盖住）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc, security, upstreamsvc  # noqa: E402


class GatewayQoderChatTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 'k.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        db._conn = None
        db.connect()
        security.save_users({
            'secret': 'S',
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('pw')}],
            'api_keys': [],
        })
        self.qoder_up = upstreamsvc.create_upstream(
            'Qoder 池', 'http://127.0.0.1:8963', api_key='bridge-tok',
            provider='qoder', realm='global')
        from server.main import app
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.cookies.clear()
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    class _Resp:
        status_code = 200

        def json(self):
            return {'id': 'x', 'choices': [], 'usage': {}}

    def _fake_client(self, sent: list):
        resp = self._Resp()

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, **kw):
                sent.append(('POST', url, kw.get('headers') or {}))
                return resp

            async def get(self, url, **kw):
                sent.append(('GET', url, kw.get('headers') or {}))
                return resp
        return _Client()

    def _chat(self, token: str, model: str = 'qmodel'):
        return self.client.post(
            '/v1/chat/completions',
            headers={'Authorization': f'Bearer {token}'},
            json={'model': model, 'messages': [{'role': 'user', 'content': 'hi'}],
                  'stream': False},
        )

    def test_chat_forwards_to_bridge_with_bearer(self) -> None:
        key = keysvc.create_key('g', realm='global', upstream_id=self.qoder_up['id'])
        sent: list = []
        with mock.patch.object(config, 'http_client',
                               lambda *a, **k: self._fake_client(sent)):
            r = self._chat(key['key'])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sent[0][1], 'http://127.0.0.1:8963/v1/chat/completions',
                         '数据面转发 URL 拼接零改动：{base}/v1/chat/completions')
        self.assertEqual(sent[0][2].get('Authorization'), 'Bearer bridge-tok',
                         '鉴权头形状不变（bridge authMiddleware 收 Bearer）')

    def test_realm_mismatch_is_503_and_never_sends(self) -> None:
        """cn 密钥绑 qoder(global) 上游：resolve 层拒绝，503，不回落不转发。"""
        key = keysvc.create_key('c', realm='cn', upstream_id=self.qoder_up['id'])
        sent: list = []
        with mock.patch.object(config, 'http_client',
                               lambda *a, **k: self._fake_client(sent)):
            r = self._chat(key['key'])
        self.assertEqual(r.status_code, 503, r.text)
        self.assertIn('不匹配', r.json()['error']['message'])
        self.assertEqual(sent, [], '一次都不该发出去——不静默回落是隔离的前提')

    def test_models_on_qoder_upstream_hit_bridge(self) -> None:
        key = keysvc.create_key('g', upstream_id=self.qoder_up['id'])
        sent: list = []
        with mock.patch.object(config, 'http_client',
                               lambda *a, **k: self._fake_client(sent)):
            r = self.client.get('/v1/models',
                                headers={'Authorization': f"Bearer {key['key']}"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sent[0][1], 'http://127.0.0.1:8963/v1/models')


if __name__ == '__main__':
    unittest.main()
