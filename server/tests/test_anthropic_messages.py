"""Anthropic Messages API 兼容层（/v1/messages）的协议转换测试。

关注点是**翻译是否正确**，不是网络层——鉴权/配额/记账复用网关既有实现，
那部分已有独立用例覆盖。这里只测三个纯函数加一个流式状态机，
全部可离线运行，不需要上游。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from server.routers.anthropic import (  # noqa: E402
    _StreamTranslator,
    to_anthropic_response,
    to_openai_request,
)


class RequestTranslationTest(unittest.TestCase):
    """Anthropic 请求 → OpenAI 请求。"""

    def test_system_goes_to_first_message(self) -> None:
        """system 是顶层字段，OpenAI 必须是 messages 里的第一条。"""
        out = to_openai_request({
            'model': 'x', 'max_tokens': 10, 'system': '你是一个助手',
            'messages': [{'role': 'user', 'content': 'hi'}],
        })
        self.assertEqual(out['messages'][0], {'role': 'system', 'content': '你是一个助手'})
        self.assertEqual(out['messages'][1], {'role': 'user', 'content': 'hi'})

    def test_system_as_block_array(self) -> None:
        """system 也允许是 block 数组（带 cache_control 时 SDK 会这么发）。"""
        out = to_openai_request({
            'model': 'x', 'max_tokens': 10,
            'system': [{'type': 'text', 'text': '第一段'}, {'type': 'text', 'text': '第二段'}],
            'messages': [{'role': 'user', 'content': 'hi'}],
        })
        self.assertEqual(out['messages'][0]['content'], '第一段\n第二段')

    def test_text_blocks_flatten_to_string(self) -> None:
        """单个纯文本块压平成字符串，多数上游对字符串更宽容。"""
        out = to_openai_request({
            'model': 'x', 'max_tokens': 10,
            'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'hello'}]}],
        })
        self.assertEqual(out['messages'][0]['content'], 'hello')

    def test_image_block_becomes_data_url(self) -> None:
        out = to_openai_request({
            'model': 'x', 'max_tokens': 10,
            'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': '看这个'},
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AAA'}},
            ]}],
        })
        parts = out['messages'][0]['content']
        self.assertEqual(parts[0], {'type': 'text', 'text': '看这个'})
        self.assertEqual(parts[1]['image_url']['url'], 'data:image/png;base64,AAA')

    def test_tool_use_becomes_tool_calls(self) -> None:
        """assistant 的 tool_use → OpenAI tool_calls，且 input(对象) → arguments(字符串)。"""
        out = to_openai_request({
            'model': 'x', 'max_tokens': 10,
            'messages': [{'role': 'assistant', 'content': [
                {'type': 'tool_use', 'id': 'toolu_1', 'name': 'get_weather', 'input': {'city': '北京'}},
            ]}],
        })
        calls = out['messages'][0]['tool_calls']
        self.assertEqual(calls[0]['id'], 'toolu_1')
        self.assertEqual(calls[0]['function']['name'], 'get_weather')
        # 关键：必须是 JSON 字符串，不是对象
        self.assertIsInstance(calls[0]['function']['arguments'], str)
        self.assertEqual(json.loads(calls[0]['function']['arguments']), {'city': '北京'})

    def test_tool_result_splits_into_own_message(self) -> None:
        """最容易搞错的一处：Anthropic 把工具结果放在 user 消息的 block 里，
        而 OpenAI 要求它是独立的 role:tool 消息，且要排在其余内容之前。"""
        out = to_openai_request({
            'model': 'x', 'max_tokens': 10,
            'messages': [{'role': 'user', 'content': [
                {'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': '25 度'},
                {'type': 'text', 'text': '那要不要带伞'},
            ]}],
        })
        msgs = out['messages']
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]['role'], 'tool')
        self.assertEqual(msgs[0]['tool_call_id'], 'toolu_1')
        self.assertEqual(msgs[0]['content'], '25 度')
        self.assertEqual(msgs[1], {'role': 'user', 'content': '那要不要带伞'})

    def test_tools_schema_mapping(self) -> None:
        """Anthropic 的 {name, input_schema} → OpenAI 的 type:function 包装。"""
        out = to_openai_request({
            'model': 'x', 'max_tokens': 10, 'messages': [],
            'tools': [{'name': 'f', 'description': 'd', 'input_schema': {'type': 'object'}}],
        })
        self.assertEqual(out['tools'][0]['type'], 'function')
        self.assertEqual(out['tools'][0]['function']['name'], 'f')
        self.assertEqual(out['tools'][0]['function']['parameters'], {'type': 'object'})

    def test_tool_choice_variants(self) -> None:
        base = {'model': 'x', 'max_tokens': 10, 'messages': []}
        self.assertEqual(to_openai_request({**base, 'tool_choice': {'type': 'auto'}})['tool_choice'], 'auto')
        self.assertEqual(to_openai_request({**base, 'tool_choice': {'type': 'any'}})['tool_choice'], 'required')
        forced = to_openai_request({**base, 'tool_choice': {'type': 'tool', 'name': 'f'}})
        self.assertEqual(forced['tool_choice']['function']['name'], 'f')

    def test_scalar_params(self) -> None:
        out = to_openai_request({
            'model': 'x', 'max_tokens': 55, 'temperature': 0.3, 'top_p': 0.9,
            'stop_sequences': ['\n\n'], 'metadata': {'user_id': 'u1'}, 'messages': [],
        })
        self.assertEqual(out['max_tokens'], 55)
        self.assertEqual(out['temperature'], 0.3)
        self.assertEqual(out['top_p'], 0.9)
        self.assertEqual(out['stop'], ['\n\n'])
        self.assertEqual(out['user'], 'u1')


class ResponseTranslationTest(unittest.TestCase):
    """OpenAI 响应 → Anthropic 响应。"""

    def test_text_and_usage(self) -> None:
        out = to_anthropic_response({
            'choices': [{'message': {'role': 'assistant', 'content': '你好'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 12, 'completion_tokens': 3},
        }, 'claude-x')
        self.assertEqual(out['type'], 'message')
        self.assertEqual(out['role'], 'assistant')
        self.assertEqual(out['model'], 'claude-x')
        self.assertEqual(out['content'], [{'type': 'text', 'text': '你好'}])
        self.assertEqual(out['stop_reason'], 'end_turn')
        # 字段名必须换：Anthropic 用 input/output_tokens
        self.assertEqual(out['usage'], {'input_tokens': 12, 'output_tokens': 3})

    def test_tool_call_becomes_tool_use(self) -> None:
        out = to_anthropic_response({
            'choices': [{
                'message': {'role': 'assistant', 'content': None, 'tool_calls': [
                    {'id': 'call_1', 'type': 'function',
                     'function': {'name': 'f', 'arguments': '{"a":1}'}},
                ]},
                'finish_reason': 'tool_calls',
            }],
        }, 'm')
        block = out['content'][0]
        self.assertEqual(block['type'], 'tool_use')
        self.assertEqual(block['id'], 'call_1')
        self.assertEqual(block['name'], 'f')
        # 对象，不是字符串
        self.assertEqual(block['input'], {'a': 1})
        self.assertEqual(out['stop_reason'], 'tool_use')

    def test_malformed_arguments_do_not_crash(self) -> None:
        """上游给出非法 JSON 时不能让整次调用失败——包一层 _raw 保留原文。"""
        out = to_anthropic_response({
            'choices': [{'message': {'tool_calls': [
                {'id': 'c', 'function': {'name': 'f', 'arguments': '{"broken'}},
            ]}, 'finish_reason': 'tool_calls'}],
        }, 'm')
        self.assertEqual(out['content'][0]['input'], {'_raw': '{"broken'})

    def test_finish_reason_mapping(self) -> None:
        for oai, anth in (('stop', 'end_turn'), ('length', 'max_tokens'), ('tool_calls', 'tool_use')):
            out = to_anthropic_response(
                {'choices': [{'message': {'content': 'x'}, 'finish_reason': oai}]}, 'm')
            self.assertEqual(out['stop_reason'], anth, oai)

    def test_empty_choices_is_tolerated(self) -> None:
        out = to_anthropic_response({}, 'm')
        self.assertEqual(out['content'], [])
        self.assertEqual(out['stop_reason'], 'end_turn')


def _parse(events: list[bytes]) -> list[tuple[str, dict]]:
    """把事件字节流解析成 (event_name, payload) 列表，便于断言。"""
    out: list[tuple[str, dict]] = []
    for raw in events:
        text = raw.decode('utf-8')
        name = ''
        payload = {}
        for line in text.split('\n'):
            if line.startswith('event:'):
                name = line[6:].strip()
            elif line.startswith('data:'):
                payload = json.loads(line[5:].strip())
        out.append((name, payload))
    return out


class StreamTranslatorTest(unittest.TestCase):
    """OpenAI SSE → Anthropic 事件流。"""

    def test_message_start_emitted_once(self) -> None:
        t = _StreamTranslator('m')
        events = _parse(t.feed({'choices': [{'delta': {'content': 'a'}}]}))
        self.assertEqual(events[0][0], 'message_start')
        # 再喂一片不应重复发 message_start
        events2 = _parse(t.feed({'choices': [{'delta': {'content': 'b'}}]}))
        self.assertNotIn('message_start', [e[0] for e in events2])

    def test_text_delta_sequence(self) -> None:
        t = _StreamTranslator('m')
        events = _parse(t.feed({'choices': [{'delta': {'content': 'hello'}}]}))
        names = [e[0] for e in events]
        self.assertEqual(names, ['message_start', 'content_block_start', 'content_block_delta'])
        self.assertEqual(events[-1][1]['delta'], {'type': 'text_delta', 'text': 'hello'})

    def test_finish_closes_block_and_stops(self) -> None:
        t = _StreamTranslator('m')
        t.feed({'choices': [{'delta': {'content': 'x'}}]})
        events = _parse(t.finish('stop'))
        names = [e[0] for e in events]
        self.assertEqual(names, ['content_block_stop', 'message_delta', 'message_stop'])
        self.assertEqual(events[1][1]['delta']['stop_reason'], 'end_turn')

    def test_finish_is_idempotent(self) -> None:
        """收尾只能发生一次——重复调用会在流末尾多吐一份 message_stop。"""
        t = _StreamTranslator('m')
        t.feed({'choices': [{'delta': {'content': 'x'}}]})
        first = t.finish('stop')
        second = t.finish('stop')
        self.assertTrue(first)
        self.assertEqual(second, [])

    def test_tool_call_uses_input_json_delta(self) -> None:
        """工具参数必须原样分片透传（我们无从判断 JSON 何时拼完整）。"""
        t = _StreamTranslator('m')
        events = _parse(t.feed({'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'id': 'call_1', 'function': {'name': 'f', 'arguments': '{"a'}},
        ]}}]}))
        start = next(e for e in events if e[0] == 'content_block_start')
        self.assertEqual(start[1]['content_block']['type'], 'tool_use')
        self.assertEqual(start[1]['content_block']['name'], 'f')
        delta = next(e for e in events if e[0] == 'content_block_delta')
        self.assertEqual(delta[1]['delta'], {'type': 'input_json_delta', 'partial_json': '{"a'})

    def test_text_to_tool_switches_blocks(self) -> None:
        """文本块与工具块不能并存：切到工具前必须先关掉文本块，索引也不能串。"""
        t = _StreamTranslator('m')
        t.feed({'choices': [{'delta': {'content': '先说话'}}]})
        events = _parse(t.feed({'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'id': 'c', 'function': {'name': 'f', 'arguments': '{}'}},
        ]}}]}))
        names = [e[0] for e in events]
        self.assertIn('content_block_stop', names)
        # 文本块 index=0，工具块应是新的 index=1
        tool_start = next(e for e in events if e[0] == 'content_block_start')
        self.assertEqual(tool_start[1]['index'], 1)
        close = next(e for e in events if e[0] == 'content_block_stop')
        self.assertEqual(close[1]['index'], 0)

    def test_usage_captured_from_final_chunk(self) -> None:
        """include_usage 的末帧要喂进 message_delta 的 usage.output_tokens。"""
        t = _StreamTranslator('m')
        t.feed({'choices': [{'delta': {'content': 'x'}}], 'usage': {'prompt_tokens': 7, 'completion_tokens': 9}})
        events = _parse(t.finish('stop'))
        msg_delta = next(e for e in events if e[0] == 'message_delta')
        self.assertEqual(msg_delta[1]['usage']['output_tokens'], 9)

    def test_force_finish_infers_tool_use_when_reason_missing(self) -> None:
        """上游没给 finish_reason 但出现过工具调用时，按 tool_use 收尾更贴近实际。"""
        t = _StreamTranslator('m')
        t.feed({'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'id': 'c', 'function': {'name': 'f', 'arguments': '{}'}},
        ]}}]})
        events = _parse(t.finish(None, force=True))
        msg_delta = next(e for e in events if e[0] == 'message_delta')
        self.assertEqual(msg_delta[1]['delta']['stop_reason'], 'tool_use')


class ModelListShapeTest(unittest.TestCase):
    """`/v1/models` 按请求头分流的形状转换。"""

    def test_openai_shape_to_anthropic(self) -> None:
        from server.routers.gateway import _as_anthropic_models
        out = _as_anthropic_models({'data': [{'id': 'm1', 'created': 0}]})
        self.assertEqual(out['data'][0]['type'], 'model')
        self.assertEqual(out['data'][0]['id'], 'm1')
        self.assertFalse(out['has_more'])
        self.assertEqual(out['first_id'], 'm1')
        self.assertEqual(out['last_id'], 'm1')

    def test_unrecognized_shape_passes_through(self) -> None:
        """认不出的结构原样返回——不在看不懂的响应上动手脚。"""
        from server.routers.gateway import _as_anthropic_models
        weird = {'error': 'nope'}
        self.assertIs(_as_anthropic_models(weird), weird)

    def test_empty_list_is_valid(self) -> None:
        from server.routers.gateway import _as_anthropic_models
        out = _as_anthropic_models({'data': []})
        self.assertEqual(out['data'], [])
        self.assertIsNone(out['first_id'])


class TokenHeaderToleranceTest(unittest.TestCase):
    """令牌取值要宽容——少认一种写法就会表现为「某客户端莫名 401」。

    真实踩到过：Claude Code 配 ANTHROPIC_AUTH_TOKEN 与配 ANTHROPIC_API_KEY
    发的头不同；部分转发工具（ccswitch 等）还会把裸 token 直接放进
    Authorization，或加自己的前缀。这几种都得认。
    """

    def _req(self, headers: dict):
        """构造一个假 request。

        headers 必须模拟 Starlette 的 `Headers`：**大小写不敏感**。
        直接用 dict 会踩坑——dict 的 .get() 区分大小写，而 HTTP 头名不区分，
        于是「X-API-Key」这种真实会被接受的头，在测试里会假失败。
        """
        class _Headers:
            def __init__(self, d): self._d = {k.lower(): v for k, v in d.items()}
            def get(self, key, default=''): return self._d.get(key.lower(), default)
            def keys(self): return list(self._d.keys())

        class _R:
            def __init__(self, h): self.headers = _Headers(h)
        return _R(headers)

    def test_accepts_known_header_shapes(self) -> None:
        # 用列表而非 dict：多个用例的头名只差大小写，放进 dict 会互相覆盖，
        # 表面上写了几条、实际只跑了一条。
        from server.routers.anthropic import _token
        cases = [
            ({'x-api-key': 'wbk_a'}, 'x-api-key 小写'),
            ({'X-API-Key': 'wbk_a'}, 'x-api-key 大写'),
            ({'x-anthropic-api-key': 'wbk_a'}, 'x-anthropic-api-key'),
            ({'Authorization': 'Bearer wbk_a'}, 'Bearer 规范'),
            ({'authorization': 'bearer wbk_a'}, 'bearer 小写'),
            ({'Authorization': 'wbk_a'}, '裸 token（无 Bearer）'),
        ]
        for headers, label in cases:
            with self.subTest(label=label):
                self.assertEqual(_token(self._req(headers)), 'wbk_a', label)

    def test_strips_surrounding_whitespace(self) -> None:
        """复制粘贴常带前后空白，不剥掉就会哈希不匹配。"""
        from server.routers.anthropic import _token
        self.assertEqual(_token(self._req({'x-api-key': '  wbk_a\n'})), 'wbk_a')
        self.assertEqual(_token(self._req({'Authorization': 'Bearer  wbk_a '})), 'wbk_a')

    def test_returns_empty_when_absent(self) -> None:
        from server.routers.anthropic import _token
        self.assertEqual(_token(self._req({})), '')
        self.assertEqual(_token(self._req({'Authorization': '   '})), '')

    def test_collects_all_candidates_deduped(self) -> None:
        """候选要**全部**收集并去重，而不是只取第一个。"""
        from server.routers.anthropic import _token_candidates
        got = _token_candidates(self._req({
            'x-api-key': 'sk-other-service',
            'Authorization': 'Bearer wbk_ours',
        }))
        self.assertEqual(got, ['sk-other-service', 'wbk_ours'])
        # 值相同（去掉 Bearer 后）只留一份
        dup = _token_candidates(self._req({
            'Authorization': 'Bearer wbk_same',
            'x-anthropic-api-key': 'wbk_same',
        }))
        self.assertEqual(dup, ['wbk_same'])

    def test_header_names_never_leaks_values(self) -> None:
        """调试日志只记头名——把凭据写进日志等于换个地方泄露。"""
        from server.routers.anthropic import _header_names
        names = _header_names(self._req({'x-api-key': 'wbk_secret_value', 'User-Agent': 'x'}))
        self.assertIn('x-api-key', names)
        self.assertIn('user-agent', names)
        self.assertNotIn('wbk_secret_value', ' '.join(names))


class CountTokensAuthTest(unittest.TestCase):
    """`/v1/messages/count_tokens` 必须鉴权。

    它不产生上游调用、不消耗额度，最容易被当成「无害的估算接口」而漏掉鉴权——
    但那样任何未认证访问者都能借它判断网关是否存活、探测部署形态，也与其余
    端点「一律先验密钥」的约定不一致。这组用例把它钉死。
    """

    @classmethod
    def setUpClass(cls) -> None:
        import tempfile

        from fastapi.testclient import TestClient

        from server import config, db, keysvc

        cls._tmp = tempfile.TemporaryDirectory()
        cls._dir = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = cls._dir / 'ct.db'
        config.USERS_FILE = cls._dir / 'users.json'
        config.STATIC_DIR = cls._dir / 'no-static'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': 'x'}],
            'api_keys': [],
        }), encoding='utf-8')

        db._conn = None
        db.connect()
        from server.main import app
        cls.app = app
        cls.key = keysvc.create_key(name='count-tokens-test')['key']
        cls.c = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        from server import config, db
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def test_rejects_missing_token(self) -> None:
        r = self.c.post('/v1/messages/count_tokens', json={'messages': []})
        self.assertEqual(r.status_code, 401)
        # 错误体要是 Anthropic 形状，客户端才读得懂
        self.assertEqual(r.json()['type'], 'error')

    def test_rejects_invalid_token(self) -> None:
        r = self.c.post('/v1/messages/count_tokens', json={'messages': []},
                        headers={'x-api-key': 'wbk_bogus'})
        self.assertEqual(r.status_code, 401)

    def test_accepts_valid_token_via_either_header(self) -> None:
        """两种传法都要能用——Anthropic SDK 用 x-api-key，Claude Code 用 Bearer。"""
        for headers in ({'x-api-key': self.key}, {'Authorization': f'Bearer {self.key}'}):
            r = self.c.post('/v1/messages/count_tokens',
                            json={'messages': [{'role': 'user', 'content': 'hi'}]},
                            headers=headers)
            self.assertEqual(r.status_code, 200, headers)
            self.assertIn('input_tokens', r.json())

    def test_messages_endpoint_also_requires_token(self) -> None:
        """主端点同理——防止将来重构时把它俩中的一个漏掉。"""
        r = self.c.post('/v1/messages',
                        json={'model': 'm', 'max_tokens': 1, 'messages': []})
        self.assertEqual(r.status_code, 401)

    def test_picks_the_credential_that_resolves(self) -> None:
        """客户端同时带多份凭据时，要挑出**能解析**的那一份。

        实测踩到：Claude Code 既发 `x-api-key`（值是另一个服务、`sk-` 开头）
        又发 `Authorization: Bearer`（值才是本网关的 `wbk_`）。只取第一个就会
        拿到不相干的那份，表现为「配对了却一直 401」，而客户端侧完全看不出问题。
        """
        r = self.c.post('/v1/messages/count_tokens',
                        json={'messages': [{'role': 'user', 'content': 'hi'}]},
                        headers={
                            'x-api-key': 'sk-some-other-service-key',
                            'Authorization': f'Bearer {self.key}',
                        })
        self.assertEqual(r.status_code, 200, '应回退到 authorization 里那份有效凭据')
        self.assertIn('input_tokens', r.json())

    def test_bogus_x_api_key_alone_still_rejected(self) -> None:
        """只有无效凭据时依然拒绝——挑不出来就老实 401，不能放行。"""
        r = self.c.post('/v1/messages/count_tokens',
                        json={'messages': []},
                        headers={'x-api-key': 'sk-some-other-service-key'})
        self.assertEqual(r.status_code, 401)


if __name__ == '__main__':
    unittest.main()
