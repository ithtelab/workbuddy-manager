"""与全局 `WB_CORS_ORIGINS`（`CORSMiddleware`）共存时的头去重 —— issue #445 回归。

发现过程：给 `/v1` 做完 CORS 后用「两套同时配置」实测，真实请求返回了

    Access-Control-Allow-Origin: https://a, https://a

因为 `CORSMiddleware` 装在全局（它也会处理 `/v1`），我们的中间件又加了一次。
浏览器把重复值当作非法来源处理，症状是「白名单明明加对了，还是跨域」——
这种只有同时配置才会出现的问题，靠单套配置的测试发现不了。
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import db  # noqa: E402
from server.services import gatewaycors as gc  # noqa: E402

_ORIGIN = 'https://both.example.com'


class GlobalCorsCoexistenceTest(unittest.TestCase):
    """`WB_CORS_ORIGINS` 与面板里的网关 CORS 同时生效时的行为。"""

    @classmethod
    def setUpClass(cls) -> None:
        """在**独立进程级**重装一个带 CORSMiddleware 的 app。

        不能直接改 `config.CORS_ORIGINS` 再 import：`main.py` 在导入时就把中间件
        装好了，改配置不会重装。这里按 `main.py` 的同一个顺序重建一个 app，
        只保留与本问题相关的两层（CORSMiddleware 在内、GatewayCors 在外）。
        """
        from fastapi import FastAPI
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.testclient import TestClient

        from server.main import GatewayCorsMiddleware

        app = FastAPI()
        app.add_middleware(CORSMiddleware, allow_origins=[_ORIGIN],
                           allow_credentials=True, allow_methods=['*'],
                           allow_headers=['*'])

        @app.post('/v1/chat/completions')
        def _chat() -> dict:
            return {'ok': True}

        app.add_middleware(GatewayCorsMiddleware)
        cls.client = TestClient(app)

    def setUp(self) -> None:
        db.connect()
        gc.save_settings({'enabled': True, 'origins': [_ORIGIN]})

    def tearDown(self) -> None:
        db.set_setting(gc.SETTING_KEY, {})

    def test_allow_origin_is_not_duplicated(self) -> None:
        r = self.client.post('/v1/chat/completions', headers={'Origin': _ORIGIN},
                             json={'model': 'x', 'messages': []})
        values = r.headers.get_list('access-control-allow-origin')
        self.assertEqual(values, [_ORIGIN],
                         f'重复的 allow-origin 会让浏览器判为非法来源：{values}')

    def test_allow_credentials_is_dropped_for_data_plane(self) -> None:
        """数据面用 Bearer，不该声明 Cookie 凭据 —— 而且与将来的 `*` 冲突。"""
        r = self.client.post('/v1/chat/completions', headers={'Origin': _ORIGIN},
                             json={'model': 'x', 'messages': []})
        self.assertNotIn('access-control-allow-credentials', r.headers)

    def test_wildcard_wins_over_global_list(self) -> None:
        """面板设成 `*` 时，即便全局名单更窄，数据面照样对任意来源放开。"""
        gc.save_settings({'enabled': True, 'origins': ['*']})
        r = self.client.post('/v1/chat/completions',
                             headers={'Origin': 'https://other.example.com'},
                             json={'model': 'x', 'messages': []})
        self.assertEqual(r.headers.get_list('access-control-allow-origin'), ['*'])
