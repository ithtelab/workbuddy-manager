"""上游官方统计的口径与失败态（issue #59）。

用户用的是**创建 workbuddy2api 时拿到的那把 api_key**，直连 7863，不经过本网关，
所以面板自己的用量统计里永远没有它。他想要的是上游**自己那份**统计
（`/v1/stats`：按模型累计的请求 / 成功 / 失败 / Token / 缓存命中 / 实付积分）。

这里钉住的是三件容易出错的事：

  1. **口径要在返回值里说清**：上游那份是「上游进程自己看到的全部调用（含直连）
     且自启动累计」，与面板按时段/按密钥统计的不是一回事。混着展示会把用户带进
     沟里（例如以为「今日请求」包含直连流量）。
  2. **取不到要说明原因**，不能返回空统计：三种失败（上游没起来 / 版本太旧没有
     这个端点 / api_key 不一致）分别给出不同的说明。返回 `available=True` +
     全 0 会被读成「确实没用量」——那是最误导的一种。
  3. **不向前端泄露上游 api_key**：只透传统计字段，不把鉴权头或配置带出去。
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.routers import stats as stats_router  # noqa: E402
from server.services import wb2api  # noqa: E402


class _Resp:
    def __init__(self, status: int = 200, payload: object = None, text: str = ''):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise json.JSONDecodeError('bad', '', 0)
        return self._payload


class _Client:
    def __init__(self, resp: _Resp):
        self._resp = resp
        self.sent: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, **kw):
        self.sent.append({'url': url, **kw})
        return self._resp


PAYLOAD = {
    'enabled': True,
    'since': '2026-09-22T04:00:00Z',
    'uptime_sec': 7200,
    'total': {'model': '', 'requests': 1234, 'success': 1200, 'failed': 34,
              'prompt_tokens': 800000, 'completion_tokens': 400000,
              'total_tokens': 1200000, 'credit': 45.6, 'cache_hit_rate': 0.62},
    'models': [
        {'model': 'glm-5.2', 'requests': 900, 'total_tokens': 900000, 'credit': 30.0},
        {'model': 'global:gpt-5.6-sol', 'requests': 334, 'total_tokens': 300000, 'credit': 15.6},
    ],
}


class UpstreamStatsTest(unittest.TestCase):
    def _call(self, resp: _Resp) -> tuple[dict, _Client]:
        client = _Client(resp)
        with mock.patch.object(config, 'http_client', lambda *a, **k: client):
            out = asyncio.run(wb2api.get_upstream_stats())
        return out, client

    def test_happy_path_passes_payload_through(self) -> None:
        out, client = self._call(_Resp(200, PAYLOAD))
        self.assertTrue(out['available'])
        self.assertEqual(out['total']['requests'], 1234)
        self.assertEqual(len(out['models']), 2)
        # 打的是上游的 /v1/stats
        self.assertTrue(client.sent[0]['url'].endswith('/v1/stats'))

    def test_sends_api_key_header(self) -> None:
        with mock.patch.object(config, 'upstream_api_key', lambda: 'k-123'):
            _, client = self._call(_Resp(200, PAYLOAD))
        self.assertEqual(client.sent[0]['headers'].get('Authorization'), 'Bearer k-123')

    def test_auth_mismatch_explains_itself(self) -> None:
        out, _ = self._call(_Resp(401))
        self.assertFalse(out['available'])
        self.assertIn('鉴权', out['error'])

    def test_old_upstream_without_the_endpoint_explains_itself(self) -> None:
        """404 = 上游版本太旧（这个端点较新），不是「用量为 0」。"""
        out, _ = self._call(_Resp(404, text='404 page not found'))
        self.assertFalse(out['available'])
        self.assertIn('版本', out['error'])

    def test_unreachable_upstream_explains_itself(self) -> None:
        client = _Client(_Resp(200, PAYLOAD))
        with mock.patch.object(config, 'http_client', side_effect=OSError('conn refused')):
            out = asyncio.run(wb2api.get_upstream_stats())
        self.assertFalse(out['available'])
        self.assertTrue(out['error'])

    def test_non_json_and_non_object_are_rejected(self) -> None:
        out, _ = self._call(_Resp(200, None))
        self.assertFalse(out['available'])
        out2, _ = self._call(_Resp(200, [1, 2, 3]))
        self.assertFalse(out2['available'])

    def test_never_returns_available_with_zero_totals(self) -> None:
        """取不到时**不能**返回 available=True + 全 0 —— 那会被读成「确实没有用量」。"""
        for resp in (_Resp(401), _Resp(404), _Resp(500), _Resp(200, None)):
            with self.subTest(status=resp.status_code):
                out, _ = self._call(resp)
                self.assertFalse(out['available'])
                self.assertNotIn('total', out)

    def test_endpoint_is_open_to_readonly_users(self) -> None:
        """用量数据对只读账号可见（与这一页其它数字一致），且不泄露 api_key。"""
        out, client = self._call(_Resp(200, PAYLOAD))
        blob = json.dumps(out, ensure_ascii=False)
        self.assertNotIn('Bearer', blob)
        async def _fake() -> dict:
            return {'available': True, 'total': {}}

        with mock.patch.object(wb2api, 'get_upstream_stats', _fake):
            got = asyncio.run(stats_router.upstream_stats(user={'role': 'viewer'}))
        self.assertTrue(got['available'])

    def test_unknown_fields_are_not_forwarded(self) -> None:
        """**白名单下发**：上游统计里出现的新字段不能原样转给前端（审核补漏）。

        与 `load_upstream_config` 同一条理由（那里的注释写得更细）：整包透传的
        失效模式是「上游加了字段 → 下发给每个登录用户（含只读账号）」，而且不会
        有任何报错；白名单的失效模式相反（界面少一列），可见可控。
        """
        payload = {
            'enabled': True,
            'since': 'x',
            'uptime_sec': 1,
            'total': {'requests': 5, 'credit': 1.0,
                      'account_uid': 'secret-uid', 'api_key_hint': 'wbk_abc123'},
            'models': [{'model': 'glm-5.2', 'requests': 5, 'internal_note': '不该下发'}],
            'something_new': {'token': 'leak-me'},
        }
        out, _ = self._call(_Resp(200, payload))
        self.assertTrue(out['available'])
        self.assertEqual(out['total'], {'requests': 5, 'credit': 1.0})
        self.assertEqual(out['models'], [{'model': 'glm-5.2', 'requests': 5}])
        blob = json.dumps(out, ensure_ascii=False)
        for secret in ('secret-uid', 'wbk_abc123', '不该下发', 'leak-me'):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, blob)

    def test_message_only_when_string(self) -> None:
        """`message` 是给界面显示的原因文本；非字符串就不带（避免把对象塞给前端）。"""
        out, _ = self._call(_Resp(200, {'enabled': False, 'message': {'nested': 1}}))
        self.assertNotIn('message', out)
        out2, _ = self._call(_Resp(200, {'enabled': False, 'message': '统计采集已关闭'}))
        self.assertEqual(out2['message'], '统计采集已关闭')

    def test_malformed_sections_degrade_without_crashing(self) -> None:
        """上游把 total/models 写成别的形状时，不能整条失败（fail-soft）。"""
        out, _ = self._call(_Resp(200, {'enabled': True, 'total': 'nope',
                                        'models': [None, 5, {'model': 'ok'}]}))
        self.assertTrue(out['available'])
        self.assertNotIn('total', out)
        self.assertEqual(out['models'], [{'model': 'ok'}])

    def test_no_api_key_configured_still_reaches(self) -> None:
        """上游没配 api_key 时不要自己造一个空 Authorization 头。"""
        with mock.patch.object(config, 'upstream_api_key', lambda: ''):
            _, client = self._call(_Resp(200, PAYLOAD))
        self.assertNotIn('Authorization', client.sent[0]['headers'])


if __name__ == '__main__':
    unittest.main()
