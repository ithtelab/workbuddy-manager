"""调用抓包（Trace Tap，见 docs/api-traffic-tap.md）的回归测试。

锁定的性质，每条都对应一个「坏了会静默出问题」的点：

  * **脱敏**：Authorization / x-api-key / Cookie 与正文里的 `sk-…`、`Bearer …`
    一律不出现在库里 —— 抓包是全项目唯一存完整对话内容的地方，漏一次就是
    永久留存的密钥；
  * **重建**：流式响应里的工具参数跨帧到达，必须按 index 拼回去（拼不上就是
    一堆半截 JSON）；
  * **会话归并**：同一任务的后续请求落在同一会话、seq 递增，换任务另起一条；
  * **限容**：超长请求体被截断并标记（truncated），不是悄悄丢掉后半段；
  * **旁路安全**：抓包关闭时不写任何行，异常输入也不抛给调用方；
  * **权限**：会话内容只给管理员；只读账号能读开关、读不到对话。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, security  # noqa: E402
from server.services import tap  # noqa: E402

SSE = '\n'.join([
    'data: ' + json.dumps({'choices': [{'delta': {'content': 'Hel'}}]}),
    'data: ' + json.dumps({'choices': [{'delta': {'content': 'lo',
                                                  'reasoning_content': '想一想'}}]}),
    'data: ' + json.dumps({'choices': [{'delta': {'tool_calls': [
        {'index': 0, 'id': 'c1',
         'function': {'name': 'read_file', 'arguments': '{"pa'}}]}}]}),
    'data: ' + json.dumps({'choices': [{'delta': {'tool_calls': [
        {'index': 0, 'function': {'arguments': 'th":"a.py"}'}}]},
        'finish_reason': 'tool_calls'}],
        'usage': {'prompt_tokens': 11, 'completion_tokens': 7,
                  'prompt_cache_hit_tokens': 5, 'prompt_cache_miss_tokens': 6}}),
    'data: [DONE]',
])

BODY = {
    'model': 'gpt-4o',
    'messages': [
        {'role': 'system', 'content': 'You are a careful coding agent.'},
        {'role': 'user', 'content': 'fix the bug in foo.py'},
    ],
    'tools': [{'type': 'function',
               'function': {'name': 'read_file', 'description': 'read a file'}}],
    'temperature': 0.2,
}


def _body(**extra: object) -> dict:
    data = json.loads(json.dumps(BODY))
    data.update(extra)
    return data


class PureTest(unittest.TestCase):
    """不碰数据库的部分：脱敏、重建、差异、指纹。"""

    def test_scrub_removes_tokens_but_stays_json(self) -> None:
        raw = json.dumps({'api_key': 'sk-abcdef0123456789', 'note': 'Bearer abcdefghijkl'})
        out = tap.scrub(raw)
        self.assertNotIn('sk-abcdef0123456789', out)
        self.assertNotIn('abcdefghijkl', out)
        # 仍然可解析：脱敏替换的是值，不是结构
        json.loads(out)

    def test_mask_keeps_credential_prefix_behind_scheme(self) -> None:
        """`Bearer xxx` 要打到凭证上，而不是只留下 `Bearer`。

        第一版就是错的：取整串前 6 个字符 → `Bearer***`，凭证一位都看不到，
        而「这是哪一把密钥」正是抓包页要对照的东西。
        """
        out = tap.redact_headers([
            ('authorization', 'Bearer wbk_6eotk5VAabcdefg'),
            ('x-api-key', 'sk-upstream-abcdef123456'),
        ])
        self.assertEqual(out['authorization'], 'Bearer wbk_6e***')
        self.assertEqual(out['x-api-key'], 'sk-ups***')
        for value in out.values():
            self.assertNotIn('abcdefg', value)

    def test_redact_headers_masks_secrets(self) -> None:
        out = tap.redact_headers([
            ('authorization', 'Bearer sk-livesecret1234'),
            ('x-api-key', 'abcd1234efgh'),
            ('cookie', 'wb_session=abcdef123456'),
            ('content-type', 'application/json'),
        ])
        for name in ('authorization', 'x-api-key', 'cookie'):
            self.assertNotIn('secret', out[name].lower())
            self.assertIn('***', out[name])
        self.assertEqual(out['content-type'], 'application/json', '普通头不该被打码')

    def test_reconstruct_merges_streaming_tool_arguments(self) -> None:
        out = tap.reconstruct(text=SSE)
        self.assertEqual(out['text'], 'Hello')
        self.assertEqual(out['reasoning'], '想一想')
        self.assertEqual(out['finish'], 'tool_calls')
        self.assertTrue(out['done'], '[DONE] 必须被识别为流结束')
        self.assertEqual(len(out['tool_calls']), 1)
        call = out['tool_calls'][0]
        self.assertEqual(call['name'], 'read_file')
        # 参数跨两帧：拼不对这里会是 '{"pa' 或非法 JSON
        self.assertEqual(json.loads(call['arguments']), {'path': 'a.py'})

    def test_reconstruct_reads_non_stream_payload(self) -> None:
        out = tap.reconstruct(payload={'choices': [{
            'message': {'content': 'done', 'tool_calls': [
                {'id': 'x', 'function': {'name': 'write_file', 'arguments': '{}'}}]},
            'finish_reason': 'stop'}]})
        self.assertEqual(out['text'], 'done')
        self.assertEqual(out['tool_calls'][0]['name'], 'write_file')

    def test_fingerprint_groups_same_run_and_separates_runs(self) -> None:
        a = _body()
        b = _body(messages=a['messages'] + [{'role': 'user', 'content': 'step 2'}])
        c = _body(messages=[a['messages'][0], {'role': 'user', 'content': '别的问题'}])
        self.assertEqual(tap._fingerprint(a, 'chat'), tap._fingerprint(b, 'chat'),
                         '同一任务追加历史后指纹必须不变，否则会话会被拆成几十条')
        self.assertNotEqual(tap._fingerprint(a, 'chat'), tap._fingerprint(c, 'chat'))

    def test_diff_reports_added_message_and_changed_param(self) -> None:
        before = {'id': 1, 'seq': 1, 'ts': 0, 'model': 'gpt-4o',
                  'request': {'body': _body()}}
        after = {'id': 2, 'seq': 2, 'ts': 0, 'model': 'gpt-4o',
                 'request': {'body': _body(
                     temperature=0.7,
                     messages=BODY['messages'] + [{'role': 'assistant', 'content': 'ok'}]),
                 }}
        # 直接调 diff 需要 turn id，这里复用内部实现：改成等价的两个假 turn
        import unittest.mock as mock
        with mock.patch.object(tap, 'turn', side_effect=[before, after]):
            out = tap.diff(1, 2)
        assert out is not None
        self.assertEqual(out['summary']['added'], 1)
        self.assertEqual([p['key'] for p in out['params']], ['temperature'])

    def test_client_reconstruct_reads_anthropic_stream(self) -> None:
        """Anthropic 客户端收到的是**翻译后**的事件流，字段名与上游完全不同。"""
        sse = '\n'.join([
            'event: message_start',
            'data: ' + json.dumps({'type': 'message_start',
                                   'message': {'id': 'msg_1', 'content': []}}),
            'event: content_block_start',
            'data: ' + json.dumps({'type': 'content_block_start', 'index': 0,
                                   'content_block': {'type': 'text', 'text': ''}}),
            'event: content_block_delta',
            'data: ' + json.dumps({'type': 'content_block_delta', 'index': 0,
                                   'delta': {'type': 'text_delta', 'text': '你好'}}),
            'event: content_block_start',
            'data: ' + json.dumps({'type': 'content_block_start', 'index': 1,
                                   'content_block': {'type': 'tool_use', 'id': 'tu_1',
                                                    'name': 'read_file', 'input': {}}}),
            'event: content_block_delta',
            'data: ' + json.dumps({'type': 'content_block_delta', 'index': 1,
                                   'delta': {'type': 'input_json_delta',
                                             'partial_json': '{"path":'}}),
            'event: content_block_delta',
            'data: ' + json.dumps({'type': 'content_block_delta', 'index': 1,
                                   'delta': {'type': 'input_json_delta',
                                             'partial_json': '"a.py"}'}}),
            'event: message_delta',
            'data: ' + json.dumps({'type': 'message_delta',
                                   'delta': {'stop_reason': 'tool_use'}}),
            'event: message_stop',
            'data: ' + json.dumps({'type': 'message_stop'}),
        ])
        out = tap.client_reconstruct(sse, 'anthropic')
        assert out is not None
        self.assertEqual(out['text'], '你好')
        self.assertEqual(out['finish'], 'tool_use')
        self.assertTrue(out['done'])
        self.assertEqual(len(out['tool_calls']), 1)
        self.assertEqual(out['tool_calls'][0]['name'], 'read_file')
        self.assertEqual(json.loads(out['tool_calls'][0]['arguments']), {'path': 'a.py'})

    def test_client_reconstruct_reads_responses_stream(self) -> None:
        sse = '\n'.join([
            'event: response.output_item.added',
            'data: ' + json.dumps({'type': 'response.output_item.added',
                                   'output_index': 0,
                                   'item': {'type': 'function_call', 'call_id': 'c1',
                                            'name': 'run_tests', 'arguments': ''}}),
            'event: response.function_call_arguments.delta',
            'data: ' + json.dumps({'type': 'response.function_call_arguments.delta',
                                   'output_index': 0, 'delta': '{"target":'}),
            'event: response.output_text.delta',
            'data: ' + json.dumps({'type': 'response.output_text.delta',
                                   'delta': 'done'}),
            'event: response.completed',
            'data: ' + json.dumps({'type': 'response.completed',
                                   'response': {'status': 'completed'}}),
        ])
        out = tap.client_reconstruct(sse, 'responses')
        assert out is not None
        self.assertEqual(out['text'], 'done')
        self.assertEqual(out['tool_calls'][0]['arguments'], '{"target":')
        self.assertTrue(out['done'])

    def test_client_reconstruct_reads_non_stream_objects(self) -> None:
        anthropic = json.dumps({'content': [
            {'type': 'text', 'text': 'hi'},
            {'type': 'tool_use', 'id': 'tu', 'name': 'read_file', 'input': {'path': 'a'}},
        ], 'stop_reason': 'tool_use'})
        out = tap.client_reconstruct(anthropic, 'anthropic')
        assert out is not None
        self.assertEqual(out['text'], 'hi')
        self.assertEqual(out['tool_calls'][0]['name'], 'read_file')

        responses = json.dumps({'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': 'ok'}]},
            {'type': 'function_call', 'call_id': 'c', 'name': 'ls', 'arguments': '{}'},
        ]})
        out2 = tap.client_reconstruct(responses, 'responses')
        assert out2 is not None
        self.assertEqual(out2['text'], 'ok')
        self.assertEqual(out2['tool_calls'][0]['arguments'], '{}')

    def test_client_reconstruct_tolerates_empty(self) -> None:
        self.assertIsNone(tap.client_reconstruct(None, 'anthropic'))
        self.assertIsNone(tap.client_reconstruct('', 'responses'))

    def test_client_family_reads_dsh_and_unknown_ua(self) -> None:
        """UA 里写着是什么客户端，就不该显示成「其它」。

        实测反馈：`deepseek-harness/0.2.2-rc.1` 的 UA 被归成 `other`，界面上只剩
        一个「其它」——UA 明明写着名字。规则里补了 dsh，并且**认不出来的 UA 回落
        成它的 product token**，而不是一律 `other`（新客户端不必改代码就能显示）。
        """
        ua = 'deepseek-harness/0.2.2-rc.1 (+https://github.com/deepseek-ai/deepseek-harness)'
        self.assertEqual(tap._client_family(ua), 'dsh')
        self.assertEqual(tap._client_version(ua), '0.2.2-rc.1')
        # 未收录的客户端：回落成小写的 product token
        self.assertEqual(tap._client_family('my-agent/1.0.3'), 'my-agent')
        self.assertEqual(tap._client_version('my-agent/1.0.3'), '1.0.3')
        # 空 UA 才记 unknown
        self.assertEqual(tap._client_family(''), 'unknown')

    def test_client_family_agrees_with_the_logs_channel(self) -> None:
        """轨迹页与日志页对同一批 UA 必须给出一致的结论。

        这两处各有一份 UA 归类（日志页要 `codex / vscode / api…` 这套词表，
        轨迹页要 `claude / dsh / cli…`），但**认不认得出同一个客户端**必须一致 ——
        实测就是这样翻车的：日志页早已认识 `deepseek-harness`（记成 dsh），
        轨迹页那张表没有它，同一批请求在日志页显示 dsh、在轨迹页显示「其它」。
        这条守卫专门抓这种「两套词表各长一半」的分叉。
        """
        from server.routers import gateway

        samples = [
            'deepseek-harness/0.2.2-rc.1 (+https://github.com/deepseek-ai/deepseek-harness)',
            'claude-cli/2.0.1 (external, cli)',
            'codex_cli_rs/0.9.0',
            'curl/8.4.0',
        ]
        for ua in samples:
            tap_family = tap._client_family(ua)
            logs_channel = gateway.client_channel(ua)
            self.assertNotIn(tap_family, ('other', 'unknown'), f'轨迹页认不出：{ua}')
            self.assertNotIn(logs_channel, ('other', 'unknown'), f'日志页认不出：{ua}')

    def test_client_family(self) -> None:
        self.assertEqual(tap._client_family('claude-code/1.0'), 'claude')
        self.assertEqual(tap._client_family('codex_cli_rs/0.9'), 'codex')
        self.assertEqual(tap._client_family('curl/8.4.0'), 'cli')
        self.assertEqual(tap._client_family(None), 'unknown')


class DbTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = d / 'tap.db'
        config.USERS_FILE = d / 'users.json'
        # STATIC_DIR 要**真的存在**（含 index.html 与 _next/）：main.py 只在
        # import 时刻看这个目录挂 SPA 兜底，而整个测试进程只 import 一次 ——
        # 若这里给一个不存在的目录，后面任何依赖兜底路由的测试
        # （如 gateway_error_contract 的「404 回 JSON 而不是 HTML」）拿到的就是
        # 没有兜底的 app，404 变成 405（路径只有 POST，GET 撞 Method 检查）。
        static = d / 'static'
        (static / '_next').mkdir(parents=True)
        (static / 'index.html').write_text('<html>panel</html>', encoding='utf-8')
        config.STATIC_DIR = static
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'T' * 64,
            'users': [
                {'username': 'admin', 'role': 'admin',
                 'pwd_hash': security.make_hash('admin-pw')},
                {'username': 'viewer', 'role': 'viewer',
                 'pwd_hash': security.make_hash('viewer-pw')},
            ],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()
        from server.main import app
        from fastapi.testclient import TestClient
        cls.c = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = cls._orig
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def setUp(self) -> None:
        security._fail.clear()
        security._user_fail.clear()
        self.c.cookies.clear()
        tap.clear()
        tap.save_settings({'enabled': False})
        db.set_setting(tap.SETTING_KEY, {'enabled': False})

    def tearDown(self) -> None:
        self.c.cookies.clear()

    @contextmanager
    def _as(self, username: str, password: str):
        r = self.c.post('/api/login', json={'username': username, 'password': password})
        self.assertEqual(r.status_code, 200, r.text)
        try:
            yield self.c
        finally:
            self.c.cookies.clear()

    def _capture(self, **kwargs) -> int | None:
        base = dict(
            body=_body(), headers=[('authorization', 'Bearer sk-live1234567890')],
            upstream_headers={'Content-Type': 'application/json',
                              'Authorization': 'Bearer sk-upstream-9999'},
            key={'id': 1}, ua='claude-code/1.0', path='/v1/chat/completions',
            protocol='chat', status=200, latency_ms=100, stream=True,
            text=SSE, usage={'prompt_tokens': 11, 'completion_tokens': 7},
        )
        base.update(kwargs)
        return tap.capture(**base)

    # ── 服务层 ───────────────────────────────────────────
    def test_disabled_writes_nothing(self) -> None:
        self.assertIsNone(self._capture())
        self.assertEqual(tap.stats()['turns'], 0)

    def test_capture_groups_turns_and_increments_seq(self) -> None:
        tap.save_settings({'enabled': True})
        self.assertIsNotNone(self._capture())
        self._capture(body=_body(messages=BODY['messages'] + [
            {'role': 'user', 'content': 'step 2'}]))
        session = tap.list_sessions()['items'][0]
        self.assertEqual(session['turns'], 2)
        detail = tap.session(session['id'])
        assert detail is not None
        self.assertEqual([t['seq'] for t in detail['items']], [1, 2])
        # 另一句话 → 另一次任务
        self._capture(body=_body(messages=[BODY['messages'][0],
                                          {'role': 'user', 'content': '别的任务'}]))
        self.assertEqual(tap.list_sessions()['total'], 2)

    def test_capture_marks_truncation(self) -> None:
        tap.save_settings({'enabled': True, 'max_body': 4096})
        big = _body(messages=BODY['messages'] + [
            {'role': 'user', 'content': 'x' * 20000}])
        turn_id = self._capture(body=big)
        assert turn_id is not None
        view = tap.turn_view(turn_id)
        assert view is not None
        self.assertTrue(view['truncated'])
        self.assertFalse(view['request_ok'], '被截断的 JSON 不该被当成完整结构')

    def test_capture_never_raises(self) -> None:
        """形状再怪的输入、再离谱的内部故障，都不能把异常抛回请求路径。"""
        tap.save_settings({'enabled': True})
        # 1) 奇怪但合法的输入：对象、空头、缺 usage —— 不抛即可（存成什么样不重要）
        tap.capture(body=object(), headers=None, key=None, ua=None, path='',
                    protocol='chat', status=0, latency_ms=None, stream=False,
                    payload=None, usage=None)
        # 2) 库写入失败：必须被吞掉并返回 None（请求已经发给客户端了）
        import unittest.mock as mock
        with mock.patch.object(db, 'execute', side_effect=RuntimeError('disk full')):
            self.assertIsNone(self._capture())

    def test_secret_never_reaches_storage(self) -> None:
        tap.save_settings({'enabled': True})
        turn_id = self._capture(headers=[('authorization', 'Bearer sk-live1234567890')])
        row = db.query_one('SELECT request_json FROM tap_turns WHERE id = ?', (turn_id,))
        assert row is not None
        self.assertNotIn('sk-live1234567890', row['request_json'])

    def test_capture_keeps_both_sides_of_the_request(self) -> None:
        """客户端原样的体与网关改写后的体必须分开存。

        这一条守的是最容易被写坏的地方：`body` 在网关里是**就地改写**的（模型
        映射、max_tokens 钳制、图片归一、stream_options 注入）。如果调用方忘了
        在改写前快照，抓包页的「客户端视角」就会显示成网关改写后的样子 ——
        界面上看起来一切正常，只是那份证据永远拿不回来了。
        """
        tap.save_settings({'enabled': True})
        original = _body(model='gpt-4o', temperature=0.2)
        processed = _body(model='cn:gpt-4o', temperature=0.2,
                          stream_options={'include_usage': True})
        turn_id = self._capture(body=original, upstream_body=processed, stream=False)
        assert turn_id is not None
        view = tap.turn_view(turn_id)
        assert view is not None
        req = view['request']
        self.assertEqual(req['body']['model'], 'gpt-4o', '客户端视角必须是原样')
        self.assertEqual(req['upstream_body']['model'], 'cn:gpt-4o', '网关视角必须是改写后')

    def test_capture_keeps_both_header_sets_masked(self) -> None:
        """客户端请求头与「网关发往上游」的请求头都要留，且都要打码。

        两边的 Authorization 是**不同的凭证**（客户端拿的是本面板密钥，上游那条
        是上游 api_key）。只留一份时，「凭证换了没换 / 打错账号池」这类问题在
        界面上完全看不出来 —— 而这正是 401 排查的第一现场。
        """
        tap.save_settings({'enabled': True})
        turn_id = self._capture(
            headers=[('authorization', 'Bearer wbk_client_abcdef123456'),
                     ('content-type', 'application/json')],
            upstream_headers={'Content-Type': 'application/json',
                              'Authorization': 'Bearer sk-upstream-abcdef123456'},
        )
        assert turn_id is not None
        view = tap.turn_view(turn_id)
        assert view is not None
        req = view['request']
        client_auth = req['headers']['authorization']
        upstream_auth = req['upstream_headers']['Authorization']
        self.assertIn('***', client_auth)
        self.assertIn('***', upstream_auth)
        # 谁也没被对方覆盖：两套头分别可见、前缀不同
        self.assertTrue(client_auth.startswith('Bearer'))
        self.assertTrue(upstream_auth.startswith('Bearer'))
        self.assertNotIn('abcdef123456', client_auth)
        self.assertNotIn('abcdef123456', upstream_auth)
        self.assertEqual(req['upstream_headers']['Content-Type'], 'application/json')

    def test_capture_stores_client_response_for_translators(self) -> None:
        """Anthropic / Responses 的「客户端视角」是翻译后的报文，不能等同上游原文。"""
        tap.save_settings({'enabled': True})
        client_doc = {'content': [{'type': 'text', 'text': '你好'}],
                      'stop_reason': 'end_turn'}
        turn_id = self._capture(protocol='anthropic', stream=False,
                                payload={'choices': [{'message': {'content': '你好'}}]},
                                client_response=client_doc)
        assert turn_id is not None
        view = tap.turn_view(turn_id)
        assert view is not None
        self.assertFalse(view['passthrough'])
        self.assertEqual(view['client_response']['content'][0]['text'], '你好')
        self.assertEqual(view['client_recon']['text'], '你好')

    def test_chat_is_marked_passthrough(self) -> None:
        """Chat 是透传：不重复存一份，界面上标注「与上游一致」。"""
        tap.save_settings({'enabled': True})
        turn_id = self._capture()  # 默认 protocol='chat'，未传 client_response
        view = tap.turn_view(turn_id)
        assert view is not None
        self.assertTrue(view['passthrough'])
        self.assertEqual(view['client_response'], view['response'])

    def test_session_keeps_raw_ua_and_backfill_fixes_old_rows(self) -> None:
        """`client` 是从 UA 派生的；规则变了要能把旧记录重算，而不是永远错着。"""
        tap.save_settings({'enabled': True})
        self._capture(
            ua='deepseek-harness/0.2.2-rc.1 (+https://x)',
            # 回填走的是「从入库的请求头里把 UA 读回来」这条路（旧记录没有
            # client_ua 列），所以这里必须真的把 UA 存进请求头。
            headers=[('authorization', 'Bearer wbk_test1234567890'),
                     ('user-agent', 'deepseek-harness/0.2.2-rc.1 (+https://x)')],
        )
        session = tap.list_sessions()['items'][0]
        self.assertEqual(session['client'], 'dsh')
        self.assertIn('deepseek-harness', session['client_ua'] or '')
        # 模拟旧记录：分类写成 other、UA 留空
        db.execute('UPDATE tap_sessions SET client = ?, client_ua = NULL WHERE id = ?',
                   ('other', session['id']))
        self.assertEqual(tap.backfill_clients(), 1)
        fixed = tap.list_sessions()['items'][0]
        self.assertEqual(fixed['client'], 'dsh')

    def test_title_call_is_aux_and_linked_to_its_session(self) -> None:
        """会话标题生成是**辅助调用**，必须挂回主会话，而不是单列成第二条记录。

        实测现场：dsh 每轮开始时发一次标题请求（system 是 "Create a concise title…"、
        无工具、237 token），它的 system 与主循环完全不同 → 指纹不同 → 早先被记成
        独立会话，看起来像「一次对话记了两条」。
        """
        tap.save_settings({'enabled': True})
        main_turn = self._capture()  # 主会话首轮（首条 user = "fix the bug in foo.py"）
        assert main_turn is not None
        main_session = tap.turn_view(main_turn)['session_id']

        title_body = {
            'model': 'deepseek-v4.1-flash',
            'messages': [
                {'role': 'system',
                 'content': 'Create a concise title for an AI coding-assistant session '
                            'from the supplied human messages.'},
                {'role': 'user',
                 'content': 'Generate the session title from this JSON array of human '
                            'messages: [{"seq":1,"text":"fix the bug in foo.py"}]'},
            ],
            'stream': True,
        }
        self.assertEqual(tap._aux_kind(title_body), 'title')
        self._capture(body=title_body)

        # 默认列表只显示主会话（辅助调用不单列）
        self.assertEqual(tap.list_sessions()['total'], 1)
        # 打开 include_aux 才看得到，并且它挂回了主会话
        items = tap.list_sessions(include_aux=True)['items']
        self.assertEqual(len(items), 2)
        aux = next(s for s in items if s['kind'] != 'main')
        self.assertEqual(aux['kind'], 'title')
        self.assertEqual(aux['parent_id'], main_session, '辅助调用没有挂回主会话')

        # 主会话要能看到「我名下挂了 1 次辅助调用」，详情里也能直接列出来
        listed = next(s for s in items if s['kind'] == 'main')
        self.assertEqual(listed['aux_count'], 1)
        detail = tap.session(main_session)
        assert detail is not None
        self.assertEqual(len(detail['aux']), 1)
        self.assertEqual(detail['aux'][0]['id'], aux['id'])
        self.assertEqual(detail['aux'][0]['kind'], 'title')

    def test_aux_link_survives_escaped_newlines(self) -> None:
        """标题请求里的文本是 JSON 序列化过的（字面量 \\n），主会话存的是真换行。

        不归一化就永远对不上：实测那条 `session-15fbe…` 的标题请求正是这种形态，
        结果辅助调用挂不上父会话，在列表里看起来又像"两条记录"。
        """
        tap.save_settings({'enabled': True})
        main_turn = self._capture()  # 首条 user = "fix the bug in foo.py"
        assert main_turn is not None
        main_session = tap.turn_view(main_turn)['session_id']

        title_body = {
            'messages': [
                {'role': 'system',
                 'content': 'Create a concise title for an AI coding-assistant session '
                            'from the supplied human messages.'},
                # 注意是字面量 \\n，不是真换行
                {'role': 'user',
                 'content': 'Generate the session title from this JSON array of human '
                            'messages:\\n[{"seq":1,"text":"fix the bug\\nin foo.py"}]'},
            ],
        }
        self._capture(body=title_body)
        aux = next(s for s in tap.list_sessions(include_aux=True)['items']
                   if s['kind'] != 'main')
        self.assertEqual(aux['parent_id'], main_session)

    def test_short_first_message_still_links(self) -> None:
        """首条消息很短（15 字以内的中文）也必须能挂上 —— 早期下限卡在 16 字时漏了。"""
        tap.save_settings({'enabled': True})
        short = '验证辅助调用归属：这条是主会话'
        main_turn = self._capture(body=_body(messages=[
            BODY['messages'][0], {'role': 'user', 'content': short}]))
        assert main_turn is not None
        main_session = tap.turn_view(main_turn)['session_id']
        self._capture(body={'messages': [
            {'role': 'system',
             'content': 'Create a concise title for an AI coding-assistant session.'},
            {'role': 'user',
             'content': f'Generate the session title from this JSON array: [{{"text":"{short}"}}]'},
        ]})
        aux = next(s for s in tap.list_sessions(include_aux=True)['items']
                   if s['kind'] != 'main')
        self.assertEqual(aux['parent_id'], main_session)

    def test_aux_link_matches_truncated_human_message(self) -> None:
        """标题请求里的人类消息可能被客户端**截断**（只保留首句）。

        只比「整条前缀」会落空：主会话的首条 user 是长段落，标题请求里只带了开头
        一句话。所以归属要由长到短试若干前缀（下限见 `_link_parent`，短消息也覆盖）。
        """
        tap.save_settings({'enabled': True})
        long_text = ('http://<面板地址>/settings/backup/ 帮我看下如何备份和还原 '
                     'workbuddy-manager 的数据，以及账号数据是否已经持久化在容器外。'
                     '如果持久化了，重建容器能不能恢复已经添加的账户。')
        main_body = _body(messages=[BODY['messages'][0],
                                    {'role': 'user', 'content': long_text}])
        main_turn = self._capture(body=main_body)
        assert main_turn is not None
        main_session = tap.turn_view(main_turn)['session_id']

        truncated = long_text[:40]  # 客户端只带了开头一句
        title_body = {
            'messages': [
                {'role': 'system',
                 'content': 'Create a concise title for an AI coding-assistant session '
                            'from the supplied human messages.'},
                {'role': 'user',
                 'content': 'Generate the session title from this JSON array of human '
                            'messages: [{"seq":1,"text":"' + truncated + '"}]'},
            ],
        }
        self._capture(body=title_body)
        aux = next(s for s in tap.list_sessions(include_aux=True)['items']
                   if s['kind'] != 'main')
        self.assertEqual(aux['parent_id'], main_session, '截断的人类消息也要能挂回主会话')

    def test_compaction_is_aux_but_normal_summary_is_not(self) -> None:
        # 压缩（compaction）同样是客户端在会话中途发的独立请求；而「帮我总结这篇
        # 文章」是正常对话，不能因为出现 summar* 就被当成辅助调用。
        compact = {'messages': [
            {'role': 'system',
             'content': 'You are performing a compaction of the conversation so far.'},
            {'role': 'user', 'content': 'Summarize the conversation so far.'},
        ]}
        self.assertEqual(tap._aux_kind(compact), 'compact')
        normal = {'messages': [
            {'role': 'system', 'content': 'You are a helpful assistant.'},
            {'role': 'user', 'content': '帮我总结这篇关于猫的文章'},
        ]}
        self.assertEqual(tap._aux_kind(normal), 'main')

    def test_truncated_turns_diff_uses_digest(self) -> None:
        """截断的两轮也必须能比出差异（不能永远是「完全一致」）。

        实测现场：请求体 2 MB、单条上限 512 KB → 两轮都切在**同一字节位置**，
        抢救出的前缀逐字相同，比前缀必然说「一致」——那是假信息。摘要在截断
        **之前**算（逐条消息的角色 + 内容哈希 + 工具名），与截断无关。
        """
        tap.save_settings({'enabled': True, 'max_body': 4096})
        big = 'x' * 20000
        body_a = _body(messages=[BODY['messages'][0],
                                   {'role': 'user', 'content': big}])
        body_b = _body(messages=[BODY['messages'][0],
                                   {'role': 'user', 'content': big},
                                   {'role': 'assistant', 'content': 'done'}])
        a = self._capture(body=body_a, stream=False)
        b = self._capture(body=body_b, stream=False)
        assert a is not None and b is not None
        self.assertTrue(tap.turn(a)['truncated'], '这一轮应当被截断（测试前提）')
        d = tap.diff(a, b)
        assert d is not None
        self.assertTrue(d.get('from_digest'), '截断场景应当走摘要比较')
        self.assertGreaterEqual(d['summary']['added'], 1,
                                '摘要比较应发现新增的那条消息')

    def test_salvage_prefix_cuts_outside_strings(self) -> None:
        """截断发生在**字符串中间**时也要能抢救出可解析的前缀。

        实测：minimax 那条 512 KB 记录，截断点落在 `"name": "bas` 里 ——
        旧实现按「最后一个逗号」切，切出来的前缀里有个未闭合的字符串，
        JSON.parse 必然失败 → 界面上请求体**整个空白**。
        """
        raw = ('{"headers": {"a": "b"}, "body": {"model": "m", "messages": ['
               '{"role": "user", "content": "hi"}, {"name": "bas')
        got = tap.salvage_json_prefix(raw)
        assert got is not None, '应当抢救出前缀'
        # 返回的是 **body 段**的内容（不是含 body 键的外层）
        assert isinstance(got, dict)
        assert got.get('model') == 'm'
        # 截断前完整的那个元素应当在里面（截断点在它之后）
        assert len(got.get('messages') or []) >= 1

    def test_prune_respects_max_sessions(self) -> None:
        tap.save_settings({'enabled': True, 'max_sessions': 10})
        for i in range(14):
            self._capture(body=_body(messages=[BODY['messages'][0],
                                             {'role': 'user', 'content': f'task {i}'}]))
        tap.prune()
        self.assertLessEqual(tap.list_sessions()['total'], 10)

    # ── 接口 ─────────────────────────────────────────────
    def test_api_requires_login(self) -> None:
        r = self.c.get('/api/tap/sessions')
        self.assertIn(r.status_code, (401, 403))

    def test_api_config_toggle_and_flow(self) -> None:
        with self._as('admin', 'admin-pw') as c:
            cfg = c.get('/api/tap/config')
            self.assertEqual(cfg.status_code, 200)
            self.assertFalse(cfg.json()['config']['enabled'])
            r = c.put('/api/tap/config', json={'enabled': True, 'retention_days': 3})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()['config']['enabled'])
            self.assertEqual(r.json()['config']['retention_days'], 3)

        # 开关打开后，网关路径上的 capture 才会写
        self._capture()
        with self._as('admin', 'admin-pw') as c:
            listed = c.get('/api/tap/sessions').json()
            self.assertEqual(listed['total'], 1)
            sid = listed['items'][0]['id']
            detail = c.get(f'/api/tap/sessions/{sid}').json()
            self.assertEqual(len(detail['items']), 1)
            turn_id = detail['items'][0]['id']
            view = c.get(f'/api/tap/turns/{turn_id}').json()
            self.assertEqual(view['recon']['text'], 'Hello')
            diff = c.get(f'/api/tap/diff?before={turn_id}&after={turn_id}').json()
            self.assertEqual(diff['summary']['added'], 0)
            export = c.get(f'/api/tap/sessions/{sid}/export')
            self.assertEqual(export.status_code, 200)
            self.assertIn('attachment', export.headers['content-disposition'])
            self.assertIn('Hello', export.text)
            self.assertEqual(c.delete(f'/api/tap/sessions/{sid}').status_code, 200)
            self.assertEqual(c.get('/api/tap/sessions').json()['total'], 0)

    def test_viewer_can_read_switch_but_not_conversations(self) -> None:
        tap.save_settings({'enabled': True})
        self._capture()
        with self._as('viewer', 'viewer-pw') as c:
            self.assertEqual(c.get('/api/tap/config').status_code, 200)
            self.assertEqual(c.get('/api/tap/sessions').status_code, 403)
            self.assertEqual(c.put('/api/tap/config', json={'enabled': False}).status_code, 403)

    def test_missing_turn_is_404(self) -> None:
        with self._as('admin', 'admin-pw') as c:
            self.assertEqual(c.get('/api/tap/turns/999999').status_code, 404)
            self.assertEqual(c.get('/api/tap/sessions/999999').status_code, 404)


if __name__ == '__main__':
    unittest.main()


class HotPathCostTest(unittest.TestCase):
    """抓包关闭时不得给转发路径加成本（回归：无条件 deepcopy / 无条件缓冲）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'hot.db'
        db._conn = None
        db.connect()
        db.set_setting(tap.SETTING_KEY, {'enabled': False})

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig
        db.set_setting(tap.SETTING_KEY, {'enabled': False})
        self._tmp.cleanup()

    def test_snapshot_returns_same_object_when_disabled(self) -> None:
        body = {'model': 'x', 'messages': [{'role': 'user', 'content': 'hi' * 1000}]}
        self.assertIs(tap.snapshot(body), body,
                      '抓包关闭时必须原样返回，不做深拷贝')

    def test_snapshot_copies_when_enabled(self) -> None:
        db.set_setting(tap.SETTING_KEY, {'enabled': True})
        body = {'model': 'x', 'messages': []}
        copy_obj = tap.snapshot(body)
        self.assertIsNot(copy_obj, body)
        self.assertEqual(copy_obj, body)
        copy_obj['model'] = 'changed'
        self.assertEqual(body['model'], 'x', '快照必须是独立副本')

    def test_raw_stream_ignores_feed_when_disabled(self) -> None:
        stream = tap.RawStream()
        for _ in range(100):
            stream.feed('data: ' + 'x' * 1000)
        self.assertEqual(stream.kept, 0, '关闭抓包时流缓冲必须整体失效')
        self.assertEqual(stream.text(), '')

    def test_raw_stream_collects_when_enabled(self) -> None:
        db.set_setting(tap.SETTING_KEY, {'enabled': True})
        stream = tap.RawStream()
        stream.feed('data: hi\n')
        self.assertGreater(stream.kept, 0)
        self.assertIn('data: hi', stream.text())
