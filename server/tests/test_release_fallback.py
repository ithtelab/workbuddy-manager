"""Release 元数据获取的限流回退回归测试（2026-10-05「HTTP 403」事故）。

真实事故：更新在「获取 Release 失败：HTTP 403」处中断——
X-RateLimit-Remaining: 0（未认证 API 限额 60 次/小时/IP，直连与共享代理
出口是同一 IP，配额被同出口用户打满）。回退路径用 releases/latest 的
302 重定向拿 tag（github.com 网页端不受 API 配额限制），资产名按发布
惯例 workbuddy-manager-<tag>.tar.gz 构造；API 请求支持
WB_GITHUB_TOKEN / GITHUB_TOKEN 认证（5000 次/小时）。

运行：python -m pytest server/tests/test_release_fallback.py -q
"""
from __future__ import annotations

import inspect
import json
import os
import pathlib
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

# 复用 worker（deploy/update.py 是独立脚本，不在包内）
DEPLOY_DIR = pathlib.Path(__file__).resolve().parents[2] / 'deploy'
sys.path.insert(0, str(DEPLOY_DIR))
import update as worker  # noqa: E402


class _HTTPServerBase:
    """共享的本地 HTTP server 生命周期管理。"""

    def start_server(self, handler_cls):
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler_cls)
        self.port = self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop_server(self):
        self.httpd.shutdown()


class RedirectFallback(_HTTPServerBase, unittest.TestCase):
    def test_redirect_fallback_builds_asset_urls(self):
        """302 的 Location 里的 tag 要变成正确的资产 URL 三件套。"""
        result = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith('/releases/latest'):
                    result['visited'] = True
                    self.send_response(302)
                    self.send_header(
                        'Location',
                        f'http://127.0.0.1:{self.server.server_port}'
                        '/ithtelab/workbuddy-manager/releases/tag/v9.9.9')
                    self.end_headers()
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):
                pass

        self.start_server(Handler)
        try:
            info = worker.fetch_release_via_redirect(
                latest_url=f'http://127.0.0.1:{self.port}/releases/latest',
                repo='ithtelab/workbuddy-manager')
        finally:
            self.stop_server()
        base = 'https://github.com/ithtelab/workbuddy-manager/releases/download/v9.9.9'
        self.assertTrue(result.get('visited'))
        self.assertEqual(info['tag'], 'v9.9.9')
        self.assertEqual(info['pkg_url'], f'{base}/workbuddy-manager-v9.9.9.tar.gz')
        self.assertEqual(info['pkg_name'], 'workbuddy-manager-v9.9.9.tar.gz')
        # 这条路径只有重定向里的 tag，拿不到发布页摘要与大小 —— 如实留空，
        # 由 verify_download 把「未比对」记进状态（别在这里编一个假的）。
        self.assertEqual(info['pkg_digest'], '')
        self.assertIsNone(info['pkg_size'])

    def test_redirect_fallback_rejects_tagless_location(self):
        """Location 里没有 /tag/ 时必须报错，不能拿空 tag 去拼下载地址。"""
        stopped = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header('Location', 'http://127.0.0.1:1/somewhere/else')
                self.end_headers()

            def log_message(self, *args):
                pass

        self.start_server(Handler)
        try:
            with self.assertRaises(RuntimeError):
                worker.fetch_release_via_redirect(
                    latest_url=f'http://127.0.0.1:{self.port}/releases/latest',
                    repo='ithtelab/workbuddy-manager')
        finally:
            self.stop_server()


class GithubTokenHeader(_HTTPServerBase, unittest.TestCase):
    def test_http_json_sends_token_header(self):
        """设了 WB_GITHUB_TOKEN 时 API 请求必须带 Authorization。"""
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen['auth'] = self.headers.get('Authorization')
                body = json.dumps({'tag_name': 'v1.0.0'}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.start_server(Handler)
        old = os.environ.get('WB_GITHUB_TOKEN')
        os.environ['WB_GITHUB_TOKEN'] = 'test-token-123'
        try:
            rel = worker.http_json(f'http://127.0.0.1:{self.port}/x')
        finally:
            self.stop_server()
            if old is None:
                os.environ.pop('WB_GITHUB_TOKEN', None)
            else:
                os.environ['WB_GITHUB_TOKEN'] = old
        self.assertEqual(seen['auth'], 'Bearer test-token-123')
        self.assertEqual(rel['tag_name'], 'v1.0.0')

    def test_http_json_without_token_has_no_auth_header(self):
        """不设 token 时不能带空/野 Authorization（避免 GitHub 拒绝无效凭据）。"""
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen['auth'] = self.headers.get('Authorization')
                self.send_response(200)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'{}')

            def log_message(self, *args):
                pass

        self.start_server(Handler)
        old = os.environ.pop('GITHUB_TOKEN', None)
        old2 = os.environ.pop('WB_GITHUB_TOKEN', None)
        try:
            worker.http_json(f'http://127.0.0.1:{self.port}/x')
        finally:
            self.stop_server()
            if old is not None:
                os.environ['GITHUB_TOKEN'] = old
            if old2 is not None:
                os.environ['WB_GITHUB_TOKEN'] = old2
        self.assertIsNone(seen['auth'])


class FallbackWiring(unittest.TestCase):
    def test_update_manager_wires_fallback(self):
        """取版本必须先走 API、失败后回退到 302 路径。

        这条接线现在住在 `_fetch_release_info()` 里 —— 管理端与上游两条路径都
        经它取包，所以回退只要修一处就两边都受益（上游同步也需要包）。
        `update_manager` 自身则必须经 `_release_root()` 取包，不能绕开。
        """
        src = inspect.getsource(worker._fetch_release_info)
        self.assertIn('fetch_release_via_api()', src)
        self.assertIn('fetch_release_via_redirect()', src)
        self.assertIn('回退', src)

        mgr = inspect.getsource(worker.update_manager)
        self.assertIn('_fetch_release_info(', mgr,
                      '管理端没经共用的取版本入口 —— 回退接线可能被绕开')
        self.assertIn('_release_root(', mgr,
                      '管理端没经共用的取包入口 —— 上游同步会另下一次包')


if __name__ == '__main__':
    unittest.main()
