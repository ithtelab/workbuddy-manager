from contextlib import asynccontextmanager
import unittest
from unittest.mock import patch

from server.services import tencent


class ProbeStreamValidationTests(unittest.IsolatedAsyncioTestCase):
    async def probe(self, chunks):
        class Response:
            status_code = 200
            async def aiter_bytes(self):
                for chunk in chunks:
                    yield chunk
        class Client:
            @asynccontextmanager
            async def stream(self, *args, **kwargs):
                yield Response()
        @asynccontextmanager
        async def client(*args, **kwargs):
            yield Client()
        with patch.object(tencent, '_account_http_client', client):
            return await tencent.probe_account({'access_token': 'test', 'uid': 'u1', 'realm': 'cn'}, 'deepseek-v4.1-flash')

    async def test_role_preview_does_not_count_as_output(self):
        ok, _ = await self.probe([b': heartbeat\n\ndata: {"choices":[{"delta":{"role":"assistant"}}]}\n\ndata: [DONE]\n\n'])
        self.assertFalse(ok)

    async def test_split_error_frame_remains_rate_limited(self):
        ok, message = await self.probe([b'data: {"error":{"code":60', b'04,"message":"rate limit"}}\n\n'])
        self.assertFalse(ok)
        self.assertEqual(tencent.classify_probe_result(ok, message)[0], 'rate')

    async def test_split_valid_output_is_usable(self):
        ok, _ = await self.probe([b'data: {"choices":[{"delta":', b'{"content":"OK"}}]}\n\n'])
        self.assertTrue(ok)
