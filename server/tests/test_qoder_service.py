"""services/qoder.py（console 客户端 + bridge 模型缓存）的契约测试。

覆盖（架构文档 B02 验收标准，打桩手法与仓库既有 httpx 测试同款）：
  * 登录取 Set-Cookie（Cookie 名原样回读，不手工拼端口）；
  * 请求 401 → 清会话 → 重登一次 → 重放成功；
  * 重登后仍 401 → QoderError(401)；
  * 未配置 → QoderNotConfigured（400）；
  * 白名单外路径 → 400（纵深防御负例）；
  * `load_console_config` 不含密码明文（write-only）；
  * `cached_model_ids` TTL 过期 / 从未拉取 → None。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.services import qoder  # noqa: E402


def _run(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Resp:
    def __init__(self, status_code=200, payload=None, text=''):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.cookies = {'qoder2api_session_3588': 'sess-1'}

    def json(self):
        if self._payload is None:
            raise ValueError('no json')
        return self._payload


class _Client:
    """可编程的假 httpx 客户端：按 (method, url) 依次出响应。"""

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        self.calls.append(('POST', url, kw))
        method, _u, _kw = self.calls[-1]
        return self.script.pop(0)

    async def get(self, url, **kw):
        self.calls.append(('GET', url, kw))
        return self.script.pop(0)


class QoderServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'k.db'
        db._conn = None
        db.connect()
        # 清空进程内缓存：会话与模型缓存都是模块级单例
        qoder._sessions.clear()
        qoder._model_cache.clear()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        qoder._sessions.clear()
        qoder._model_cache.clear()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _configure(self) -> None:
        qoder.save_console_config('http://127.0.0.1:3588', 'pw-1')

    # ── 凭据 write-only ──────────────────────────────────

    def test_load_config_never_returns_password(self) -> None:
        self._configure()
        cfg = qoder.load_console_config()
        self.assertEqual(set(cfg), {'configured', 'url', 'password_set'})
        self.assertTrue(cfg['configured'])
        self.assertTrue(cfg['password_set'])
        self.assertNotIn('pw-1', str(cfg))

    def test_save_with_password_none_keeps_old(self) -> None:
        self._configure()
        out = qoder.save_console_config('http://127.0.0.1:3588', None)
        self.assertTrue(out['password_set'], 'password=None = 不修改（PATCH 语义）')
        # 显式空串 = 清空（能生效的动作，不能被真值判断吞掉）
        out = qoder.save_console_config('http://127.0.0.1:3588', '')
        self.assertFalse(out['password_set'])
        self.assertTrue(out['configured'] is False or out['password_set'] is False)

    def test_save_rejects_bad_url(self) -> None:
        with self.assertRaises(ValueError):
            qoder.save_console_config('127.0.0.1:3588', 'pw')

    # ── console_request ──────────────────────────────────

    def test_not_configured_raises(self) -> None:
        with self.assertRaises(qoder.QoderNotConfigured) as ctx:
            _run(qoder.console_request('GET', '/api/status'))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_path_outside_whitelist_is_400(self) -> None:
        """纵深防御负例：将来有人拼接用户输入，也出不了白名单。"""
        self._configure()
        for bad in ('/api/accounts/add', '/api/accounts/delete', '/api/oauth/start',
                    '/api/cleanup', '/../etc/passwd', '/api/status/../settings'):
            with self.assertRaises(qoder.QoderError) as ctx:
                _run(qoder.console_request('GET', bad))
            self.assertEqual(ctx.exception.status_code, 400, bad)

    def test_request_uses_session_cookie_from_login(self) -> None:
        self._configure()
        client = _Client([
            _Resp(200, {'ok': True, 'expires_at': int(time.time()) + 3600}),
            _Resp(200, {'running': True}),
        ])
        with mock.patch.object(config, 'http_client', lambda *a, **k: client):
            out = _run(qoder.console_request('GET', '/api/status'))
        self.assertEqual(out, {'running': True})
        login_url, login_kw = client.calls[0][1], client.calls[0][2]
        self.assertEqual(login_url, 'http://127.0.0.1:3588/api/auth/login')
        self.assertEqual(login_kw['json'], {'password': 'pw-1'})
        # 数据请求带的是登录响应里的 Cookie，名与值都原样回读
        data_kw = client.calls[1][2]
        self.assertEqual(data_kw['headers']['Cookie'],
                         'qoder2api_session_3588=sess-1')

    def test_401_relogs_once_and_replays(self) -> None:
        self._configure()
        client = _Client([
            _Resp(200, {'ok': True, 'expires_at': int(time.time()) + 3600}),
            _Resp(401, {'error': 'x'}),                                   # 旧会话失效
            _Resp(200, {'ok': True, 'expires_at': int(time.time()) + 3600}),
            _Resp(200, {'accounts': []}),                                 # 重放成功
        ])
        with mock.patch.object(config, 'http_client', lambda *a, **k: client):
            out = _run(qoder.console_request('GET', '/api/accounts'))
        self.assertEqual(out, {'accounts': []})
        self.assertEqual(len(client.calls), 4, '重登一次 + 重放一次')

    def test_double_401_raises_401(self) -> None:
        """重登后仍 401 = 密码本身错了：不再重试，避免 401 风暴。"""
        self._configure()
        client = _Client([
            _Resp(200, {'ok': True, 'expires_at': int(time.time()) + 3600}),
            _Resp(401, {'error': 'x'}),
            _Resp(200, {'ok': True, 'expires_at': int(time.time()) + 3600}),
            _Resp(401, {'error': 'x'}),
        ])
        with mock.patch.object(config, 'http_client', lambda *a, **k: client):
            with self.assertRaises(qoder.QoderError) as ctx:
                _run(qoder.console_request('GET', '/api/status'))
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn('密码', ctx.exception.message)

    def test_connection_error_maps_to_502(self) -> None:
        self._configure()

        class _Boom:
            async def __aenter__(self):
                raise OSError('refused')

            async def __aexit__(self, *a):
                return False

        with mock.patch.object(config, 'http_client', lambda *a, **k: _Boom()):
            with self.assertRaises(qoder.QoderError) as ctx:
                _run(qoder.console_request('GET', '/api/status'))
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn('无法连接', ctx.exception.message)

    def test_non_json_response_returns_raw(self) -> None:
        """console 的失败页可能是 HTML：吞掉换 500 会让面板看不到上游说了什么。"""
        self._configure()
        client = _Client([
            _Resp(200, {'ok': True, 'expires_at': int(time.time()) + 3600}),
            _Resp(200, payload=None, text='<html>login</html>'),
        ])
        with mock.patch.object(config, 'http_client', lambda *a, **k: client):
            out = _run(qoder.console_request('GET', '/api/status'))
        self.assertIn('raw', out)
        self.assertIn('<html>', out['raw'])

    # ── probe_console ────────────────────────────────────

    def test_probe_console_ok_and_fail(self) -> None:
        self._configure()
        client = _Client([
            _Resp(200, {'ok': True, 'expires_at': int(time.time()) + 3600}),
            _Resp(200, {'running': True, 'active_account': 'acct-1'}),
        ])
        with mock.patch.object(config, 'http_client', lambda *a, **k: client):
            ok, msg = _run(qoder.probe_console())
        self.assertTrue(ok)
        self.assertIn('acct-1', msg)
        client2 = _Client([])
        with mock.patch.object(config, 'http_client', lambda *a, **k: client2):
            pass
        # 未配置（清空凭据）→ probe 失败但不抛
        qoder.save_console_config('', '')
        with mock.patch.object(config, 'http_client', lambda *a, **k: _Client([])):
            ok, msg = _run(qoder.probe_console())
        self.assertFalse(ok)

    # ── bridge 模型缓存 ──────────────────────────────────

    def test_fetch_model_ids_parses_bridge_shape(self) -> None:
        upstream = {'base_url': 'http://127.0.0.1:8963', 'api_key': 'tok',
                    'provider': 'qoder'}

        class _Client2:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, **kw):
                self.url, self.kw = url, kw
                return _Resp(200, {'object': 'list', 'data': [
                    {'id': 'qmodel', 'owned_by': 'qoder'},
                    {'id': 'qfmodel'},
                    {'no-id': True},
                ]})

        c = _Client2()
        with mock.patch.object(config, 'http_client', lambda *a, **k: c):
            ids = _run(qoder.fetch_model_ids(upstream))
        self.assertEqual(ids, {'qmodel', 'qfmodel'})
        self.assertEqual(c.url, 'http://127.0.0.1:8963/v1/models')
        self.assertEqual(c.kw['headers'].get('Authorization'), 'Bearer tok')
        self.assertEqual(qoder.cached_model_ids(), {'qmodel', 'qfmodel'})

    def test_cached_model_ids_none_when_never_fetched_or_expired(self) -> None:
        self.assertIsNone(qoder.cached_model_ids(), '从未拉取 → None（不判，不假警报）')
        qoder._model_cache['http://x'] = (frozenset({'m'}), time.time() - 601)
        self.assertIsNone(qoder.cached_model_ids(), 'TTL 过期 → None')

    def test_cached_model_ids_unions_all_upstreams(self) -> None:
        qoder._model_cache['http://a'] = (frozenset({'m1'}), time.time())
        qoder._model_cache['http://b'] = (frozenset({'m2'}), time.time())
        self.assertEqual(qoder.cached_model_ids(), {'m1', 'm2'})


if __name__ == '__main__':
    unittest.main()
