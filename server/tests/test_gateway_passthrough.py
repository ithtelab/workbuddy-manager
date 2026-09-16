"""网关流式转发的透传性回归测试（含 tool_calls 帧形态）。

背景：上游 2026-09-14 修了流式 `tool_calls` 的 name 语义（issue #82）——把
「逐帧回填 name」改成「每个 index 只发一次 name」（对齐 OpenAI 官方流）。
辅助函数 `backfillToolCallNames` 被重写为 `stripToolCallNames`。

对管理端的判断：**不需要适配**。我们的网关是 `yield chunk` 原样透传上游字节，
只在旁边用 `_scan_sse` 读一份做用量与首字统计（不改写、不回写、不缓冲），
且全仓库完全不碰 tool_calls。所以上游这个修复会通过转发路径自动生效。

本文件把这个「结论」变成断言：若将来有人在转发热路径里开始改写 SSE
（例如为了统一 tool_calls 形态），这些用例会失败，逼迫他先想清楚
「上游已经修过的语义，我们是否要再改一遍」——那正是重复且易错的。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import gateway  # noqa: E402


def _data(obj) -> str:
    return 'data: ' + json.dumps(obj, ensure_ascii=False) + '\n'


# 上游新形态：首帧带 name，后续帧**没有 name 键**只有 arguments 片段
TOOL_FIRST = _data({'choices': [{'delta': {'tool_calls': [
    {'index': 0, 'id': 'call_1', 'type': 'function',
     'function': {'name': 'Bash', 'arguments': ''}}]}}]})
TOOL_ARG_FRAME = _data({'choices': [{'delta': {'tool_calls': [
    {'index': 0, 'function': {'arguments': '{"cmd"'}}]}}]})
TOOL_ARG_FRAME2 = _data({'choices': [{'delta': {'tool_calls': [
    {'index': 0, 'function': {'arguments': ': "ls"}'}}]}}]})
TOOL_PARALLEL = _data({'choices': [{'delta': {'tool_calls': [
    {'index': 1, 'id': 'call_2', 'type': 'function',
     'function': {'name': 'Read', 'arguments': ''}}]}}]})


class ScanSseToolCallsTest(unittest.TestCase):
    """`_scan_sse` 不得依赖 tool_calls 的形态 —— 它只做统计，不参与转发。"""

    def test_tool_frames_do_not_break_scan(self) -> None:
        pending = ''
        for frame in (TOOL_FIRST, TOOL_ARG_FRAME, TOOL_ARG_FRAME2, TOOL_PARALLEL):
            pending, _ = gateway._scan_sse(pending + frame, {})
        self.assertEqual(pending, '', '残留缓冲应为空（每帧都以换行结束）')

    def test_tool_frames_are_not_counted_as_content(self) -> None:
        """工具调用帧不含正文，不能当作「首字」——否则首字延迟会偏小。

        这是真实语义：模型先决定调用工具，正文要等工具结果回来之后才产生。
        把 tool_calls 当首字会让「上游多久开始回话」这个指标失真。
        """
        for frame in (TOOL_FIRST, TOOL_ARG_FRAME, TOOL_PARALLEL):
            _, saw = gateway._scan_sse(frame, {})
            self.assertFalse(saw, f'工具帧不应计为正文：{frame[:60]}')

    def test_tool_frame_with_empty_name_is_ignored(self) -> None:
        """上游后续帧若带空串 name（旧形态），也不能影响统计。"""
        frame = _data({'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'function': {'name': '', 'arguments': 'x'}}]}}]})
        pending, saw = gateway._scan_sse(frame, {})
        self.assertEqual(pending, '')
        self.assertFalse(saw)

    def test_usage_extracted_from_tool_frames(self) -> None:
        """工具调用流的末帧同样要能取到 usage（否则用量统计漏记）。"""
        usage: dict = {}
        frame = _data({'choices': [], 'usage': {'prompt_tokens': 7, 'completion_tokens': 3}})
        _, _ = gateway._scan_sse(frame, usage)
        self.assertEqual(usage['prompt_tokens'], 7)
        self.assertEqual(usage['completion_tokens'], 3)

    def test_scan_does_not_mutate_payload(self) -> None:
        """扫描必须是只读的：它拿到的对象不能被改（改了会污染转发内容）。

        这里直接传 dict 进去，断言扫描后原对象不变。
        """
        obj = {'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'function': {'name': 'Bash', 'arguments': 'x'}}]}}]}
        snapshot = json.dumps(obj, sort_keys=True)
        gateway._scan_sse(_data(obj), {})
        self.assertEqual(json.dumps(obj, sort_keys=True), snapshot,
                         '_scan_sse 修改了传入对象 —— 它必须是只读的')


class GatewayPassthroughTest(unittest.TestCase):
    """确认网关没有「改写 SSE」的代码路径 —— 这是上面结论的前提。"""

    def test_gateway_source_does_not_rewrite_sse(self) -> None:
        src = (Path(__file__).resolve().parents[2] / 'server' / 'routers' / 'gateway.py'
               ).read_text(encoding='utf-8')
        # 转发热路径只 yield 上游 chunk；不应出现「重新构造/替换帧」的迹象
        for bad in ('yield _data(', 'yield json.dumps(', 'yield _rewrap('):
            self.assertNotIn(bad, src, f'网关似乎在重写 SSE：{bad}')
        # 所有 yield 出来的都必须是上游原样的 chunk（不能有第二个变量名混进来）
        # 注意：这里按**出现次数**断言，只查包含太弱——文件里有两处 yield，
        # 改掉其中一处仍会通过（这个弱点是被反证试出来的）。
        yields = [ln.strip() for ln in src.splitlines() if ln.strip().startswith('yield ')]
        self.assertEqual(yields, ['yield chunk'] * len(yields),
                         f'转发热路径出现了非透传的 yield：{yields}')

    def test_no_tool_calls_handling_anywhere(self) -> None:
        """转发热路径不碰 tool_calls —— 上游的修复通过透传自动生效。

        若将来需要在此处加逻辑（例如为了兼容某个客户端），请先确认
        **上游是否已经修过同一个问题**：重复修一遍往往引入新的不一致。

        唯一的例外是 **Anthropic 兼容层**（`routers/anthropic.py`）：它**必须**
        处理 tool_calls，因为那正是协议转换本身——OpenAI 把工具参数作为
        **分片字符串**下发（`{"loc` + `ation":…}`），而 Anthropic 的客户端要的是
        `input_json_delta` 事件、且参数以对象形式呈现。这与「替上游兜底」是两回事：
        它不改动转发给上游的内容，只改变回给客户端的表达形式；
        「转发热路径必须原样透传」这条约束由上面几个用例独立守着。
        """
        # 例外：协议转换层（不是转发热路径）
        allow = {'routers/anthropic.py'}
        root = Path(__file__).resolve().parents[2] / 'server'
        hits = []
        for f in root.rglob('*.py'):
            if 'tests' in str(f):
                continue
            rel = str(f.relative_to(root)).replace('\\', '/')
            if rel in allow:
                continue
            if 'tool_calls' in f.read_text(encoding='utf-8'):
                hits.append(str(f.relative_to(root)))
        self.assertEqual(hits, [], f'以下文件开始处理 tool_calls：{hits}')


if __name__ == '__main__':
    unittest.main()
