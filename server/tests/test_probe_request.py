"""上游探测请求与错误码翻译的回归测试。

两个独立但同源的问题（都源于「上游改了默认行为，我们这边没跟上」）：

1. **探测请求缺少 system 首条消息**：上游要求 `messages[0].role == 'system'`，
   否则返回 `11-128`。以前不报错是因为 `prompt.mode` 缺省为 `custom`，上游会
   用自有提示词在头部插一条 system；2026-09-14 起缺省改为 `passthrough`
   （原样透传），我们这条只带 user 的探测就被拒了。

2. **错误码提示从未生效**：`_CODE_HINTS` 里的键写成 `11-128`（没加引号），
   Python 把它当算术表达式算成 `-117`。于是这个提示永远匹配不上，
   而真正收到字符串 "11-128" 时 `int()` 又会抛异常被吞掉。
   表现是用户看到「上游返回 code=11-128 …」而不是可读的中文说明。
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
from server.services import tencent  # noqa: E402


class _StreamResp:
    status_code = 200
    headers = {'content-type': 'text/event-stream'}

    async def aiter_bytes(self):
        yield b'data: {"choices":[{"delta":{"content":"h"}}]}\n\n'

    async def aread(self):
        return b''

    async def aclose(self):
        return None


class _Client:
    """记录探测请求的 body 与 headers。"""

    sent: dict = {}

    def __init__(self, *a, **k) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, **kw):
        _Client.sent = {'method': method, 'url': url, 'json': kw.get('json'),
                        'headers': kw.get('headers') or {}}

        class _Ctx:
            async def __aenter__(self_inner):
                return _StreamResp()

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()

    async def aclose(self):
        return None


class ProbeRequestShapeTest(unittest.TestCase):
    """链路测试：探测请求必须带 system 首条消息，否则上游 11-128。"""

    AUTH = {'access_token': 'TOK', 'uid': 'u1', 'realm': 'cn'}

    def setUp(self) -> None:
        _Client.sent = {}
        p = mock.patch.object(config, 'http_client', _Client)
        p.start()
        self.addCleanup(p.stop)

    def _probe(self):
        return asyncio.run(tencent.probe_account(self.AUTH, 'glm-5.2'))

    def test_first_message_is_system(self) -> None:
        self._probe()
        msgs = _Client.sent['json']['messages']
        self.assertTrue(msgs, '探测请求没有 messages')
        self.assertEqual(
            msgs[0]['role'], 'system',
            '首条必须是 system —— 上游 passthrough 模式下不再自动注入，'
            '缺了会被拒 11-128「first message is not system prompt」',
        )

    def test_still_streaming_with_user_turn(self) -> None:
        """补 system 不能破坏原有的流式探测语义。"""
        self._probe()
        body = _Client.sent['json']
        self.assertTrue(body['stream'], '必须流式（上游拒绝非流式）')
        roles = [m['role'] for m in body['messages']]
        self.assertIn('user', roles, '仍需一条 user 消息作为实际输入')
        self.assertEqual(len(body['messages']), 2, '不必多发消息，保持最小探测')

    def test_probe_sends_conversation_header_family(self) -> None:
        """探测请求必须带**会话头族**（issue #193）。

        上游 2026-10-16 起对**部分账号**灰度启用渠道校验：出站缺 X-Conversation-ID
        即判 `11-128 conversation id missing` —— 被灰度到的账号在探测里必红。那是
        **探测请求形态**的问题，不是账号坏了（真实对话走网关，同一族头由上游补）。
        报告方的剥离实测：校验对象是头，body 里的 conversationId 不是必填项。
        """
        self._probe()
        h = _Client.sent['headers'] or {}
        self.assertIn('X-Conversation-ID', h, '缺这个头会被判 11-128（渠道校验）')
        for name in ('X-Conversation-Request-ID', 'X-Conversation-Message-ID',
                     'X-Root-Request-ID', 'X-Trace-ID'):
            self.assertIn(name, h, f'会话头族缺 {name}')
        ids = {h['X-Conversation-ID'], h['X-Conversation-Request-ID'],
               h['X-Conversation-Message-ID']}
        self.assertEqual(len(ids), 3, '会话 / 轮 / 消息三个 ID 语义不同，不该复用同一个值')

    def test_probe_succeeds(self) -> None:
        ok, msg = self._probe()
        self.assertTrue(ok, msg)

    def test_probe_content_has_no_known_fingerprints(self) -> None:
        """探测内容不得含上游内容审核的指纹词。

        上游 2026-09-14 又修了脱敏的三处盲区（裸键名 / reasoning_content /
        大小写），说明这份指纹词表是**活的**——它按逐字精确匹配拦截，命中即
        返回 `11-128 Illegal API invocation from an unapproved channel`。

        我们这条探测是**自己造的内容**（不是客户端透传），所以只要别写出
        指纹词就永远安全。这条测试守住这一点：将来有人把探测内容改得像
        Claude Code / Codex 的自述（那些正是被拦的模板句），会在这里失败。

        词表来源：上游 internal/upstream/sanitize.go 的 sanitizeFeatures。
        """
        self._probe()
        body = _Client.sent['json']
        blob = json.dumps(body, ensure_ascii=False).lower()
        offenders = [w for w in self.FINGERPRINT_WORDS if w.lower() in blob]
        self.assertEqual(
            offenders, [],
            f'探测内容含上游内容审核指纹词：{offenders} —— 会被判 11-128 拦截',
        )

    # 上游 sanitizeFeatures 的词表（保持小写比较）
    FINGERPRINT_WORDS = (
        'x-anthropic-billing-header',
        'You are Claude Code',
        'Main branch (',
        'You are a coding agent running in the Codex CLI',
        'github.com/anthropics/',
        '11-128',
    )


class ErrorCodeHintTest(unittest.TestCase):
    """错误码翻译：数字与「数字-数字」字符串都要能查到提示。"""

    def test_dash_code_is_a_string_key(self) -> None:
        """`11-128` 若不加引号会被算成 -117 —— 这条锁住那个坑。"""
        self.assertIn('11-128', tencent._CODE_HINTS)
        self.assertNotIn(-117, tencent._CODE_HINTS,
                         '出现 -117 说明 11-128 又被当成算术表达式了')

    def test_dash_code_translates(self) -> None:
        text = tencent._explain_code('11-128', 'first message is not system prompt')
        self.assertIn('system', text, '应给出「首条必须是 system」的说明')

    def test_numeric_and_string_codes(self) -> None:
        for code in (10001, '10001'):
            self.assertIn('今日已签到', tencent._explain_code(code))

    def test_credit_exhaustion_code(self) -> None:
        """14018 = 积分耗尽（对齐上游 2026-09-20 的分类）。

        上游在 429 上只认这个结构化业务码，不靠「额度不足」这类跨计费/限流两界
        的文案猜。本端跟着翻译：欠费号的连通性测试要说清是没积分，而不是让用户
        以为账号坏了（对应本批适配的 upstream 提交）。
        """
        for code in (14018, '14018'):
            self.assertIn('积分耗尽', tencent._explain_code(code))

    def test_unknown_code_does_not_crash(self) -> None:
        """未知码（含带横线的）不能抛异常 —— 之前 int() 会在这里炸。"""
        for code in ('99-1', 999999, 'weird', None):
            out = tencent._explain_code(code)
            self.assertIn('上游返回 code=', out)

    def test_message_is_preserved(self) -> None:
        out = tencent._explain_code('11-128', 'first message is not system prompt')
        self.assertIn('first message is not system prompt', out,
                      '上游原文必须保留，便于排查')


if __name__ == '__main__':
    unittest.main()
