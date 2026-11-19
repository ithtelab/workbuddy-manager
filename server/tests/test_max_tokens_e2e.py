"""端到端：越界 max_tokens 不得原样出现在发往上游的请求体里。

为什么需要端到端而不是只测钳制函数
----------------------------------
`clamp_max_tokens` 是纯函数，单测能证明它自己正确，但证明不了**三个入口都真的
调了它**。漏接一个入口（`/v1/chat/completions` / `/v1/messages` / `/v1/responses`）
就等于防护缺失，而这类漏接在纯函数测试里完全看不出来。

所以这里用假的 httpx 传输层截获**真正要发往上游的字节**，断言落在网线上的值。
这是本 bug 唯一的判据：上游 int32 拒收的是**线上那个值**，不是入参那个。
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import config, db, keysvc  # noqa: E402
from server.routers import gateway  # noqa: E402

# 上游成功响应（只求走到转发那一步，内容无关）
_OK = {
    'id': 'x', 'object': 'chat.completion', 'created': 0, 'model': 'm',
    'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'ok'},
                 'finish_reason': 'stop'}],
    'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
}


class SentToUpstreamTest(unittest.TestCase):
    """截获发往上游的请求体，检查 max_tokens 的实际取值。

    环境搭建照抄 test_anthropic_messages（临时 DB + 临时 users.json），
    避免碰到真实数据库。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls._dir = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = cls._dir / 'mt.db'
        config.USERS_FILE = cls._dir / 'users.json'
        config.STATIC_DIR = cls._dir / 'no-static'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': 'x'}],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()
        cls.key = keysvc.create_key(name='max-tokens-clamp-test')['key']

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

    def _sent_body(self, path: str, body: dict) -> dict:
        """走一次真实网关请求，返回**发往上游**的那份 JSON。"""
        captured: dict = {}

        def respond(request: httpx.Request) -> httpx.Response:
            if request.method == 'POST':
                captured.update(json.loads(request.content.decode()))
                return httpx.Response(200, json=_OK)
            return httpx.Response(200, json={'data': []})

        def client(*args, **kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(respond))

        from starlette.testclient import TestClient
        from server.main import app

        with patch.object(config, 'http_client', side_effect=client):
            with TestClient(app) as c:
                c.post(path, json=body,
                       headers={'Authorization': f'Bearer {self.key}'})
        return captured

    def test_chat_completions_clamps_on_the_wire(self) -> None:
        """/v1/chat/completions：99.99 亿不得出现在发往上游的报文里。"""
        sent = self._sent_body('/v1/chat/completions', {
            'model': 'm',
            'messages': [{'role': 'user', 'content': 'hi'}],
            'max_tokens': 9999969112,
        })
        self.assertEqual(sent.get('max_tokens'), gateway.MAX_TOKENS_CEILING)
        self.assertLessEqual(sent.get('max_tokens', 0), 2147483647)

    def test_chat_completions_normal_value_untouched(self) -> None:
        """正常值必须原样到达上游——钳制不能改变既有请求语义。"""
        sent = self._sent_body('/v1/chat/completions', {
            'model': 'm',
            'messages': [{'role': 'user', 'content': 'hi'}],
            'max_tokens': 4096,
        })
        self.assertEqual(sent.get('max_tokens'), 4096)

    def test_max_completion_tokens_clamps_on_the_wire(self) -> None:
        """OpenAI 新字段名 max_completion_tokens：同样不得原样到达上游。

        这是端到端才有意义的用例——纯函数测试证明不了 `_chat` 真的对所有字段
        都调用了钳制（例如只钳了 max_tokens 而漏掉新名字）。
        """
        sent = self._sent_body('/v1/chat/completions', {
            'model': 'm',
            'messages': [{'role': 'user', 'content': 'hi'}],
            'max_completion_tokens': 9999969112,
        })
        self.assertEqual(sent.get('max_completion_tokens'),
                         gateway.MAX_TOKENS_CEILING)

    def test_responses_clamps_on_the_wire(self) -> None:
        """/v1/responses 用的是 max_output_tokens，同样要拦。"""
        sent = self._sent_body('/v1/responses', {
            'model': 'm',
            'input': 'hi',
            'max_output_tokens': 9999969112,
        })
        self.assertEqual(sent.get('max_tokens'), gateway.MAX_TOKENS_CEILING)
        self.assertLessEqual(sent.get('max_tokens', 0), 2147483647)


if __name__ == '__main__':
    unittest.main()
