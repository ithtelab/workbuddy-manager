"""Probes must observe model output, not heartbeat traffic or error frames."""
import asyncio
from contextlib import asynccontextmanager
import json
import unittest
from unittest import mock

import httpx

from server.services import tencent


class Response:
    def __init__(self, *chunks):
        self.chunks = chunks

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk


def frame(data):
    return ('data: ' + json.dumps(data, ensure_ascii=False) + '\n\n').encode()


class ProbeStreamEvidenceTest(unittest.TestCase):
    def read(self, *chunks):
        return asyncio.run(tencent._probe_stream_result(Response(*chunks)))

    def test_heartbeat_is_not_success(self):
        ok, msg = self.read(b': heartbeat\r\n\r\n')
        self.assertFalse(ok)
        self.assertIn('未返回模型输出', msg)

    def test_empty_role_then_error_reports_error(self):
        ok, detail = self.read(
            b': heartbeat\n\n',
            frame({'choices': [{'delta': {'role': 'assistant', 'content': ''}}]}),
            frame({'error': {'code': 6004, 'message': 'model usage limit exceeded'}}),
        )
        self.assertFalse(ok)
        self.assertEqual(tencent._parse_error_body(detail, 200), (6004, 'model usage limit exceeded'))

    def test_split_utf8_output_after_heartbeat(self):
        data = frame({'choices': [{'delta': {'content': '好'}}]})
        cut = data.index('好'.encode()) + 1
        self.assertTrue(self.read(b': heartbeat\n\n', data[:cut], data[cut:])[0])

    def test_reasoning_is_model_output(self):
        self.assertTrue(self.read(frame({'choices': [{'delta': {'reasoning_content': 'pong'}}]}))[0])

    def test_empty_role_and_done_is_failure(self):
        self.assertFalse(self.read(
            frame({'choices': [{'delta': {'role': 'assistant'}}]}),
            b'data: [DONE]\n\n',
        )[0])

    def test_json_error_under_http_200(self):
        ok, detail = self.read(b'{"code":11102,"msg":"model service info not found"}')
        self.assertFalse(ok)
        self.assertEqual(json.loads(detail)['code'], 11102)

    def test_eof_with_only_role_is_failure(self):
        self.assertFalse(self.read(frame({'choices': [{'delta': {'role': 'assistant'}}]}))[0])

    def test_empty_function_placeholder_does_not_hide_later_error(self):
        ok, detail = self.read(
            frame({'choices': [{'delta': {'function_call': {'name': '', 'arguments': ''}}}]}),
            frame({'error': 'request rejected'}),
        )
        self.assertFalse(ok)
        self.assertEqual(tencent._parse_error_body(detail, 200), (200, 'request rejected'))

    def test_tool_output_is_valid_model_evidence(self):
        self.assertTrue(self.read(frame({'choices': [{'delta': {
            'tool_calls': [{'function': {'name': 'get_time'}}],
        }}]}))[0])

    def test_pretty_printed_json_error_is_preserved(self):
        data = json.dumps({'code': 11102, 'msg': 'model not found'}, indent=2).encode()
        ok, detail = self.read(data[:10], data[10:])
        self.assertFalse(ok)
        self.assertEqual(tencent._parse_error_body(detail, 200), (11102, 'model not found'))

    def test_multiline_sse_and_split_crlf(self):
        data = b'data: {"choices":\r\ndata: [{"delta":{"content":"OK"}}]}\r\n\r\n'
        cut = data.index(b'\r') + 1
        self.assertTrue(self.read(data[:cut], data[cut:])[0])

    def test_final_event_without_blank_line(self):
        self.assertTrue(self.read(b'data: {"choices":[{"delta":{"content":"OK"}}]}')[0])

    def test_multiline_event_size_is_bounded(self):
        line = b'data: ' + b'x' * (tencent._PROBE_EVENT_LIMIT // 2) + b'\n'
        ok, detail = self.read(line, line)
        self.assertFalse(ok)
        self.assertIn('过大', detail)

    def test_invalid_frame_is_not_success(self):
        self.assertFalse(self.read(b'data: not-json\n\n')[0])

    def test_http_200_channel_error_uses_same_fallback_as_http_400(self):
        denied = frame({'code': 11128, 'msg': 'request illegal',
                        'displayTips': {'en': 'channel verification failed'}})
        bodies = [denied, frame({'choices': [{'delta': {'content': 'OK'}}]})]
        seen = []

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            @asynccontextmanager
            async def stream(self, method, url, **kwargs):
                seen.append(url)
                yield httpx.Response(200, content=bodies[len(seen) - 1])

        with mock.patch.object(tencent, '_account_http_client', return_value=Client()):
            ok, detail = asyncio.run(tencent.probe_account({'access_token': 'TEST', 'realm': 'cn'}))
        self.assertTrue(ok, detail)
        self.assertEqual(len(seen), 2)
        self.assertIn('/console/', seen[1])

    def test_probe_times_out_even_if_heartbeats_continue(self):
        class EndlessResponse:
            status_code = 200

            async def aiter_bytes(self):
                while True:
                    yield b': heartbeat\n\n'
                    await asyncio.sleep(0.001)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class Client(EndlessResponse):
            def stream(self, *args, **kwargs):
                return EndlessResponse()

        with mock.patch.object(tencent, '_account_http_client', return_value=Client()), \
                mock.patch.object(tencent.config, 'TENCENT_TIMEOUT', 0.02):
            ok, detail = asyncio.run(tencent.probe_account({'access_token': 'TEST', 'realm': 'cn'}))
        self.assertFalse(ok)
        self.assertIn('探测超时', detail)


if __name__ == '__main__':
    unittest.main()
