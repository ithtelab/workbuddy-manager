"""请求体上限的回归（跟随上游配置 + 读取时强制截断）。

真实问题：用户通过反代调用时报「请求体过大（上限 8 MiB）」，但上游
`server.max_body_mb` 是**可配置**的（管理端设置页也能改）。本端当时把它写死
8 MiB，于是成了隐性瓶颈——用户调大上游上限后，请求仍在本端先被 413 拦掉。

另一个漏洞：原先只看 `Content-Length` 判断大小。分块传输时该头缺失、也
可以伪造，因此超大请求体仍会被整段读进内存（正是当初要防的内存耗尽）。
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile
import unittest

from starlette.requests import Request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.routers import gateway  # noqa: E402


def _reset_cache() -> None:
    gateway._body_limit_cache.update({'at': 0.0, 'bytes': 0, 'source': ''})
    gateway._BODY_LIMIT_SOURCE['value'] = ''


def _request(body: bytes, headers: dict | None = None) -> Request:
    scope = {
        'type': 'http',
        'method': 'POST',
        'path': '/v1/chat/completions',
        'headers': [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    sent = {'done': False}

    async def receive():
        if sent['done']:
            return {'type': 'http.disconnect'}
        sent['done'] = True
        return {'type': 'http.request', 'body': body, 'more_body': False}

    return Request(scope, receive)


class BodyLimitResolution(unittest.TestCase):
    """上限的取值来源。

    上游 9d1a21b **移除了** `server.max_body_mb`（chat handler 也去掉了预拦截），
    所以「跟随上游配置」不再是唯一来源：

      · 旧版上游（配置里仍有该键）→ 继续尊重它，升级顺序不受限；
      · 新版上游（无该键）→ 用**本端自己**的默认值（WB_GATEWAY_MAX_BODY_MB）。

    为什么本端不能也取消上限：上游是 Go，读的是流、能背压；本端要把完整 body
    读进内存再 json.loads（见 _read_json_body），几个并发大请求就能把内存吃光。
    保留上限是必要的，只是默认值要够大（32 MB），免得变成比上游更紧的隐性瓶颈。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = pathlib.Path(self._tmp.name) / 'config.json'
        self._orig = config.UPSTREAM_CONFIG
        config.UPSTREAM_CONFIG = self.cfg
        _reset_cache()

    def tearDown(self) -> None:
        config.UPSTREAM_CONFIG = self._orig
        _reset_cache()
        self._tmp.cleanup()

    def _mb(self, value) -> int:
        if value == 'missing':
            self.cfg.unlink(missing_ok=True)
        else:
            self.cfg.write_text(json.dumps(value), encoding='utf-8')
        _reset_cache()
        return gateway.max_body_bytes() // 1024 // 1024

    def test_follows_config(self) -> None:
        self.assertEqual(self._mb({'server': {'max_body_mb': 32}}), 32)
        self.assertEqual(self._mb({'server': {'max_body_mb': 64}}), 64)

    def test_missing_config_falls_back(self) -> None:
        """读不到配置不能导致拒绝服务，退回本端默认值。"""
        self.assertEqual(self._mb('missing'), gateway.DEFAULT_MAX_BODY_MB)

    def test_default_is_generous_after_upstream_removed_the_key(self) -> None:
        """上游移除该键后，本端默认值必须足够大，否则会成为隐性瓶颈。

        上游现在不限制请求体；本端若还按老默认 8 MB 拦，用户发个正常的大上下文
        请求就会被 413，而上游其实能处理 —— 且报错指的配置项在上游已不存在。
        """
        self.assertGreaterEqual(gateway.DEFAULT_MAX_BODY_MB, 16,
                                '默认值太小会让本端比上游更先拒绝')

    def test_new_upstream_without_the_key_uses_our_default(self) -> None:
        """新版上游的配置里没有 server 段 → 用本端默认值，不是「无限制」。"""
        self.assertEqual(self._mb({}), gateway.DEFAULT_MAX_BODY_MB)
        self.assertGreater(gateway.max_body_bytes(), 0, '上限不能变成 0（等于全拒）')

    def test_invalid_values_fall_back(self) -> None:
        for bad in ({'server': {}}, {'server': {'max_body_mb': 0}},
                    {'server': {'max_body_mb': -5}}, {'server': {'max_body_mb': 'x'}}, {}):
            self.assertEqual(self._mb(bad), gateway.DEFAULT_MAX_BODY_MB, repr(bad))

    def test_result_is_cached_then_refreshes(self) -> None:
        self.assertEqual(self._mb({'server': {'max_body_mb': 32}}), 32)
        # 改文件但不清缓存 → 仍读旧值
        self.cfg.write_text(json.dumps({'server': {'max_body_mb': 1}}), encoding='utf-8')
        self.assertEqual(gateway.max_body_bytes() // 1024 // 1024, 32, 'TTL 内应命中缓存')
        # 缓存过期 → 生效新值
        gateway._body_limit_cache['at'] = 0.0
        self.assertEqual(gateway.max_body_bytes() // 1024 // 1024, 1)


class BodyLimitEnforcement(unittest.TestCase):
    """大小校验必须落在实际读取上，而不只是信任 Content-Length。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = pathlib.Path(self._tmp.name) / 'config.json'
        self._orig = config.UPSTREAM_CONFIG
        config.UPSTREAM_CONFIG = self.cfg
        # 设为 1 MiB，便于构造超限体
        self.cfg.write_text(json.dumps({'server': {'max_body_mb': 1}}), encoding='utf-8')
        _reset_cache()
        self.big = json.dumps(
            {'model': 'x', 'messages': [{'role': 'user', 'content': 'a' * (2 * 1024 * 1024)}]}
        ).encode()
        self.small = json.dumps({'model': 'x', 'messages': []}).encode()

    def tearDown(self) -> None:
        config.UPSTREAM_CONFIG = self._orig
        _reset_cache()
        self._tmp.cleanup()

    def _read(self, body: bytes, headers: dict | None = None):
        return asyncio.run(gateway._read_json_body(_request(body, headers)))

    def test_too_large_with_content_length(self) -> None:
        _, err = self._read(self.big, {'content-length': str(len(self.big))})
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 413)

    def test_too_large_without_content_length(self) -> None:
        """分块/缺头时原先可绕过，现在必须也拦。"""
        _, err = self._read(self.big, {})
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 413)

    def test_too_large_with_forged_content_length(self) -> None:
        """伪造偏小的 Content-Length 不能绕过。"""
        _, err = self._read(self.big, {'content-length': '100'})
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 413)

    def test_small_body_passes(self) -> None:
        body, err = self._read(self.small, {'content-length': str(len(self.small))})
        self.assertIsNone(err)
        self.assertIsInstance(body, dict)

    def test_non_json_is_400(self) -> None:
        _, err = self._read(b'not json', {'content-length': '8'})
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 400)

    def test_non_object_is_400(self) -> None:
        raw = json.dumps([1, 2, 3]).encode()
        _, err = self._read(raw, {'content-length': str(len(raw))})
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 400)

    def test_message_points_at_the_upstream_key_when_it_wins(self) -> None:
        """上限来自**旧版上游**的 config.json 时，报错必须指上游那个键。

        issue #180 的实际形状：用户的上游是旧版、`server.max_body_mb` 还在，
        面板优先听它；旧文案却一律写「由环境变量 WB_GATEWAY_MAX_BODY_MB 决定」，
        用户把环境变量改大了毫无效果，还以为面板不认这个配置。
        """
        with tempfile.TemporaryDirectory() as tmp:
            cfg = pathlib.Path(tmp) / 'config.json'
            cfg.write_text(json.dumps({'server': {'max_body_mb': 1}}), encoding='utf-8')
            orig = config.UPSTREAM_CONFIG
            config.UPSTREAM_CONFIG = cfg
            _reset_cache()
            try:
                _, err = self._read(self.big, {})
                msg = json.loads(err.body)['error']['message']
                self.assertIn('上游', msg)
                self.assertIn('server.max_body_mb', msg,
                              '要指出上游那个键 —— 那才是用户该改的地方')
                self.assertIn('WB_GATEWAY_MAX_BODY_MB', msg, '也要给出删键后的替代做法')
            finally:
                config.UPSTREAM_CONFIG = orig
                _reset_cache()

    def test_message_says_default_when_nothing_is_configured(self) -> None:
        """既没有上游那个键、也没设环境变量时，说清「这是默认上限、可以调大」。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = pathlib.Path(tmp) / 'config.json'
            cfg.write_text(json.dumps({}), encoding='utf-8')
            orig = config.UPSTREAM_CONFIG
            orig_default = gateway.DEFAULT_MAX_BODY_MB
            config.UPSTREAM_CONFIG = cfg
            gateway.DEFAULT_MAX_BODY_MB = 1          # 与 self.big（2 MB）配对，便于构造超限
            _reset_cache()
            try:
                gateway.max_body_bytes()
                self.assertEqual(gateway._BODY_LIMIT_SOURCE['value'], '', '没设过就应是默认值')
                _, err = self._read(self.big, {})
                msg = json.loads(err.body)['error']['message']
                self.assertIn('默认上限', msg)
                self.assertIn('WB_GATEWAY_MAX_BODY_MB', msg)
            finally:
                config.UPSTREAM_CONFIG = orig
                gateway.DEFAULT_MAX_BODY_MB = orig_default
                _reset_cache()

    def test_error_message_has_guidance(self) -> None:
        """报错要说清怎么改，而不是只丢一个 413。

        指导必须指向**真正能改的地方**：上游 9d1a21b 起移除了
        `server.max_body_mb`，再让用户去调那个键等于指错路（按上游文档怎么找
        都找不到）。现在指本网关自己的环境变量。
        """
        _, err = self._read(self.big, {})
        msg = json.loads(err.body)['error']['message']
        self.assertIn('上限', msg)
        self.assertIn('WB_GATEWAY_MAX_BODY_MB', msg,
                      '要指出本网关自己的环境变量（上游那个键已经不存在了）')


if __name__ == '__main__':
    unittest.main()
