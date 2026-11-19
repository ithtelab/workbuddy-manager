"""可信代理默认收敛 + Upstash 已存 Token 不外带（2026-10-18 审计）。

## 一、可信代理

以前 `WB_TRUSTED_PROXY_CIDRS` 默认包含全部 RFC1918。同一局域网 / 同一 Docker 网络
里的任意主机都能伪造 `X-Real-IP`，绕过登录失败锁定、密钥 IP 白名单与红包限领。
现在默认只信回环；反代在另一台机器时显式配置。

## 二、Upstash

`test_upstash(url, token)` 在 token 留空时会读 `config.json` 里已保存的 Token，而
URL 由调用方给 —— 于是「URL 填自己的服务器、Token 留空」就能把已存 Token 以
Authorization 头带出去。现在：用已存 Token 时，地址必须与已保存的**完全一致**。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.services import wb2api  # noqa: E402


class TrustedProxyDefaultTest(unittest.TestCase):
    def test_default_is_loopback_only(self) -> None:
        self.assertEqual(config.TRUSTED_PROXY_CIDRS, ['127.0.0.0/8', '::1/128'],
                         '默认可信代理必须只有回环；私网要显式配置')


class FakeResponse:
    def __init__(self, status: int = 200, text: str = 'PONG') -> None:
        self.status_code = status
        self.text = text


class RecordingClient:
    """记录请求；作为 `config.http_client` 的替身（支持 async with）。"""

    calls: list[tuple[str, dict]] = []

    def __init__(self, *a, **k) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, **kw):  # noqa: ANN001, ANN201
        RecordingClient.calls.append((url, dict(headers or {})))
        return FakeResponse()


class UpstashSavedTokenTest(unittest.TestCase):
    def setUp(self) -> None:
        RecordingClient.calls = []
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self._tmp.name) / 'config.json'
        self.cfg.write_text(json.dumps({
            'upstash': {'url': 'https://saved.upstash.io', 'token': 'SAVED-TOKEN'},
        }), encoding='utf-8')
        self._patch_cfg = mock.patch.object(config, 'UPSTREAM_CONFIG', self.cfg)
        self._patch_http = mock.patch.object(config, 'http_client', RecordingClient)
        self._patch_cfg.start()
        self._patch_http.start()

    def tearDown(self) -> None:
        self._patch_http.stop()
        self._patch_cfg.stop()
        self._tmp.cleanup()

    def test_empty_token_with_foreign_url_is_refused_without_request(self) -> None:
        ok, msg = asyncio.run(wb2api.test_upstash('https://evil.example', None))
        self.assertFalse(ok)
        self.assertIn('不会把已存 Token 发往新地址', msg)
        self.assertEqual(RecordingClient.calls, [], '已存 Token 被发往了外部地址')

    def test_empty_token_with_saved_url_uses_saved_token(self) -> None:
        ok, msg = asyncio.run(wb2api.test_upstash('saved.upstash.io', None))
        self.assertTrue(ok, msg)
        self.assertEqual(len(RecordingClient.calls), 1)
        url, headers = RecordingClient.calls[0]
        self.assertEqual(url, 'https://saved.upstash.io/ping')
        self.assertEqual(headers.get('Authorization'), 'Bearer SAVED-TOKEN')

    def test_explicit_token_still_allows_any_public_url(self) -> None:
        """显式给了 Token 就与「已存」无关：用户是在测自己填的地址。"""
        ok, msg = asyncio.run(wb2api.test_upstash(
            'https://another.upstash.io', 'EXPLICIT'))
        self.assertTrue(ok, msg)
        url, headers = RecordingClient.calls[0]
        self.assertEqual(url, 'https://another.upstash.io/ping')
        self.assertEqual(headers.get('Authorization'), 'Bearer EXPLICIT')


if __name__ == '__main__':
    unittest.main()
