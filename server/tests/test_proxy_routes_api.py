"""「设置 → 代理」的接口：线路表 CRUD 与全局出口代理。

三条最要紧的性质（写成用例而不是只写在注释里）：

  1. **凭据不回显**：代理地址里带用户名密码，列表接口只能出现脱敏串；
  2. **只动 `proxies` 段**：写线路时上游 config.json 的其它键（api_key / schedule…）
     一个都不能丢 —— 丢一次就是用户的口令没了；
  3. **改名 / 删除要管绑定**：线路改名后账号要跟着改，删除前要让人确认，
     确认后账号改回默认出口（而不是留在一条不存在的线路上、请求直接失败）。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import config, db, security
from server.routers import accounts, proxies
from server.services import proxyroutes

ADMIN = {'username': 'admin', 'role': 'admin'}


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.auth_dir = self.root / 'auths'
        self.auth_dir.mkdir()
        self.cfg = self.root / 'config.json'
        self.cfg.write_text(json.dumps({
            # 线路表旁边必须留着别的键：写线路时它们要原样保留
            'api_key': 'wbk_keep-me',
            'schedule': {'checkin': '08:00'},
            'proxies': {'px-a': 'socks5://user:secret@eu.example.dev:10000'},
        }, ensure_ascii=False), encoding='utf-8')
        for name, value in (('AUTH_DIR', self.auth_dir), ('UPSTREAM_CONFIG', self.cfg),
                            ('DEFAULT_ACCOUNT_PROXY', ''), ('HTTP_PROXY', '')):
            patch = mock.patch.object(config, name, value)
            patch.start()
            self.addCleanup(patch.stop)

        self.db_path = self.root / 'manager.db'
        patch = mock.patch.object(config, 'DB_PATH', self.db_path)
        patch.start()
        self.addCleanup(patch.stop)
        db._conn = None
        db.connect()
        self.addCleanup(self._close_db)

        self._account('workbuddy-one.json', 'uid-1', '小王', 'px-a')
        self._account('workbuddy-two.json', 'uid-2', '小李', '')

        app = FastAPI()
        app.include_router(proxies.router)
        app.dependency_overrides[security.current_user] = lambda: ADMIN
        app.dependency_overrides[security.require_session_admin] = lambda: ADMIN
        self.client = TestClient(app)

    def _close_db(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None

    def _account(self, name: str, uid: str, nickname: str, proxy: str) -> None:
        doc = {'account': {'uid': uid, 'nickname': nickname},
               'auth': {'accessToken': 'token'}}
        if proxy:
            doc['proxy'] = proxy
        (self.auth_dir / name).write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')

    def _account_proxy(self, name: str) -> str:
        raw = json.loads((self.auth_dir / name).read_text(encoding='utf-8'))
        return str(raw.get('proxy') or '')

    def _routes(self) -> dict:
        return json.loads(self.cfg.read_text(encoding='utf-8')).get('proxies') or {}


class ProxyRoutesApiTest(_Base):
    def test_list_masks_credentials(self) -> None:
        body = self.client.get('/api/proxy-routes').json()
        route = body['routes'][0]
        self.assertEqual(route['name'], 'px-a')
        self.assertEqual(route['scheme'], 'socks5')
        self.assertTrue(route['has_auth'])
        self.assertIn('***', route['masked'])
        self.assertNotIn('secret', json.dumps(body, ensure_ascii=False),
                         '列表里出现了代理密码')
        self.assertEqual(route['bound'], 1, '绑定账号数不对')

    def test_reads_are_session_admin_only(self) -> None:
        """列表与全局设置是**仅会话管理员**可读。

        脱敏串里留着用户名 —— 而住宅代理的用户名往往就是账号标识
        （`api07…_c_CN_city_Shanghai_s_…`），只读令牌 / 只读账号不该看到它。
        这里按源码钉住这两个依赖，免得日后被顺手放宽（接口在鉴权上属于
        「凭据载体」那一类，与账号导出同级）。
        """
        source = (Path(__file__).resolve().parents[1] / 'routers' / 'proxies.py'
                  ).read_text(encoding='utf-8')
        for head in ("@router.get('/proxy-routes')", "@router.get('/settings/proxy')"):
            with self.subTest(endpoint=head):
                block = source.split(head, 1)[1].split(') -> dict:', 1)[0]
                self.assertIn('require_session_admin', block,
                              f'{head} 不再要求会话管理员')

    def test_create_validates_scheme_and_name(self) -> None:
        bad_scheme = self.client.post('/api/proxy-routes',
                                      json={'name': 'px-b', 'url': 'socks4://h:1080'})
        self.assertEqual(bad_scheme.status_code, 400)
        bad_name = self.client.post('/api/proxy-routes',
                                    json={'name': '有中文', 'url': 'http://h:8080'})
        self.assertEqual(bad_name.status_code, 400)
        ok = self.client.post('/api/proxy-routes',
                              json={'name': 'px-b', 'url': 'http://user:pw@h:8080'})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(self._routes()['px-b'], 'http://user:pw@h:8080')
        self.assertEqual(ok.json()['masked'], 'http://user:***@h:8080')

    def test_create_keeps_other_config_keys(self) -> None:
        self.client.post('/api/proxy-routes', json={'name': 'px-b', 'url': 'http://h:1'})
        cfg = json.loads(self.cfg.read_text(encoding='utf-8'))
        self.assertEqual(cfg['api_key'], 'wbk_keep-me', '写线路把 api_key 弄丢了')
        self.assertEqual(cfg['schedule'], {'checkin': '08:00'}, '写线路弄丢了其它配置')

    def test_masked_url_on_create_is_rejected(self) -> None:
        """新建时地址里还留着 `***`：明确拒绝，而不是存一条没密码的记录。"""
        response = self.client.post('/api/proxy-routes',
                                    json={'name': 'px-new', 'url': 'socks5://u:***@h:1080'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('***', response.json()['detail'])
        self.assertNotIn('px-new', self._routes())

    def test_duplicate_is_rejected(self) -> None:
        again = self.client.post('/api/proxy-routes',
                                 json={'name': 'px-a', 'url': 'http://h:1'})
        self.assertEqual(again.status_code, 409)

    def test_edit_with_masked_password_keeps_the_secret(self) -> None:
        masked = self.client.get('/api/proxy-routes').json()['routes'][0]['masked']
        changed = masked.replace('eu.example.dev', 'new.example.dev')
        response = self.client.put('/api/proxy-routes/px-a', json={'url': changed})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self._routes()['px-a'],
                         'socks5://user:secret@new.example.dev:10000',
                         '提交脱敏串时没有沿用原来的密码')

    def test_edit_with_new_password_replaces_it(self) -> None:
        self.client.put('/api/proxy-routes/px-a',
                        json={'url': 'socks5://user:brand-new@eu.example.dev:10000'})
        self.assertEqual(self._routes()['px-a'],
                         'socks5://user:brand-new@eu.example.dev:10000')

    def test_rename_follows_bound_accounts(self) -> None:
        response = self.client.put('/api/proxy-routes/px-a',
                                   json={'new_name': 'px-cn', 'url': ''})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['bound_updated'], 1)
        self.assertIn('px-cn', self._routes())
        self.assertNotIn('px-a', self._routes())
        self.assertEqual(self._account_proxy('workbuddy-one.json'), 'px-cn',
                         '改名后账号仍绑着旧名字 —— 那些请求会直接失败')

    def test_delete_requires_force_when_accounts_are_bound(self) -> None:
        blocked = self.client.delete('/api/proxy-routes/px-a')
        self.assertEqual(blocked.status_code, 409)
        self.assertIn('px-a', self._routes())
        forced = self.client.delete('/api/proxy-routes/px-a?force=1')
        self.assertEqual(forced.status_code, 200, forced.text)
        self.assertEqual(forced.json()['bound_cleared'], 1)
        self.assertNotIn('px-a', self._routes())
        self.assertEqual(self._account_proxy('workbuddy-one.json'), '',
                         '删除线路后账号还留着绑定 —— 按设计会让请求失败')

    def test_import_reports_per_line_problems(self) -> None:
        text = '\n'.join([
            '# 注释行会被跳过',
            'px-x=http://user:pw@h1:8080',
            'px-y socks5://user:pw@h2:1080',
            '坏名字! =http://h3:8080',
            '',
        ])
        response = self.client.post('/api/proxy-routes/import', json={'text': text})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['imported'], 2)
        self.assertEqual(len(body['problems']), 1)
        self.assertEqual(self._routes()['px-y'], 'socks5://user:pw@h2:1080')

    def test_probe_reports_failure_reason(self) -> None:
        self.client.post('/api/proxy-routes', json={'name': 'px-dead',
                                                    'url': 'http://127.0.0.1:1'})
        body = self.client.post('/api/proxy-routes/probe', json={'name': 'px-dead'}).json()
        self.assertFalse(body['ok'])
        self.assertIn('Error', body['error'], f'没把失败原因说出来：{body}')
        self.assertGreaterEqual(body['ms'], 0)

    def test_writes_are_audited_without_secrets(self) -> None:
        self.client.post('/api/proxy-routes',
                         json={'name': 'px-b', 'url': 'http://user:pw@h:8080'})
        rows = db.query("SELECT action, target, detail FROM audit_logs ORDER BY id DESC")
        self.assertTrue(rows, '写线路没有留下审计记录')
        self.assertEqual(rows[0]['action'], 'create_proxy_route')
        self.assertEqual(rows[0]['target'], 'px-b')
        self.assertNotIn('pw', rows[0]['detail'], '审计详情里出现了密码')


class ProxySettingsApiTest(_Base):
    def test_env_is_the_default_source(self) -> None:
        with mock.patch.object(config, 'HTTP_PROXY', 'http://env-proxy:7890'):
            body = self.client.get('/api/settings/proxy').json()
        self.assertEqual(body['source'], 'env')
        self.assertFalse(body['http_configured'])
        self.assertIn('env-proxy', body['env_http_proxy'])
        self.assertEqual(body['effective'], 'http://env-proxy:7890')

    def test_save_overrides_env_and_masks_on_read(self) -> None:
        with mock.patch.object(config, 'HTTP_PROXY', 'http://env-proxy:7890'):
            saved = self.client.put('/api/settings/proxy',
                                    json={'http_proxy': 'socks5://u:pw@h:1080'})
            self.assertEqual(saved.status_code, 200, saved.text)
            body = self.client.get('/api/settings/proxy').json()
        self.assertEqual(body['source'], 'panel')
        self.assertTrue(body['http_configured'])
        self.assertEqual(body['effective'], 'socks5://u:***@h:1080')
        self.assertEqual(body['env_http_proxy'], 'http://env-proxy:7890',
                         '面板设置生效时也要把环境变量的值显示出来（用户要能分辨）')
        self.assertNotIn('pw@', json.dumps(body), '读接口回显了代理密码')
        self.assertEqual(db.get_setting('http_proxy'), 'socks5://u:pw@h:1080',
                         '完整地址应落在面板设置里（服务端要用）')
        self.assertEqual(config.effective_http_proxy(), 'socks5://u:pw@h:1080')

    def test_masked_value_keeps_the_stored_password(self) -> None:
        self.client.put('/api/settings/proxy', json={'http_proxy': 'socks5://u:pw@h:1080'})
        self.client.put('/api/settings/proxy', json={'http_proxy': 'socks5://u:***@h:2080'})
        self.assertEqual(db.get_setting('http_proxy'), 'socks5://u:pw@h:2080')

    def test_masked_value_without_stored_secret_is_rejected(self) -> None:
        response = self.client.put('/api/settings/proxy',
                                   json={'http_proxy': 'socks5://u:***@h:1080'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('***', response.json()['detail'])

    def test_clear_falls_back_to_env(self) -> None:
        self.client.put('/api/settings/proxy', json={'http_proxy': 'http://panel:1111'})
        self.client.put('/api/settings/proxy', json={'http_proxy': ''})
        with mock.patch.object(config, 'HTTP_PROXY', 'http://env-proxy:7890'):
            body = self.client.get('/api/settings/proxy').json()
        self.assertEqual(body['source'], 'env')
        self.assertEqual(body['effective'], 'http://env-proxy:7890')

    def test_global_probe_says_so_when_nothing_is_configured(self) -> None:
        with mock.patch.object(config, 'HTTP_PROXY', ''):
            body = self.client.post('/api/settings/proxy/probe').json()
        self.assertFalse(body['ok'])
        self.assertIn('没有配置', body['error'])


class UpdaterEnvInjectionTest(_Base):
    """面板里配的代理要交给更新器进程（它只认环境变量）。"""

    def test_proxies_are_passed_to_the_updater(self) -> None:
        self.client.put('/api/settings/proxy', json={
            'http_proxy': 'socks5://u:pw@h:1080',
            'update_proxy': 'http://u:pw@up:7890',
        })
        from server.services import updater

        script = self.root / 'update.py'
        script.write_text('# 占位：只为让「脚本存在」这一检查通过\n', encoding='utf-8')
        seen: dict = {}

        class _FakeProc:
            pid = 1

        def fake_popen(cmd, **kwargs):
            seen.update(kwargs.get('env') or {})
            return _FakeProc()

        # LOCK_FILE 也要指到临时目录：不 patch 时它落在仓库的 data/ 下，而
        # `_claim_lock()` 会真的建锁并把 pid 改写成假 worker pid（1）。下次跑
        # 同一用例时 `_lock_active()` 认为更新仍在进行，这条与锁无关的用例就会
        # 莫名其妙变红 —— 套件因此不可重复。
        with mock.patch.object(updater, '_updater_script', return_value=script), \
                mock.patch.object(updater, '_lock_active', return_value=False), \
                mock.patch.object(updater.subprocess, 'Popen', fake_popen), \
                mock.patch.object(updater, '_write_initial_status'), \
                mock.patch.object(updater, 'STATUS_FILE', self.root / 'status.json'), \
                mock.patch.object(updater, 'LOG_FILE', self.root / 'update.log'), \
                mock.patch.object(updater, 'LOCK_FILE', self.root / 'update.lock'):
            ok, message = updater.start_update('manager')
        self.assertTrue(ok, message)
        self.assertEqual(seen.get('WB_HTTP_PROXY'), 'socks5://u:pw@h:1080')
        self.assertEqual(seen.get('WB_UPDATE_PROXY'), 'http://u:pw@up:7890')


if __name__ == '__main__':
    unittest.main()


class ProxyWriteFailureTest(_Base):
    """配置文件读得到、写不进去时必须**说清原因**，不能裸露成 500（issue #341）。

    现场：界面上只弹一句英文 `Internal Server Error`，而真实原因是面板进程对
    `config.json` 没有写权限（只读挂载 / root 属主 / SELinux 标签）。读得到、
    写不了是最迷惑的一种 —— 页面能把线路全列出来，一保存就 500。
    """

    def test_readonly_config_reports_409_with_path(self) -> None:
        from server.services.proxyroutes import RouteError
        real = proxyroutes._atomic_write_json

        def boom(path, payload):
            raise PermissionError(13, 'Permission denied')

        with mock.patch.object(proxyroutes, '_atomic_write_json', side_effect=boom):
            response = self.client.post('/api/proxy-routes',
                                        json={'name': 'px-new', 'url': 'socks5://h.example.dev:1080'})
        self.assertEqual(response.status_code, 409,
                         f'写失败不该是 5xx：{response.status_code} {response.text}')
        detail = response.json()['detail']
        self.assertIn(str(self.cfg), detail, '提示里要有具体文件路径')
        self.assertIn('写权限', detail, '提示里要说清是权限问题')
        self.assertTrue(real)  # 原函数仍在（没被误改）

    def test_write_failure_does_not_touch_the_file(self) -> None:
        """写失败后配置文件必须原样（不能留下半截或丢了别的键）。"""
        before = self.cfg.read_text(encoding='utf-8')
        with mock.patch.object(proxyroutes, '_atomic_write_json',
                               side_effect=OSError('disk full')):
            self.client.post('/api/proxy-routes',
                             json={'name': 'px-new', 'url': 'socks5://h.example.dev:1080'})
        self.assertEqual(self.cfg.read_text(encoding='utf-8'), before)


class DirectRouteBindingTest(unittest.TestCase):
    """账号绑「仅直连」（`__direct__`）不能被当成不存在的线路（issue #342）。

    `__direct__` 是与上游 `DirectRouteName` 同一个保留名，`auths/*.json` 里就写它。
    面板侧此前把它当普通线路名去查表，于是抛「代理线路 '__direct__' 不存在」——
    而这个绑定是**合法**的，上游完全认。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        cfg = root / 'config.json'
        cfg.write_text(json.dumps({'proxies': {'px-a': 'socks5://h.example.dev:1080'}}),
                       encoding='utf-8')
        for name, value in (('UPSTREAM_CONFIG', cfg), ('HTTP_PROXY', ''),
                            ('DEFAULT_ACCOUNT_PROXY', '')):
            patch = mock.patch.object(config, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_direct_route_returns_empty_not_error(self) -> None:
        from server import config as cfg
        self.assertEqual(cfg.resolve_account_proxy({'proxy': cfg.DIRECT_ROUTE}), '')

    def test_direct_route_bypasses_pool_and_global(self) -> None:
        """即使代理池开着、全局代理配了，也要直连（这才是「仅直连」的语义）。"""
        from server import config as cfg
        with mock.patch.object(cfg, 'proxy_pool_config',
                               return_value={'enabled': True, 'strategy': 'weight', 'interval_seconds': 0}),                 mock.patch.object(cfg, 'pool_routes',
                                  return_value={'px-a': 'socks5://h.example.dev:1080'}),                 mock.patch.object(cfg, 'global_http_proxy',
                                  return_value='http://global.example.dev:3128'):
            self.assertEqual(cfg.resolve_account_proxy({'proxy': cfg.DIRECT_ROUTE}), '')

    def test_unknown_route_still_rejected(self) -> None:
        from server import config as cfg
        with self.assertRaises(ValueError) as ctx:
            cfg.resolve_account_proxy({'proxy': 'px-gone'})
        self.assertIn('不存在', str(ctx.exception))

    def test_constant_matches_upstream_reserved_name(self) -> None:
        """与上游 `internal/upstream/transport.go` 的 DirectRouteName 必须是同一个值。"""
        from server import config as cfg
        self.assertEqual(cfg.DIRECT_ROUTE, '__direct__')
