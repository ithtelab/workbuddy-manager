"""网关错误契约的回归：11102 → 404、429 → Retry-After、/v1 下的 404/405 回 JSON。

三处缺陷的共同点是**客户端拿到的信号会把它带偏**：

  1. 模型不存在时，上游把它包成 `503 no_healthy_account`（`11102 model …
     service info not found` 藏在 message 里）。客户端按 5xx 当故障反复重试，
     运维也以为号池挂了 —— 但换多少账号都不会有这个模型，重试毫无意义。
  2. 429（密钥配额用尽 / 上游 6004 模型限额）不带 `Retry-After`：上游文案里
     明明给了「将在 YYYY-MM-DD HH:MM:SS 重置」的墙钟，客户端却只能从中文里
     自己抠，退避只能靠猜。
  3. `GET /v1/chat/completions` 会落到 SPA 兜底、返回整页 HTML（404 的 Next
     外壳），`PUT /v1/models` 回 `{"detail": …}`：同一个网关路径上，错误形状
     取决于「命中哪条路由」，客户端没法统一处理。

这些用例同时钉住**流式分支**：上游 4xx/5xx 本来就不是 SSE，以前按流原样透传，
既补不了头也改不了状态码。
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from server.routers import gateway  # noqa: E402


class RetryAfterParseTest(unittest.TestCase):
    """`_retry_after_seconds`：从上游文案里取重置墙钟。"""

    # 上游文案固定带 `UTC+8`。构造时间时必须**按 UTC+8 造**：拿本机
    # `datetime.now()` 拼一个再贴上 UTC+8，断言会随机器时区漂移（面板跑在
    # UTC 的 CI 上会差 8 小时）。
    _CST = datetime.timezone(datetime.timedelta(hours=8))

    def _text(self, when: datetime.datetime) -> str:
        return ('{"error":{"message":"您的使用量已超出频率限制，将在 '
                + when.strftime('%Y-%m-%d %H:%M:%S')
                + ' UTC+8 重置，您也可以切换其他模型继续使用。"}}')

    def _now(self) -> datetime.datetime:
        return datetime.datetime.now(self._CST)

    def test_parses_future_reset_clock(self) -> None:
        secs = gateway._retry_after_seconds(
            self._text(self._now() + datetime.timedelta(hours=2)))
        self.assertIsNotNone(secs, '带重置墙钟的 429 必须能算出 Retry-After')
        self.assertGreater(secs, 7000)
        self.assertLessEqual(secs, 7300)

    def test_expired_clock_returns_none(self) -> None:
        """墙钟已经过去（时钟偏移 / 旧文案）时不要给一个「负数等待」。"""
        self.assertIsNone(gateway._retry_after_seconds(
            self._text(self._now() - datetime.timedelta(hours=1))))

    def test_missing_clock_returns_none(self) -> None:
        self.assertIsNone(gateway._retry_after_seconds(
            '{"error":{"message":"您的使用量已超出频率限制"}}'))
        self.assertIsNone(gateway._retry_after_seconds(''))
        self.assertIsNone(gateway._retry_after_seconds(None))

    def test_capped_at_one_day(self) -> None:
        """上游给一个月后的重置时间也不该让客户端睡一个月。"""
        secs = gateway._retry_after_seconds(
            self._text(self._now() + datetime.timedelta(days=30)))
        self.assertEqual(secs, 86400)

    def test_negative_offset_is_parsed(self) -> None:
        """`UTC-5` 也要认：同一个上游在不同区域部署时文案可能不同。"""
        tz = datetime.timezone(datetime.timedelta(hours=-5))
        when = datetime.datetime.now(tz) + datetime.timedelta(hours=3)
        text = '将在 ' + when.strftime('%Y-%m-%d %H:%M:%S') + ' UTC-5 重置'
        secs = gateway._retry_after_seconds(text)
        self.assertIsNotNone(secs)
        self.assertGreater(secs, 10500)   # 期望 ≈3 小时
        self.assertLessEqual(secs, 10900)

    def test_offset_is_honored_regardless_of_panel_tz(self) -> None:
        """结果只该由文案里的 UTC±N 决定，与面板进程的时区无关。"""
        text = self._text(self._now() + datetime.timedelta(hours=4))
        first = gateway._retry_after_seconds(text)
        old = os.environ.get('TZ')
        try:
            os.environ['TZ'] = 'UTC'
            if hasattr(time, 'tzset'):
                time.tzset()
            second = gateway._retry_after_seconds(text)
        finally:
            if old is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = old
            if hasattr(time, 'tzset'):
                time.tzset()
        for secs in (first, second):
            self.assertIsNotNone(secs)
            self.assertGreater(secs, 14000)   # 期望 ≈4 小时
            self.assertLessEqual(secs, 14600)

    def test_header_helper(self) -> None:
        self.assertIsNone(gateway._retry_after_header(None))
        self.assertIsNone(gateway._retry_after_header(0))
        self.assertEqual(gateway._retry_after_header(60), {'Retry-After': '60'})


class ModelMissingRewriteTest(unittest.TestCase):
    """`_model_missing_body`：11102 改判 404，其余 5xx 原样。"""

    _TEXT = json.dumps({'error': {
        'code': 'no_healthy_account',
        'gateway_hint': 'upstream has no such model on this backend; switch model or retry on another account',
        'message': json.dumps({'code': 11102, 'msg': 'model [nope] service info not found'}),
    }}, ensure_ascii=False)

    def test_11102_becomes_model_not_found(self) -> None:
        body = gateway._model_missing_body(json.loads(self._TEXT), self._TEXT)
        self.assertIsNotNone(body, '11102 必须改判，否则客户端会把「模型不存在」当故障重试')
        self.assertEqual(body['error']['code'], 'model_not_found')
        self.assertEqual(body['error']['type'], 'invalid_request_error')
        self.assertIn('11102', body['error']['message'], '上游原文要保留，排障要看 requestId')
        self.assertIn('gateway_hint', body['error'], '上游给的可执行建议不能丢')

    def test_plain_5xx_untouched(self) -> None:
        self.assertIsNone(gateway._model_missing_body(
            {'error': {'message': 'internal error'}}, 'internal error'))

    def test_non_json_text_is_tolerated(self) -> None:
        """上游回纯文本 11102 时也要改判（payload 为 None）。"""
        body = gateway._model_missing_body(None, '{"code":11102,"msg":"model [x] service info not found"}')
        self.assertIsNotNone(body)
        self.assertEqual(body['error']['code'], 'model_not_found')


class OaiErrorHeaderTest(unittest.TestCase):
    def test_retry_after_lands_on_response(self) -> None:
        resp = gateway._oai_error('配额已用尽', 429, 'insufficient_quota', 'quota_exhausted',
                                  retry_after=60)
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.headers['retry-after'], '60')

    def test_no_header_by_default(self) -> None:
        resp = gateway._oai_error('模型不允许', 400, 'invalid_request_error', 'model_not_allowed')
        self.assertNotIn('retry-after', {k.lower() for k in resp.headers})


class GatewayErrorContractHTTPTest(unittest.TestCase):
    """端到端：网关路径上的错误形状（含流式分支的 11102 / 429）。"""

    # 与 RetryAfterParseTest 同口径：按 UTC+8 构造，不随机器时区漂移
    _CST = datetime.timezone(datetime.timedelta(hours=8))
    _RESET_IN = datetime.datetime.now(_CST) + datetime.timedelta(hours=2)
    _BODY_11102 = json.dumps({'error': {
        'code': 'no_healthy_account',
        'gateway_hint': 'upstream has no such model on this backend; switch model or retry on another account',
        'message': json.dumps({'code': 11102, 'msg': 'model [nope] service info not found',
                               'requestId': 'req-11102'}),
    }}, ensure_ascii=False)
    _BODY_6004 = json.dumps({'error': {
        'code': 'rate_limit_exceeded',
        'gateway_hint': 'rate limited by upstream; retry after reset',
        # 上游的真实响应体里中文是**原样 UTF-8**（不是 \u 转义），这里必须照抄，
        # 否则解析逻辑看起来"能用"、实际只对测试样例有效。
        'message': json.dumps({'code': 6004,
                               'msg': '您的使用量已超出频率限制，将在 '
                                      + _RESET_IN.strftime('%Y-%m-%d %H:%M:%S')
                                      + ' UTC+8 重置，您也可以切换其他模型继续使用。'},
                              ensure_ascii=False),
    }}, ensure_ascii=False)

    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.testclient import TestClient

        from server import config, db, keysvc

        cls._tmp = tempfile.TemporaryDirectory()
        cls._dir = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = cls._dir / 'contract.db'
        config.USERS_FILE = cls._dir / 'users.json'
        # 造一个「有静态产物」的目录：生产形态下 /v1 会先撞上 SPA 兜底路由，
        # 那正是这次要修的那条路径。
        static = cls._dir / 'static'
        (static / '_next').mkdir(parents=True)
        (static / 'index.html').write_text('<html>panel</html>', encoding='utf-8')
        config.STATIC_DIR = static
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': 'x'}],
            'api_keys': [],
        }), encoding='utf-8')

        db._conn = None
        db.connect()
        from server.main import app
        cls.key = keysvc.create_key(name='gateway-contract-test')['key']
        cls.c = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        from server import config, db
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    # ── 假上游 ────────────────────────────────────────────
    def _post(self, body: dict, upstream_status: int, upstream_body: str):
        from server import config

        class _Resp:
            status_code = upstream_status
            headers = {'content-type': 'application/json'}
            text = upstream_body

            def json(self):
                return json.loads(upstream_body)

            async def aread(self):
                return upstream_body.encode()

            async def aiter_bytes(self):
                yield upstream_body.encode()

            async def aclose(self):
                return None

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, **kw):
                return _Resp()

            def build_request(self, *a, **kw):
                return object()

            async def send(self, req, **kw):
                return _Resp()

            async def aclose(self):
                return None

        with mock.patch.object(config, 'http_client', lambda *a, **k: _Client()):
            return self.c.post('/v1/chat/completions', json=body,
                               headers={'Authorization': 'Bearer ' + self.key})

    def _body(self, **kw) -> dict:
        body = {'model': 'cn:glm-5.3-flash',
                'messages': [{'role': 'user', 'content': 'hi'}],
                'max_tokens': 8}
        body.update(kw)
        return body

    # ── 路由形状 ─────────────────────────────────────────
    def test_get_chat_returns_json_not_spa_html(self) -> None:
        r = self.c.get('/v1/chat/completions')
        self.assertEqual(r.status_code, 404)
        self.assertIn('application/json', r.headers.get('content-type', ''))
        self.assertEqual(r.json()['error']['code'], 'not_found')

    def test_wrong_method_returns_json(self) -> None:
        r = self.c.put('/v1/models')
        self.assertEqual(r.status_code, 405)
        self.assertIn('application/json', r.headers.get('content-type', ''))
        self.assertEqual(r.json()['error']['code'], 'method_not_allowed')

    # ── 非流式 ───────────────────────────────────────────
    def test_unknown_model_nonstream_is_404(self) -> None:
        r = self._post(self._body(model='nope'), 503, self._BODY_11102)
        self.assertEqual(r.status_code, 404,
                         '11102 不能以 503 出去：客户端会当故障重试，运维会查号池')
        self.assertEqual(r.json()['error']['code'], 'model_not_found')

    def test_upstream_429_nonstream_carries_retry_after(self) -> None:
        r = self._post(self._body(), 429, self._BODY_6004)
        self.assertEqual(r.status_code, 429)
        self.assertIn('retry-after', {k.lower() for k in r.headers},
                      '上游已经给了重置墙钟，网关必须把它变成标准的 Retry-After')
        self.assertGreater(int(r.headers['retry-after']), 7000)

    # ── 流式（本次唯一的行为变更）──────────────────────────
    def test_unknown_model_stream_is_json_404(self) -> None:
        r = self._post(self._body(model='nope', stream=True), 503, self._BODY_11102)
        self.assertEqual(r.status_code, 404)
        self.assertIn('application/json', r.headers.get('content-type', ''))
        self.assertEqual(r.json()['error']['code'], 'model_not_found')

    def test_upstream_429_stream_carries_retry_after(self) -> None:
        r = self._post(self._body(stream=True), 429, self._BODY_6004)
        self.assertEqual(r.status_code, 429)
        self.assertGreater(int(r.headers['retry-after']), 7000)

    def test_upstream_500_stream_stays_json(self) -> None:
        """普通 5xx 不该被 11102 的改判规则误伤。"""
        body = json.dumps({'error': {'message': 'upstream boom', 'code': 'api_error'}})
        r = self._post(self._body(stream=True), 500, body)
        self.assertEqual(r.status_code, 500)
        self.assertIn('upstream boom', r.text)


if __name__ == '__main__':
    unittest.main()
