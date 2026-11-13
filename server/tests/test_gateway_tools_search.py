"""/v1/tools/search 透传路由的行为契约（0057 配套管理端路由）。

验证点（全部离线：不联网、mock 上游转发）：
  1. 鉴权失败（无密钥 / 坏密钥）→ 401，不触上游；
  2. query 为空 / 请求体不是 JSON → 400，不触上游；
  3. 成功路径 → 上游信封原样透传（含 code/results/total_results）；
  4. 上游 429（quota_web_search）→ 状态码与信封原样透传；
  5. 上游 200 但业务码非 0 → 原样透传（不做过滤语义）；
  6. 上游不可达 → 502 upstream_unavailable；
  7. 每次可观测调用落一条请求日志（_record）。
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from fastapi.responses import JSONResponse  # noqa: E402

from server.routers import gateway  # noqa: E402


def _request(body: bytes | None, key: str | None = 'wbk_test') -> object:
    """构造一个最小 Request 形状（json()/headers/url.path/client_ip 由 mock 承担）。"""
    sent = {'done': False}

    async def receive():
        if sent['done'] or body is None:
            return {'type': 'http.disconnect'}
        sent['done'] = True
        return {'type': 'http.request', 'body': body, 'more_body': False}

    class _Req:
        headers = {'user-agent': 'test'} if True else {}

        class url:  # noqa: N801
            path = '/v1/tools/search'

        @staticmethod
        async def json():
            if body is None:
                raise ValueError('no body')
            return json.loads(body)

    req = _Req()
    req._receive = receive
    return req


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class ToolSearchRoute(unittest.TestCase):
    """在 mock 掉鉴权/上游/记账之后，验证路由的分支行为。"""

    def setUp(self):
        # 默认全部 mock：各测试自己覆盖需要的点
        self._patches = []

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _patch_common(self, *, key=None, auth_err=None, upstream=None, up_err=None,
                      response=None, up_exc=None, recorded=None):
        """统一 mock：_authorize / _upstream_for / config.http_client / _record。"""
        p1 = mock.patch.object(gateway, '_authorize',
                               return_value=(key or {'name': 'k'}, '1.2.3.4', auth_err))
        p2 = mock.patch.object(gateway, '_upstream_for',
                               return_value=(upstream, up_err))
        p3 = mock.patch.object(gateway, '_record')
        p4 = mock.patch.object(gateway, 'tap_svc')
        p4b = mock.patch.object(gateway.tap_svc, 'enabled', return_value=False)
        p4c = mock.patch.object(gateway.tap_svc, 'capture')
        p5 = mock.patch.object(gateway.iputil, 'client_ip', return_value='1.2.3.4')
        for p in (p1, p2, p3, p4, p4b, p4c, p5):
            p.start()
            self._patches.append(p)
        self.recorded = gateway._record
        self.tap_capture = gateway.tap_svc.capture

        if up_exc is not None:
            # http_client 上下文抛异常 → 路由 502 分支
            import contextlib

            @contextlib.asynccontextmanager
            async def broken(*a, **kw):
                raise up_exc
                yield None  # pragma: no cover
            p6 = mock.patch.object(gateway.config, 'http_client', broken)
        elif response is not None:
            import contextlib

            class _Resp:
                def __init__(self, status, payload):
                    self.status_code = status
                    self._payload = payload
                    self.text = json.dumps(payload)

                def json(self):
                    if self._payload is None:
                        raise ValueError('not json')
                    return self._payload

            @contextlib.asynccontextmanager
            async def fake_client(*a, **kw):
                class _C:
                    async def post(self, url, json=None, headers=None):
                        self.posted_url = url
                        self.posted_json = json
                        self.posted_headers = headers
                        return _Resp(*response)
                yield _C()
            p6 = mock.patch.object(gateway.config, 'http_client', fake_client)
        else:
            # 请求在校验阶段就会被拒（400/401 等），不会走到 http_client；
            # 给一个永不触发的占位 mock 保持 patch 对称。
            import contextlib

            @contextlib.asynccontextmanager
            async def unused(*a, **kw):
                raise AssertionError('http_client must not be reached')
                yield None  # pragma: no cover
            p6 = mock.patch.object(gateway.config, 'http_client', unused)
        p6.start()
        self._patches.append(p6)
        self.client_holder = p6

    # ---- 鉴权 ----

    def test_missing_key_rejected_without_upstream_call(self):
        self._patch_common(key=None, auth_err=JSONResponse({'e': 1}, status_code=401))
        resp = _run(gateway.tools_search(_request(b'{"query":"x"}', key=None)))
        self.assertEqual(resp.status_code, 401)
        self.recorded.assert_not_called()

    # ---- 请求体 ----

    def test_empty_query_400(self):
        self._patch_common(key={'name': 'k'}, upstream={'base_url': 'http://up', 'api_key': 'gk'})
        resp = _run(gateway.tools_search(_request(b'{"query":"  "}')))
        self.assertEqual(resp.status_code, 400)
        self.recorded.assert_called()

    def test_invalid_json_400(self):
        self._patch_common(key={'name': 'k'}, upstream={'base_url': 'http://up', 'api_key': 'gk'})
        resp = _run(gateway.tools_search(_request(b'not json')))
        self.assertEqual(resp.status_code, 400)

    # ---- 透传 ----

    def test_success_passthrough_envelope(self):
        upstream_body = {'code': 0, 'msg': '',
                         'results': [{'title': 't', 'url': 'https://a/x', 'snippet': 's'}],
                         'total_results': 1, 'response_time_ms': 9, 'provider': '1'}
        self._patch_common(key={'name': 'k'},
                           upstream={'base_url': 'http://up.example', 'api_key': 'gwk'},
                           response=(200, upstream_body))
        resp = _run(gateway.tools_search(_request(b'{"query":"go","max_results":3}')))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.body), upstream_body)  # 语义等价（序列化空格可不同）
        self.recorded.assert_called_once()
        args = self.recorded.call_args.args
        self.assertEqual(args[4], 200)  # status

    def test_quota_429_passthrough(self):
        quota = {'error': 'web search quota exhausted on all available accounts',
                 'code': 'quota_web_search', 'tried': 2}
        self._patch_common(key={'name': 'k'},
                           upstream={'base_url': 'http://up', 'api_key': 'gk'},
                           response=(429, quota))
        resp = _run(gateway.tools_search(_request(b'{"query":"q"}')))
        self.assertEqual(resp.status_code, 429)
        self.assertIn(b'quota_web_search', resp.body)

    def test_bizcode_200_passthrough(self):
        self._patch_common(key={'name': 'k'},
                           upstream={'base_url': 'http://up', 'api_key': 'gk'},
                           response=(200, {'code': 15001, 'msg': 'quota_web_search'}))
        resp = _run(gateway.tools_search(_request(b'{"query":"q"}')))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'15001', resp.body)

    # ---- 上游故障 ----

    def test_upstream_down_502(self):
        self._patch_common(key={'name': 'k'},
                           upstream={'base_url': 'http://up', 'api_key': 'gk'},
                           up_exc=ConnectionError('refused'))
        resp = _run(gateway.tools_search(_request(b'{"query":"q"}')))
        self.assertEqual(resp.status_code, 502)
        self.recorded.assert_called_once()
        self.assertEqual(self.recorded.call_args.args[4], 502)

    def test_upstream_unbound_503(self):
        from fastapi.responses import JSONResponse as JR
        self._patch_common(key={'name': 'k'}, upstream=None,
                           up_err=JR({'e': 'unavailable'}, status_code=503))
        resp = _run(gateway.tools_search(_request(b'{"query":"q"}')))
        self.assertEqual(resp.status_code, 503)


if __name__ == '__main__':
    unittest.main()
