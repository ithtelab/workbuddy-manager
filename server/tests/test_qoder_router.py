"""routers/qoder.py（面板代理路由）的契约测试。

覆盖（架构文档 B03 验收标准）：
  * 未登录 401；
  * 只读端点 `current_user` 可达（console 响应原样代理）；
  * 写操作用 `wbt_` API Token 被拒（require_session_admin）；
  * checkin 成功与失败各落一条 `audit_logs` action=qoder_checkin；
  * 未配置 400 / console 401 透传 / 网络异常 502 的 mock 三态；
  * main.py 已注册（/api/qoder/* 走真实路由表）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, security, tokensvc  # noqa: E402
from server.services import qoder as qoder_svc  # noqa: E402


class QoderRouterTest(unittest.TestCase):
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
        qoder_svc._sessions.clear()
        qoder_svc._model_cache.clear()
        from server.main import app
        self.client = TestClient(app)
        r = self.client.post('/api/login', json={'username': 'admin', 'password': 'pw'})
        assert r.status_code == 200, r.text
        self.client.cookies.update(dict(r.cookies))

    def tearDown(self) -> None:
        self.client.cookies.clear()
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        qoder_svc._sessions.clear()
        qoder_svc._model_cache.clear()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    # ── 鉴权 ─────────────────────────────────────────────

    def test_unauthenticated_is_401(self) -> None:
        self.client.cookies.clear()
        for path in ('/api/qoder/settings', '/api/qoder/status', '/api/qoder/accounts',
                     '/api/qoder/models'):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 401, path)

    def test_readonly_endpoints_reachable_for_session(self) -> None:
        with mock.patch.object(qoder_svc, 'console_request',
                               new=self._async_return({'running': True})):
            self.assertEqual(self.client.get('/api/qoder/status').status_code, 200)
        with mock.patch.object(qoder_svc, 'console_request',
                               new=self._async_return({'accounts': []})):
            self.assertEqual(self.client.get('/api/qoder/accounts').status_code, 200)

    def test_write_endpoints_reject_api_token(self) -> None:
        """高危写操作拒绝 `wbt_` Token（与「保存上游 / 一键更新」同级）。"""
        tok = tokensvc.create_token('ci', scope='admin')['token']
        self.client.cookies.clear()
        headers = {'Authorization': f'Bearer {tok}'}
        for method, path in (('put', '/api/qoder/settings'),
                             ('post', '/api/qoder/checkin'),
                             ('post', '/api/qoder/active'),
                             ('post', '/api/qoder/bridge/start'),
                             ('post', '/api/qoder/bridge/stop')):
            r = getattr(self.client, method)(path, headers=headers, json={})
            self.assertEqual(r.status_code, 403, f'{method.upper()} {path}: {r.text}')
            self.assertIn('API Token', r.json()['detail'])

    # ── 设置端点 ─────────────────────────────────────────

    def test_settings_roundtrip_hides_password(self) -> None:
        r = self.client.put('/api/qoder/settings',
                            json={'url': 'http://127.0.0.1:3588', 'password': 'secret-pw'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(set(body), {'configured', 'url', 'password_set'})
        self.assertNotIn('secret-pw', r.text, '密码绝不出接口（write-only）')
        g = self.client.get('/api/qoder/settings')
        self.assertNotIn('secret-pw', g.text)

    def test_settings_put_rejects_bad_url(self) -> None:
        r = self.client.put('/api/qoder/settings',
                            json={'url': 'no-scheme', 'password': 'x'})
        self.assertEqual(r.status_code, 400, r.text)

    # ── 错误三态（mock services 层）───────────────────────

    def test_not_configured_maps_to_400(self) -> None:
        async def _raise(*a, **k):
            raise qoder_svc.QoderNotConfigured()
        with mock.patch.object(qoder_svc, 'console_request', new=_raise):
            r = self.client.get('/api/qoder/status')
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn('未配置', r.json()['detail'])

    def test_console_401_passthrough(self) -> None:
        async def _raise(*a, **k):
            raise qoder_svc.QoderError(401, 'Qoder 控制台密码错误或已变更，'
                                            '请在「设置 → Qoder」更新')
        with mock.patch.object(qoder_svc, 'console_request', new=_raise):
            r = self.client.get('/api/qoder/status')
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn('密码错误', r.json()['detail'])

    def test_console_down_maps_to_502(self) -> None:
        async def _raise(*a, **k):
            raise qoder_svc.QoderError(502, '无法连接 Qoder 控制台：refused')
        with mock.patch.object(qoder_svc, 'console_request', new=_raise):
            r = self.client.get('/api/qoder/status')
        self.assertEqual(r.status_code, 502, r.text)

    # ── 审计 ─────────────────────────────────────────────

    def _audit_actions(self) -> list[dict]:
        rows = db.query("SELECT action, detail FROM audit_logs "
                        "WHERE action LIKE 'qoder%' ORDER BY id ASC")
        return [{'action': r['action'], 'detail': r['detail']} for r in rows]

    def test_checkin_success_and_failure_both_audited(self) -> None:
        """审计是留痕不是成功日志：失败也必须落一条。"""
        with mock.patch.object(qoder_svc, 'console_request',
                               new=self._async_return({'ok': True})):
            r = self.client.post('/api/qoder/checkin')
        self.assertEqual(r.status_code, 200, r.text)
        actions = self._audit_actions()
        self.assertEqual([a['action'] for a in actions], ['qoder_checkin'])
        self.assertIn('成功', actions[0]['detail'])

        async def _raise(*a, **k):
            raise qoder_svc.QoderError(502, '无法连接 Qoder 控制台：refused')
        with mock.patch.object(qoder_svc, 'console_request', new=_raise):
            r = self.client.post('/api/qoder/checkin')
        self.assertEqual(r.status_code, 502)
        actions = self._audit_actions()
        self.assertEqual(len(actions), 2, '失败也要留痕')
        self.assertEqual(actions[1]['action'], 'qoder_checkin')
        self.assertIn('失败', actions[1]['detail'])

    def test_activate_audits_target_account(self) -> None:
        with mock.patch.object(qoder_svc, 'console_request',
                               new=self._async_return({'ok': True})):
            r = self.client.post('/api/qoder/active', json={'id': 'acct-9'})
        self.assertEqual(r.status_code, 200, r.text)
        actions = self._audit_actions()
        self.assertEqual(actions[0]['action'], 'qoder_account_activated')

    def test_console_settings_requires_session_admin(self) -> None:
        """console 自身设置可能含凭据，收紧到会话管理员。"""
        # 会话 admin 可达
        with mock.patch.object(qoder_svc, 'console_request',
                               new=self._async_return({'bridge_token': 't'})):
            self.assertEqual(self.client.get('/api/qoder/console-settings').status_code, 200)
        # wbt_ Token 不可达
        tok = tokensvc.create_token('cs', scope='admin')['token']
        self.client.cookies.clear()
        r = self.client.get('/api/qoder/console-settings',
                            headers={'Authorization': f'Bearer {tok}'})
        self.assertEqual(r.status_code, 403)

    @staticmethod
    def _async_return(value):
        async def _call(*a, **k):
            return value
        return _call


if __name__ == '__main__':
    unittest.main()
