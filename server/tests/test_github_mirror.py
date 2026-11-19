"""GitHub 镜像加速（issue #216）的回归测试。

背景：国内机器直连 github.com / api.github.com 经常超时，更新检查失败、发布包
下载卡住。gh-proxy 一类加速服务的用法是「镜像基址 + 完整原始地址」：

    https://gh-proxy.com/https://api.github.com/repos/<owner>/<repo>/releases/latest

锁定以下不变量：
  1. 未配置镜像时行为与从前**完全一致**（不能给所有人平白加一跳）；
  2. 配置后 API 与资产下载都先走镜像；镜像失败**自动回退直连**；
  3. **走镜像时绝不发送 GitHub Token**（镜像会把请求转给第三方）；
  4. 镜像基址要做规范化：缺协议补 https，带查询串/片段的直接拒绝
     （拼出来必然是错误页）；
  5. 面板接口的读写与来源（面板 / 环境变量）要如实回显。

测试策略：起本地真实 HTTP 服务扮演「镜像」，不 mock 标准库。
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from server import db, security  # noqa: E402
from server.services import updater  # noqa: E402


def _load_worker(**env):
    """加载 deploy/update.py（每次全新模块：镜像配置是模块级常量）。"""
    keys = list(env)
    old = {k: os.environ.get(k) for k in keys}
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    try:
        spec = importlib.util.spec_from_file_location(
            'upd_mirror', str(_ROOT / 'deploy' / 'update.py'))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _Rep:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def log(self, msg: str, level: str = 'info') -> None:
        self.lines.append((level, msg))

    def set_integrity(self, status: str, digest: str = '') -> None:
        pass

    def text(self) -> str:
        return '\n'.join(m for _, m in self.lines)

    def warned(self) -> bool:
        return any(lvl == 'warn' for lvl, _ in self.lines)


class NormalizeMirrorTest(unittest.TestCase):
    """基址规范化：拼地址的规则是「基址 + 完整原始地址」，形态不对就必然拼错。"""

    def test_accepts_the_documented_forms(self) -> None:
        cases = {
            'https://gh-proxy.com': 'https://gh-proxy.com/',
            'https://gh-proxy.com/': 'https://gh-proxy.com/',
            'https://gh-proxy.org': 'https://gh-proxy.org/',
            'gh-proxy.com': 'https://gh-proxy.com/',
            'http://127.0.0.1:8080': 'http://127.0.0.1:8080/',
            'https://example.com/gh/': 'https://example.com/gh/',
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(updater.normalize_mirror(raw), want)

    def test_rejects_values_that_would_build_broken_urls(self) -> None:
        for raw in ('', '   ', 'ftp://mirror.example', 'https://', 'https://x/?a=1',
                    'https://x/#frag', 'not a url'):
            with self.subTest(raw=raw):
                self.assertEqual(updater.normalize_mirror(raw), '')

    def test_worker_module_uses_the_same_rules(self) -> None:
        """面板与更新器各有一份实现（worker 不能 import 面板），结论必须一致。"""
        mod = _load_worker(WB_GITHUB_MIRROR='gh-proxy.com')
        self.assertEqual(mod.GITHUB_MIRROR, 'https://gh-proxy.com/')
        for raw in ('https://x/?a=1', 'ftp://x', ''):
            self.assertEqual(mod.normalize_mirror(raw), updater.normalize_mirror(raw))


class WorkerMirrorUrlTest(unittest.TestCase):
    def test_disabled_mirror_leaves_urls_alone(self) -> None:
        mod = _load_worker(WB_GITHUB_MIRROR=None)
        self.assertEqual(mod.GITHUB_MIRROR, '')
        url = 'https://api.github.com/repos/a/b/releases/latest'
        self.assertEqual(mod.mirror_url(url), url)
        self.assertFalse(mod.is_mirrored(url))

    def test_github_urls_get_the_prefix(self) -> None:
        mod = _load_worker(WB_GITHUB_MIRROR='https://gh-proxy.com/')
        for url in ('https://api.github.com/repos/a/b/releases/latest',
                    'https://github.com/a/b/releases/download/v1/x.tar.gz',
                    'https://objects.githubusercontent.com/abc'):
            with self.subTest(url=url):
                self.assertEqual(mod.mirror_url(url), f'https://gh-proxy.com/{url}')

    def test_non_github_urls_are_not_proxied(self) -> None:
        """镜像只管 GitHub；把别的地址也套上去等于把无关流量交给第三方。"""
        mod = _load_worker(WB_GITHUB_MIRROR='https://gh-proxy.com/')
        for url in ('http://127.0.0.1:7863/healthz', 'https://example.com/x'):
            with self.subTest(url=url):
                self.assertEqual(mod.mirror_url(url), url)

    def test_token_is_never_sent_through_the_mirror(self) -> None:
        """镜像会把请求转给第三方 —— 凭据不能为了下载快一点而离开本机。"""
        mod = _load_worker(WB_GITHUB_MIRROR='https://gh-proxy.com/')
        with mock.patch.dict(os.environ, {'WB_GITHUB_TOKEN': 'ghp_secret'}):
            mirrored = 'https://gh-proxy.com/https://api.github.com/repos/a/b'
            self.assertNotIn('Authorization', mod._github_headers(mirrored))
            # 直连那条仍然带（否则限流问题又回来了）
            self.assertEqual(mod._github_headers('https://api.github.com/repos/a/b')
                             .get('Authorization'), 'Bearer ghp_secret')


class _Server:
    """本地假镜像：记录收到的路径与请求头。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.seen: list[tuple[str, dict]] = []
        self.fail = fail
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *a) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                outer.seen.append((self.path, dict(self.headers)))
                if outer.fail:
                    self.send_error(502)
                    return
                if self.path.endswith('/releases/latest'):
                    body = json.dumps({'tag_name': 'v9.9.9', 'assets': [{
                        'name': 'workbuddy-manager-v9.9.9.tar.gz',
                        'browser_download_url': 'https://github.com/a/b/releases/download/v9.9.9/x.tar.gz',
                        'size': 123, 'digest': 'sha256:abc',
                    }]}).encode()
                else:
                    body = b'x' * 101_000
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._httpd.daemon_threads = True
        self.url = f'http://127.0.0.1:{self._httpd.server_address[1]}/'

    def __enter__(self):
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class WorkerMirrorRequestTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = pathlib.Path(self._tmp.name)

    def test_api_goes_through_the_mirror_first(self) -> None:
        with _Server() as mirror:
            mod = _load_worker(WB_GITHUB_MIRROR=mirror.url)
            rel = mod.http_json('https://api.github.com/repos/a/b/releases/latest')
        self.assertEqual(rel['tag_name'], 'v9.9.9')
        self.assertTrue(mirror.seen, '镜像一个请求都没收到 —— API 没走镜像')
        self.assertTrue(mirror.seen[0][0].startswith('/https://api.github.com/'),
                        f'镜像收到的不是「基址 + 完整原始地址」：{mirror.seen[0][0]}')

    def test_api_falls_back_to_direct_when_mirror_fails(self) -> None:
        """镜像自己也会抖：它挂了不能让更新直接不可用。"""
        with _Server(fail=True) as mirror:
            mod = _load_worker(WB_GITHUB_MIRROR=mirror.url)
            # 直连那条用一个必失败的本地地址，确认它**试过**（异常里能看出来）
            with self.assertRaises(Exception) as ctx:
                mod.http_json(f'{mirror.url}https://api.github.com/x', timeout=3)
        self.assertTrue(mirror.seen, '镜像没被尝试')
        # 镜像 502 之后应该去试直连；直连地址也是本地端口（已关闭）→ 连接被拒
        self.assertIsNotNone(ctx.exception)

    def test_download_goes_through_the_mirror_then_falls_back(self) -> None:
        with _Server() as mirror:
            mod = _load_worker(WB_GITHUB_MIRROR=mirror.url)
            dest = self.dir / 'pkg.tar.gz'
            rep = _Rep()
            mod.download('https://github.com/a/b/releases/download/v1/x.tar.gz', dest, rep)
        self.assertEqual(dest.stat().st_size, 101_000)
        self.assertTrue(mirror.seen, '下载没走镜像')
        self.assertTrue(mirror.seen[0][0].startswith('/https://github.com/'),
                        f'镜像收到的路径不对：{mirror.seen[0][0]}')
        self.assertIn('经镜像下载', rep.text())

    def test_download_falls_back_when_the_mirror_returns_an_error_page(self) -> None:
        """镜像返回一个很小的错误页：要当成失败并回退，而不是把错误页当发布包。"""
        with _Server(fail=True) as mirror:
            mod = _load_worker(WB_GITHUB_MIRROR=mirror.url)
            dest = self.dir / 'pkg.tar.gz'
            rep = _Rep()
            with self.assertRaises(Exception):
                mod.download('https://github.com/a/b/releases/download/v1/x.tar.gz',
                             dest, rep, )
        self.assertIn('回退直连', rep.text())

    def test_redirect_fallback_stays_direct(self) -> None:
        """releases/latest 的网页正文不被镜像放行（403），这条路径必须直连。"""
        src = (_ROOT / 'deploy' / 'update.py').read_text(encoding='utf-8')
        body = src.split('def fetch_release_via_redirect(', 1)[1].split('\ndef ', 1)[0]
        self.assertNotIn('mirror_url(', body)
        self.assertNotIn('GITHUB_MIRROR', body)


