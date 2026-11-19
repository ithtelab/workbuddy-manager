"""代理池方案的行为测试（参照 Antigravity-Manager 的池 + 权重 + 绑定 + 全局兜底）。

锁五件事：
  1. **优先级**：账号绑定的线路 > 代理池（按策略挑）> 全局上游代理 > 直连；
  2. **绑定的线路不可用要报错**（删了 / 停用了），而不是悄悄换出口 —— 出口被换掉
     在风控里就是特征变化；
  3. **池策略**：priority（按权重降序取第一）/ round_robin（依次轮）/
     random（加权随机）/ least_connections（最近调用最少）；
  4. **间隔内粘住**：同一个账号在间隔窗口内固定走同一条（别一会儿换一个出口）；
  5. **单绑定语义**：绑定就是覆盖原来那条；解除 = 跟随池策略。
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
from server.routers import proxies
from server.services import proxyroutes as pr

ADMIN = {'username': 'admin', 'role': 'admin'}
A = 'socks5://u:pw@a.example.dev:10000'
B = 'socks5://u:pw@b.example.dev:10000'


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.auth_dir = self.root / 'auths'
        self.auth_dir.mkdir()
        self.cfg = self.root / 'config.json'
        self._write_config({'px-a': A, 'px-b': B})
        for name, value in (('AUTH_DIR', self.auth_dir), ('UPSTREAM_CONFIG', self.cfg),
                            ('HTTP_PROXY', '')):
            patch = mock.patch.object(config, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(config, 'DB_PATH', self.root / 'm.db')
        patch.start()
        self.addCleanup(patch.stop)
        db._conn = None
        db.connect()
        self.addCleanup(self._close_db)
        for key in ('proxy_pool', 'proxy_sticky', 'http_proxy', 'global_proxy_enabled'):
            db.set_setting(key, None if key == 'proxy_pool' else {})
        db.set_setting('proxy_pool', {})

        self._account('workbuddy-a.json', 'uid-aaa', '账号A')
        self._account('workbuddy-b.json', 'uid-bbb', '账号B')

        app = FastAPI()
        app.include_router(proxies.router)
        app.dependency_overrides[security.current_user] = lambda: ADMIN
        app.dependency_overrides[security.require_session_admin] = lambda: ADMIN
        self.client = TestClient(app)

    def _close_db(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None

    def _write_config(self, routes: dict) -> None:
        self.cfg.write_text(json.dumps({'api_key': 'keep', 'proxies': routes},
                                       ensure_ascii=False, indent=2), encoding='utf-8')

    def _account(self, name: str, uid: str, nickname: str, **extra) -> None:
        doc = {'account': {'uid': uid, 'nickname': nickname}, 'auth': {'accessToken': 't'}}
        doc.update(extra)
        (self.auth_dir / name).write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')

    def _read(self, name: str) -> dict:
        return json.loads((self.auth_dir / name).read_text(encoding='utf-8'))

    def _auth(self, name: str) -> dict:
        """账号文件 → 路由层实际传下去的**扁平**字典（uid / 昵称 / 线路 / 会话名）。"""
        doc = self._read(name)
        account = doc.get('account') or {}
        return {'uid': str(account.get('uid') or ''),
                'nickname': str(account.get('nickname') or ''),
                'proxy': str(doc.get('proxy') or ''),
                'proxy_session': str(doc.get('proxy_session') or '')}

    def _pool(self, **over) -> None:
        db.set_setting('proxy_pool', {'enabled': True, 'strategy': 'priority',
                                      'interval_seconds': 300, **over})
        config._RECENT_CALLS.clear()
        config._ROUND_ROBIN['cursor'] = 0


class PriorityOrderTest(_Base):
    def test_bound_route_wins_over_pool_and_global(self) -> None:
        self._pool()
        db.set_setting('http_proxy', 'socks5://global:pw@g.example.dev:1080')
        self._account('workbuddy-a.json', 'uid-aaa', '账号A', proxy='px-b')
        chosen = config.resolve_account_proxy(self._auth('workbuddy-a.json'))
        self.assertEqual(chosen, B, '绑定优先于池与全局')

    def test_pool_beats_global(self) -> None:
        self._pool()
        db.set_setting('http_proxy', 'socks5://global:pw@g.example.dev:1080')
        self.assertIn(config.resolve_account_proxy(self._auth('workbuddy-a.json')), (A, B))

    def test_global_proxy_is_the_fallback(self) -> None:
        db.set_setting('http_proxy', 'socks5://global:pw@g.example.dev:1080')
        chosen = config.resolve_account_proxy(self._auth('workbuddy-a.json'))
        self.assertEqual(chosen, 'socks5://global:pw@g.example.dev:1080')

    def test_global_proxy_switch_off_falls_back_to_direct(self) -> None:
        db.set_setting('http_proxy', 'socks5://global:pw@g.example.dev:1080')
        db.set_setting('global_proxy_enabled', False)
        self.assertIsNone(config.resolve_account_proxy(self._auth('workbuddy-a.json')))

    def test_nothing_configured_is_direct(self) -> None:
        self.assertIsNone(config.resolve_account_proxy(self._auth('workbuddy-a.json')))

    def test_disabled_or_missing_binding_raises(self) -> None:
        """绑定的线路被停用 / 被删：报错，不静默换出口。"""
        self._account('workbuddy-a.json', 'uid-aaa', '账号A', proxy='px-a')
        self._write_config({'px-a': {'url': A, 'enabled': False}})
        with self.assertRaises(ValueError) as ctx:
            config.resolve_account_proxy(self._auth('workbuddy-a.json'))
        self.assertIn('停用', str(ctx.exception))

        self._write_config({'px-b': B})
        with self.assertRaises(ValueError) as ctx2:
            config.resolve_account_proxy(self._auth('workbuddy-a.json'))
        self.assertIn('不存在', str(ctx2.exception))


class StrategyTest(_Base):
    def test_priority_prefers_the_heaviest_weight(self) -> None:
        self._write_config({'px-a': {'url': A, 'weight': 10}, 'px-b': {'url': B, 'weight': 500}})
        self._pool(strategy='priority')
        self.assertEqual(config.resolve_account_proxy(self._auth('workbuddy-a.json')), B)

    def test_round_robin_cycles(self) -> None:
        self._pool(strategy='round_robin', interval_seconds=0)
        seen = [config.resolve_account_proxy(self._auth('workbuddy-a.json')) for _ in range(4)]
        self.assertEqual(set(seen), {A, B}, f'轮询没轮到两条：{seen}')
        self.assertNotEqual(seen[0], seen[1], '连续两次应当换一条')

    def test_random_respects_weights(self) -> None:
        self._write_config({'px-a': {'url': A, 'weight': 1000}, 'px-b': {'url': B, 'weight': 1}})
        self._pool(strategy='random', interval_seconds=0)
        picks = [config.resolve_account_proxy(self._auth('workbuddy-a.json')) for _ in range(80)]
        self.assertGreater(picks.count(A), 60, f'权重没起作用：{picks.count(A)}/80')

    def test_least_connections_prefers_the_idle_route(self) -> None:
        self._pool(strategy='least_connections')
        # 先让 A 被用掉一次，下一次应当轮到 B
        first = config.resolve_account_proxy(self._auth('workbuddy-a.json'))
        second = config.resolve_account_proxy(self._auth('workbuddy-b.json'))
        self.assertNotEqual(first, second, 'least_connections 没挑空闲的那条')

    def test_interval_keeps_the_same_route(self) -> None:
        self._pool(strategy='round_robin', interval_seconds=300)
        first = config.resolve_account_proxy(self._auth('workbuddy-a.json'))
        for _ in range(4):
            self.assertEqual(config.resolve_account_proxy(self._auth('workbuddy-a.json')), first,
                             '间隔窗口内换了出口 —— 风控里这就是特征')

    def test_pool_skips_disabled_routes(self) -> None:
        self._write_config({'px-a': {'url': A, 'enabled': False}, 'px-b': B})
        self._pool()
        self.assertEqual(config.resolve_account_proxy(self._auth('workbuddy-a.json')), B,
                         '池里挑到了停用的线路')


class BindingApiTest(_Base):
    def test_bind_and_unbind_are_single_and_keep_other_fields(self) -> None:
        response = self.client.post('/api/proxy-routes/px-a/accounts',
                                    json={'files': ['workbuddy-a.json'], 'bound': True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self._read('workbuddy-a.json')['proxy'], 'px-a')
        again = self.client.post('/api/proxy-routes/px-b/accounts',
                                 json={'files': ['workbuddy-a.json'], 'bound': True})
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(self._read('workbuddy-a.json')['proxy'], 'px-b', '绑定应覆盖原来那条')

        cleared = self.client.post('/api/proxy-routes/px-b/accounts',
                                   json={'files': ['workbuddy-a.json'], 'bound': False}).json()
        self.assertEqual(cleared['updated'], 1)
        self.assertNotIn('proxy', self._read('workbuddy-a.json'),
                         '解除绑定后应跟随池策略')

    def test_binding_manager_lists_accounts_and_routes(self) -> None:
        self.client.post('/api/proxy-routes/px-a/accounts',
                         json={'files': ['workbuddy-a.json'], 'bound': True})
        body = self.client.get('/api/proxy-routes/bindings').json()
        by_file = {a['file']: a for a in body['accounts']}
        self.assertEqual(by_file['workbuddy-a.json']['proxy'], 'px-a')
        self.assertEqual(by_file['workbuddy-b.json']['proxy'], '')
        self.assertEqual(body['routes'], ['px-a', 'px-b'])
        self.assertIn('pool', body)

    def test_legacy_pool_field_is_cleaned_and_session_kept(self) -> None:
        """上一版的 proxy_pool 绑定时清掉；proxy_session 留着（`{account}` 模板还靠它）。"""
        self._account('workbuddy-a.json', 'uid-aaa', '账号A',
                      proxy_pool=['px-a', 'px-b'], proxy_session='old')
        self.client.post('/api/proxy-routes/px-a/accounts',
                         json={'files': ['workbuddy-a.json'], 'bound': True})
        doc = self._read('workbuddy-a.json')
        self.assertNotIn('proxy_pool', doc)
        self.assertEqual(doc.get('proxy_session'), 'old')

    def test_weight_and_enabled_roundtrip(self) -> None:
        self.client.put('/api/proxy-routes/px-a', json={'weight': 30, 'enabled': False})
        stored = json.loads(self.cfg.read_text(encoding='utf-8'))['proxies']['px-a']
        self.assertEqual(stored, {'url': A, 'weight': 30, 'enabled': False})
        listing = self.client.get('/api/proxy-routes').json()
        item = {r['name']: r for r in listing['routes']}['px-a']
        self.assertEqual(item['weight'], 30)
        self.assertFalse(item['enabled'])
        self.assertIn('pool', listing)
        bad = self.client.put('/api/proxy-routes/px-a', json={'weight': 0})
        self.assertEqual(bad.status_code, 400)

    def test_pool_settings_are_saved_and_validated(self) -> None:
        saved = self.client.put('/api/settings/proxy-pool',
                                json={'enabled': True, 'strategy': 'round_robin',
                                      'interval_seconds': 60}).json()
        self.assertTrue(saved['enabled'])
        self.assertEqual(saved['strategy'], 'round_robin')
        self.assertEqual(config.proxy_pool_config()['interval_seconds'], 60)
        bad = self.client.put('/api/settings/proxy-pool', json={'strategy': 'nope'})
        self.assertEqual(bad.status_code, 400)

    def test_global_proxy_switch_roundtrip(self) -> None:
        self.client.put('/api/settings/proxy', json={'http_proxy': 'http://p:1', 'global_proxy_enabled': True})
        self.assertTrue(self.client.get('/api/settings/proxy').json()['global_proxy_enabled'])
        self.client.put('/api/settings/proxy', json={'global_proxy_enabled': False})
        self.assertFalse(self.client.get('/api/settings/proxy').json()['global_proxy_enabled'])
        self.assertEqual(self.client.get('/api/settings/proxy').json()['source'], 'off')

    def test_delete_and_rename_follow_bindings(self) -> None:
        self.client.post('/api/proxy-routes/px-a/accounts',
                         json={'files': ['workbuddy-a.json'], 'bound': True})
        self.client.put('/api/proxy-routes/px-a', json={'new_name': 'px-cn'})
        self.assertEqual(self._read('workbuddy-a.json')['proxy'], 'px-cn', '改名没跟着绑定')
        blocked = self.client.delete('/api/proxy-routes/px-cn')
        self.assertEqual(blocked.status_code, 409)
        forced = self.client.delete('/api/proxy-routes/px-cn?force=1').json()
        self.assertEqual(forced['bound_cleared'], 1)
        self.assertNotIn('proxy', self._read('workbuddy-a.json'))

    def test_auto_assign_fills_unbound_first(self) -> None:
        self.client.post('/api/proxy-routes/px-b/accounts',
                         json={'files': ['workbuddy-b.json'], 'bound': True})
        result = self.client.post('/api/proxy-routes/px-a/auto-assign',
                                  json={'count': 1}).json()
        self.assertEqual(result['updated'], 1)
        self.assertEqual(self._read('workbuddy-a.json')['proxy'], 'px-a',
                         '自动分配应优先挑没绑线路的账号')
        self.assertEqual(result['unbound_left'], 0)

    def test_import_is_capped(self) -> None:
        """一次导入有条数上限（每条都要写一次配置，粘贴错文件不该变成几千次写盘）。"""
        text = '\n'.join(f'px-{i}=socks5://u:pw@h{i}.example.dev:10000'
                         for i in range(pr.MAX_IMPORT + 1))
        bad = self.client.post('/api/proxy-routes/import', json={'text': text})
        self.assertEqual(bad.status_code, 400)
        self.assertIn('最多导入', bad.text)
        stored = json.loads(self.cfg.read_text(encoding='utf-8'))['proxies']
        self.assertNotIn('px-1', stored, '被拒绝时不该已经写进去一部分')

    def test_health_is_recorded_and_listed(self) -> None:
        pr.record_probe('px-a', {'ok': False, 'ms': 12, 'error': 'ConnectError: boom'})
        item = {r['name']: r for r in self.client.get('/api/proxy-routes').json()['routes']}
        self.assertFalse(item['px-a']['probe']['ok'])
        self.assertEqual(item['px-a']['probe']['error'], 'ConnectError: boom')
        self.assertEqual(item['px-a']['bound'], 0)


class LegacyTemplateTest(_Base):
    """1.0.84 的 `{account}` 会话模板地址：新界面不再提供，但**必须还能用**。

    那一版已经发布过，用户可能照着写了这种地址。现在换成池方案后，如果不再渲染
    占位符，这些账号的请求会拿着字面量 `{account}` 去代理认证 → 直接挂掉。所以
    渲染保留（会话名沿用账号文件里存下来的 `proxy_session`），只是界面不再教。
    """

    TPL = 'socks5://demo_s_{account}:pw@gw.example.dev:10000'

    def test_bound_template_renders_per_account(self) -> None:
        self._write_config({'px-tpl': self.TPL})
        self._account('workbuddy-a.json', 'uid-aaa', '账号A', proxy='px-tpl',
                      proxy_session='sess-a')
        self._account('workbuddy-b.json', 'uid-bbb', '账号B', proxy='px-tpl',
                      proxy_session='sess-b')
        a = config.resolve_account_proxy(self._auth('workbuddy-a.json'))
        b = config.resolve_account_proxy(self._auth('workbuddy-b.json'))
        self.assertEqual(a, 'socks5://demo_s_sess-a:pw@gw.example.dev:10000')
        self.assertEqual(b, 'socks5://demo_s_sess-b:pw@gw.example.dev:10000')
        self.assertNotEqual(a, b, '两个账号各自一个会话名 = 各自一个出口 IP')

    def test_pool_route_renders_too(self) -> None:
        self._write_config({'px-tpl': self.TPL})
        self._pool()
        self._account('workbuddy-a.json', 'uid-aaa', '账号A', proxy_session='sess-a')
        self.assertEqual(config.resolve_account_proxy(self._auth('workbuddy-a.json')),
                         'socks5://demo_s_sess-a:pw@gw.example.dev:10000')

    def test_session_name_falls_back_to_nickname_then_uid(self) -> None:
        self._write_config({'px-tpl': self.TPL})
        self._account('workbuddy-a.json', 'uid-aaa', 'Account-A', proxy='px-tpl')
        self.assertEqual(config.resolve_account_proxy(self._auth('workbuddy-a.json')),
                         'socks5://demo_s_account-a:pw@gw.example.dev:10000')
        self._account('workbuddy-b.json', 'uid-bbb', '中文昵称', proxy='px-tpl')
        self.assertEqual(config.resolve_account_proxy(self._auth('workbuddy-b.json')),
                         'socks5://demo_s_uid-bbb:pw@gw.example.dev:10000')

    def test_placeholder_survives_storage_and_masking(self) -> None:
        """写进去、读出来都不能把它百分号编码 —— 否则再也渲染不出来。"""
        created = self.client.post('/api/proxy-routes',
                                   json={'name': 'px-tpl', 'url': self.TPL})
        self.assertEqual(created.status_code, 200, created.text)
        stored = json.loads(self.cfg.read_text(encoding='utf-8'))['proxies']['px-tpl']
        self.assertEqual(stored, self.TPL)
        item = {r['name']: r for r in self.client.get('/api/proxy-routes').json()['routes']}
        self.assertEqual(item['px-tpl']['masked'],
                         'socks5://demo_s_{account}:***@gw.example.dev:10000',
                         '列表里显示的应是用户当初写的那串（密码脱敏）')

    def test_plain_route_is_untouched(self) -> None:
        self._pool()
        self.assertIn(config.resolve_account_proxy(self._auth('workbuddy-a.json')), (A, B),
                      '没有占位符的线路按原样使用（池策略挑一条）')

    def test_legacy_multi_bind_keeps_its_primary_route(self) -> None:
        """1.0.84 多绑的账号（`proxy` + `proxy_pool`）：升级后仍走原来那条主线路。

        那一版把「第一条」写进 `proxy`、整组写进 `proxy_pool`；新方案一个账号只认
        `proxy`，所以这类账号不会因为升级而变成直连 —— 只是不再在多条之间分摊。
        """
        self._write_config({'px-a': A, 'px-b': B, 'px-c': 'socks5://u:pw@c.example.dev:10000'})
        self._account('workbuddy-a.json', 'uid-aaa', '账号A', proxy='px-b',
                      proxy_pool=['px-b', 'px-c'])
        chosen = config.resolve_account_proxy(self._auth('workbuddy-a.json'))
        self.assertEqual(chosen, B, '升级后仍走 proxy 里那条（原来的第一条）')


if __name__ == '__main__':
    unittest.main()


class ProxyErrorHintTest(unittest.TestCase):
    """代理测活失败时给可行动提示（issue #379）。"""

    def test_protocol_mismatch(self) -> None:
        hint = pr._proxy_error_hint(ConnectionError('Malformed reply from SOCKS server'))
        self.assertIn('协议不匹配', hint)

    def test_wrong_version_number(self) -> None:
        hint = pr._proxy_error_hint(ssl_error('SSL: WRONG_VERSION_NUMBER'))
        self.assertIn('TLS 握手失败', hint)

    def test_socks_ruleset(self) -> None:
        hint = pr._proxy_error_hint(ConnectionError(
            'Proxy Server could not connect: Connection not allowed by ruleset'))
        self.assertIn('白名单', hint)

    def test_unknown_error_has_no_hint(self) -> None:
        self.assertEqual(pr._proxy_error_hint(RuntimeError('boom')), '')


class ssl_error(Exception):
    """测试用异常：类名里带 SSL，模拟 ssl.SSLError 的文案。"""
