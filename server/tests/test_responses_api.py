"""OpenAI Responses API 兼容层（`/v1/responses`）。

判据不是「我们自己觉得事件长得对」，而是**照着客户端解析器逐条校验**
（`@earendil-works/pi-ai` 的 `api/openai-responses-shared.js`，DeepSeek Harness
走它）。该解析器的两条硬性要求直接决定实现，也直接决定下面的断言：

  1. 流必须以终止事件收尾，否则抛
     `stream ended before a terminal response event`；
  2. 文本增量与 `output_item.added` 的 `output_index` 必须一致，否则增量被
     **静默丢弃**（表现为「有回复但内容为空」）。

因此这里把解析器的关键行为复刻成一个"影子解析器"，用它来消费我们产生的事件流
——只有能被它拼出正确文本/工具调用，才算通过。纯断言「事件名在列表里」是不够的：
index 错位同样会让事件名全都"对"，而客户端什么都拿不到。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, keysvc, security  # noqa: E402
from server.routers import responses as R  # noqa: E402


# ── 客户端解析器的影子实现（只保留与本层契约有关的行为）────────────

def parse_events(chunks: list[bytes]) -> tuple[str, list[dict], dict]:
    """消费我们产生的事件流，返回 (文本, 工具调用列表, 终止状态)。

    复刻 `openai-responses-shared.js` 的槽位模型：按 `output_index` 建槽，
    delta 只喂给**已存在**的槽——这正是"index 错位则内容静默丢失"的成因。
    """
    slots: dict[int, dict] = {}
    text = ''
    terminal: dict | None = None
    for raw in chunks:
        for line in raw.decode().split('\n'):
            if not line.startswith('data:'):
                continue
            payload = json.loads(line[5:].strip())
            kind = payload.get('type')
            if kind == 'response.output_item.added':
                item = payload['item']
                if item['type'] == 'message':
                    slots[payload['output_index']] = {'type': 'text', 'buf': ''}
                elif item['type'] == 'function_call':
                    slots[payload['output_index']] = {
                        'type': 'tool', 'name': item['name'],
                        'call_id': item['call_id'], 'args': ''}
            elif kind == 'response.output_text.delta':
                slot = slots.get(payload['output_index'])
                if slot is None or slot['type'] != 'text':
                    continue          # 与客户端一致：错位就丢
                slot['buf'] += payload['delta']
            elif kind == 'response.function_call_arguments.delta':
                slot = slots.get(payload['output_index'])
                if slot is None or slot['type'] != 'tool':
                    continue
                slot['args'] += payload['delta']
            elif kind in ('response.completed', 'response.incomplete', 'response.failed'):
                terminal = payload
        if terminal is not None:
            break

    tools: list[dict] = []
    # 文本按 output_index 顺序拼接
    for index in sorted(slots):
        slot = slots[index]
        if slot['type'] == 'text':
            text += slot['buf']
        else:
            tools.append({'name': slot['name'], 'call_id': slot['call_id'],
                          'arguments': slot['args']})
    if terminal is None:
        raise AssertionError('流没有终止事件——客户端会报 '
                            'stream ended before a terminal response event')
    return text, tools, terminal


def sse(*objs: dict) -> list[bytes]:
    """构造上游风格的 SSE 帧。"""
    return [f'data: {json.dumps(o, ensure_ascii=False)}\n\n'.encode() for o in objs]


def delta(content: str | None = None, reasoning: str | None = None,
          tool: dict | None = None, finish: str | None = None,
          usage: dict | None = None) -> dict:
    d: dict = {}
    if content is not None:
        d['content'] = content
    if reasoning is not None:
        d['reasoning_content'] = reasoning
    if tool is not None:
        d['tool_calls'] = [tool]
    obj: dict = {'choices': [{'index': 0, 'delta': d}]}
    if finish:
        obj['choices'][0]['finish_reason'] = finish
    if usage:
        obj['usage'] = usage
    return obj


# ── 测试 ──────────────────────────────────────────────────────

class ToolOutputImageTest(unittest.TestCase):
    """function_call_output 里的图片：不能丢、更不能被 repr 成文本。

    与 Anthropic 层同一个坑（那边由 PR #25 修复并被采纳）：tool 消息的 content
    在 OpenAI 协议里只能是字符串，放不下结构化图片。两种错法都要避免：
      · 只取文字 → 工具输出的图片**静默丢失**，模型看不到图；
      · 把整段内容 str() → 图片 base64 被当**文本**分词，一张几 MB 的图能算出
        上百万 token，直接撑爆上下文（Anthropic 层实测 3.36MB ≈ 234 万 token）。
    正确做法：文字留在 tool 消息，图片提升为紧随其后的 user 消息。
    """

    def test_image_only_output_is_lifted_not_stringified(self) -> None:
        big = 'A' * 3_360_000
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'function_call', 'call_id': 'c1', 'name': 'read', 'arguments': '{}'},
            {'type': 'function_call_output', 'call_id': 'c1', 'output': [
                {'type': 'input_image', 'image_url': 'data:image/png;base64,' + big}]},
        ]})
        tool_msg = next(m for m in out['messages'] if m['role'] == 'tool')
        self.assertIsInstance(tool_msg['content'], str)
        # 关键断言：base64 **不能**出现在文本里
        self.assertNotIn('AAAA', tool_msg['content'])
        self.assertLess(len(tool_msg['content']), 50,
                        f'图片被序列化进文本了：长度 {len(tool_msg["content"])}')
        img_msg = next(m for m in out['messages'] if m['role'] == 'user')
        self.assertEqual([b['type'] for b in img_msg['content']], ['image_url'])

    def test_text_and_image_keeps_both(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'function_call_output', 'call_id': 'c1', 'output': [
                {'type': 'input_text', 'text': '截图如下'},
                {'type': 'input_image', 'image_url': 'data:image/png;base64,QUJD'}]},
        ]})
        tool_msg = next(m for m in out['messages'] if m['role'] == 'tool')
        self.assertEqual(tool_msg['content'], '截图如下')
        img_msg = next(m for m in out['messages'] if m['role'] == 'user')
        self.assertEqual(img_msg['content'][0]['type'], 'image_url', '图片被丢掉了')

    def test_plain_text_output_unchanged(self) -> None:
        """纯文本工具结果的行为必须与改动前一致（不凭空多出 user 消息）。"""
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'function_call_output', 'call_id': 'c1', 'output': 'plain result'},
        ]})
        self.assertEqual([m['role'] for m in out['messages']], ['tool'])
        self.assertEqual(out['messages'][0]['content'], 'plain result')

    def test_string_image_url_accepted(self) -> None:
        """image_url 允许是字符串或 {url:...}；两种都要提到 user 消息里。"""
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'function_call_output', 'call_id': 'c1', 'output': [
                {'type': 'input_image', 'image_url': 'https://x/y.png'}]},
        ]})
        img_msg = next(m for m in out['messages'] if m['role'] == 'user')
        self.assertEqual(img_msg['content'][0]['image_url']['url'], 'https://x/y.png')

    def test_image_follows_its_own_tool_message(self) -> None:
        """图片必须紧跟它那次工具调用的 tool 消息（顺序即归属）。"""
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'function_call_output', 'call_id': 'c1', 'output': [
                {'type': 'input_image', 'image_url': 'https://x/a.png'}]},
            {'type': 'function_call_output', 'call_id': 'c2', 'output': 'done'},
        ]})
        roles = [m['role'] for m in out['messages']]
        self.assertEqual(roles, ['tool', 'user', 'tool'])


class RequestConversionTest(unittest.TestCase):
    """Responses 请求 → Chat Completions 请求。"""

    def test_instructions_becomes_system_message(self) -> None:
        out = R.to_chat_request({
            'model': 'gpt-5.6-sol',
            'instructions': 'be terse',
            'input': 'hi',
        })
        self.assertEqual(out['messages'][0], {'role': 'system', 'content': 'be terse'})
        self.assertEqual(out['messages'][1], {'role': 'user', 'content': 'hi'})

    def test_string_input(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': 'hello'})
        self.assertEqual(out['messages'], [{'role': 'user', 'content': 'hello'}])

    def test_message_items_with_roles(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'message', 'role': 'user',
             'content': [{'type': 'input_text', 'text': 'q'}]},
        ]})
        self.assertEqual(out['messages'], [{'role': 'user', 'content': 'q'}])

    def test_developer_role_maps_to_system(self) -> None:
        """Chat Completions 没有 developer 角色，原样传会被上游拒。"""
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'message', 'role': 'developer',
             'content': [{'type': 'input_text', 'text': 'sys'}]},
        ]})
        self.assertEqual(out['messages'][0]['role'], 'system')

    def test_consecutive_function_calls_share_one_assistant_message(self) -> None:
        """多个工具调用必须合并进**同一条** assistant 消息，否则上游 400。"""
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'function_call', 'call_id': 'c1', 'name': 'a', 'arguments': '{}'},
            {'type': 'function_call', 'call_id': 'c2', 'name': 'b', 'arguments': '{}'},
        ]})
        self.assertEqual(len(out['messages']), 1)
        self.assertEqual(out['messages'][0]['role'], 'assistant')
        self.assertEqual(len(out['messages'][0]['tool_calls']), 2)

    def test_function_call_output_becomes_tool_message(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'function_call', 'call_id': 'c1', 'name': 'a', 'arguments': '{}'},
            {'type': 'function_call_output', 'call_id': 'c1', 'output': 'result'},
        ]})
        self.assertEqual(out['messages'][0]['role'], 'assistant')
        self.assertEqual(out['messages'][1],
                         {'role': 'tool', 'tool_call_id': 'c1', 'content': 'result'})

    def test_reasoning_trace_kept_on_assistant_message(self) -> None:
        """reasoning item 的推理文本必须**带回** assistant 消息（社区反馈的 11155 死循环）。

        曾经的错误做法是直接丢掉 reasoning item，理由是「OpenAI 专有形状，透给
        Chat Completions 会被拒」。那个理由只对**形状**成立、对**内容**不成立：
        腾讯要求 DeepSeek 多轮回传推理内容，上游 workbuddy2api 见到 assistant 上的
        reasoning 痕迹就会给所有 assistant 补 `reasoning_content` —— 痕迹被我们丢掉，
        那道补丁就永远不触发，于是 400 → 账号连败 → 降级冷却 → 池空 → 503 死循环。

        正确做法：把推理文本挂到紧随其后的 assistant 消息上，字段名用上游认识的
        `reasoning_content`（扁平字符串，两侧都认）。
        """
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'q1'}]},
            {'type': 'reasoning', 'id': 'rs_1',
             'summary': [{'type': 'summary_text', 'text': '我先想一想'}]},
            {'type': 'message', 'role': 'assistant',
             'content': [{'type': 'output_text', 'text': 'a1'}]},
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'q2'}]},
        ]})
        # 两个字段名都要有：`reasoning` 是上游**请求侧**校验读的字段，
        # `reasoning_content` 是响应侧命名（上游兜底逻辑按它判断有无痕迹）。
        # 只写后者等于没写（issue #37 的 8 组对照实验）。
        self.assertEqual(out['messages'], [
            {'role': 'user', 'content': 'q1'},
            {'role': 'assistant', 'content': 'a1',
             'reasoning': '我先想一想', 'reasoning_content': '我先想一想'},
            {'role': 'user', 'content': 'q2'},
        ])

    def test_reasoning_without_text_does_not_set_field(self) -> None:
        """推理项里**没有可用文本**时不挂字段（而不是挂空串）。

        这是判断修正。早先这里挂空串，理由是「上游按字段存在判断有无痕迹」。
        但社区实测（issue #37 的取值矩阵）表明 `reasoning` **为空串会被拒**、
        非空才过 —— 若成立，挂空串把「没痕迹」变成「有痕迹但内容为空」，更糟；
        若不成立（本仓复现不出那个开关），挂空串与不挂又没差别（上游兜底本就会
        补空串）。两种情况都指向：**挂空串是无收益的风险**，故不挂。

        注意区分「没有文本」与「没有这个项」：后者本来就不挂任何字段，
        本测试覆盖的是前者（客户端发了空的推理项 —— 畸形输入）。
        """
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'reasoning', 'id': 'rs_2', 'summary': []},
            {'type': 'message', 'role': 'assistant',
             'content': [{'type': 'output_text', 'text': 'a'}]},
        ]})
        msg = out['messages'][0]
        self.assertNotIn('reasoning', msg, '空推理不该挂 reasoning（会被上游拒）')
        self.assertNotIn('reasoning_content', msg)

    def test_reasoning_attached_to_tool_call_message(self) -> None:
        """工具调用回合的推理同样要保留（模型先思考再调工具）。"""
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'reasoning', 'id': 'rs_3',
             'summary': [{'type': 'summary_text', 'text': '需要查工具'}]},
            {'type': 'function_call', 'call_id': 'c1', 'name': 'f', 'arguments': '{}'},
            {'type': 'function_call_output', 'call_id': 'c1', 'output': 'ok'},
        ]})
        asst = out['messages'][0]
        self.assertEqual(asst['role'], 'assistant')
        self.assertEqual(asst['reasoning_content'], '需要查工具')

    def test_no_reasoning_item_means_no_field(self) -> None:
        """普通对话**不能**凭空多出 reasoning_content —— 那会改变发给上游的形状。"""
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'q'}]},
            {'type': 'message', 'role': 'assistant',
             'content': [{'type': 'output_text', 'text': 'a'}]},
        ]})
        for m in out['messages']:
            self.assertNotIn('reasoning_content', m)

    def test_reasoning_not_immediately_before_assistant_is_kept(self) -> None:
        """推理项后面跟的不是 assistant 时，**保留**给后面那条 assistant。

        这条断言原来写的是反过来的——「孤立的 reasoning 宁可丢掉，也不能挂到
        不相关的回合上」。那个取舍是错的，代价证实很高：

        腾讯要求 assistant 消息上**必须有** `reasoning_content`（缺了就 400
        `11155 reasoning_content_missing`，issue #36 报的正是它）。把这段推理
        丢掉，客户端辛苦带回来的凭据就白带了；而挂上去最多是多一段无害的文本
        （腾讯只校验字段存在，不校验这段推理是否属于该回合）。

        两害相权：**丢掉的代价是被拒（且连带账号被判失败），多挂的代价是多余文本。**
        """
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'reasoning', 'id': 'rs_x',
             'summary': [{'type': 'summary_text', 'text': '推理'}]},
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'q'}]},
            {'type': 'message', 'role': 'assistant',
             'content': [{'type': 'output_text', 'text': 'a'}]},
        ]})
        assistant = [m for m in out['messages'] if m['role'] == 'assistant']
        self.assertEqual(len(assistant), 1)
        self.assertEqual(assistant[0].get('reasoning_content'), '推理',
                         '推理被丢掉了 —— 上游会因缺字段报 11155')

    def test_reasoning_never_attached_to_user_messages(self) -> None:
        """但绝不能挂到 user 消息上：腾讯的校验只针对 assistant 回合。"""
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'reasoning', 'id': 'rs_x',
             'summary': [{'type': 'summary_text', 'text': '推理'}]},
            {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'q'}]},
        ]})
        for m in out['messages']:
            if m['role'] != 'assistant':
                self.assertNotIn('reasoning_content', m,
                                 f'{m["role"]} 消息上不该有 reasoning_content')

    def test_reasoning_flat_string_forms(self) -> None:
        """客户端把推理写成扁平字符串时同样认（各客户端形状不一）。"""
        for key in ('reasoning_content', 'reasoning'):
            with self.subTest(key=key):
                out = R.to_chat_request({'model': 'm', 'input': [
                    {'type': 'reasoning', key: '想法'},
                    {'type': 'message', 'role': 'assistant',
                     'content': [{'type': 'output_text', 'text': 'a'}]},
                ]})
                self.assertEqual(out['messages'][0]['reasoning_content'], '想法')

    def test_image_keeps_block_structure(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'message', 'role': 'user', 'content': [
                {'type': 'input_text', 'text': 'what is this'},
                {'type': 'input_image', 'image_url': 'https://x/y.png'},
            ]},
        ]})
        blocks = out['messages'][0]['content']
        self.assertIsInstance(blocks, list)
        self.assertEqual(blocks[1]['type'], 'image_url')

    def test_max_output_tokens_maps_to_max_tokens(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': 'x', 'max_output_tokens': 99})
        self.assertEqual(out['max_tokens'], 99)

    def test_flat_tools_normalized_to_nested(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': 'x', 'tools': [
            {'type': 'function', 'name': 'get', 'description': 'd',
             'parameters': {'type': 'object', 'properties': {}}},
        ]})
        self.assertEqual(out['tools'][0]['function']['name'], 'get')

    def test_nested_tools_also_accepted(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': 'x', 'tools': [
            {'type': 'function', 'function': {'name': 'get', 'parameters': {}}},
        ]})
        self.assertEqual(out['tools'][0]['function']['name'], 'get')

    def test_openai_only_fields_not_passed_upstream(self) -> None:
        """store/include/prompt_cache_key/reasoning 是 OpenAI 专有，透过去就 400。

        `stream` **不在此列**：它必须转告上游（上游据此决定回 SSE 还是 JSON）。
        这条断言是 E2E 试出来的——早先把 stream 一并"过滤"掉，流式请求会静默
        变成空回答。
        """
        out = R.to_chat_request({
            'model': 'm', 'input': 'x', 'store': True,
            'include': ['reasoning.encrypted_content'],
            'prompt_cache_key': 'k', 'prompt_cache_retention': '24h',
            'reasoning': {'effort': 'high'},
        })
        for leaked in ('store', 'include', 'prompt_cache_key',
                       'prompt_cache_retention', 'reasoning'):
            self.assertNotIn(leaked, out, f'{leaked} 不该透给上游')

    def test_stream_flag_forwarded_to_upstream(self) -> None:
        """`stream` 必须转告上游：漏掉它，上游回 JSON，网关按 SSE 解析 →
        一个 data 帧都解不出来 → 客户端收到"成功但空"的回答。"""
        self.assertTrue(R.to_chat_request({'model': 'm', 'input': 'x', 'stream': True})['stream'])
        self.assertFalse(R.to_chat_request({'model': 'm', 'input': 'x'})['stream'])

    def test_tool_choice_by_name(self) -> None:
        out = R.to_chat_request({'model': 'm', 'input': 'x',
                                 'tool_choice': {'type': 'function', 'name': 'get'}})
        self.assertEqual(out['tool_choice'],
                         {'type': 'function', 'function': {'name': 'get'}})

    def test_custom_tool_is_wrapped_and_history_roundtrips(self) -> None:
        patch = '*** Begin Patch\n*** Add File: probe.txt\n+ok\n*** End Patch'
        body = {
            'model': 'm',
            'reasoning': {'effort': 'max', 'summary': 'ignored'},
            'tools': [{
                'type': 'custom',
                'name': 'apply_patch',
                'description': 'Apply a patch',
                'format': {'type': 'grammar', 'definition': 'start: /[\\\\s\\\\S]+/'},
            }],
            'input': [
                {'type': 'custom_tool_call', 'call_id': 'c1',
                 'name': 'apply_patch', 'input': patch},
                {'type': 'custom_tool_call_output', 'call_id': 'c1', 'output': 'ok'},
            ],
            'tool_choice': {'type': 'custom', 'name': 'apply_patch'},
        }
        out = R.to_chat_request(body)
        self.assertEqual(out['reasoning_effort'], 'max')
        self.assertEqual(out['tools'][0]['function']['parameters']['required'], ['input'])
        self.assertIn('Requested grammar', out['tools'][0]['function']['description'])
        self.assertEqual(out['messages'][0]['tool_calls'][0]['function']['arguments'],
                         json.dumps({'input': patch}, ensure_ascii=False))
        self.assertEqual(out['messages'][1]['role'], 'tool')
        self.assertEqual(out['tool_choice'],
                         {'type': 'function', 'function': {'name': 'apply_patch'}})

    def test_custom_tool_history_requires_string_input(self) -> None:
        with self.assertRaises(R.CustomToolArgumentsError):
            R.to_chat_request({
                'model': 'm',
                'input': [{'type': 'custom_tool_call', 'name': 'apply_patch',
                           'input': {'not': 'raw text'}}],
            })


class NonStreamingResponseTest(unittest.TestCase):
    def test_text_and_usage(self) -> None:
        obj = R.to_responses_object({
            'choices': [{'message': {'role': 'assistant', 'content': 'hey'},
                         'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 3},
        }, 'm', 'resp_1')
        self.assertEqual(obj['object'], 'response')
        self.assertEqual(obj['status'], 'completed')
        self.assertEqual(obj['output'][0]['type'], 'message')
        self.assertEqual(obj['output'][0]['content'][0]['text'], 'hey')
        self.assertEqual(obj['usage']['input_tokens'], 10)
        self.assertEqual(obj['usage']['output_tokens'], 3)
        self.assertEqual(obj['usage']['total_tokens'], 13)

    def test_cached_tokens_are_not_subtracted_from_input(self) -> None:
        """OpenAI 口径里 input_tokens 含缓存命中；客户端自己会减。我们先减就重复了。"""
        obj = R.to_responses_object({
            'choices': [{'message': {'content': 'x'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 100, 'completion_tokens': 1,
                      'prompt_tokens_details': {'cached_tokens': 80}},
        }, 'm', 'resp_1')
        self.assertEqual(obj['usage']['input_tokens'], 100)
        self.assertEqual(obj['usage']['input_tokens_details']['cached_tokens'], 80)

    def test_length_finish_becomes_incomplete(self) -> None:
        obj = R.to_responses_object({
            'choices': [{'message': {'content': 'x'}, 'finish_reason': 'length'}],
        }, 'm', 'resp_1')
        self.assertEqual(obj['status'], 'incomplete')
        self.assertEqual(obj['incomplete_details'], {'reason': 'max_output_tokens'})

    def test_tool_call_becomes_function_call_item(self) -> None:
        obj = R.to_responses_object({
            'choices': [{'message': {'content': None, 'tool_calls': [
                {'id': 'call_1', 'type': 'function',
                 'function': {'name': 'get', 'arguments': '{"a":1}'}},
            ]}, 'finish_reason': 'tool_calls'}],
        }, 'm', 'resp_1')
        item = obj['output'][0]
        self.assertEqual(item['type'], 'function_call')
        self.assertEqual(item['call_id'], 'call_1')
        self.assertEqual(item['name'], 'get')
        self.assertEqual(item['arguments'], '{"a":1}')
        self.assertTrue(item['id'].startswith('fc_'))

    def test_custom_tool_call_is_restored(self) -> None:
        patch = '*** Begin Patch\n*** Add File: probe.txt\n+ok\n*** End Patch'
        obj = R.to_responses_object({
            'choices': [{
                'message': {
                    'content': None,
                    'tool_calls': [{
                        'id': 'call_1',
                        'type': 'function',
                        'function': {
                            'name': 'apply_patch',
                            'arguments': json.dumps({'input': patch}),
                        },
                    }],
                },
                'finish_reason': 'tool_calls',
            }],
        }, 'm', 'resp_1', {'apply_patch'})
        item = obj['output'][0]
        self.assertEqual(item['type'], 'custom_tool_call')
        self.assertEqual(item['input'], patch)
        self.assertNotIn('arguments', item)


class StreamContractTest(unittest.TestCase):
    """流式：用影子解析器校验客户端能否拼出正确结果。"""

    def test_text_stream_roundtrip(self) -> None:
        t = R._StreamTranslator('m', 'resp_1')
        chunks: list[bytes] = []
        for piece in ('He', 'llo', ' world'):
            chunks += t.feed(delta(content=piece))
        chunks += t.feed(delta(finish='stop',
                               usage={'prompt_tokens': 5, 'completion_tokens': 3}))
        text, tools, terminal = parse_events(chunks)
        self.assertEqual(text, 'Hello world', '文本没被客户端拼出来——index 可能错位')
        self.assertEqual(tools, [])
        self.assertEqual(terminal['response']['status'], 'completed')
        self.assertEqual(terminal['response']['usage']['output_tokens'], 3)

    def test_stream_starts_with_response_created(self) -> None:
        t = R._StreamTranslator('m', 'resp_1')
        first = t.feed(delta(content='x'))
        self.assertIn(b'response.created', first[0])

    def test_terminal_event_always_emitted(self) -> None:
        """没有终止事件客户端会直接抛错（不是"空回答"）。"""
        for chunk_seq in (
            [delta(content='hi')],
            [],                                   # 上游一个字都没给
            [delta(reasoning='think')],           # 只有推理
        ):
            with self.subTest(chunk_seq=len(chunk_seq)):
                t = R._StreamTranslator('m', 'resp_x')
                chunks: list[bytes] = []
                for obj in chunk_seq:
                    chunks += t.feed(obj)
                chunks += t.finish(None, force=True)
                _text, _tools, terminal = parse_events(chunks)
                self.assertIn(terminal['response']['status'], ('completed', 'incomplete'))

    def test_tool_call_arguments_roundtrip(self) -> None:
        t = R._StreamTranslator('m', 'resp_1')
        chunks: list[bytes] = []
        chunks += t.feed(delta(tool={'index': 0, 'id': 'call_1',
                                     'function': {'name': 'get', 'arguments': '{"a"'}}))
        chunks += t.feed(delta(tool={'index': 0,
                                     'function': {'arguments': ':1}'}}))
        chunks += t.feed(delta(finish='tool_calls'))
        _text, tools, _terminal = parse_events(chunks)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]['name'], 'get')
        self.assertEqual(tools[0]['arguments'], '{"a":1}', '参数分片没被拼回')

    def test_custom_tool_stream_uses_raw_input_events(self) -> None:
        t = R._StreamTranslator('m', 'resp_1', {'apply_patch'})
        chunks: list[bytes] = []
        chunks += t.feed(delta(tool={
            'index': 0,
            'id': 'call_1',
            'function': {
                'name': 'apply_',
                'arguments': '{"input":"patch"}',
            },
        }))
        chunks += t.feed(delta(tool={
            'index': 0,
            'function': {'name': 'patch'},
        }))
        chunks += t.feed(delta(finish='tool_calls'))
        events = [
            json.loads(line[5:].strip())
            for chunk in chunks
            for line in chunk.decode().splitlines()
            if line.startswith('data:')
        ]
        custom = [event for event in events
                  if event['type'] == 'response.custom_tool_call_input.delta']
        done = [event for event in events
                if event['type'] == 'response.custom_tool_call_input.done']
        output = [event for event in events
                  if event['type'] == 'response.output_item.done'][-1]['item']
        self.assertEqual([event['delta'] for event in custom], ['patch'])
        self.assertEqual(done[0]['input'], 'patch')
        self.assertEqual(output['type'], 'custom_tool_call')
        self.assertEqual(output['input'], 'patch')

    def test_malformed_custom_stream_fails_without_executable_call(self) -> None:
        t = R._StreamTranslator('m', 'resp_1', {'apply_patch'})
        chunks = t.feed(delta(tool={
            'index': 0,
            'id': 'call_1',
            'function': {
                'name': 'apply_patch',
                'arguments': '{"input": 1}',
            },
        }))
        chunks += t.feed(delta(finish='tool_calls'))
        events = [
            json.loads(line[5:].strip())
            for chunk in chunks
            for line in chunk.decode().splitlines()
            if line.startswith('data:')
        ]
        self.assertEqual(events[-1]['type'], 'response.failed')
        self.assertFalse(any(event['type'] == 'response.output_item.done'
                             and event.get('item', {}).get('type') == 'custom_tool_call'
                             for event in events))

    def test_parallel_tool_calls_keep_separate_slots(self) -> None:
        """并发工具各自编号：混进一个槽位会让两个调用互相污染参数。"""
        t = R._StreamTranslator('m', 'resp_1')
        chunks: list[bytes] = []
        chunks += t.feed(delta(tool={'index': 0, 'id': 'c1',
                                     'function': {'name': 'a', 'arguments': '{"x":1}'}}))
        chunks += t.feed(delta(tool={'index': 1, 'id': 'c2',
                                     'function': {'name': 'b', 'arguments': '{"y":2}'}}))
        chunks += t.feed(delta(finish='tool_calls'))
        _text, tools, _terminal = parse_events(chunks)
        self.assertEqual([x['name'] for x in tools], ['a', 'b'])
        self.assertEqual([x['arguments'] for x in tools], ['{"x":1}', '{"y":2}'])

    def test_reasoning_becomes_separate_item(self) -> None:
        t = R._StreamTranslator('m', 'resp_1')
        chunks: list[bytes] = []
        chunks += t.feed(delta(reasoning='let me think'))
        chunks += t.feed(delta(content='answer'))
        chunks += t.feed(delta(finish='stop'))
        text, _tools, terminal = parse_events(chunks)
        self.assertEqual(text, 'answer')
        kinds = [item['type'] for item in terminal['response']['output']]
        self.assertIn('reasoning', kinds)
        self.assertIn('message', kinds)
        # 推理与正文必须是两个 item，否则客户端思考区与正文区错位
        self.assertEqual(kinds.count('reasoning'), 1)
        self.assertEqual(kinds.count('message'), 1)

    def test_events_after_finish_are_ignored(self) -> None:
        """收尾后再吐事件会污染协议（客户端已按完成处理）。"""
        t = R._StreamTranslator('m', 'resp_1')
        t.feed(delta(content='a'))
        t.feed(delta(finish='stop'))
        self.assertEqual(t.feed(delta(content='late')), [])

    def test_finish_is_idempotent(self) -> None:
        t = R._StreamTranslator('m', 'resp_1')
        t.feed(delta(content='a', finish='stop'))
        self.assertEqual(t.finish(None, force=True), [])

    def test_force_finish_closes_dangling_tool_call(self) -> None:
        """上游给了工具参数却没给 finish_reason：仍要发 arguments.done，
        否则客户端参数累积停在半截、拿半截 JSON 去执行。"""
        t = R._StreamTranslator('m', 'resp_1')
        chunks = t.feed(delta(tool={'index': 0, 'id': 'c1',
                                    'function': {'name': 'a', 'arguments': '{"x":1}'}}))
        chunks += t.finish(None, force=True)
        _text, tools, terminal = parse_events(chunks)
        self.assertEqual(tools[0]['arguments'], '{"x":1}')
        self.assertEqual(terminal['response']['status'], 'completed')

    def test_created_event_declares_requested_model(self) -> None:
        """回填请求的模型名（不是映射后的上游名），否则客户端以为模型被换掉。"""
        t = R._StreamTranslator('global:gpt-5.6-sol', 'resp_1')
        chunks = t.feed(delta(content='x', finish='stop'))
        text, _tools, terminal = parse_events(chunks)
        self.assertEqual(terminal['response']['model'], 'global:gpt-5.6-sol')
        created = json.loads(
            next(c for c in chunks if b'response.created' in c).decode().split('data:')[1])
        self.assertEqual(created['response']['model'], 'global:gpt-5.6-sol')

    def test_every_event_carries_event_line(self) -> None:
        """`event:` 行不能省：解析器按它分派，只给 data 会被整条跳过。"""
        t = R._StreamTranslator('m', 'resp_1')
        chunks: list[bytes] = []
        chunks += t.feed(delta(reasoning='r'))
        chunks += t.feed(delta(content='c'))
        chunks += t.feed(delta(tool={'index': 0, 'id': 'c1',
                                     'function': {'name': 'f', 'arguments': '{}'}}))
        chunks += t.feed(delta(finish='stop'))
        for chunk in chunks:
            head = chunk.decode().split('\n', 1)[0]
            self.assertTrue(head.startswith('event: '), f'缺少 event: 行: {chunk!r}')


class EndpointTest(unittest.TestCase):
    """真实 HTTP：鉴权、错误状态码、双路径注册。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 'r.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        db._conn = None
        db.connect()
        security.save_users({
            'secret': 'S',
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('p')}],
            'api_keys': [],
        })
        from server.main import app
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _post(self, path: str, payload: dict, token: str | None = None):
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        with mock.patch.object(config, 'WB2API_BASE', 'http://127.0.0.1:1'):
            return self.client.post(path, headers=headers, json=payload)

    def test_both_paths_registered(self) -> None:
        """/v1/responses 与 /responses 都要在：SDK 的 baseURL 有无 /v1 两种都常见。"""
        token = keysvc.create_key('t')['key']
        for path in ('/v1/responses', '/responses'):
            with self.subTest(path):
                r = self._post(path, {'model': 'glm-5.2', 'input': 'hi'}, token)
                # 鉴权通过 → 上游连不上 → 502
                self.assertEqual(r.status_code, 502, r.text)

    def test_requires_api_key(self) -> None:
        r = self._post('/v1/responses', {'model': 'glm-5.2', 'input': 'hi'})
        self.assertEqual(r.status_code, 401, r.text)

    def test_empty_input_rejected(self) -> None:
        token = keysvc.create_key('t')['key']
        r = self._post('/v1/responses', {'model': 'glm-5.2'}, token)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn('input', r.text)

    def test_realm_isolation_applies(self) -> None:
        """Responses 路径必须与 Chat Completions 同结论（同一把密钥、同一版本规则）。"""
        token = keysvc.create_key('g', realm='global')['key']
        r = self._post('/v1/responses', {'model': 'glm-5.2', 'input': 'hi'}, token)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn('国际版', r.text)
        # 且真实原因不会被折叠成「API 密钥无效」
        self.assertNotIn(r.status_code, (401, 403))

    def test_missing_model_rejected(self) -> None:
        token = keysvc.create_key('t')['key']
        r = self._post('/v1/responses', {'input': 'hi'}, token)
        self.assertEqual(r.status_code, 400, r.text)

    def test_upstream_error_keeps_status_code(self) -> None:
        """上游报错要**在开流之前**用真实状态码回掉：一旦以 200 开流，
        客户端会把错误事件当作"成功但空回答"。"""
        token = keysvc.create_key('t')['key']

        class _Resp:
            status_code = 503
            text = '{"error":{"message":"no healthy account"}}'

            async def aread(self):
                return self.text.encode()

            async def aiter_bytes(self):
                yield self.text.encode()

            async def aclose(self):
                return None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _Client:
            def build_request(self, *a, **kw):
                return object()

            async def send(self, req, stream=True):
                return _Resp()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, **kw):
                return _Resp()

            async def aclose(self):
                return None

        with mock.patch.object(config, 'http_client', lambda *a, **k: _Client()):
            r = self.client.post('/v1/responses',
                                 headers={'Authorization': f'Bearer {token}'},
                                 json={'model': 'glm-5.2', 'input': 'hi', 'stream': True})
        self.assertEqual(r.status_code, 503, r.text)
        self.assertIn('no healthy account', r.text)
        self.assertNotIn('text/event-stream', r.headers.get('content-type', ''))

    def test_successful_stream_delivers_text(self) -> None:
        """整条链路（含上游 SSE 解析）走通，客户端能拼出文本。"""
        token = keysvc.create_key('t')['key']
        upstream = b''.join(sse(
            delta(content='Hello'),
            delta(content=' there'),
            delta(finish='stop', usage={'prompt_tokens': 4, 'completion_tokens': 2}),
        ))

        class _Resp:
            status_code = 200
            headers = {'content-type': 'text/event-stream'}

            async def aiter_bytes(self):
                yield upstream

            async def aclose(self):
                return None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _Client:
            def build_request(self, *a, **kw):
                return object()

            async def send(self, req, stream=True):
                return _Resp()

            async def aclose(self):
                return None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        with mock.patch.object(config, 'http_client', lambda *a, **k: _Client()):
            r = self.client.post('/v1/responses',
                                 headers={'Authorization': f'Bearer {token}'},
                                 json={'model': 'glm-5.2', 'input': 'hi', 'stream': True})
        self.assertEqual(r.status_code, 200, r.text)
        text, _tools, terminal = parse_events([r.content])
        self.assertEqual(text, 'Hello there')
        self.assertEqual(terminal['response']['status'], 'completed')
        self.assertEqual(terminal['response']['usage']['total_tokens'], 6)

    def test_upstream_midstream_error_becomes_response_failed(self) -> None:
        """中途出错要以 response.failed 收尾——该事件会让客户端抛错，
        而"静默的空回答"不会触发任何重试。"""
        token = keysvc.create_key('t')['key']
        upstream = b''.join(sse(delta(content='partial'))) + \
            b'data: {"error":{"message":"boom"}}\n\n'

        class _Resp:
            status_code = 200

            async def aiter_bytes(self):
                yield upstream

            async def aclose(self):
                return None

        class _Client:
            def build_request(self, *a, **kw):
                return object()

            async def send(self, req, stream=True):
                return _Resp()

            async def aclose(self):
                return None

        with mock.patch.object(config, 'http_client', lambda *a, **k: _Client()):
            r = self.client.post('/v1/responses',
                                 headers={'Authorization': f'Bearer {token}'},
                                 json={'model': 'glm-5.2', 'input': 'hi', 'stream': True})
        self.assertEqual(r.status_code, 200)
        self.assertIn('response.failed', r.content.decode())
        self.assertIn('boom', r.content.decode())


if __name__ == '__main__':
    unittest.main()
