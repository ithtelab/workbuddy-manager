"""Named account routes: authorization, persistence and outbound isolation."""
from __future__ import annotations

import asyncio
import json
import re
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from server import config, security
from server.routers import accounts
from server.services import modelcatalog, renew, tencent, wb2api


class AccountProxyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.auth_dir = self.root / 'auths'
        self.auth_dir.mkdir()
        self.cfg = self.root / 'config.json'
        self.cfg.write_text(json.dumps({'proxies': {
            'route-a': 'http://proxy.example:8080',
            'route-b': 'https://proxy.example:8443',
        }}))
        for name, value in [('AUTH_DIR', self.auth_dir), ('UPSTREAM_CONFIG', self.cfg),
                            ('DEFAULT_ACCOUNT_PROXY', '')]:
            patch = mock.patch.object(config, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.raw = {'account': {'uid': 'example'},
                    'auth': {'accessToken': 'test-token'}, 'custom': {'keep': True}}
        self.file = self.auth_dir / 'workbuddy-example.json'
        self.file.write_text(json.dumps(self.raw))
        self.file.chmod(0o600)
        app = FastAPI()
        app.include_router(accounts.router)
        self.app = app
        app.dependency_overrides[security.current_user] = lambda: {'username': 'test', 'role': 'admin'}
        app.dependency_overrides[security.require_admin] = lambda: {'username': 'test', 'role': 'admin'}
        self.client = TestClient(app)
        patch = mock.patch.object(accounts.reload, 'request_restart', return_value=True)
        self.restart = patch.start()
        self.addCleanup(patch.stop)

    def test_route_list_never_exposes_proxy_urls(self):
        response = self.client.get('/api/proxies')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'routes': ['route-a', 'route-b'], 'default': ''})
        self.assertNotIn('proxy.example', response.text)

    def test_route_list_requires_authentication(self):
        self.app.dependency_overrides.clear()
        self.assertEqual(self.client.get('/api/proxies').status_code, 401)

    def test_viewer_cannot_change_route(self):
        def viewer():
            raise HTTPException(status_code=403, detail='需要管理员权限')
        self.app.dependency_overrides[security.require_admin] = viewer
        response = self.client.put('/api/accounts/workbuddy-example.json/proxy',
                                   json={'proxy': 'route-a'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(json.loads(self.file.read_text()), self.raw)

    def test_unknown_route_cannot_modify_account(self):
        response = self.client.put('/api/accounts/workbuddy-example.json/proxy',
                                   json={'proxy': 'missing'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(self.file.read_text()), self.raw)
        self.restart.assert_not_called()

    def test_invalid_route_type_is_rejected(self):
        for value in [None, 1, [], {}]:
            with self.subTest(value=value):
                response = self.client.put('/api/accounts/workbuddy-example.json/proxy',
                                           json={'proxy': value})
                self.assertEqual(response.status_code, 400)

    def test_route_update_preserves_credentials_owner_and_mode(self):
        original = self.file.stat()
        response = self.client.put('/api/accounts/workbuddy-example.json/proxy',
                                   json={'proxy': 'route-a'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(self.file.read_text()), {**self.raw, 'proxy': 'route-a'})
        current = self.file.stat()
        if os.name == 'posix':
            self.assertEqual((current.st_uid, current.st_gid), (original.st_uid, original.st_gid))
            self.assertEqual(current.st_mode & 0o777, 0o600)
        self.assertEqual(list(self.auth_dir.glob('*.tmp')), [])
        self.restart.assert_called_once_with(None)

    def test_remove_binding_retains_other_fields(self):
        wb2api.set_account_proxy(self.file.name, 'route-a')
        wb2api.set_account_proxy(self.file.name, '')
        self.assertEqual(json.loads(self.file.read_text()), self.raw)

    def test_update_targets_selected_group_only(self):
        other = self.root / 'other'
        other.mkdir()
        target = other / self.file.name
        target.write_text(json.dumps(self.raw))
        group = {'id': 7, 'name': 'Example group', 'auth_dir': str(other),
                 'is_default': False, 'container': 'example-gateway'}
        with mock.patch.object(accounts.upstreamsvc, 'get_upstream', return_value=group):
            response = self.client.put('/api/accounts/workbuddy-example.json/proxy?upstream_id=7',
                                       json={'proxy': 'route-b'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(self.file.read_text()), self.raw)
        self.assertEqual(json.loads(target.read_text())['proxy'], 'route-b')
        self.restart.assert_called_once_with(group)

    def test_invalid_filename_is_rejected(self):
        with self.assertRaises(ValueError):
            wb2api.set_account_proxy('../workbuddy-example.json', 'route-a')

    def test_missing_account_is_reported(self):
        response = self.client.put('/api/accounts/workbuddy-missing.json/proxy',
                                   json={'proxy': 'route-a'})
        self.assertEqual(response.status_code, 404)

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlink behavior')
    def test_symlink_cannot_import_an_external_account(self):
        external = self.root / 'external.json'
        external.write_text(json.dumps(self.raw))
        link = self.auth_dir / 'workbuddy-link.json'
        link.symlink_to(external)
        response = self.client.put('/api/accounts/workbuddy-link.json/proxy',
                                   json={'proxy': 'route-a'})
        self.assertEqual(response.status_code, 400)
        self.assertTrue(link.is_symlink())
        self.assertEqual(json.loads(external.read_text()), self.raw)

    def test_unknown_route_never_creates_http_client(self):
        with mock.patch.object(config, 'http_client') as client:
            with self.assertRaises(ValueError):
                tencent._account_http_client({'proxy': 'missing'})
            client.assert_not_called()

    def test_invalid_proxy_url_is_not_disclosed(self):
        # socks4 是 httpx 不认的协议（Unknown scheme），仍然算无效地址
        self.cfg.write_text(json.dumps({'proxies': {'route-a': 'socks4://proxy.example:1080'}}))
        with self.assertRaises(ValueError) as ctx:
            config.account_proxy({'proxy': 'route-a'})
        self.assertNotIn('proxy.example', str(ctx.exception))

    def test_socks5_routes_are_accepted(self):
        """住宅代理服务商给的凭据常常就是 socks5 写法（实测 ProxyLane 如此），
        线路表里要能直接填 —— 此前只认 http/https，填 socks5 会在调用时才报
        「配置无效」。socks5h（域名解析交给代理）同样放行。"""
        self.cfg.write_text(json.dumps({'proxies': {
            'route-a': 'socks5://user:pass@proxy.example:1080',
            'route-b': 'socks5h://user:pass@proxy.example:1080',
        }}))
        self.assertEqual(config.account_proxy({'proxy': 'route-a'}),
                         'socks5://user:pass@proxy.example:1080')
        self.assertEqual(config.account_proxy({'proxy': 'route-b'}),
                         'socks5h://user:pass@proxy.example:1080')

    def test_unbound_accounts_keep_existing_global_proxy_behavior(self):
        with mock.patch.object(config, 'http_client') as client:
            tencent._account_http_client({})
            client.assert_called_once_with(config.TENCENT_TIMEOUT, connect=5)

    def test_configured_default_and_explicit_unbound_login(self):
        with mock.patch.object(config, 'DEFAULT_ACCOUNT_PROXY', 'route-a'), \
                mock.patch.object(accounts.tencent, 'start_login', new_callable=mock.AsyncMock) as start:
            start.return_value = {'state': '', 'authUrl': ''}
            response = self.client.post('/api/auth/start', json={'realm': 'cn'})
            self.assertEqual(response.status_code, 200)
            start.assert_awaited_once_with('cn', 'route-a')
            start.reset_mock()
            response = self.client.post('/api/auth/start', json={'realm': 'cn', 'proxy': ''})
            self.assertEqual(response.status_code, 200)
            start.assert_awaited_once_with('cn')

    def test_login_poll_keeps_selected_route_and_persists_it(self):
        seen = []
        def reply(request):
            seen.append(request.url.path)
            data = {'accessToken': 'test-token', 'expiresIn': 3600} if request.url.path.endswith('/token') else {'uid': 'example'}
            return httpx.Response(200, json={'code': 0, 'data': data})
        client = httpx.AsyncClient(transport=httpx.MockTransport(reply))
        tencent._state_cache['test-state'] = (time.time(), 'cn', 'route-a')
        self.addCleanup(tencent.drop_state, 'test-state')
        with mock.patch.object(config, 'http_client', return_value=client) as make_client:
            result = asyncio.run(tencent.poll_login('test-state', 'cn'))
        make_client.assert_called_once_with(config.TENCENT_TIMEOUT, connect=5, proxy='http://proxy.example:8080')
        self.assertEqual(result['proxy'], 'route-a')
        self.assertEqual(len(seen), 2)
        tencent.write_auth_file(result)
        self.assertEqual(json.loads(self.file.read_text())['proxy'], 'route-a')

    def test_reauthentication_preserves_binding_when_not_supplied(self):
        wb2api.set_account_proxy(self.file.name, 'route-a')
        tencent.write_auth_file({'uid': 'example', 'access_token': 'new-token', 'expires_at': 99})
        self.assertEqual(json.loads(self.file.read_text())['proxy'], 'route-a')

    def test_token_renewal_carries_binding_and_preserves_file_mode(self):
        self.raw['proxy'] = 'route-a'
        self.file.write_text(json.dumps(self.raw))
        self.assertEqual(renew._account_payload(self.raw)['proxy'], 'route-a')
        self.assertEqual(accounts._auth_dict(self.raw)['proxy'], 'route-a')
        tencent.update_auth_tokens(self.file.name, {'access_token': 'new-token'})
        saved = json.loads(self.file.read_text())
        self.assertEqual(saved['proxy'], 'route-a')
        self.assertEqual(saved['custom'], self.raw['custom'])
        if os.name == 'posix':
            self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()


class ProxyUiGatingTest(unittest.TestCase):
    """「线路」这一列没东西可绑时**不能显示**（维护者复核补）。

    线路表读自上游配置的 `proxies`；没配过时那个下拉里只有「直连」一项 —— 摆出来
    就是纯噪声，还会让人以为能选却选不动。所以只在「有线路可选」或「确实有账号绑着
    线路」（配置被移除后仍要看得见、改得回来）时显示。
    """

    PAGE = Path(__file__).resolve().parents[2] / 'web' / 'app' / '(main)' / 'accounts' / 'page.tsx'
    DIALOG = (Path(__file__).resolve().parents[2] / 'web' / 'components' / 'common'
              / 'accounts' / 'AddAccountDialog.tsx')

    def test_accounts_page_hides_the_column_when_there_is_nothing_to_bind(self) -> None:
        src = self.PAGE.read_text(encoding='utf-8')
        self.assertIn('hasProxyUi', src, '账号页没有「要不要显示线路这一列」的判据')
        m = re.search(r'const hasProxyUi = [\s\S]{0,200}?;', src)
        self.assertIsNotNone(m, '找不到 hasProxyUi 的定义')
        body = m.group(0)
        self.assertIn('proxyRoutes', body, '判据没有看「有没有线路可选」')
        self.assertIn('a.proxy', body, '判据没有看「有没有账号绑着线路」')
        # 三个渲染点（手机端那一行、表头、单元格）都要走这个判据
        self.assertEqual(src.count('hasProxyUi &&'), 3,
                         '有渲染点没按 hasProxyUi 收口 —— 没东西可绑时仍会显示那一列')

    def test_dialog_hides_the_selector_without_routes(self) -> None:
        src = self.DIALOG.read_text(encoding='utf-8')
        self.assertRegex(src, r'\{proxyRoutes\.length > 0 && \(',
                         '添加账号弹窗在没有线路可选时仍会渲染那个下拉')


class LoginNetworkHintTest(unittest.TestCase):
    """扫码登录/轮询的网络失败要给**可执行的中文**（issue #194）。

    现场：用户配了一条解析不了 .cn 的国家线路，点「添加账号（国内版）」二维码一直
    出不来，界面上只显示英文 `Internal Server Error`；关掉代理就好了 —— 也就是说
    原因明明是线路，但错误里一个字都没提线路。
    """

    def _failing_client(self):
        class _Boom(httpx.AsyncClient):  # noqa: D401 - 只为抛异常
            async def post(self, *a, **k):
                raise httpx.ProxyError('proxy refused to connect')

            async def get(self, *a, **k):
                raise httpx.ConnectTimeout('timed out')

        return _Boom

    def test_start_login_names_the_route(self) -> None:
        with mock.patch.object(config, 'http_client', self._failing_client()):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(tencent.start_login('cn', 'route-x'))
        msg = str(ctx.exception)
        self.assertIn('route-x', msg, '错误里要点出是哪条线路')
        self.assertIn('线路', msg)
        self.assertIn('换一条线路', msg, '要给下一步动作')
        self.assertNotIn('Internal Server Error', msg)

    def test_start_login_without_route_mentions_network(self) -> None:
        with mock.patch.object(config, 'http_client', self._failing_client()):
            with self.assertRaises(RuntimeError) as ctx:
                asyncio.run(tencent.start_login('cn', ''))
        msg = str(ctx.exception)
        self.assertIn('网络出口', msg)
        self.assertNotIn('route-x', msg)

    def test_poll_login_names_the_route(self) -> None:
        # 先登记一个 state（正常路径里由 start_login 写入），再让出站失败
        state = 'unit-test-state'
        with mock.patch.dict(tencent._state_cache, {state: (time.time(), 'cn', 'route-y')}):
            with mock.patch.object(config, 'http_client', self._failing_client()):
                with self.assertRaises(RuntimeError) as ctx:
                    asyncio.run(tencent.poll_login(state, 'cn'))
        msg = str(ctx.exception)
        self.assertIn('route-y', msg)
        self.assertIn('换一条线路', msg)
