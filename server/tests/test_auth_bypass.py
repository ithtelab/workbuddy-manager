"""管理端认证绕过与提权的回归测试（严重安全事件）。

真实事故：生产站被拿到管理员权限，攻击者调用 `PATCH /api/users/admin` 改掉了
管理员密码，并在群里公布了自建账号。代码层面有**两条独立通路**，都源自
早前路径穿越泄露的 users.json：

  通路 A（X-API-Key 后门）：`current_user` 曾接受 `X-API-Key` 头，只要该值出现在
    `users.json` 的 `api_keys` 数组里，就**无条件返回 role=admin**——不校验用户、
    不校验过期、无作用域。而那个数组没有任何代码去写、没有管理界面，唯一作用
    就是这条提权后门。泄露的 users.json 里恰好带着一个 wbk_ 值。

  通路 B（伪造 cookie）：`users.json.secret` 是 HMAC 签名密钥；且 `_unsign` 只验签
    不回查用户表，导致「已删除用户的 cookie 在 7 天内仍有效」。

这些用例锁定修复后的行为。**其中 test_apikey_header_no_longer_grants_admin 是
反证**：修复前它必然失败（后门成立），修复后通过。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI, Depends  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, security  # noqa: E402


def _users_file(tmp: Path, **kw) -> Path:
    """写一份 users.json。默认含一个 admin 与一个泄露用的 api_keys 项。"""
    path = tmp / 'users.json'
    path.write_text(json.dumps({
        'secret': kw.get('secret', 'S' * 64),
        'users': kw.get('users', [
            {'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('admin-pw')},
            {'username': 'guest', 'role': 'viewer', 'pwd_hash': security.make_hash('guest-pw')},
        ]),
        # 模拟从线上泄露出去的那份：带一个手工写入的 api_key
        'api_keys': kw.get('api_keys', ['wbk_leaked_key_from_users_json']),
        'epoch': kw.get('epoch', 1),
    }, ensure_ascii=False), encoding='utf-8')
    return path


class AuthBypassTest(unittest.TestCase):
    """直接测依赖函数，不经过 HTTP 层——它就是唯一的身份入口。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmp.name)
        self._orig = config.USERS_FILE
        self._path = _users_file(self._dir)
        config.USERS_FILE = self._path
        security.clear_fail('1.2.3.4')
        security.clear_fail('9.9.9.9', 'admin')

    def tearDown(self) -> None:
        config.USERS_FILE = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _req(self, *, cookie: str | None = None, api_key: str | None = None):
        """构造一个最小的 Request，只带我们关心的头。"""
        from starlette.requests import Request
        headers = []
        if cookie:
            headers.append((b'cookie', f'{config.COOKIE_NAME}={cookie}'.encode()))
        if api_key:
            headers.append((b'x-api-key', api_key.encode()))
        scope = {'type': 'http', 'method': 'GET', 'path': '/api/x',
                 'headers': headers, 'query_string': b''}
        return Request(scope)

    # ── 通路 A：X-API-Key 后门 ──────────────────────────

    def test_apikey_header_no_longer_grants_admin(self) -> None:
        """反证：修复前该头能直接换到 admin，修复后必须被拒。

        这是本次事故最可能的实际入口——泄露的 users.json 里的 api_keys 数组
        就是一把万能钥匙，且它跟 SQLite 里给下游用的密钥表**完全无关**。
        """
        cfg = security.load_users()
        leaked = cfg['api_keys'][0]  # 攻击者从泄露文件里拿到的那个值
        with self.assertRaises(Exception) as ctx:
            security.current_user(self._req(api_key=leaked))
        self.assertIn('未登录', str(ctx.exception))

    def test_apikey_header_alone_cannot_authenticate(self) -> None:
        """任何值都不行——不只是泄露的那个。"""
        for v in ('wbk_whatever', 'anything', ''):
            with self.assertRaises(Exception):
                security.current_user(self._req(api_key=v))

    def test_role_from_api_key_would_have_been_admin(self) -> None:
        """记录后门曾经的效果，防止将来有人「顺手加回来」。

        这里断言的是「如果还有那条分支，它会给出 admin」——修复后该分支已删，
        故直接断言代码里不再出现这种授权。
        """
        import inspect
        src = inspect.getsource(security.current_user)
        self.assertNotIn("'role': 'admin'", src.replace(' ', '').replace("'role':'admin'", "'role': 'admin'"))
        self.assertNotIn("cfg.get('api_keys'", src)

    # ── 通路 B：签名 cookie ────────────────────────────

    def _forge(self, payload: dict, secret: str) -> str:
        """伪造一个签名 cookie（用于测试「secret 泄露后能伪造」这条事实）。

        **自动补齐 iat/orig**：会话自 2026-09-17 起带空闲滑动窗口，缺 `iat` 的
        载荷会被判失效（fail-closed，见 `security.idle_expired`）。这些用例测的是
        **别的**性质（secret 是信任根、删号后失效、降权立即生效），不该因为漏填
        时间字段就在更早的一层被拦下 —— 那会让人误以为那些防线失效了。
        要测「缺 iat 会被拒」请见 `test_session_hardening.IdleExpiryTest`。
        """
        import base64
        import hashlib
        import hmac
        now = int(time.time())
        full = {'iat': now, 'orig': now, **payload}
        raw = json.dumps(full)
        sig = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
        return base64.urlsafe_b64encode(f'{raw}|{sig}'.encode()).decode()

    def test_forged_cookie_with_leaked_secret_still_works(self) -> None:
        """用已泄露的 secret 伪造仍然有效——这是设计使然（secret 是信任根）。

        这条不是缺陷本身，而是说明：**轮换 secret 是唯一能吊销伪造会话的手段**。
        所以处置清单里把它列为第 1 步。
        """
        token = self._forge(
            {'username': 'admin', 'role': 'admin', 'exp': int(time.time()) + 3600}, 'S' * 64)
        user = security.current_user(self._req(cookie=token))
        self.assertEqual(user['role'], 'admin')

    def test_deleted_user_cookie_is_rejected(self) -> None:
        """删掉用户后，其旧 cookie 必须立即失效（此前 7 天内仍然有效）。"""
        token = self._forge(
            {'username': 'guest', 'role': 'viewer', 'exp': int(time.time()) + 3600}, 'S' * 64)
        self.assertEqual(security.current_user(self._req(cookie=token))['username'], 'guest')
        # 删掉 guest
        cfg = security.load_users()
        cfg['users'] = [u for u in cfg['users'] if u['username'] != 'guest']
        security.save_users(cfg)
        with self.assertRaises(Exception):
            security.current_user(self._req(cookie=token))

    def test_role_downgrade_takes_effect_immediately(self) -> None:
        """把 admin 降为 viewer 后，旧 cookie 不能还是 admin。"""
        token = self._forge(
            {'username': 'admin', 'role': 'admin', 'exp': int(time.time()) + 3600}, 'S' * 64)
        cfg = security.load_users()
        for u in cfg['users']:
            if u['username'] == 'admin':
                u['role'] = 'viewer'
        security.save_users(cfg)
        user = security.current_user(self._req(cookie=token))
        self.assertEqual(user['role'], 'viewer', 'role 必须以用户表为准，而非 cookie 快照')

    def test_password_change_revokes_existing_sessions(self) -> None:
        """改密码后，旧会话必须失效（否则被盗会话改密码也挡不住）。"""
        token = self._forge(
            {'username': 'admin', 'role': 'admin', 'exp': int(time.time()) + 3600}, 'S' * 64)
        self.assertEqual(security.current_user(self._req(cookie=token))['username'], 'admin')
        security.revoke_sessions('admin')
        with self.assertRaises(Exception):
            security.current_user(self._req(cookie=token))

    def test_other_user_sessions_survive(self) -> None:
        """吊销某个用户的会话，不应影响其他用户。"""
        admin_tok = self._forge(
            {'username': 'admin', 'role': 'admin', 'exp': int(time.time()) + 3600}, 'S' * 64)
        guest_tok = self._forge(
            {'username': 'guest', 'role': 'viewer', 'exp': int(time.time()) + 3600}, 'S' * 64)
        security.revoke_sessions('admin')
        with self.assertRaises(Exception):
            security.current_user(self._req(cookie=admin_tok))
        self.assertEqual(security.current_user(self._req(cookie=guest_tok))['username'], 'guest')

    def test_expired_cookie_rejected(self) -> None:
        token = self._forge(
            {'username': 'admin', 'role': 'admin', 'exp': int(time.time()) - 10}, 'S' * 64)
        with self.assertRaises(Exception):
            security.current_user(self._req(cookie=token))

    def test_wrong_secret_rejected(self) -> None:
        token = self._forge(
            {'username': 'admin', 'role': 'admin', 'exp': int(time.time()) + 3600}, 'WRONG' * 13)
        with self.assertRaises(Exception):
            security.current_user(self._req(cookie=token))