class WorkerReleaseInfoTest(unittest.TestCase):
    def test_mirror_is_announced_in_the_log(self) -> None:
        """流量经过第三方这件事必须让用户看得见，不能静默加速。"""
        mod = _load_worker(WB_GITHUB_MIRROR='https://gh-proxy.com/')
        mod._release_info_cache = None
        mod.fetch_release_via_api = lambda: {  # type: ignore[assignment]
            'tag': 'v1.0.0', 'pkg_url': 'https://github.com/a/b/x.tar.gz',
            'pkg_name': 'x.tar.gz', 'pkg_size': 1, 'pkg_digest': '',
        }
        rep = _Rep()
        mod._fetch_release_info(rep)
        self.assertIn('gh-proxy.com', rep.text())


class ServerGhGetMirrorTest(unittest.TestCase):
    """面板侧（版本检测）也要走镜像，且同样不带 Token。"""

    def setUp(self) -> None:
        self._store: dict = {}
        self._patches = [
            mock.patch.object(db, 'get_setting',
                              side_effect=lambda k, d=None: self._store.get(k, d)),
            mock.patch.object(db, 'set_setting',
                              side_effect=lambda k, v: self._store.__setitem__(k, v)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def test_gh_get_uses_mirror_and_hides_token(self) -> None:
        with _Server() as mirror:
            self._store['github_mirror'] = mirror.url
            with mock.patch.dict(os.environ, {'WB_GITHUB_TOKEN': 'ghp_secret'}):
                rel = updater._gh_get('https://api.github.com/repos/a/b/releases/latest')
        self.assertEqual(rel['tag_name'], 'v9.9.9')
        self.assertTrue(mirror.seen, '面板版本检测没走镜像')
        self.assertNotIn('Authorization', mirror.seen[0][1],
                         'Token 被发给镜像了 —— 它是第三方')

    def test_panel_mirror_beats_env(self) -> None:
        self._store['github_mirror'] = 'https://panel.example/'
        with mock.patch.dict(os.environ, {'WB_GITHUB_MIRROR': 'https://env.example/'}):
            self.assertEqual(updater.github_mirror(), 'https://panel.example/')

    def test_env_used_when_panel_is_empty(self) -> None:
        self._store.pop('github_mirror', None)
        with mock.patch.dict(os.environ, {'WB_GITHUB_MIRROR': 'env.example'}):
            self.assertEqual(updater.github_mirror(), 'https://env.example/')

    def test_no_mirror_means_direct(self) -> None:
        self._store.pop('github_mirror', None)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('WB_GITHUB_MIRROR', None)
            self.assertEqual(updater.github_mirror(), '')


class MirrorEndpointTest(unittest.TestCase):
    """面板接口：读写、规范化、来源回显。"""

    def setUp(self) -> None:
        from fastapi.testclient import TestClient

        from server.main import app
        self.app = app
        self.client = TestClient(app)
        self._store: dict = {}
        app.dependency_overrides[security.current_user] = lambda: {'username': 'admin', 'role': 'admin'}
        app.dependency_overrides[security.require_admin] = lambda: {'username': 'admin', 'role': 'admin'}
        self.addCleanup(app.dependency_overrides.clear)
        for pat in (
            mock.patch.object(db, 'get_setting',
                              side_effect=lambda k, d=None: self._store.get(k, d)),
            mock.patch.object(db, 'set_setting',
                              side_effect=lambda k, v: self._store.__setitem__(k, v)),
            mock.patch.object(security, 'audit', return_value=None),
        ):
            pat.start()
            self.addCleanup(pat.stop)
        os.environ.pop('WB_GITHUB_MIRROR', None)

    def test_put_then_get_normalizes_and_reports_source(self) -> None:
        r = self.client.put('/api/settings/github-mirror', json={'mirror': 'gh-proxy.com'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['mirror'], 'https://gh-proxy.com/')
        self.assertEqual(body['stored'], 'https://gh-proxy.com/')
        self.assertEqual(body['source'], 'panel')
        self.assertIn('https://gh-proxy.com/', body['presets'])

    def test_clear_falls_back_to_env(self) -> None:
        self.client.put('/api/settings/github-mirror', json={'mirror': 'https://panel.example/'})
        with mock.patch.dict(os.environ, {'WB_GITHUB_MIRROR': 'https://env.example/'}):
            body = self.client.put('/api/settings/github-mirror', json={'mirror': ''}).json()
            self.assertEqual(body['source'], 'env')
            self.assertEqual(body['mirror'], 'https://env.example/')

    def test_bad_value_is_400_with_a_reason(self) -> None:
        r = self.client.put('/api/settings/github-mirror', json={'mirror': 'https://x/?a=1'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('镜像地址', r.text)

    def test_updater_env_gets_the_mirror(self) -> None:
        """更新器是独立进程：面板里配的镜像要随环境传下去。"""
        self._store['github_mirror'] = 'https://gh-proxy.com/'
        script = pathlib.Path(tempfile.mkdtemp()) / 'update.py'
        script.write_text('# placeholder\n', encoding='utf-8')
        seen: dict = {}

        class _FakeProc:
            pid = 1

        def fake_popen(cmd, **kwargs):
            seen.update(kwargs.get('env') or {})
            return _FakeProc()

        with mock.patch.object(updater, '_updater_script', return_value=script), \
                mock.patch.object(updater, '_lock_active', return_value=False), \
                mock.patch.object(updater.subprocess, 'Popen', fake_popen), \
                mock.patch.object(updater, '_write_initial_status'), \
                mock.patch.object(updater, 'STATUS_FILE', script.parent / 's.json'), \
                mock.patch.object(updater, 'LOG_FILE', script.parent / 'u.log'), \
                mock.patch.object(updater, 'LOCK_FILE', script.parent / 'update.lock'):
            ok, message = updater.start_update('manager')
        self.assertTrue(ok, message)
        self.assertEqual(seen.get('WB_GITHUB_MIRROR'), 'https://gh-proxy.com/')


if __name__ == '__main__':
    unittest.main()
