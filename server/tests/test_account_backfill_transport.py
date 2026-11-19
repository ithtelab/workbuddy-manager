"""三种公开 API 在建连前失败不应期待账号；读超时仍可能已使用账号。"""
from __future__ import annotations

import asyncio
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

import httpx

from server import config, db, upstreamsvc
from server.routers import anthropic, gateway, responses


class AccountTransportTest(unittest.TestCase):
    def test_error_classification_preserves_uncertain_sent_requests(self) -> None:
        for kind in (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            with self.subTest(kind=kind.__name__):
                self.assertFalse(gateway._account_expected_after_error(kind('synthetic failure')))
        for kind in (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError,
                     httpx.RemoteProtocolError, RuntimeError):
            with self.subTest(kind=kind.__name__):
                self.assertTrue(gateway._account_expected_after_error(kind('synthetic failure')))

    def exercise(self, protocol: str, stream: bool, error_type):
        class FailingClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, *args, **kwargs):
                raise error_type('synthetic transport failure')

            def build_request(self, *args, **kwargs):
                return httpx.Request('POST', 'http://example.invalid/v1/chat/completions')

            async def send(self, *args, **kwargs):
                raise error_type('synthetic transport failure')

            async def aclose(self):
                pass

        body = {'model': 'model-a', 'stream': stream, 'messages': [{'role': 'user', 'content': 'test'}],
                'input': 'test', 'max_tokens': 16}
        request = SimpleNamespace(headers={})
        with ExitStack() as patches:
            patches.enter_context(mock.patch.object(gateway, '_read_json_body',
                new=mock.AsyncMock(return_value=(body, None))))
            patches.enter_context(mock.patch.object(gateway, '_map_model', return_value='model-a'))
            patches.enter_context(mock.patch.object(gateway, '_authorize',
                return_value=(None, '127.0.0.1', None)))
            patches.enter_context(mock.patch.object(anthropic, '_authorize',
                return_value=(None, '127.0.0.1', None)))
            patches.enter_context(mock.patch.object(gateway, '_upstream_for',
                return_value=({'base_url': 'http://example.invalid'}, None)))
            patches.enter_context(mock.patch.object(upstreamsvc, 'resolve_for_key',
                return_value={'base_url': 'http://example.invalid'}))
            patches.enter_context(mock.patch.object(gateway, '_upstream_headers', return_value={}))
            patches.enter_context(mock.patch.object(config, 'http_client', return_value=FailingClient()))
            add = patches.enter_context(mock.patch.object(db, 'add_request_log'))
            if protocol == 'chat':
                response = asyncio.run(gateway._chat(request, '/v1/chat/completions'))
            elif protocol == 'responses':
                response = asyncio.run(responses._handle(request))
            else:
                response = asyncio.run(anthropic.messages(request))
        self.assertEqual(response.status_code, 502)
        self.assertEqual(add.call_count, 1)
        return add.call_args.kwargs['account_expected']

    def test_all_protocols_and_stream_modes_classify_actual_error_paths(self) -> None:
        for protocol in ('chat', 'responses', 'anthropic'):
            for stream in (False, True):
                for error_type, expected in ((httpx.ConnectError, 0), (httpx.ConnectTimeout, 0),
                                             (httpx.PoolTimeout, 0), (httpx.ReadTimeout, 1)):
                    with self.subTest(protocol=protocol, stream=stream, error=error_type.__name__):
                        self.assertEqual(self.exercise(protocol, stream, error_type), expected)