class LoginLockoutTest(unittest.TestCase):
    """锁定计数不能因为字典被灌满而被清零。"""

    def setUp(self) -> None:
        security._fail.clear()
        security._user_fail.clear()

    def tearDown(self) -> None:
        security._fail.clear()
        security._user_fail.clear()

    def test_flooding_does_not_reset_admin_lockout(self) -> None:
        """灌满 5000 个陌生用户名后，admin 的失败计数必须还在。

        原实现超限时 `store.clear()` 整体清空——攻击者只要用大量不存在的
        用户名刷请求，就能把 admin 的锁定状态一起抹掉，然后无限猜密码。
        """
        for _ in range(security.MAX_FAILS):
            security.record_fail('1.1.1.1', 'admin')
        self.assertTrue(security.login_blocked('1.1.1.1', 'admin'))

        # 灌入远超上限的陌生用户名（每次来自不同 IP，避免触发同 IP 锁定）
        for i in range(security.MAX_TRACKED + 200):
            security.record_fail(f'10.{i // 250}.{i % 250}.1', f'nobody{i}')
        self.assertTrue(security.login_blocked('1.1.1.1', 'admin'),
                        'admin 的锁定被灌满操作清掉了')

    def test_store_prunes_expired_entries(self) -> None:
        """已过锁定窗口的条目会被淘汰——上限的意义是防内存膨胀。"""
        # 塞满一个超过窗口的旧条目 + 若干新条目
        old = time.time() - security.LOCK_SECONDS - 60
        for i in range(security.MAX_TRACKED + 100):
            security._fail[f'old{i}'] = [1, old]
        security._fail['fresh'] = [3, time.time()]
        # 触发一次 prune（记一次失败即可）
        security.record_fail('3.3.3.3', 'someone')
        self.assertLessEqual(len(security._fail), security.MAX_TRACKED + 2,
                             '过期条目应被淘汰')
        # 窗口内的条目必须留着
        self.assertIn('fresh', security._fail)

    def test_lock_window_entries_are_never_evicted(self) -> None:
        """安全属性：只要还在锁定窗口内，任何灌入都不能把条目挤掉。

        这是本次事故的直接教训——安全计数器不能被「容量」策略牺牲掉。
        代价是上限在极端灌入下会临时超出，但 LOCK_SECONDS 只有 10 分钟，
        超出的量受请求速率与时间共同约束，不会无限增长。
        """
        for _ in range(security.MAX_FAILS):
            security.record_fail('4.4.4.4', 'admin')
        for i in range(security.MAX_TRACKED + 300):
            security.record_fail(f'172.{i // 250}.{i % 250}.9', f'flood{i}')
        self.assertTrue(security.login_blocked('4.4.4.4', 'admin'),
                        '窗口内的 admin 锁定条目被灌入操作挤掉了')
        # 按用户名维度的锁定只拦「自己也失败过」的来源 IP（2026-10-21 安全
        # 检查 L2）：干净的管理员 IP 不该被匿名者的失败计数锁在门外。
        self.assertFalse(security.login_blocked('5.5.5.5', 'admin'),
                         '从未输错的来源不该被按用户名的锁定拦截（远程锁定 DoS）')
        # 但「失败过的」来源仍要被拦——这就是防换 IP 爆破的那一半。
        security.record_fail('5.5.5.5', 'someone-else')
        self.assertTrue(security.login_blocked('5.5.5.5', 'admin'),
                        '自己失败过的来源在用户名锁定期内必须被拦')

    def test_successful_login_clears_counter(self) -> None:
        for _ in range(3):
            security.record_fail('2.2.2.2', 'admin')
        security.clear_fail('2.2.2.2', 'admin')
        self.assertFalse(security.login_blocked('2.2.2.2', 'admin'))


