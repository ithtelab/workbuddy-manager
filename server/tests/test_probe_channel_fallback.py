"""连通性测试在 /v2 被渠道校验拦下时，要换 /console 再试（2026-10-17 用户报告）。

现场：上游给 `/v2/chat/completions` 加了渠道强校验，被校验到的请求回
「没通过渠道校验，可能不是官方客户端发出的」；而官方客户端走的 `/console` 放行
（报告方实测：换过去连通性测试与对话都恢复）。

此前探测只试 `/v2`，于是**账号明明是好的**、测试却报红 —— 用户会以为凭证坏了去重扫。
这里钉三件事：① 换路径；② 两条都被拦时说清「不是账号问题」；③ 内容审核类的
同一业务码**不**触发换路径（那是内容问题，换路径没用）。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.services import tencent  # noqa: E402

# 业务码拼接构造：手抄极易写成 1138/1128，而它正是判据本身。
DASHED = "1" + "1" + "-" + "1" + "2" + "8"
PLAIN = "1" + "1" + "1" + "2" + "8"


def _channel_body() -> bytes:
    return (
        '{"code":' + PLAIN + ',"msg":"request illegal",'
        '"displayTips":{"zh":"这次调用没通过渠道校验，可能不是官方客户端发出的。",'
        '"en":"This call failed channel verification, possibly because it was not sent '
        'from an official client."}}'
    ).encode()


AUTH = {"access_token": "TOK", "uid": "u1", "realm": "cn"}


class _PathAwareClient:
    """按路径应答：/v2 回 400（给定 body），/console 回一条正常的流式数据。"""

    seen: list[str] = []
    body: bytes = b""

    def __init__(self, *a, **k) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, **kw):
        cls = type(self)
        cls.seen.append(url)
        is_v2 = "/v2/" in url

        class _Resp:
            status_code = 400 if is_v2 else 200
            headers = {"content-type": "application/json"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def aread(self):
                return b"" if not is_v2 else cls.body

            async def aiter_bytes(self):
                if is_v2:
                    return
                yield b'data: {"choices":[{"delta":{"content":"h"}}]}\n\n'

        return _Resp()


class _BothPathsRejected(_PathAwareClient):
    def stream(self, method, url, **kw):
        cls = type(self)
        cls.seen.append(url)

        class _Resp:
            status_code = 400
            headers = {"content-type": "application/json"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def aread(self):
                return cls.body

        return _Resp()


class ProbeConsoleFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        _PathAwareClient.seen = []
        _PathAwareClient.body = _channel_body()

    def test_falls_back_to_console_on_channel_check(self) -> None:
        with mock.patch.object(config, "http_client", _PathAwareClient):
            ok, msg = asyncio.run(tencent.probe_account(dict(AUTH), "glm-5.2"))
        self.assertTrue(ok, msg)
        self.assertTrue(any("/v2/" in u for u in _PathAwareClient.seen),
                        _PathAwareClient.seen)
        self.assertTrue(any("/console/" in u for u in _PathAwareClient.seen),
                        "被渠道校验拦下后必须换 /console 再试，否则账号好的也报红")

    def test_both_paths_rejected_says_not_an_account_problem(self) -> None:
        _BothPathsRejected.body = _channel_body()
        with mock.patch.object(config, "http_client", _BothPathsRejected):
            ok, msg = asyncio.run(tencent.probe_account(dict(AUTH), "glm-5.2"))
        self.assertFalse(ok)
        self.assertIn("渠道校验", msg)
        self.assertIn("账号本身没问题", msg, "要把「不是账号问题」说出来，别让人去重扫登录")
        self.assertEqual(len(_BothPathsRejected.seen), 2, "两条路径都应试过")

    def test_content_block_does_not_switch_paths(self) -> None:
        """内容审核拦截（同一个业务码）不该触发换路径：那是内容问题，换端点没用。"""
        _PathAwareClient.body = (
            '{"code":' + DASHED + ',"msg":"blocked by security policy"}'
        ).encode()
        with mock.patch.object(config, "http_client", _PathAwareClient):
            ok, msg = asyncio.run(tencent.probe_account(dict(AUTH), "glm-5.2"))
        self.assertFalse(ok)
        self.assertEqual(len(_PathAwareClient.seen), 1,
                         "内容审核拦截不该换路径（换端点解决不了内容问题）")

    def test_is_channel_check_accepts_both_code_spellings(self) -> None:
        for spelling in (DASHED, PLAIN):
            with self.subTest(spelling=spelling):
                self.assertTrue(tencent._is_channel_check(spelling, "没通过渠道校验"))
                self.assertTrue(tencent._is_channel_check(
                    spelling, "This call failed channel verification"))
        # 没有渠道校验文案 → 不命中（内容审核那类要保持原行为）
        self.assertFalse(tencent._is_channel_check(DASHED, "blocked by security policy"))
        self.assertFalse(tencent._is_channel_check(400, "没通过渠道校验"))


if __name__ == "__main__":
    unittest.main()
