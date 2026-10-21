"""2026-10-21 安全检查发现的回归测试。

覆盖本轮修复的五个点（编号对应 security-check-2026-10-21.md）：

  M1  /api/* 请求体上限只查 Content-Length，分块传输可绕过（未鉴权内存 DoS）
  M2  CORS 配成 `*` 只告警不拒绝（误配即任意站点接管）
  L1  登录时用户不存在短路跳过 PBKDF2（用户名枚举时间侧信道）
  L2  按用户名的锁定可被匿名者武器化（干净 IP 被远程锁定 DoS，断言在
      test_auth_bypass.LoginLockoutTest 里）
  L4  /v1/usage 未过全局入站 IP 规则
  L5  cookie Secure 判定（auto）无条件信任 X-Forwarded-Proto
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from starlette.requests import Request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import config, db, keysvc, security  # noqa: E402
from server.main import ApiBodyLimitMiddleware, _validate_cors_origins  # noqa: E402
from server.routers import gateway  # noqa: E402


# ── M2：CORS 星号硬失败 ────────────────────────────────────
class CorsStarHardFailTest(unittest.TestCase):
    def test_star_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            _validate_cors_origins(['*'])

    def test_star_among_origins_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            _validate_cors_origins(['https://a.com', '*'])

    def test_explicit_origins_pass(self) -> None:
        _validate_cors_origins(['https://a.com', 'https://b.com'])

    def test_empty_means_same_origin_and_is_not_configured(self) -> None:
        """默认（留空）根本不会装 CORS 中间件，这里钉住语义。"""
        self.assertEqual(config.CORS_ORIGINS, [])


# ── M1：/api 请求体的读取层上限 ────────────────────────────
class _StubApp:
    """模拟一个会把 body 读完的 handler，记录读到的块与是否完成。"""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.completed = False

    async def __call__(self, scope, receive, send):
        while True:
            msg = await receive()
            if msg.get('type') == 'http.disconnect':
                break
            if msg.get('type') == 'http.request':
                self.chunks.append(msg.get('body') or b'')
                if not msg.get('more_body'):
                    break
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})
        self.completed = True


def _scope(path: str) -> dict:
    return {'type': 'http', 'method': 'POST', 'path': path, 'headers': []}


def _receive_chunks(chunks: list[bytes]):
    """按块依次产出 http.request，之后一律 http.disconnect（模拟客户端收尾）。"""
    state = {'i': 0}

    async def receive():
        i = state['i']
        state['i'] += 1
        if i < len(chunks):
            return {'type': 'http.request', 'body': chunks[i],
                    'more_body': i < len(chunks) - 1}
        return {'type': 'http.disconnect'}

    return receive


class _SendCollector:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message):
        self.messages.append(message)


class ApiBodyLimitTest(unittest.TestCase):
    """分块传输（无 Content-Length 参与判定）也必须被读取层拦住。"""

    def _run(self, path: str, chunks: list[bytes], *, limit: int = 100,
             package_limit: int = 200):
        stub = _StubApp()
        sent = _SendCollector()
        mw = ApiBodyLimitMiddleware(
            stub, limit=limit, package_limit=package_limit,
            package_paths=('/api/system/update-package',))
        import asyncio
        asyncio.run(mw(_scope(path), _receive_chunks(chunks), sent))
        return stub, sent

    def _status(self, sent: _SendCollector) -> int | None:
        for m in sent.messages:
            if m.get('type') == 'http.response.start':
                return m['status']
        return None

    def test_chunked_oversize_gets_413(self) -> None:
        stub, sent = self._run('/api/login', [b'x' * 60, b'y' * 60])
        self.assertEqual(self._status(sent), 413)
        self.assertFalse(stub.completed, '超限后 handler 必须被中止')
        self.assertEqual(stub.chunks, [b'x' * 60], '只应读到第一个块')

    def test_chunked_within_limit_passes(self) -> None:
        stub, sent = self._run('/api/login', [b'x' * 60])
        self.assertEqual(self._status(sent), 200)
        self.assertTrue(stub.completed)

    def test_package_upload_path_uses_higher_limit(self) -> None:
        stub, sent = self._run('/api/system/update-package', [b'x' * 150])
        self.assertEqual(self._status(sent), 200)
        self.assertTrue(stub.completed)

    def test_non_api_path_not_affected(self) -> None:
        stub, sent = self._run('/v1/chat/completions', [b'x' * 500])
        self.assertEqual(self._status(sent), 200)
        self.assertTrue(stub.completed, '网关有自己的逐块限额（gateway._read_json_body）')


# ── L5：cookie Secure 的 X-Forwarded-Proto 只信可信代理 ───
class CookieSecureTrustedProxyTest(unittest.TestCase):
    def _req(self, peer: str, scheme: str, headers: dict | None = None) -> Request:
        scope = {
            'type': 'http', 'method': 'GET', 'path': '/', 'scheme': scheme,
            'headers': [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
            'client': (peer, 50000) if peer else None, 'query_string': b'',
        }
        return Request(scope)

    def test_untrusted_peer_cannot_downgrade_secure(self) -> None:
        """直连 TLS 部署（无反代）时，伪造 X-Forwarded-Proto: http 不能让
        cookie 漏发 Secure。修复前这里返回 False（头被无条件采信）。"""
        req = self._req('203.0.113.9', 'https', {'x-forwarded-proto': 'http'})
        self.assertTrue(security.cookie_secure(req))

    def test_untrusted_peer_cannot_upgrade_either(self) -> None:
        """反向同理：不可信对端的头一律不采信，判据回到实际协议。"""
        req = self._req('203.0.113.9', 'http', {'x-forwarded-proto': 'https'})
        self.assertFalse(security.cookie_secure(req))

    def test_trusted_proxy_header_still_honored(self) -> None:
        req = self._req('127.0.0.1', 'http', {'x-forwarded-proto': 'https'})
        self.assertTrue(security.cookie_secure(req))

    def test_trusted_proxy_plain_http_no_secure(self) -> None:
        req = self._req('127.0.0.1', 'http', {'x-forwarded-proto': 'http'})
        self.assertFalse(security.cookie_secure(req))

    def test_explicit_setting_overrides_all(self) -> None:
        req = self._req('203.0.113.9', 'http', {'x-forwarded-proto': 'https'})
        with mock.patch.object(config, 'SECURE_COOKIE', 'true'):
            self.assertTrue(security.cookie_secure(req))
        with mock.patch.object(config, 'SECURE_COOKIE', 'false'):
            self.assertFalse(security.cookie_secure(req))


# ── L1 + L4：走真实 HTTP 的两处 ───────────────────────────
class _AppFixture:
    """临时库 + 临时 users.json + 真实 app（与 test_auth_bypass 同一套做法）。"""

    def setUp(self) -> None:
        import os
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = pathlib.Path(self._tmp.name)
        self._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = self._dir / 't.db'
        config.USERS_FILE = self._dir / 'users.json'
        config.STATIC_DIR = self._dir / 'no-static'
        os.environ['WB_ADMIN_PASSWORD'] = 'audit-admin-pw'
        security.save_users({
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('audit-admin-pw'), 'sv': 1}],
            'secret': 'audit-test-secret',
        })
        security._fail.clear()
        security._user_fail.clear()
        db._conn = None
        db.connect()
        from server.main import app
        from fastapi.testclient import TestClient
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = self._orig
        security._fail.clear()
        security._user_fail.clear()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass


class LoginTimingSideChannelTest(_AppFixture, unittest.TestCase):
    def test_missing_user_still_burns_pbkdf2(self) -> None:
        """用户不存在时必须执行哑哈希校验，对齐响应耗时。"""
        with mock.patch.object(security, 'dummy_verify',
                               wraps=security.dummy_verify) as dv:
            r = self.client.post('/api/login',
                                 json={'username': 'no-such-user', 'password': 'x'})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(dv.call_count, 1)

    def test_existing_user_does_not_need_dummy(self) -> None:
        with mock.patch.object(security, 'dummy_verify') as dv:
            r = self.client.post('/api/login',
                                 json={'username': 'admin', 'password': 'wrong'})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(dv.call_count, 0)

    def test_correct_login_unaffected(self) -> None:
        r = self.client.post('/api/login',
                             json={'username': 'admin', 'password': 'audit-admin-pw'})
        self.assertEqual(r.status_code, 200, r.text)


class KeyUsageIpRulesTest(_AppFixture, unittest.TestCase):
    def _usage(self, token: str):
        return self.client.get('/v1/usage',
                               headers={'Authorization': f'Bearer {token}'})

    def test_blocked_ip_gets_403(self) -> None:
        key = keysvc.create_key('audit-usage')
        with mock.patch.object(gateway, 'get_security_config',
                               return_value={'enabled': True, 'mode': 'blacklist'}), \
             mock.patch.object(gateway.iputil, 'evaluate', return_value=False):
            r = self._usage(key['key'])
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()['error'].get('code'), 'ip_blocked')

    def test_allowed_ip_gets_usage(self) -> None:
        key = keysvc.create_key('audit-usage-ok')
        with mock.patch.object(gateway, 'get_security_config',
                               return_value={'enabled': True, 'mode': 'blacklist'}), \
             mock.patch.object(gateway.iputil, 'evaluate', return_value=True):
            r = self._usage(key['key'])
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json().get('object'), 'key.usage')


if __name__ == '__main__':
    unittest.main()
