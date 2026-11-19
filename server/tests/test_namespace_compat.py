"""Responses `type=namespace` / `type=custom` 工具的桥接（PR #42）。

Responses 侧有而 Chat Completions 侧没有的两种工具形态：

  · `type=custom`（自由文本工具，Codex 的 `apply_patch` 就是）：Responses 用
    `custom_tool_call` + 原始字符串 `input`，Chat 侧没有对应形态——桥接时临时
    包成严格的 `{input: string}` 函数定义，回来时再还原成 `custom_tool_call`。
    不这么做，客户端会报 `tool apply_patch invoked with incompatible payload`。
  · `type=namespace`（带子工具的分组）：Chat 侧没有分组概念，子工具要递归展开
    成平铺的 `function`，重名时改名，**回程要还原成客户端原名**。

后者是这里最容易出错的地方：客户端下一轮按**自己声明的名字**回传工具结果，
一旦回程没还原（发出去的是内部改名），客户端就匹配不上。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import responses as R  # noqa: E402


def ns_tools():
    return [
        {'type': 'function', 'name': 'exec_command',
         'parameters': {'type': 'object', 'properties': {'cmd': {'type': 'string'}}}},
        {'type': 'namespace', 'name': 'shell', 'tools': [
            {'type': 'custom', 'name': 'apply_patch'},
            {'type': 'function', 'name': 'read_file',
             'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}}},
        ]},
        {'type': 'namespace', 'name': 'nested', 'tools': [
            {'type': 'namespace', 'name': 'inner', 'tools': [
                {'type': 'function', 'name': 'stat',
                 'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}}},
            ]},
        ]},
        {'type': 'function', 'name': 'read_file',
         'parameters': {'type': 'object', 'properties': {'path': {'type': 'string'}}}},
    ]


class NamespaceCompatTests(unittest.TestCase):
    def test_children_are_flattened_and_conflicts_renamed(self):
        bridge = R._ToolBridge(ns_tools())
        names = [t['function']['name'] for t in bridge.chat_tools]
        self.assertEqual(names, ['exec_command', 'apply_patch', 'read_file', 'stat', 'read_file_2'])
        self.assertIn('apply_patch', bridge.custom_names)
        self.assertEqual(bridge.outbound_name('apply_patch'), 'apply_patch')
        self.assertEqual(bridge.outbound_name('read_file'), 'read_file')
        self.assertEqual(bridge.restore('read_file_2'), ('read_file', 'function', None))
        self.assertEqual(bridge.restore('read_file'), ('read_file', 'function', 'shell'))
        self.assertEqual(bridge.restore('stat'), ('stat', 'function', 'nested.inner'))
        self.assertEqual(bridge.restore('unknown'), ('unknown', 'function', None))
        self.assertEqual(bridge.outbound_name('stat', 'nested.inner'), 'stat')

    def test_history_and_choice_use_outbound_names(self):
        body = {
            'model': 'm',
            'tools': ns_tools(),
            'tool_choice': {'type': 'custom', 'name': 'apply_patch'},
            'input': [
                {'type': 'custom_tool_call', 'call_id': 'c1', 'name': 'apply_patch', 'input': 'patch'},
                {'type': 'custom_tool_call_output', 'call_id': 'c1', 'output': 'ok'},
                {'type': 'function_call', 'call_id': 'c2', 'name': 'read_file',
                 'arguments': '{"path":"a"}'},
                {'type': 'function_call_output', 'call_id': 'c2', 'output': 'a'},
            ],
        }
        chat = R.to_chat_request(body)
        self.assertEqual(chat['tool_choice'], {'type': 'function', 'function': {'name': 'apply_patch'}})
        names = [m['tool_calls'][0]['function']['name'] for m in chat['messages'] if m.get('tool_calls')]
        self.assertEqual(names, ['apply_patch', 'read_file'])
        self.assertEqual(json.loads(chat['messages'][0]['tool_calls'][0]['function']['arguments']),
                         {'input': 'patch'})

    def test_response_restores_original_child_names(self):
        bridge = R._ToolBridge(ns_tools())
        data = {'choices': [{'finish_reason': 'tool_calls', 'message': {
            'tool_calls': [
                {'id': '1', 'function': {'name': 'apply_patch', 'arguments': '{"input":"patch"}'}},
                {'id': '2', 'function': {'name': 'read_file_2', 'arguments': '{"path":"a"}'}},
            ]
        }}]}
        out = R.to_responses_object(data, 'm', 'resp_1', bridge.custom_names, bridge)['output']
        self.assertEqual([(i['type'], i['name']) for i in out],
                         [('custom_tool_call', 'apply_patch'), ('function_call', 'read_file')])
        self.assertEqual(out[0]['input'], 'patch')

    def test_stream_restores_custom_child(self):
        bridge = R._ToolBridge(ns_tools())
        t = R._StreamTranslator('m', 'resp_1', bridge.custom_names, bridge)
        events = t.feed({'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'id': 'call', 'function': {'name': 'apply_patch', 'arguments': '{"input":"p"}'}}
        ]}, 'finish_reason': 'tool_calls'}]})
        events += t.finish('tool_calls')
        items = [e for e in events if e.startswith(b'event: response.output_item.done')]
        self.assertTrue(items)
        payload = json.loads(items[-1].split(b'\ndata: ', 1)[1].split(b'\n\n', 1)[0])
        self.assertEqual(payload['item']['type'], 'custom_tool_call')
        self.assertEqual(payload['item']['name'], 'apply_patch')
        self.assertEqual(payload['item']['input'], 'p')


class BothPathsRestoreNamesTest(unittest.TestCase):
    """**流式与非流式必须还原同一套名字**（审查发现的缺陷）。

    桥接给了两个出口：`to_responses_object`（非流式）与 `_StreamTranslator`
    （流式）。两者都要拿到 `bridge` 才能把展开后的 Chat 名还原成客户端原名
    ——`read_file_2` 是**我们内部**为了避免重名造出来的，客户端不认识它；
    客户端下一轮按自己声明的 `read_file` 回传结果，名字对不上就匹配不了。

    原实现的非流式出口漏传了 `bridge`，于是同一个请求只因为 `stream` 标志不同，
    客户端就拿到两套工具名。上面那条 `test_response_restores_original_child_names`
    测不出来，因为它是**手动**把 bridge 传进去的——测的是函数本身，不是生产
    调用点。所以这里直接锚住调用点。
    """

    def test_non_streaming_callsite_passes_bridge(self):
        """调用点必须把 bridge 传给 to_responses_object。"""
        src = (Path(__file__).resolve().parents[1]
               / 'routers' / 'responses.py').read_text(encoding='utf-8')
        # 参数表在 issue #450 之后多了 parallel_tool_calls（PR #451），这里只锚
        # **前五个位置参数**仍然按序传对，避免每加一个可选参数就要改一次断言。
        self.assertIn(
            'to_responses_object(data, model, resp_id, custom_tool_names, bridge,',
            src,
            '非流式出口漏传 bridge —— 改名后的工具名会原样发给客户端，'
            '而流式路径会还原，两条路径不一致',
        )

    def test_both_paths_agree_on_restored_name(self):
        """同一次上游响应，两条路径还原出的名字必须一致。"""
        bridge = R._ToolBridge(ns_tools())
        data = {'choices': [{'finish_reason': 'tool_calls', 'message': {
            'tool_calls': [
                {'id': '2', 'function': {'name': 'read_file_2',
                                         'arguments': '{"path":"a"}'}},
            ]}}]}
        non_stream = R.to_responses_object(
            data, 'm', 'r', bridge.custom_names, bridge)['output']
        self.assertEqual(non_stream[0]['name'], 'read_file',
                         '非流式没还原成客户端原名')

        t = R._StreamTranslator('m', 'r', bridge.custom_names, bridge)
        events = t.feed(data)
        events += t.finish('tool_calls')
        done = [e for e in events
                if e.startswith(b'event: response.output_item.done')]
        self.assertTrue(done)
        payload = json.loads(done[-1].split(b'\ndata: ', 1)[1].split(b'\n\n', 1)[0])
        self.assertEqual(payload['item']['name'], 'read_file',
                         '流式没还原成客户端原名')
        self.assertEqual(non_stream[0]['name'], payload['item']['name'],
                         '两条路径给出的工具名不一致')


class NamespacedCustomStreamTest(unittest.TestCase):
    """命名空间下的 **custom 子工具**在流式回程也要还原成 custom 形态。

    这是两处判据叠加的情形：它既是 custom（要走 `{input}` 包装），又在命名空间里
    （名字要还原）。判据任一命中即可，但两条都留着——上游分片异常时可能只给出半个
    名字，那时只认其中一条就会把 custom 当普通 function 返回（客户端报
    incompatible payload 的那类表现）。
    """

    def test_namespaced_custom_child_stays_custom(self) -> None:
        tools = [{'type': 'namespace', 'name': 'shell', 'tools': [
            {'type': 'custom', 'name': 'apply_patch'}]}]
        bridge = R._ToolBridge(tools)
        self.assertIn('apply_patch', bridge.custom_names)

        t = R._StreamTranslator('m', 'r', bridge.custom_names, bridge)
        events = t.feed({'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'id': 'c1',
             'function': {'name': 'apply_patch', 'arguments': '{"input":"PATCH"}'}}]},
            'finish_reason': 'tool_calls'}]})
        events += t.finish('tool_calls')
        done = [e for e in events
                if e.startswith(b'event: response.output_item.done')]
        payload = json.loads(done[-1].split(b'\ndata: ', 1)[1].split(b'\n\n', 1)[0])
        self.assertEqual(payload['item']['type'], 'custom_tool_call')
        self.assertEqual(payload['item']['name'], 'apply_patch')
        self.assertEqual(payload['item']['input'], 'PATCH')
        self.assertNotIn('namespace', payload['item'])

    def test_custom_decision_uses_one_criterion(self) -> None:
        """「是不是 custom」与「取哪份入参」必须是**同一个判据**。

        曾经的写法是 `custom = seq in custom_inputs or restored_kind == 'custom'`，
        随后又用 `custom_inputs[seq] if custom else ...` 取值——当两条判据不一致时
        （判为 custom 而该字典没有这一项）直接 KeyError，把整条流打断。

        判据统一用 `custom_inputs` 的成员关系就够：它的键正是「名字在
        custom_names 里的 seq」，与 `restore` 认为的 custom 是同一批
        （`_reverse` 里标 custom 的名字必然也在 custom_names 里）。
        """
        src = (Path(__file__).resolve().parents[1]
               / 'routers' / 'responses.py').read_text(encoding='utf-8')
        seg = src[src.index('def _close_tools'):]
        seg = seg[:seg.index('def ', seg.index('def _close_tools') + 10)]
        import re as _re
        # 判据行必须**只**看 custom_inputs，不能带 or
        m = _re.search(r'^\s*custom = (.+)$', seg, _re.M)
        self.assertIsNotNone(m, '找不到 custom 判据那一行')
        self.assertNotIn(' or ', m.group(1),
                         '判据带了第二个条件 —— 与取值判据不一致时会 KeyError：'
                         f'{m.group(1).strip()}')
        self.assertIn('custom_inputs', m.group(1))
        self.assertIn('custom_inputs[seq] if custom else', seg)

    def test_namespaced_plain_function_child_stays_function(self) -> None:
        """对照组：命名空间里的**普通** function 子工具不能被当成 custom。"""
        tools = [{'type': 'namespace', 'name': 'shell', 'tools': [
            {'type': 'function', 'name': 'read_file',
             'parameters': {'type': 'object', 'properties': {}}}]}]
        bridge = R._ToolBridge(tools)
        self.assertEqual(bridge.custom_names, set())

        t = R._StreamTranslator('m', 'r', bridge.custom_names, bridge)
        events = t.feed({'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'id': 'c2',
             'function': {'name': 'read_file', 'arguments': '{"path":"a"}'}}]},
            'finish_reason': 'tool_calls'}]})
        events += t.finish('tool_calls')
        done = [e for e in events
                if e.startswith(b'event: response.output_item.done')]
        payload = json.loads(done[-1].split(b'\ndata: ', 1)[1].split(b'\n\n', 1)[0])
        self.assertEqual(payload['item']['type'], 'function_call')
        self.assertEqual(payload['item']['arguments'], '{"path":"a"}')


if __name__ == '__main__':
    unittest.main()
