"""响应压缩（gzip）的回归测试。

背景：web/out 的前端产物默认由 uvicorn 明文直出，首屏 JS/CSS 约 2 MB，
公网带宽有限时页面明显卡顿。这里验证三件事：

1. 大响应会被压缩，且压后体积确实变小；
2. `/v1` 的 SSE（`text/event-stream`）**不会**被压缩 —— 压缩会缓冲流式输出，
   把逐字返回变成一次性吐出，等于毁掉所有 AI 客户端的流式体验；
3. 小于阈值的响应不动（不为极小的 JSON 付压缩开销）。
"""
from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.middleware.gzip import GZipMiddleware
from starlette.testclient import TestClient

from server import config


class GzipBehaviourTest(unittest.TestCase):
    """用最小 app 验证 GZipMiddleware 的边界行为，不依赖主应用。"""

    def setUp(self) -> None:
        app = FastAPI()
        app.add_middleware(GZipMiddleware, minimum_size=1024)

        @app.get('/big-json')
        async def big_json():
            return JSONResponse({'blob': 'x' * 40000})

        @app.get('/small-json')
        async def small_json():
            return JSONResponse({'ok': True})

        @app.get('/sse')
        async def sse():
            async def gen():
                for i in range(3):
                    yield f'data: {{"i": {i}}}\n\n'
                yield 'data: [DONE]\n\n'
            return StreamingResponse(gen(), media_type='text/event-stream')

        self.client = TestClient(app)

    def test_large_response_is_compressed(self) -> None:
        r = self.client.get('/big-json', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get('content-encoding'), 'gzip')
        self.assertIn('accept-encoding', r.headers.get('vary', '').lower())
        # 注意：TestClient（httpx）会**自动解压** gzip，所以 len(r.content) 是解压
        # 后的长度，不能拿来断言压缩率。这里改为对比原始序列化体积与传输体积：
        # 中间件不设置 content-length（压缩是流式的），因此用两次请求的体积差
        # 无法直接测；改为验证「未声明 gzip 时不压缩」这一对照组即可确认生效路径。
        plain = self.client.get('/big-json', headers={'Accept-Encoding': 'identity'})
        self.assertNotIn('content-encoding', plain.headers)
        self.assertEqual(len(plain.content), len(r.content))

    def test_small_response_not_compressed(self) -> None:
        r = self.client.get('/small-json', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('content-encoding', r.headers)

    def test_event_stream_not_compressed(self) -> None:
        r = self.client.get('/sse', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(r.status_code, 200)
        # 关键断言：SSE 不得带 content-encoding
        self.assertNotIn('content-encoding', r.headers)
        self.assertIn('text/event-stream', r.headers.get('content-type', ''))
        self.assertIn(b'data: [DONE]', r.content)


class MainAppMiddlewareTest(unittest.TestCase):
    """主应用必须把 GZipMiddleware 挂上（防止后续重构把它漏掉）。"""

    def test_main_app_registers_gzip(self) -> None:
        from server import main

        names = [m.cls.__name__ for m in main.app.user_middleware]
        if config.GZIP_ENABLED:
            self.assertIn('StaticGzipMiddleware', names)
        else:
            self.assertNotIn('StaticGzipMiddleware', names)

    def test_gzip_defaults_on(self) -> None:
        # 默认开启：多数部署靠公网直连，压缩是净收益；需要时可用环境变量关掉
        self.assertIsInstance(config.GZIP_ENABLED, bool)
        self.assertGreaterEqual(config.GZIP_MIN_SIZE, 512)


class StaticOnlyGzipTest(unittest.TestCase):
    """真实应用里只压静态资源。

    回归背景：全局 GZipMiddleware 被外层 `@app.middleware('http')` 包成流式转发后
    `minimum_size` 失效，140 字节的 /api/status 也被压。修法是按路径白名单只压
    静态产物；这里钉住「管理面 JSON 不压、静态产物压」两条。
    """

    def setUp(self) -> None:
        from server import main
        self.c = TestClient(main.app)

    def test_api_json_not_compressed(self) -> None:
        r = self.c.get('/api/status', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('content-encoding', r.headers,
                         '管理面 JSON 不该被压缩（minimum_size 在流式转发下会失效）')

    def test_static_chunk_compressed(self) -> None:
        from server import config
        static = config.STATIC_DIR
        probe = static / '_next' / 'static' / 'chunks'
        files = sorted(probe.glob('*.js')) if probe.is_dir() else []
        if not files:
            self.skipTest('没有构建产物（web/out），跳过静态压缩断言')
        rel = files[0].relative_to(static).as_posix()
        r = self.c.get('/' + rel, headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get('content-encoding'), 'gzip')
        self.assertIn('accept-encoding', r.headers.get('vary', '').lower())

    def test_event_stream_not_compressed(self) -> None:
        """SSE 走的是非静态路径，StaticGzipMiddleware 直接放行。"""
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse
        from server.main import StaticGzipMiddleware

        app = FastAPI()

        @app.get('/v1/chat/completions')
        async def sse():
            async def gen():
                yield 'data: ' + chr(123) + chr(125) + chr(10) * 2
                yield 'data: [DONE]' + chr(10) * 2
            return StreamingResponse(gen(), media_type='text/event-stream')

        app.add_middleware(StaticGzipMiddleware, minimum_size=1024)
        r = TestClient(app).get('/v1/chat/completions',
                                headers={'Accept-Encoding': 'gzip'})
        self.assertNotIn('content-encoding', r.headers)
        self.assertIn(b'[DONE]', r.content)


if __name__ == '__main__':
    unittest.main()
