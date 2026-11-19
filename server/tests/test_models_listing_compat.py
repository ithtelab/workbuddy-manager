"""面板 `/v1/models` 的能力字段兜底（issue #211）。

#156 只在上游侧补了 `modalities` / `supported_reasoning_levels`；面板是独立进程，
随包上游可能比面板旧，于是下游客户端经面板拿到的清单仍缺字段，整条模型被跳过。

这里锁三条：
  1. 多模态模型的 `modalities.input` 含 image；纯文本模型也有 `modalities`（只含 text）
     —— 缺整个字段才是 #211 的现场；
  2. `supported_reasoning_levels` 由 `reasoning_supported_efforts` 派生，
     没有档位就不编造；
  3. **只增不改**：上游已经给了的字段、其它顶层键、认不出的条目一律原样保留。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers.gateway import _listing_compat_models  # noqa: E402


class ListingCompatTest(unittest.TestCase):
    def test_multimodal_and_reasoning(self) -> None:
        payload = {'object': 'list', 'data': [{
            'id': 'global:deepseek-v4.1-flash',
            'supports_images': True,
            'reasoning_supported_efforts': ['low', 'high', 'max'],
        }]}
        out = _listing_compat_models(payload)
        entry = out['data'][0]
        self.assertEqual(entry['modalities'], {'input': ['text', 'image']})
        self.assertEqual(entry['supported_reasoning_levels'],
                         [{'effort': 'low'}, {'effort': 'high'}, {'effort': 'max'}])
        self.assertTrue(entry['supports_reasoning'])
        # 原有字段与顶层结构不变
        self.assertTrue(entry['supports_images'])
        self.assertEqual(entry['reasoning_supported_efforts'], ['low', 'high', 'max'])
        self.assertEqual(out['object'], 'list')

    def test_text_only_model_still_gets_modalities(self) -> None:
        """纯文本模型也要有 modalities.input —— 缺字段会让客户端整条跳过。"""
        out = _listing_compat_models({'data': [{'id': 'cn:glm-5.2'}]})
        self.assertEqual(out['data'][0]['modalities'], {'input': ['text']})
        self.assertNotIn('supported_reasoning_levels', out['data'][0])
        self.assertNotIn('supports_reasoning', out['data'][0])

    def test_upstream_fields_are_not_overwritten(self) -> None:
        """上游已经给了的字段原样保留（新上游自己会补，别把它的口径改掉）。"""
        entry = {
            'id': 'x', 'supports_images': True,
            'modalities': {'input': ['text']},
            'supported_reasoning_levels': [{'effort': 'medium'}],
            'supports_reasoning': False,
            'reasoning_supported_efforts': ['high'],
        }
        out = _listing_compat_models({'data': [entry]})
        got = out['data'][0]
        self.assertEqual(got['modalities'], {'input': ['text']})
        self.assertEqual(got['supported_reasoning_levels'], [{'effort': 'medium'}])
        self.assertFalse(got['supports_reasoning'])

    def test_unknown_shapes_pass_through(self) -> None:
        self.assertEqual(_listing_compat_models('not a dict'), 'not a dict')
        self.assertEqual(_listing_compat_models({'error': 'x'}), {'error': 'x'})
        # 缺 id 的条目（认不出是什么）原样保留，不臆造能力字段
        mixed = {'data': [{'no_id': True}, {'id': 'm'}]}
        out = _listing_compat_models(mixed)
        self.assertEqual(out['data'][0], {'no_id': True})
        self.assertIn('modalities', out['data'][1])


if __name__ == '__main__':
    unittest.main()
