"""流式转发的「内容空闲」看门狗与客户端断连检测（issue #485）。

现场：上游连接还活着、保活帧一直在来，但正文早就不产出了。`UPSTREAM_TIMEOUT`
是 httpx 的 read timeout，**每收到一个字节就重置**，保活帧会不断把它刷新 ——
于是一条请求可以从 1.8 秒的首帧一直挂到 999 秒，客户端早在 600 秒就弃连了。
这段时间里它一直占着上游连接与并发名额。

这里用假上游复现同一形态：先给一个正文帧，然后只给保活帧，确认
  1. 达到 `STREAM_IDLE_TIMEOUT` 后主动中断，不再无限等待；
  2. 客户端收到一个明确的 SSE 错误帧，而不是静默截断；
  3. 日志里这条记录带错误说明（不会呈现为「200 正常长耗时」）。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from server import config, db, keysvc, security

# 正文帧（choices[0].delta.content 非空）
CONTENT_FRAME = (
    b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
)
# 保活帧：只有注释行 / 空事件，没有任何正文。
KEEPALIVE_FRAME = b': keepalive\n\n'


class _IdleUpstream:
    """状态 200 的 SSE 上游：一帧正文之后只发保活帧。"""

    status_code = 200
    headers = {'content-type': 'text/event-stream'}

    def __init__(self) -> None:
        self.closed = False

    async def aiter_bytes(self):
        yield CONTENT_FRAME
        # 每 30ms 一帧保活，持续到看门狗触发为止（测试里把上限缩到 100ms）。
        for _ in range(20):
            await asyncio.sleep(0.03)
            yield KEEPALIVE_FRAME

    async def aclose(self) -> None:
        self.closed = True


class _Client:
    def __init__(self, resp: _IdleUpstream) -> None:
        self._resp = resp

    def build_request(self, *a, **kw):
        return object()

    async def send(self, req, stream=True):
        return self._resp

    async def post(self, url, **kw):
        return self._resp

    async def aclose(self) -> None:
        return None


class StreamIdleWatchdogTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        self._orig_idle = config.STREAM_IDLE_TIMEOUT
        config.DB_PATH = Path(self._tmp.name) / 's.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        # 100ms 足够区分「会中断」与「一直等」：假上游要发 20×30ms=600ms。
        config.STREAM_IDLE_TIMEOUT = 0.1
        db._conn = None
        db.connect()
        security.save_users({
            'secret': 'S',
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('p')}],
            'api_keys': [],
        })
        from server.main import app
        self.client = TestClient(app)
        self.token = keysvc.create_key('t')['key']
        self.upstream = _IdleUpstream()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        config.STREAM_IDLE_TIMEOUT = self._orig_idle
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _chat(self):
        with mock.patch.object(config, 'http_client',
                               lambda *a, **k: _Client(self.upstream)):
            return self.client.post(
                '/v1/chat/completions',
                headers={'Authorization': f'Bearer {self.token}'},
                json={'model': 'glm-5.2',
                      'messages': [{'role': 'user', 'content': 'hi'}],
                      'stream': True},
            )

    def test_idle_stream_is_aborted_with_explicit_error(self) -> None:
        started = time.monotonic()
        r = self._chat()
        elapsed = time.monotonic() - started

        self.assertEqual(200, r.status_code, r.text[:300])
        # 必须比假上游的总时长（600ms）明显短，说明是看门狗中断的。
        self.assertLess(elapsed, 0.55, '看门狗没有生效：整条假流被读完了')
        body = r.content
        self.assertIn(b'hi', body, '首帧正文应已转发给客户端')
        # 转发热路径必须原样透传：中断时**不能**注入网关自己构造的 SSE 帧。
        # 客户端靠「流没有正常结束」判定截断，这与
        # test_gateway_source_does_not_rewrite_sse 是同一条约束。
        self.assertNotIn(b'upstream_idle_timeout', body,
                         '网关不应改写 SSE：中断靠断开连接，不靠注入错误帧')
        self.assertTrue(self.upstream.closed, '中断后必须关闭上游连接')

    def test_abort_is_recorded_as_error(self) -> None:
        self._chat()
        row = db.query_one(
            'SELECT error FROM request_logs ORDER BY id DESC LIMIT 1')
        self.assertIsNotNone(row)
        self.assertIn('idle', (row['error'] or ''),
                      '中断必须落进日志，否则监控会当成正常长耗时请求')


if __name__ == '__main__':
    unittest.main()
