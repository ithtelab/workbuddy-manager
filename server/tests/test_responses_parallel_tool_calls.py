"""`parallel_tool_calls` 的透传与回显。

`parallel_tool_calls` 在 Responses 与 Chat Completions 里是**同名同语义**的顶层
字段，但两边的缺省不同：

  - Responses 的缺省是 `true`（OpenAI 文档），所以回显时不给就按 true；
  - 上游 Chat 侧把「没写这个键」当成它自己的默认，因此**客户端没说就不要替它写**。

要守住的就是这两条边界：

  1. 客户端显式 `false` 时必须原样转告上游——否则上游照旧并发多个工具调用，
     按「只该有一个」解析的客户端会把多出来的调用丢掉或直接报参数错；
  2. 客户端没给（或给的不是布尔值）时**不许**补一个值上去：补 `true` 会悄悄改掉
     上游默认行为，补 `false` 更糟；
  3. 响应体回显的应当就是客户端给的那个值，未给时才是历史默认 true。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import responses as R  # noqa: E402


def _chat_doc(**extra) -> dict:
    """最小可翻译的 Chat Completions 响应。"""
    doc = {
        'id': 'chatcmpl-x',
        'choices': [{
            'message': {'role': 'assistant', 'content': 'ok'},
            'finish_reason': 'stop',
        }],
        'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
    }
    doc.update(extra)
    return doc


class ToChatRequestTest(unittest.TestCase):
    """请求方向：Responses 请求体 → 上游 Chat 请求体。"""

    def test_false_is_forwarded(self) -> None:
        out = R.to_chat_request({
            'model': 'm', 'input': 'hi', 'parallel_tool_calls': False,
        })
        self.assertIs(out['parallel_tool_calls'], False)

    def test_true_is_forwarded(self) -> None:
        out = R.to_chat_request({
            'model': 'm', 'input': 'hi', 'parallel_tool_calls': True,
        })
        self.assertIs(out['parallel_tool_calls'], True)

    def test_absent_key_is_not_invented(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': 'hi'})
        self.assertNotIn(
            'parallel_tool_calls', out,
            '客户端没给就不能替它写：写了会改掉上游自己的默认行为')

    def test_non_bool_is_ignored(self) -> None:
        # 客户端把字段写成字符串（`"false"`）不是合法布尔值，按「没给」处理，
        # 而不是猜它的意思。
        for raw in ('false', 'true', 1, 0, None):
            with self.subTest(raw=raw):
                out = R.to_chat_request({
                    'model': 'm', 'input': 'hi', 'parallel_tool_calls': raw,
                })
                self.assertNotIn('parallel_tool_calls', out)

    def test_other_fields_unaffected(self) -> None:
        """带上该字段不影响其它字段的转换（回归守卫）。"""
        body = {
            'model': 'm',
            'input': 'hi',
            'parallel_tool_calls': False,
            'max_output_tokens': 128,
            'reasoning': {'effort': 'high'},
            'tools': [{
                'type': 'function',
                'name': 'read_file',
                'parameters': {'type': 'object', 'properties': {}},
            }],
        }
        out = R.to_chat_request(body)
        self.assertEqual(out['max_tokens'], 128)
        self.assertEqual(out['reasoning_effort'], 'high')
        self.assertEqual(out['tools'][0]['function']['name'], 'read_file')
        self.assertIs(out['parallel_tool_calls'], False)


class ToResponsesObjectTest(unittest.TestCase):
    """响应方向：回显客户端给的值，未给时才是 true。"""

    def _build(self, **kwargs) -> dict:
        return R.to_responses_object(
            _chat_doc(), 'm', 'resp_1', set(), R._ToolBridge([]), **kwargs)

    def test_echo_false(self) -> None:
        self.assertIs(self._build(parallel_tool_calls=False)['parallel_tool_calls'], False)

    def test_echo_true(self) -> None:
        self.assertIs(self._build(parallel_tool_calls=True)['parallel_tool_calls'], True)

    def test_default_true(self) -> None:
        self.assertIs(self._build()['parallel_tool_calls'], True)

    def test_non_bool_falls_back_to_true(self) -> None:
        self.assertIs(self._build(parallel_tool_calls='false')['parallel_tool_calls'], True)


if __name__ == '__main__':
    unittest.main()
