"""子路径部署：反代**保留**前缀时，服务端自己把前缀剥掉（PR #72）。

## 为什么要有这层

面板挂在 `https://example.com/workbuddy-manager/` 这类子路径下时，反向代理有
两种写法：

  · `proxy_pass http://127.0.0.1:7864/;`（带尾斜杠）——反代自己把前缀剥掉，
    服务端收到的就是 `/api/keys`；
  · `proxy_pass http://127.0.0.1:7864;`（不带）——请求带着前缀进来，
    服务端看到 `/workbuddy-manager/api/keys`。

第二种是**面板类工具（1Panel 等）默认拼出来的写法**，手写配置的人也很容易漏掉
那个斜杠。此前只支持第一种，第二种的表现是「页面能打开、点什么都 404」
（HTML 是静态文件直接命中，而 API/路由全都不匹配）。

`StripBasePathMiddleware` 在最外层剥掉前缀，两种写法就都能用；
**已经剥过前缀的请求原样放行**，所以改动对现有部署是零影响。

## 这里钉住的

  1. 前缀被正确剥掉（含边界：恰好等于前缀、前缀+斜杠、以及**只是前缀开头相似**
     的路径不能被误剥——`/workbuddy-manager-x` 不是我们的请求）；
  2. 响应里的站内 `Location`（如 SPA 的 RSC 兜底 302）要补回前缀，否则会把
     用户带出子路径、落到域名根（那里通常是别人的站点）；
  3. 非 http scope（websocket）与已经带前缀的 Location 都不能被二次处理；
  4. 主程序里的接线（`if config.BASE_PATH:` 才挂中间件）——否则设了环境变量
     却没有任何效果，症状与「反代没配」完全一样，很难查。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.main import StripBasePathMiddleware  # noqa: E402

PREFIX = '/workbuddy-manager'


class _Capture:
    """假的下游 ASGI 应用：记下它看到的 scope，并回一条带头部的响应。"""

    def __init__(self, location: bytes | None = None) -> None:
        self.seen_path: str | None = None
        self.seen_raw: bytes | None = None
        self.seen_scope_type: str | None = None
        self.messages: list[dict] = []
        self._location = location

    async def __call__(self, scope, receive, send):
        self.seen_scope_type = scope.get('type')
        self.seen_path = scope.get('path')
        self.seen_raw = scope.get('raw_path')
        headers = [(b'content-type', b'text/plain')]
        if self._location is not None:
            headers.append((b'location', self._location))
        await send({'type': 'http.response.start', 'status': 200, 'headers': headers})
        await send({'type': 'http.response.body', 'body': b'ok'})
        self.messages = [{'type': 'http.response.start', 'status': 200, 'headers': headers},
                         {'type': 'http.response.body', 'body': b'ok'}]


def _run(path: str, scope_type: str = 'http') -> tuple[_Capture, list[dict]]:
    """跑一次请求，返回（下游看到的东西, 客户端收到的消息）。"""
    inner = _Capture()
    app = StripBasePathMiddleware(inner, PREFIX)
    sent: list[dict] = []

    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}

    async def send(message):
        sent.append(message)

    scope = {'type': scope_type, 'path': path, 'raw_path': path.encode(), 'headers': []}
    asyncio.run(app(scope, receive, send))
    return inner, sent


class StripTest(unittest.TestCase):
    def test_prefix_is_stripped(self) -> None:
        got, _ = _run(f'{PREFIX}/api/keys')
        self.assertEqual(got.seen_path, '/api/keys')
        self.assertEqual(got.seen_raw, b'/api/keys')

    def test_bare_prefix_becomes_root(self) -> None:
        """`/workbuddy-manager`（没有尾斜杠）要变成 `/`，而不是空路径。"""
        got, _ = _run(PREFIX)
        self.assertEqual(got.seen_path, '/')

    def test_already_stripped_passes_through(self) -> None:
        """反代已经剥过前缀时原样放行——这是「对现有部署零影响」的依据。"""
        got, _ = _run('/api/keys')
        self.assertEqual(got.seen_path, '/api/keys')

    def test_similar_prefix_is_not_stripped(self) -> None:
        """`/workbuddy-manager-x` 不是前缀命中，不能被削掉一截。

        削错的后果是把一个本来就不存在的路径改写成另一个不存在的路径，
        错误信息会指向一个用户从没请求过的地址，排查时驴唇不对马嘴。
        """
        got, _ = _run(f'{PREFIX}-x/api/keys')
        self.assertEqual(got.seen_path, f'{PREFIX}-x/api/keys')

    def test_websocket_scope_untouched(self) -> None:
        """非 http scope 原样转发（剥前缀只对 HTTP 有意义）。"""
        got, _ = _run(f'{PREFIX}/ws', scope_type='websocket')
        self.assertEqual(got.seen_scope_type, 'websocket')
        self.assertEqual(got.seen_path, f'{PREFIX}/ws')


class LocationRewriteTest(unittest.TestCase):
    """出去的方向：站内绝对地址要补回前缀，其余一概不动。"""

    def _location(self, value: bytes) -> bytes | None:
        inner = _Capture(location=value)
        app = StripBasePathMiddleware(inner, PREFIX)
        sent: list[dict] = []

        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}

        async def send(message):
            sent.append(message)

        asyncio.run(app({'type': 'http', 'path': '/x', 'raw_path': b'/x',
                         'headers': []}, receive, send))
        for m in sent:
            if m.get('type') == 'http.response.start':
                for k, v in m.get('headers') or []:
                    if k.lower() == b'location':
                        return v
        return None

    def test_internal_absolute_path_gets_prefix(self) -> None:
        """SPA 的 RSC 兜底会 302 到 `/dashboard`，不带前缀就把用户送出去了。"""
        self.assertEqual(self._location(b'/dashboard'), f'{PREFIX}/dashboard'.encode())

    def test_already_prefixed_not_doubled(self) -> None:
        """main.py 里那处 302 自己已经补过前缀，不能再叠一层。"""
        self.assertEqual(self._location(f'{PREFIX}/dashboard'.encode()),
                         f'{PREFIX}/dashboard'.encode())

    def test_protocol_relative_and_absolute_untouched(self) -> None:
        for value in (b'//evil.example/x', b'https://example.com/x', b'relative/path'):
            with self.subTest(value=value):
                self.assertEqual(self._location(value), value)

    def test_body_and_other_headers_untouched(self) -> None:
        _inner, sent = _run(f'{PREFIX}/x')
        self.assertEqual(sent[-1], {'type': 'http.response.body', 'body': b'ok'})
        ctype = [v for k, v in sent[0]['headers'] if k.lower() == b'content-type']
        self.assertEqual(ctype, [b'text/plain'])



class WiringTest(unittest.TestCase):
    """主程序里的接线：**设了 WB_BASE_PATH 才挂**，且挂上后确实生效。

    这条只能做源码级检查：中间件是在模块导入时按 `config.BASE_PATH` 决定是否
    `add_middleware` 的，而测试进程里那个值恒为空，改它需要重新导入整个应用。
    判据很窄（一行 if + 一次 add_middleware），源码检查足够，且能挡住
    「中间件写好了、忘了挂」这种漏法。
    """

    def test_main_installs_middleware_only_when_prefix_set(self) -> None:
        src = (Path(__file__).resolve().parents[2] / 'server' / 'main.py').read_text(encoding='utf-8')
        self.assertIn('StripBasePathMiddleware', src, '中间件没被引用，等于没实现')
        self.assertRegex(src, r'(?m)^if config\.BASE_PATH:\s*$',
                         '没有按 config.BASE_PATH 判断就无条件挂载')
        self.assertRegex(src, r'app\.add_middleware\(\s*StripBasePathMiddleware,\s*prefix=config\.BASE_PATH',
                         '挂载时没有把前缀传给中间件')

    def test_prefix_is_normalized_in_config(self) -> None:
        """`WB_BASE_PATH` 必须被归一化成 `/xxx`（无尾斜杠）——中间件的匹配依赖它。

        归一化在 config 里做（`strip('/')` 后补前导斜杠），所以 `foo/`、
        `/foo/`、`foo` 三种写法结果一致。若哪天归一化没了，`prefix + path`
        会拼出 `foo/xxx` 这种既不是绝对路径也匹配不上的东西。
        """
        src = (Path(__file__).resolve().parents[2] / 'server' / 'config.py').read_text(encoding='utf-8')
        self.assertIn("_BASE_PATH_RAW = _env('WB_BASE_PATH', '').strip('/')", src)
        self.assertIn("BASE_PATH = f'/{_BASE_PATH_RAW}' if _BASE_PATH_RAW else ''", src)


if __name__ == '__main__':
    unittest.main()
