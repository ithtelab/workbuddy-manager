"""管理面作用域化 API Token 的回归测试（见 docs/api-tokens.md）。

锁定的性质（每一条都能在实现里追溯到动机）：

  * 明文**只在创建时返回一次**，列表永远不回传明文或哈希；
  * scope 决定角色，且**非法 scope 降级为最小权限**（宁可给少了）；
  * readonly 不能调写接口；admin 能调一般写接口；
  * **token 管理接口只接受会话**——泄露的 token 不能拿来自助提权；
  * **高危接口只接受会话**——即便 token 是 admin scope（更新、清日志、用户管理…）；
  * 过期 / 停用 / 删除后**立即失效**（逐次校验，无缓存）；
  * 失败**不泄露原因**（不存在 / 停用 / 过期 都是同一个 401）；
  * 令牌不绑定用户：改动用户表不影响已签发的令牌。

注意：鉴权**会话优先**，而 `TestClient` 自带 cookie jar——若登录后不清空，
后续带 Bearer 的请求会被会话顶掉，用例会全部「假通过」（其实测的是会话）。
所有会话调用都走 `_as_session()`，退出即清空 jar。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, security, tokensvc  # noqa: E402


class ApiTokenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        d = Path(cls._tmp.name)
        cls._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = d / 'tokens.db'
        config.USERS_FILE = d / 'users.json'
        config.STATIC_DIR = d / 'no-static'
        config.USERS_FILE.write_text(json.dumps({
            'secret': 'S' * 64,
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('admin-pw')}],
            'api_keys': [],
        }), encoding='utf-8')
        db._conn = None
        db.connect()
        from server.main import app
        cls.c = TestClient(app)

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

    def setUp(self) -> None:
        # 令牌校验失败会走按 IP 的失败计数（防爆破）。用例里会刻意发坏令牌，
        # 必须每次清干净，否则后面的登录会被自己锁掉（429）。
        security._fail.clear()
        security._user_fail.clear()
        self.c.cookies.clear()

    def tearDown(self) -> None:
        security._fail.clear()
        security._user_fail.clear()
        self.c.cookies.clear()

    # ── 工具 ─────────────────────────────────────────────
    @contextmanager
    def _as_session(self):
        """以**会话**身份执行，退出时清空 cookie jar。"""
        r = self.c.post('/api/login', json={'username': 'admin', 'password': 'admin-pw'})
        self.assertEqual(r.status_code, 200, r.text)
        self.c.cookies.update(dict(r.cookies))
        try:
            yield self.c
        finally:
            self.c.cookies.clear()

    def _mint(self, name: str, scope: str = 'readonly', expires_at=None) -> str:
        """用会话创建一个令牌，返回明文。"""
        with self._as_session() as c:
            r = c.post('/api/tokens',
                       json={'name': name, 'scope': scope, 'expires_at': expires_at})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()['token']

    @staticmethod
    def _bearer(token: str) -> dict:
        return {'Authorization': f'Bearer {token}'}

    def _id_of(self, token: str) -> int:
        row = tokensvc.resolve(token)
        assert row is not None
        return int(row['id'])

    # ── 创建 / 列表 ───────────────────────────────────────
    def test_create_returns_plaintext_once_and_list_hides_it(self) -> None:
        with self._as_session() as c:
            created = c.post('/api/tokens', json={'name': 'ci', 'scope': 'readonly'}).json()
            listed = c.get('/api/tokens').json()
        self.assertTrue(created['token'].startswith('wbt_'), '明文应带 wbt_ 前缀')
        self.assertEqual(created['prefix'], created['token'][:12])
        row = next(t for t in listed if t['id'] == created['id'])
        self.assertNotIn('token', row, '列表绝不能回传明文')
        self.assertNotIn('token_hash', row, '列表绝不能回传哈希')

    def test_blank_name_rejected(self) -> None:
        with self._as_session() as c:
            r = c.post('/api/tokens', json={'name': '   '})
        self.assertEqual(r.status_code, 400)

    def test_invalid_scope_downgrades_to_readonly(self) -> None:
        """非法 scope 必须降级为最小权限，而不是报错或意外给管理员。"""
        self.assertEqual(tokensvc.normalize_scope('ADMIN '), 'admin')
        self.assertEqual(tokensvc.normalize_scope('root'), 'readonly')
        self.assertEqual(tokensvc.normalize_scope(''), 'readonly')
        self.assertEqual(tokensvc.normalize_scope(None), 'readonly')

    # ── 鉴权 ─────────────────────────────────────────────
    def test_token_authenticates_and_role_follows_scope(self) -> None:
        ro = self._mint('ro', 'readonly')
        r = self.c.get('/api/me', headers=self._bearer(ro))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['role'], 'viewer')
        self.assertTrue(r.json()['username'].startswith('token:'))

        adm = self._mint('adm', 'admin')
        self.assertEqual(self.c.get('/api/me', headers=self._bearer(adm)).json()['role'], 'admin')

    def test_readonly_cannot_write(self) -> None:
        ro = self._mint('ro2', 'readonly')
        r = self.c.post('/api/accounts/checkin-all', headers=self._bearer(ro))
        self.assertEqual(r.status_code, 403, '只读令牌不能调写接口')

    def test_admin_token_can_write_general_endpoint(self) -> None:
        """admin 令牌能调一般写接口（密钥轮换是自动化的典型用途）。"""
        adm = self._mint('adm2', 'admin')
        h = self._bearer(adm)
        r = self.c.post('/api/keys', json={'name': 'by-token'}, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        key_id = r.json()['id']
        self.assertEqual(self.c.delete(f'/api/keys/{key_id}', headers=h).status_code, 200)

    def test_bad_tokens_do_not_leak_reason(self) -> None:
        """不存在的 / 停用的 / 过期的，都必须是同一个笼统 401。"""
        disabled = self._mint('dis')
        with self._as_session() as c:
            c.patch(f'/api/tokens/{self._id_of(disabled)}', json={'enabled': False})
        expired = self._mint('exp', expires_at=int(time.time()) - 10)
        for tok in ('wbt_not_a_real_token', disabled, expired, 'wbk_gateway_key'):
            r = self.c.get('/api/me', headers=self._bearer(tok))
            self.assertEqual(r.status_code, 401, f'{tok[:12]} 应被拒')
            self.assertEqual(r.json().get('detail'), '未登录', '不得泄露失败原因')

    def test_deleted_token_immediately_invalid(self) -> None:
        tok = self._mint('todelete')
        self.assertEqual(self.c.get('/api/me', headers=self._bearer(tok)).status_code, 200)
        with self._as_session() as c:
            r = c.delete(f'/api/tokens/{self._id_of(tok)}')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.get('/api/me', headers=self._bearer(tok)).status_code, 401)

    def test_expired_token_immediately_invalid(self) -> None:
        tok = self._mint('exp2', expires_at=int(time.time()) - 1)
        self.assertEqual(self.c.get('/api/me', headers=self._bearer(tok)).status_code, 401)

    def test_tampered_token_rejected(self) -> None:
        tok = self._mint('tamper')
        bad = tok[:-1] + ('A' if tok[-1] != 'A' else 'B')
        self.assertIsNone(tokensvc.resolve(bad), '改一个字符就应解析不出来')

    # ── 边界：token 不能管 token、不能碰高危接口 ──────────────
    def test_token_cannot_manage_tokens(self) -> None:
        """泄露的 token 不能用来创建 / 列表 / 删除令牌（防自助提权与持久化）。"""
        h = self._bearer(self._mint('adm3', 'admin'))
        self.assertEqual(self.c.get('/api/tokens', headers=h).status_code, 403)
        self.assertEqual(self.c.post('/api/tokens', json={'name': 'self'}, headers=h).status_code, 403)
        self.assertEqual(self.c.delete('/api/tokens/1', headers=h).status_code, 403)

    def test_admin_token_rejected_on_session_only_endpoints(self) -> None:
        """高危 / 不可逆接口只对会话开放——即便 token 是 admin scope。"""
        h = self._bearer(self._mint('adm4', 'admin'))
        cases = [
            ('post', '/api/logs/clear', None),
            ('post', '/api/system/update', {'target': 'manager'}),
            ('post', '/api/security/logs/clear', None),
            ('post', '/api/users', {'username': 'x', 'password': '12345678', 'role': 'viewer'}),
            # 评审补：这条同样是「重启上游容器」，与 /api/restart 同一个动作 ——
            # 不能因为走的是设置页那个路由就放宽给令牌（矩阵守卫也钉着它）。
            ('post', '/api/settings/upstash/reload', None),
        ]
        for method, path, body in cases:
            r = getattr(self.c, method)(path, headers=h, **({'json': body} if body else {}))
            self.assertEqual(r.status_code, 403, f'{path} 不应接受 API Token')
            self.assertIn('API Token', r.json().get('detail', ''))

    def test_session_still_allowed_on_session_only_endpoint(self) -> None:
        """会话调高危接口不受影响（新约束不能误伤正常人）。"""
        with self._as_session() as c:
            r = c.post('/api/logs/clear')
        self.assertEqual(r.status_code, 200, r.text)

    # ── 记账 ─────────────────────────────────────────────
    def test_last_used_is_throttled(self) -> None:
        """last_used 写库要节流：窗口内第二次不该再写。"""
        tid = self._id_of(self._mint('throttle'))
        tokensvc.touch(tid, '1.1.1.1')
        first = db.query_one('SELECT last_used_ip FROM api_tokens WHERE id = ?', (tid,))
        self.assertEqual(first['last_used_ip'], '1.1.1.1')
        tokensvc.touch(tid, '2.2.2.2')  # 窗口内 → 跳过
        again = db.query_one('SELECT last_used_ip FROM api_tokens WHERE id = ?', (tid,))
        self.assertEqual(again['last_used_ip'], '1.1.1.1', '节流窗口内不应改写')

    def test_create_and_delete_are_audited_without_plaintext(self) -> None:
        tok = self._mint('audited')
        with self._as_session() as c:
            c.delete(f'/api/tokens/{self._id_of(tok)}')
            logs = c.get('/api/audit-logs', params={'limit': 200}).json()
        actions = [row['action'] for row in logs['items']]
        self.assertIn('create_token', actions)
        self.assertIn('delete_token', actions)
        self.assertNotIn(tok, json.dumps(logs, ensure_ascii=False), '审计日志绝不能出现明文令牌')

    def test_token_does_not_depend_on_users_table(self) -> None:
        """令牌不绑定用户：改动用户表不影响已签发的令牌。"""
        tok = self._mint('decoupled', 'admin')
        cfg = security.load_users()
        cfg['users'] = []
        security.save_users(cfg)
        try:
            self.assertEqual(self.c.get('/api/me', headers=self._bearer(tok)).status_code, 200)
        finally:
            cfg['users'] = [{'username': 'admin', 'role': 'admin',
                             'pwd_hash': security.make_hash('admin-pw')}]
            security.save_users(cfg)


class WriteEndpointScopeMatrixTest(unittest.TestCase):
    """把「哪些写接口拿什么凭据」钉成清单——权限变更必须显式改这里。

    为什么要有：作用域是靠**每个接口的依赖**表达的（`require_session_admin` /
    `require_admin` / `current_user`），散在十几个文件里。评审时我逐个枚举过一遍，
    抓到一处不一致：`/api/settings/upstash/reload` 与 `/api/restart` 是**同一个动作**
    （都走 `reload.restart_now()`，重启上游容器），前者却只要求 `require_admin`，
    于是管理令牌能把上游重启掉——那条「服务级动作只对真人开放」的边界就穿了。

    这类漏洞靠人读 diff 很容易漏（分散、且每个接口单看都合理），所以：

      · `SESSION_ONLY` 里的接口一旦放宽，这里立刻红；
      · `ANY_LOGGED_IN`（**只读令牌也能调的写接口**）最危险，单独钉住——
        加进去就必须先回答「只读为什么可以写」；
      · 只断言类别归属，不锁与权限无关的新增接口（新增写接口若落在
        `require_admin` 里不会红，那是允许的；落到另外两类才需要人来确认）。
    """

    # 服务级 / 不可逆 / 能提权或抹痕迹的动作，只对**会话**开放。
    SESSION_ONLY = {
        'DELETE /api/accounts/{filename}',
        'DELETE /api/system/update-status',   # 清除更新结果与日志：属于「抹掉痕迹」类
        'DELETE /api/security/rules/{rule_id}',
        'DELETE /api/tokens/{token_id}',
        'DELETE /api/users/{username}',
        'PATCH /api/tokens/{token_id}',
        'PATCH /api/users/{username}',
        'POST /api/checkin-logs/clear',
        'POST /api/logs/clear',
        'POST /api/restart',
        'POST /api/security/config',
        'POST /api/security/logs/clear',
        'POST /api/security/rules',
        'POST /api/sessions/revoke',
        'POST /api/settings/upstream',
        # 离线升级包（issue #153）：上传的是**可执行的代码包**，且下一步就是落盘重启
        'POST /api/system/update-package',
        'DELETE /api/system/update-package',
        # 代理线路与全局出口代理（设置 → 代理）：能改「谁的流量从哪个出口出去」，
        # 而且是**凭据载体**（地址里带用户名密码）—— 泄露的 token 不该能读能写。
        'POST /api/proxy-routes',
        'PUT /api/proxy-routes/{name}',
        'DELETE /api/proxy-routes/{name}',
        'POST /api/proxy-routes/import',
        'POST /api/proxy-routes/{name}/accounts',
        'POST /api/proxy-routes/{name}/auto-assign',
        'PUT /api/settings/proxy-pool',
        'POST /api/proxy-routes/probe',
        # Upstash 连通性测试：会拿**已保存的 Token** 向外部发请求（审计 2026-10-18），
        # 泄露的 token 不该能借它探测 / 外带凭据。
        'POST /api/settings/upstash/test',
        'PUT /api/settings/proxy',
        'POST /api/settings/proxy/probe',
        'POST /api/settings/upstash/reload',   # 评审补：与 /api/restart 同动作
        'POST /api/system/update',
        'POST /api/task-logs/clear',
        'POST /api/tokens',
        'POST /api/users',
        # 多上游（账号池分组）：这几条带着上游的 api_key，而且能改「谁的流量走哪个池」——
        # 与 /api/settings/upstream 同级（改配置级凭据 + 影响全部账号行为），只对会话开放。
        'DELETE /api/upstreams/{upstream_id}',
        'PATCH /api/upstreams/{upstream_id}',
        'POST /api/upstreams',
        # 账号分组互转：把账号文件在分组的目录之间移动——等于改「这个号属于哪个
        # 池」，与删账号同级（都会改变池的构成），只对会话开放。
        'POST /api/accounts/{filename}/move',
        # 账号导入（PR #145）：把外部凭据写进号池并触发上游重载 —— 该是「人坐在
        # 面板前做」的动作，令牌（哪怕是管理员令牌）不行。导出虽然也是只对会话
        # 开放，但它是**读**接口，本清扫只看写接口，另见 ExportRequiresSessionTest。
        'POST /api/accounts/upload',
        # PostgreSQL 异地备份：配置里带着能写整库的数据库凭据；导出会把全部数据
        # （含密钥哈希、请求日志）复制出去，恢复会**覆盖本地库**。三条都远超
        # 只读令牌该有的权限，与 /api/system/update 同级，只对会话开放。
        'POST /api/settings/pg-sync',
        'POST /api/settings/pg-sync/test',
        'POST /api/settings/pg-sync/export',
        'POST /api/settings/pg-sync/import',
    }
    # 写方法但只要求「已登录」——只读令牌也能调。必须逐个有理由。
    ANY_LOGGED_IN = {
        'POST /api/accounts/refresh-credits',   # 刷新积分快照；force=true 内部再查 admin
        'POST /api/keys/check-models',          # 只比对已缓存模型清单，不发网络
    }

    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.routing import APIRoute
        from server.main import app

        cls.rows: list[tuple[str, list[str]]] = []
        routes: list[APIRoute] = []
        for r in app.routes:
            inner = getattr(r, 'original_router', None)
            if inner is not None:
                routes.extend(x for x in inner.routes if isinstance(x, APIRoute))
            elif isinstance(r, APIRoute):
                routes.append(r)

        for r in routes:
            if not r.path.startswith('/api'):
                continue
            methods = {m for m in (r.methods or set())
                       if m in {'POST', 'PUT', 'PATCH', 'DELETE'}}
            if not methods:
                continue
            names: list[str] = []

            def walk(dep) -> None:
                if dep is None:
                    return
                call = getattr(dep, 'call', None)
                if call is not None:
                    names.append(getattr(call, '__name__', str(call)))
                for sub in getattr(dep, 'dependencies', []) or []:
                    walk(sub)

            walk(getattr(r, 'dependant', None))
            cls.rows.append((f"{','.join(sorted(methods))} {r.path}", names))

    def test_scan_is_not_vacuous(self) -> None:
        """先确认真的扫到了接口——否则下面两条会在空集合上「通过」。"""
        self.assertGreater(len(self.rows), 30,
                           '没扫到写接口，扫描逻辑可能失配（_IncludedRouter 没摊平？）')

    def _bucket(self, kind: str) -> set[str]:
        """按**最外层**的鉴权依赖归类。

        注意 `require_admin` 与 `require_session_admin` 内部都依赖 `current_user`，
        所以不能简单看「依赖树里有没有 current_user」——那会把全部写接口都算进来
        （第一版就是，报出一长串「新增」）。这里按依赖名出现的组合判断。
        """
        out = set()
        for key, names in self.rows:
            if kind == 'session_only':
                if 'require_session_admin' in names:
                    out.add(key)
            elif kind == 'any_logged_in':
                if ('current_user' in names and 'require_admin' not in names
                        and 'require_session_admin' not in names):
                    out.add(key)
        return out

    def test_session_only_set_is_exactly_as_documented(self) -> None:
        got = self._bucket('session_only')
        self.assertEqual(got, self.SESSION_ONLY,
                         '仅会话接口的集合变了。放宽一个接口前请先想清楚：'
                         '它是否能让 token 泄露者扩大权限 / 抹掉痕迹 / 影响服务可用性？'
                         f'\n新增了：{sorted(got - self.SESSION_ONLY)}'
                         f'\n消失了：{sorted(self.SESSION_ONLY - got)}')

    def test_write_endpoints_open_to_any_logged_in_are_declared(self) -> None:
        got = self._bucket('any_logged_in')
        self.assertEqual(got, self.ANY_LOGGED_IN,
                         '有写接口只要求「已登录」——只读令牌也能调它。'
                         f'\n新增了：{sorted(got - self.ANY_LOGGED_IN)}'
                         f'\n消失了：{sorted(self.ANY_LOGGED_IN - got)}')




class ExportRequiresSessionTest(unittest.TestCase):
    """账号导出是**读**接口，但读出来的是凭据 —— 同样只对会话开放。

    上面那份清单只扫写接口（`WriteEndpointScopeMatrixTest` 的定位：POST/PUT/PATCH/
    DELETE），GET 导出落不进那个闸门，所以这里单独钉一条。放宽它之前先想清楚：
    导出包里是 access / refresh token，能拷走就等于能接管账号。
    """

    def _export_routes(self) -> list:
        from fastapi.routing import APIRoute
        from server.main import app

        found = []
        for r in app.routes:
            inner = getattr(r, 'original_router', None)
            if inner is not None:
                candidates = [x for x in inner.routes if isinstance(x, APIRoute)]
            else:
                candidates = [r] if isinstance(r, APIRoute) else []
            found.extend(x for x in candidates if x.path == '/api/accounts/export')
        return found

    def test_export_is_session_only(self) -> None:
        routes = self._export_routes()
        self.assertTrue(routes, '找不到 /api/accounts/export —— 路由改名了？这条守卫会空转')

        def dep_names(route) -> list[str]:  # noqa: ANN001
            names: list[str] = []

            def walk(dep) -> None:  # noqa: ANN001
                # 与 WriteEndpointScopeMatrixTest 同一套取法：依赖的可调用挂在
                # `dep.call` 上（不是 `dep.dependency`——那个只有子依赖才有）。
                if dep is None:
                    return
                call = getattr(dep, 'call', None)
                if call is not None:
                    names.append(getattr(call, '__name__', str(call)))
                for sub in getattr(dep, 'dependencies', []) or []:
                    walk(sub)

            walk(getattr(route, 'dependant', None))
            return names

        for route in routes:
            self.assertIn('GET', route.methods or set())
            names = dep_names(route)
            self.assertIn('require_session_admin', names,
                          '导出接口不再要求会话管理员 —— 它会把账号凭据打包给调用方')


if __name__ == '__main__':
    unittest.main()
