"""思考档位写入请求日志：三条协议路径都要带上（issue #319）。

背景：`request_logs.effort` 与日志页的「思考强度」列早就有了，但只有
`/v1/chat/completions` 那条路径把 `reasoning_effort` 传给 `_record`。
`/v1/responses` 与 `/v1/messages`（Anthropic）的用户在界面上一直看到「—」，
即使他们明明选了档位。

判据是**实际发往上游**的那个值：Responses 的档位藏在 `reasoning.effort`、
Anthropic 的藏在 `output_config.effort` / `thinking.budget_tokens`，都要等
转换后才变成 `reasoning_effort`。所以测试断言「转换后取」，而不是「原样读 body」。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import anthropic as A  # noqa: E402
from server.routers import responses as R  # noqa: E402


class ResponsesEffortTest(unittest.TestCase):
    def test_reasoning_effort_survives_conversion(self) -> None:
        """Responses 的 reasoning.effort 要变成上游的 reasoning_effort。"""
        out = R.to_chat_request({
            'model': 'm',
            'input': [{'role': 'user', 'content': 'hi'}],
            'reasoning': {'effort': 'high'},
        })
        self.assertEqual(out.get('reasoning_effort'), 'high')

    def test_missing_effort_is_absent(self) -> None:
        out = R.to_chat_request({
            'model': 'm',
            'input': [{'role': 'user', 'content': 'hi'}],
        })
        self.assertNotIn('reasoning_effort', out)


class AnthropicEffortTest(unittest.TestCase):
    def test_output_config_effort_maps(self) -> None:
        self.assertEqual(
            A._reasoning_effort({'output_config': {'effort': 'high'}}),  # noqa: SLF001
            'high',
        )

    def test_thinking_budget_maps(self) -> None:
        # 16k 预算落在 high 档（见 _reasoning_effort 的分界说明）
        self.assertEqual(
            A._reasoning_effort({'thinking': {'budget_tokens': 20000}}),  # noqa: SLF001
            'high',
        )

    def test_absent_returns_none(self) -> None:
        self.assertIsNone(A._reasoning_effort({}))  # noqa: SLF001


class RecordWiringTest(unittest.TestCase):
    """两条协议路径的 `_record` 调用都必须带 `effort=`。

    这是 issue #319 的**回归点**：函数签名早就有这个参数，只是没人传。
    源码级检查比模拟整条 HTTP 链路更直接，也不会因为上游桩变化而失效。
    """

    def test_responses_passes_effort(self) -> None:
        src = (Path(R.__file__).read_text(encoding='utf-8'))
        self.assertIn('effort = payload.get(\'reasoning_effort\')', src,
                      'Responses 没有从转换后的 payload 取档位')
        self.assertGreaterEqual(src.count('effort=effort'), 2,
                                'Responses 的非流式与流式两条路径都要传 effort')

    def test_anthropic_passes_effort(self) -> None:
        src = (Path(A.__file__).read_text(encoding='utf-8'))
        self.assertIn("effort = payload.get('reasoning_effort')", src,
                      'Anthropic 没有从转换后的 payload 取档位')
        self.assertGreaterEqual(src.count('effort=effort'), 2,
                                'Anthropic 的非流式与流式两条路径都要传 effort')

    def test_chat_path_still_passes_effort(self) -> None:
        from server.routers import gateway as G
        src = Path(G.__file__).read_text(encoding='utf-8')
        self.assertIn('effort=effort', src, 'chat 路径的档位记录不能回退')


if __name__ == '__main__':
    unittest.main()