class UserAdminHttpTest(unittest.TestCase):
    """走真实 HTTP 的用户管理流程。

    为什么必须有这一层：上面的用例直接调依赖函数，覆盖不到路由里的实现细节。
    **我自己就在 update_user 里写错过 audit() 的调用参数**（多传一个位置参数导致
    TypeError → 500），而单元测试全绿——因为没人从 HTTP 打这个端点。
    改密码是本次事故的核心操作，必须端到端跑通。
    """

    @classmethod
    def setUpClass(cls) -> None:
        import os
        cls._tmp = tempfile.TemporaryDirectory()
        cls._dir = Path(cls._tmp.name)
        cls._orig_db = config.DB_PATH
        cls._orig_users = config.USERS_FILE
        cls._orig_static = config.STATIC_DIR
        config.DB_PATH = cls._dir / 'h.db'
        config.USERS_FILE = cls._dir / 'users.json'
        config.STATIC_DIR = cls._dir / 'no-static'
        os.environ['WB_ADMIN_PASSWORD'] = 'http-admin-pw'
        _users_file(cls._dir, users=[
            {'username': 'admin', 'role': 'admin',
             'pwd_hash': security.make_hash('http-admin-pw')},
            {'username': 'guest', 'role': 'viewer',
             'pwd_hash': security.make_hash('guest-pw-123')},
        ], api_keys=['wbk_leaked_from_production'])
        db._conn = None
        db.connect()
        from server.main import app
        cls.app = app

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = cls._orig_db
        config.USERS_FILE = cls._orig_users
        config.STATIC_DIR = cls._orig_static
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def setUp(self) -> None:
        # 每个用例重置 users.json：改密码/删用户会改共享文件，
        # 不重置就会互相污染（前一个用例把密码改了，后一个就登不进来）
        _users_file(self._dir, users=[
            {'username': 'admin', 'role': 'admin',
             'pwd_hash': security.make_hash('http-admin-pw')},
            {'username': 'guest', 'role': 'viewer',
             'pwd_hash': security.make_hash('guest-pw-123')},
        ], api_keys=['wbk_leaked_from_production'])
        security.initial_password_path().unlink(missing_ok=True)
        try:
            db.execute('DELETE FROM audit_logs')
        except Exception:  # noqa: BLE001
            pass
        self.c = TestClient(self.app)

    def _login(self, username: str, password: str) -> int:
        r = self.c.post('/api/login', json={'username': username, 'password': password})
        return r.status_code

    # ── 后门（历史攻击入口）──

    def test_leaked_api_key_cannot_reach_admin_api(self) -> None:
        """事故里那条路：用泄露的 X-API-Key 打 /api/users 建号。"""
        r = self.c.post('/api/users',
                        json={'username': 'sanguine', 'password': 'ssa123', 'role': 'admin'},
                        headers={'X-API-Key': 'wbk_leaked_from_production'})
        self.assertEqual(r.status_code, 401)
        # 账号确实没被建出来
        self.assertEqual(self._login('sanguine', 'ssa123'), 401)

    # ── 正常流程 ──

    def test_admin_can_change_password_and_old_session_dies(self) -> None:
        """改密码必须成功（不是 500），且旧会话立即失效。"""
        password_path = security.initial_password_path()
        password_path.write_text('initial-admin-pw\n', encoding='utf-8')
        self.assertEqual(self._login('admin', 'http-admin-pw'), 200)
        self.assertEqual(self.c.get('/api/me').status_code, 200)
        r = self.c.patch('/api/users/admin', json={'password': 'brand-new-pw-1'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(password_path.exists(), '修改 admin 密码后应清理初始密码文件')
        self.assertTrue(r.json().get('relogin_required'))
        # 同一个 client（持有旧 cookie）应被踢下线
        self.assertEqual(self.c.get('/api/me').status_code, 401)
        # 新密码可用
        self.assertEqual(self._login('admin', 'brand-new-pw-1'), 200)

    def test_changing_non_admin_password_preserves_initial_admin_password_file(self) -> None:
        """改其他用户密码时，初始 admin 密码仍有效，恢复文件应保留。"""
        password_path = security.initial_password_path()
        password_path.write_text('initial-admin-pw\n', encoding='utf-8')

        self.assertEqual(self._login('admin', 'http-admin-pw'), 200)
        r = self.c.patch('/api/users/guest', json={'password': 'guest-new-pw-1'})

        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(password_path.exists())
        self.assertEqual(password_path.read_text(encoding='utf-8'), 'initial-admin-pw\n')

    def test_short_password_rejected(self) -> None:
        self._login('admin', 'http-admin-pw')
        r = self.c.patch('/api/users/admin', json={'password': 'abc'})
        self.assertEqual(r.status_code, 400)

    def test_weak_password_rejected_on_create_and_change(self) -> None:
        """生产事故根因：admin123 这类弱口令不能再被设置。"""
        self.assertEqual(self._login('admin', 'http-admin-pw'), 200)

        r = self.c.post('/api/users', json={
            'username': 'weakling', 'password': 'admin123', 'role': 'viewer'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('12', r.json().get('detail', ''))

        r = self.c.patch('/api/users/admin', json={'password': 'admin9876543210'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('用户名加数字', r.json().get('detail', ''))

        # 被拒绝的两次都不能改掉原来的密码
        self.assertEqual(self._login('admin', 'http-admin-pw'), 200)

    def test_viewer_cannot_manage_users(self) -> None:
        self.assertEqual(self._login('guest', 'guest-pw-123'), 200)
        self.assertEqual(
            self.c.patch('/api/users/admin', json={'password': 'hacked-pw-1'}).status_code, 403)
        self.assertEqual(self.c.post('/api/users', json={
            'username': 'x', 'password': 'yyyyyyyy', 'role': 'admin'}).status_code, 403)

    def test_viewer_cannot_read_audit_logs(self) -> None:
        self._login('guest', 'guest-pw-123')
        self.assertEqual(self.c.get('/api/audit-logs').status_code, 403)

    def test_delete_user_revokes_its_session(self) -> None:
        """删号后其 cookie 立即失效（此前 7 天内仍可用）。"""
        admin = TestClient(self.app)
        admin.post('/api/login', json={'username': 'admin', 'password': 'http-admin-pw'})
        victim = TestClient(self.app)
        victim.post('/api/login', json={'username': 'guest', 'password': 'guest-pw-123'})
        self.assertEqual(victim.get('/api/me').status_code, 200)
        self.assertEqual(admin.delete('/api/users/guest').status_code, 200)
        self.assertEqual(victim.get('/api/me').status_code, 401)

    def test_audit_records_sensitive_actions(self) -> None:
        """敏感操作要留痕（事故里没有任何记录可查）。"""
        self._login('admin', 'http-admin-pw')

        # 攻击者试密码（登录失败也要记）
        TestClient(self.app).post('/api/login',
                                  json={'username': 'admin', 'password': 'wrong-guess'})

        # 改密码：会吊销自己的会话，所以之后必须用新密码重新登录
        r = self.c.patch('/api/users/admin', json={'password': 'final-pw-123'})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.c.get('/api/audit-logs').status_code, 401,
                         '改密码后旧会话应失效')

        self.assertEqual(self._login('admin', 'final-pw-123'), 200)
        r2 = self.c.get('/api/audit-logs')
        self.assertEqual(r2.status_code, 200, r2.text)
        rows = r2.json()['items']
        actions = {it['action'] for it in rows}
        self.assertIn('login', actions, '登录成功要留痕')
        self.assertIn('login_failed', actions, '登录失败要留痕（攻击者试密码就在这里）')
        self.assertIn('update_user', actions, '改密码要留痕')
        for action in ('login', 'login_failed', 'update_user'):
            row = next(it for it in rows if it['action'] == action)
            self.assertTrue(row.get('ip'), f'{action} 应记录结构化来源 IP')


if __name__ == '__main__':
    unittest.main()

class KeysAuditRegressionTest(unittest.TestCase):
    """v1.0.23 回归：创建/删除密钥因漏导入 client_ip 而 500。

    这是我改审计时引入的疏漏——调用点用了 `client_ip(request)`，但文件头只
    `from .. import keysvc, security`，没导入它。后果是所有升级者的**密钥功能
    完全不可用**（创建与删除都 500），属核心功能故障。
    外部安全报告以 500 的形式发现；这里补测试，避免同类"加了调用忘了导入"再发生。
    """

    @classmethod
    def setUpClass(cls) -> None:
        import os
        cls._tmp = tempfile.TemporaryDirectory()
        cls._dir = Path(cls._tmp.name)
        cls._orig_db = config.DB_PATH
        cls._orig_users = config.USERS_FILE
        cls._orig_static = config.STATIC_DIR
        config.DB_PATH = cls._dir / 'k.db'
        config.USERS_FILE = cls._dir / 'users.json'
        config.STATIC_DIR = cls._dir / 'no-static'
        os.environ['WB_ADMIN_PASSWORD'] = 'keys-admin-pw'
        _users_file(cls._dir, users=[
            {'username': 'admin', 'role': 'admin',
             'pwd_hash': security.make_hash('keys-admin-pw')},
        ], api_keys=[])
        db._conn = None
        db.connect()
        from server.main import app
        cls.app = app

    @classmethod
    def tearDownClass(cls) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = cls._orig_db
        config.USERS_FILE = cls._orig_users
        config.STATIC_DIR = cls._orig_static
        try:
            cls._tmp.cleanup()
        except PermissionError:
            pass

    def setUp(self) -> None:
        _users_file(self._dir, users=[
            {'username': 'admin', 'role': 'admin',
             'pwd_hash': security.make_hash('keys-admin-pw')},
        ], api_keys=[])
        self.c = TestClient(self.app)
        self.c.post('/api/login', json={'username': 'admin', 'password': 'keys-admin-pw'})

    def test_create_key_returns_200_with_secret_once(self) -> None:
        r = self.c.post('/api/keys', json={'name': 'test-key'})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(str(body.get('key', '')).startswith('wbk_'),
                        '创建时必须回一次明文密钥')

    def test_delete_key_returns_200(self) -> None:
        created = self.c.post('/api/keys', json={'name': 'to-delete'}).json()
        r = self.c.delete(f"/api/keys/{created['id']}")
        self.assertEqual(r.status_code, 200, r.text)

    def test_update_key_returns_200(self) -> None:
        created = self.c.post('/api/keys', json={'name': 'to-update'}).json()
        r = self.c.patch(f"/api/keys/{created['id']}", json={'max_ips': 3})
        self.assertEqual(r.status_code, 200, r.text)

    def test_create_key_is_audited(self) -> None:
        self.c.post('/api/keys', json={'name': 'audited'})
        rows = self.c.get('/api/audit-logs').json()['items']
        self.assertIn('create_key', {it['action'] for it in rows})


if __name__ == '__main__':
    unittest.main()


class MalformedLoginBodyTest(unittest.TestCase):
    """登录接口对「合法 JSON 但非对象」的 body 必须返回 400，而不是 500。

    实测（真实部署环境）：`null` 与 `[]` 会让 `body.get()`
    抛 AttributeError → 500。虽然 500 的响应体只有 "Internal Server Error"、
    不泄露堆栈，但这属于未处理异常：

      * 每次触发都在服务端留下一条错误日志 —— 可被用来刷日志、淹没真实告警；
      * 暴露输入校验不完整（`null`/`[]`/`"str"`/数字都是**合法 JSON**，
        原有 try 只拦解析失败，拦不住它们）。

    注意区别：解析失败的（如 `{`）本来就返回 400，这里补的是「能解析但类型不对」。
    """

    def setUp(self) -> None:
        self._dir = Path(tempfile.mkdtemp())
        self._orig = (config.DB_PATH, config.USERS_FILE, config.STATIC_DIR)
        config.DB_PATH = self._dir / 'malformed.db'
        config.USERS_FILE = self._dir / 'users.json'
        config.STATIC_DIR = self._dir / 'no-static'

        def _restore() -> None:
            config.DB_PATH, config.USERS_FILE, config.STATIC_DIR = self._orig

        self.addCleanup(_restore)
        security.save_users({
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('pw'), 'sv': 1}],
            'secret': 'test-secret',
        })
        security._fail.clear()
        security._user_fail.clear()
        self.addCleanup(security._fail.clear)
        self.addCleanup(security._user_fail.clear)
        from server.main import app
        self.c = TestClient(app)

    def test_non_object_json_returns_400_not_500(self) -> None:
        for body in ('null', '[]', '"str"', '123', 'true'):
            r = self.c.post('/api/login', content=body,
                            headers={'Content-Type': 'application/json'})
            self.assertEqual(
                r.status_code, 400,
                f'body={body!r} 应返回 400（实际 {r.status_code}）—— '
                '500 说明崩在未处理的异常上',
            )

    def test_unparseable_json_still_400(self) -> None:
        r = self.c.post('/api/login', content='{',
                        headers={'Content-Type': 'application/json'})
        self.assertEqual(r.status_code, 400)

    def test_valid_object_still_reaches_auth(self) -> None:
        """修复不能把正常登录一起挡掉。"""
        r = self.c.post('/api/login', json={'username': 'admin', 'password': 'wrong'})
        self.assertEqual(r.status_code, 401, '正常对象 body 应走到鉴权逻辑（这里密码错→401）')

    def test_correct_password_still_logs_in(self) -> None:
        r = self.c.post('/api/login', json={'username': 'admin', 'password': 'pw'})
        self.assertEqual(r.status_code, 200, r.text)
