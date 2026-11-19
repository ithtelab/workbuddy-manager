"""账号 × 模型主动探测（issue #270）。

钉住两件事：

  1. **分类口径**与上游一致：6004 是模型限流、11102 是「该账号无此模型」
     （不是错误）、401/12153 是凭证失效、402/14018 是额度不足；
  2. 组合数上限与模型名去前缀（`cn:` / `global:` 同名合并）。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import accounts as accounts_router  # noqa: E402
from server.services import tencent  # noqa: E402


class ClassifyProbeResultTest(unittest.TestCase):
    def test_ok(self) -> None:
        self.assertEqual(
            tencent.classify_probe_result(True, '连通正常（glm-5.2，120ms）'),
            ('ok', None),
        )

    def test_no_model_is_not_an_error(self) -> None:
        status, code = tencent.classify_probe_result(
            False, '上游返回 code=11102 该后端无此模型')
        self.assertEqual((status, code), ('no_model', '11102'))

    def test_model_rate_limit(self) -> None:
        self.assertEqual(
            tencent.classify_probe_result(False, '上游返回 code=6004 限流')[0],
            'rate',
        )

    def test_account_rate_limit_without_code(self) -> None:
        self.assertEqual(
            tencent.classify_probe_result(False, '上游返回 code=429 too many')[0],
            'rate',
        )

    def test_credit(self) -> None:
        self.assertEqual(
            tencent.classify_probe_result(False, '上游返回 code=14018 积分耗尽')[0],
            'credit',
        )
        self.assertEqual(
            tencent.classify_probe_result(False, '上游返回 code=402 请充值')[0],
            'credit',
        )
        self.assertEqual(
            tencent.classify_probe_result(False, '请求异常: 积分不足，请充值')[0],
            'credit',
        )

    def test_auth(self) -> None:
        self.assertEqual(
            tencent.classify_probe_result(False, '上游返回 code=12153 会话已失效')[0],
            'auth',
        )
        self.assertEqual(
            tencent.classify_probe_result(False, '上游返回 code=401')[0],
            'auth',
        )

    def test_other_errors(self) -> None:
        self.assertEqual(
            tencent.classify_probe_result(False, '上游返回 code=11115 prompt 过长')[0],
            'error',
        )
        self.assertEqual(
            tencent.classify_probe_result(False, '请求异常: timeout')[0],
            'error',
        )


class ProbeEndpointTest(unittest.TestCase):
    RAW = {
        'account': {'uid': 'u1', 'nickname': '猫猫', 'enterpriseId': ''},
        'auth': {'accessToken': 'tok', 'realm': 'cn', 'domain': ''},
        'device_token': '',
    }

    def _run(self, body, outcomes=None, raw=None):
        calls: list[str] = []
        outcomes = outcomes or {}

        async def fake_probe(auth, model):
            calls.append(model)
            status, code = outcomes.get(model, ('ok', None))
            return {'status': status, 'code': code, 'message': f'{model}:{status}',
                    'latency_ms': 1}

        with mock.patch.object(accounts_router, '_load',
                               lambda fn, d: raw or self.RAW), \
             mock.patch.object(accounts_router.tencent, 'probe_model', fake_probe), \
             mock.patch.object(accounts_router, '_PROBE_ACCOUNT_GAP_SECONDS', 0):
            out = asyncio.run(accounts_router.probe_models(
                body, upstream_id=None, user={'role': 'admin'}))
        return out, calls

    def test_matrix_dedupes_prefixed_models(self) -> None:
        out, calls = self._run({
            'accounts': ['a.json'],
            'models': ['cn:glm-5.2', 'global:glm-5.2', 'deepseek-v4.1-flash'],
        }, outcomes={'glm-5.2': ('rate', '6004'),
                     'deepseek-v4.1-flash': ('no_model', '11102')})
        self.assertEqual(calls, ['glm-5.2', 'deepseek-v4.1-flash'],
                         'cn:/global: 同名模型合并成一条')
        self.assertEqual(out['requests'], 2)
        self.assertEqual(out['summary']['rate'], 1)
        self.assertEqual(out['summary']['no_model'], 1)
        self.assertEqual(out['results'][0]['nickname'], '猫猫')
        self.assertEqual(out['results'][0]['realm'], 'cn')

    def test_combo_limit(self) -> None:
        body = {'accounts': [f'{i}.json' for i in range(7)],
                'models': [f'm{i}' for i in range(10)]}
        with self.assertRaises(accounts_router.HTTPException) as ctx:
            self._run(body)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn('组合', str(ctx.exception.detail))

    def test_account_limit(self) -> None:
        body = {'accounts': [f'{i}.json' for i in range(21)], 'models': ['m']}
        with self.assertRaises(accounts_router.HTTPException) as ctx:
            self._run(body)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_requires_both_sides(self) -> None:
        with self.assertRaises(accounts_router.HTTPException) as ctx:
            self._run({'accounts': ['a.json'], 'models': []})
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == '__main__':
    unittest.main()
