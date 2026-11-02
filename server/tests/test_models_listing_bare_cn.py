"""网关 `/v1/models` 下发的国内版 id 不带 `cn:` 前缀。

模型中心早已只显示裸名，但网关仍透出上游的 `cn:xxx`。Cherry Studio 等客户端
按模型 id 识别推理档位，认不出 `cn:deepseek-v4.1-flash`，推理档位选项不出现；
同一个模型换成裸名 `deepseek-v4.1-flash` 就能识别出 low/high/max。

钉住的约束：
  · 只去 `cn:`，**保留 `global:`**（去掉会被路由到国内账号池）；
  · 去前缀后同名的条目只留一条，取字段更全的那条；
  · 列表里的每个 id 用同一把密钥调用都要被放行（与 #46 的不变量一致）；
  · 认不出的结构原样透传，不改调用方的入参。
"""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import keysvc  # noqa: E402
from server.routers import gateway as G  # noqa: E402

UPSTREAM = {'object': 'list', 'data': [
    {'id': 'cn:deepseek-v4.1-flash', 'object': 'model',
     'reasoning_supported_efforts': ['low', 'high', 'max']},
    {'id': 'cn:glm-5.2', 'object': 'model'},
    {'id': 'global:deepseek-v4.1-flash', 'object': 'model'},
    {'id': 'global:gpt-5.6-sol', 'object': 'model'},
]}


def _ids(payload: dict) -> list[str]:
    return [m['id'] for m in payload['data']]


class BareCnModelIdsTest(unittest.TestCase):
    def test_strips_cn_keeps_global(self) -> None:
        out = G._bare_cn_model_ids(copy.deepcopy(UPSTREAM))
        self.assertEqual(_ids(out), [
            'deepseek-v4.1-flash', 'glm-5.2',
            'global:deepseek-v4.1-flash', 'global:gpt-5.6-sol'])

    def test_other_fields_survive(self) -> None:
        out = G._bare_cn_model_ids(copy.deepcopy(UPSTREAM))
        self.assertEqual(out['data'][0]['reasoning_supported_efforts'], ['low', 'high', 'max'])
        self.assertEqual(out['object'], 'list')

    def test_does_not_mutate_input(self) -> None:
        src = copy.deepcopy(UPSTREAM)
        G._bare_cn_model_ids(src)
        self.assertEqual(src, UPSTREAM)

    def test_duplicate_keeps_richer_entry(self) -> None:
        payload = {'data': [
            {'id': 'glm-5.2'},
            {'id': 'cn:glm-5.2', 'name': 'GLM-5.2', 'supports_images': True},
        ]}
        out = G._bare_cn_model_ids(payload)
        self.assertEqual(out['data'], [{'id': 'glm-5.2', 'name': 'GLM-5.2', 'supports_images': True}])

    def test_unknown_shapes_pass_through(self) -> None:
        for payload in (None, [], {'models': []}, {'data': 'x'}):
            self.assertIs(G._bare_cn_model_ids(payload), payload)
        odd = {'data': [None, 'x', {'id': ''}, {'name': 'no id'}]}
        self.assertEqual(G._bare_cn_model_ids(odd)['data'], odd['data'])

    def test_listed_ids_stay_callable(self) -> None:
        """列表里的裸名与调用侧同一口径：裸名就是国内版，`global:` 仍是国际版。"""
        out = G._bare_cn_model_ids(copy.deepcopy(UPSTREAM))
        for mid in _ids(out):
            self.assertFalse(mid.lower().startswith('cn:'))
            self.assertEqual(keysvc._bare_model(mid), mid)


if __name__ == '__main__':
    unittest.main()
