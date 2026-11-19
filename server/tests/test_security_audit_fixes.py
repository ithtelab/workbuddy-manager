"""第二轮安全审计发现的缺陷的回归测试。

这些是 2026-09-14 深度审计的结果——**既包含已发生的入侵面，也包含审计中发现
但尚未被利用的缺陷**。每条都对应一个具体可复现的问题，不是泛泛的加固。

  1. `load_users` 的破坏性兜底：文件损坏时**自动重建并覆盖**，导致全部账号
     （含其他管理员）被静默清空、secret 轮换、新密码只打印到 stderr。
     实测复现过：截断文件 → 三个账号只剩一个新建的 admin。
  2. `save_users` 非原子：O_TRUNC 覆盖，写窗口内被中断就留下半截文件，
     正是上一条的触发条件。改为 临时文件 + fsync + os.replace。
  3. 模型白名单可被「不带 model 字段」绕过：限定单模型的密钥可用不带 model 的
     请求走上游默认模型。
  4. 网关 `/healthz` 未认证透传上游 JSON，泄露账号池规模（线上实测有 total）。
  5. 日志/审计换行注入：外部输入（用户名、UA）含换行即可伪造出额外的行。
  6. `key_id` 非数字 → int() 抛错 → 500。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, keysvc, security  # noqa: E402


class LoadUsersDestructiveFallbackTest(unittest.TestCase):
    """损坏的 users.json 必须报错，**绝不自动重建**。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._path = Path(self._tmp.name) / 'users.json'
        self._orig = config.USERS_FILE
        config.USERS_FILE = self._path

    def tearDown(self) -> None:
        config.USERS_FILE = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _write_good(self) -> None:
        security.save_users({
            'secret': 'ORIGINAL_SECRET',
            'users': [
                {'username': 'admin', 'role': 'admin', 'pwd_hash': 'x'},
                {'username': 'guest', 'role': 'viewer', 'pwd_hash': 'y'},
                {'username': 'mate', 'role': 'admin', 'pwd_hash': 'z'},
            ],
            'api_keys': [],
        })

    def test_missing_file_bootstraps(self) -> None:
        """文件不存在 = 首次启动，正常 bootstrap。"""
        self.assertFalse(self._path.exists())
        d = security.load_users()
        self.assertTrue(d.get('secret'))
        self.assertEqual([u['username'] for u in d['users']], ['admin'])

    def test_corrupt_file_raises_and_preserves_origin(self) -> None:
        """核心回归：损坏时抛错、且**不覆盖**原文件。"""
        self._write_good()
        raw = self._path.read_text(encoding='utf-8')
        self._path.write_text(raw[:len(raw) // 2], encoding='utf-8')  # 模拟半写
        broken = self._path.read_text(encoding='utf-8')

        with self.assertRaises(RuntimeError) as ctx:
            security.load_users()
        msg = str(ctx.exception)
        self.assertIn('损坏', msg)
        self.assertIn(str(self._path), msg, '错误信息应给出文件路径便于处置')
        # 关键：文件内容没被改写（旧实现会在这里覆盖成新 admin）
        self.assertEqual(self._path.read_text(encoding='utf-8'), broken)

    def test_structurally_wrong_file_raises(self) -> None:
        for bad in ('[]', '{}', '{"users": "not-a-list"}', '{"users": []}'):
            self._path.write_text(bad, encoding='utf-8')
            with self.assertRaises(RuntimeError, msg=bad):
                security.load_users()

    def test_save_is_atomic_no_partial_file(self) -> None:
        """写完后文件必须始终是完整 JSON（临时文件 + 原子替换）。"""
        self._write_good()
        security.save_users({'secret': 'S2', 'users': [
            {'username': 'admin', 'role': 'admin', 'pwd_hash': 'x'}], 'api_keys': []})
        data = json.loads(self._path.read_text(encoding='utf-8'))
        self.assertEqual(data['secret'], 'S2')
        # 临时文件不应残留
        self.assertFalse((self._path.parent / f'.{self._path.name}.tmp').exists())

    def test_save_failure_keeps_old_content(self) -> None:
        """写入中途失败时，旧文件必须完好（不会只剩半截）。"""
        self._write_good()
        good = self._path.read_text(encoding='utf-8')
        with mock.patch('os.replace', side_effect=OSError('disk full')):
            try:
                security.save_users({'secret': 'NEW', 'users': [], 'api_keys': []})
            except OSError:
                pass
        self.assertEqual(self._path.read_text(encoding='utf-8'), good,
                         '替换失败时旧文件应保持原样')


class InitialPasswordFileTest(unittest.TestCase):
    """issue #179：随机初始密码必须留一份能取回的明文，且改密后抹掉。

    背景：随机密码只在 stderr 打一次。journal 会被轮转、安装脚本可能因竞态
    没抓到、用户在几屏日志里也可能翻过去 —— 一旦错过，users.json 里只有
    pbkdf2 哈希，于是**没有任何办法登录**。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._path = Path(self._tmp.name) / 'users.json'
        self._orig_users = config.USERS_FILE
        self._orig_pwd = os.environ.pop('WB_ADMIN_PASSWORD', None)
        config.USERS_FILE = self._path

    def tearDown(self) -> None:
        config.USERS_FILE = self._orig_users
        if self._orig_pwd is not None:
            os.environ['WB_ADMIN_PASSWORD'] = self._orig_pwd
        else:
            os.environ.pop('WB_ADMIN_PASSWORD', None)
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _pw_file(self) -> Path:
        return self._path.with_name('initial-admin-password.txt')

    def test_bootstrap_writes_recoverable_plaintext(self) -> None:
        """随机生成时必须落一份明文，且内容与真正能登录的密码一致。"""
        security.load_users()
        self.assertTrue(self._pw_file().exists(), '初始密码未落盘，错过日志就登不进去')
        pwd = self._pw_file().read_text(encoding='utf-8').strip()
        self.assertTrue(pwd)
        stored = json.loads(self._path.read_text(encoding='utf-8'))['users'][0]['pwd_hash']
        self.assertTrue(security.verify_pwd(pwd, stored),
                        '落盘的密码必须能对上哈希，否则只是看着像找回入口')

    def test_env_password_not_persisted(self) -> None:
        """用户自己设了 WB_ADMIN_PASSWORD 时不落盘 —— 那是他已知的值。"""
        os.environ['WB_ADMIN_PASSWORD'] = 'operator-chosen-pw'
        security.load_users()
        self.assertFalse(self._pw_file().exists(),
                         '显式配置的密码不该被复制到磁盘')

    def test_weak_env_password_refused(self) -> None:
        """生产事故根因：WB_ADMIN_PASSWORD=admin123 必须拒绝启动，不能落库。"""
        os.environ['WB_ADMIN_PASSWORD'] = 'admin123'
        with self.assertRaises(RuntimeError) as ctx:
            security.load_users()
        self.assertIn('密码策略', str(ctx.exception))
        self.assertFalse(self._path.exists(), '拒绝启动时不应写出 users.json')

    def test_existing_weak_admin_password_detected(self) -> None:
        """升级上来的 users.json 若仍是 admin123，启动时必须能被点出来。"""
        self._path.write_text(json.dumps({
            'secret': 'x',
            'users': [{'username': 'admin', 'role': 'admin',
                       'pwd_hash': security.make_hash('admin123')}],
            'api_keys': [],
        }), encoding='utf-8')
        self.assertEqual(security.weak_admin_passwords(), ['admin'])

    def test_change_password_clears_plaintext(self) -> None:
        """改过密码后必须抹掉，否则那是一份永远有效的凭据。"""
        security.load_users()
        self.assertTrue(self._pw_file().exists())
        security.clear_initial_password_file()
        self.assertFalse(self._pw_file().exists())

    def test_clear_is_idempotent(self) -> None:
        """文件本来就不存在时抹除不能报错（改密路径总会调它）。"""
        security.clear_initial_password_file()
        security.clear_initial_password_file()

    def test_second_start_does_not_overwrite(self) -> None:
        """非首次启动（文件已存在）不得重新生成/覆盖 —— 那会把用户的密码换掉。"""
        security.load_users()
        first = self._pw_file().read_text(encoding='utf-8').strip()
        security.load_users()
        self.assertEqual(self._pw_file().read_text(encoding='utf-8').strip(), first)
        stored = json.loads(self._path.read_text(encoding='utf-8'))['users'][0]['pwd_hash']
        self.assertTrue(security.verify_pwd(first, stored),
                        '重启后落盘密码必须仍然有效')


class PasswordPolicyTest(unittest.TestCase):
    """管理端密码策略：长度、常见弱口令、用户名+数字、纯数字/重复字符。"""

    def test_common_and_short_passwords_rejected(self) -> None:
        for pwd in ('admin123', 'admin123456789', 'password1234', '123456789012',
                    'aaaaaaaaaaaa'):
            self.assertIsNotNone(security.password_problem(pwd), pwd)

    def test_username_plus_digits_rejected(self) -> None:
        self.assertIn('用户名加数字',
                      security.password_problem('admin9876543210', 'admin') or '')

    def test_strong_password_accepted(self) -> None:
        self.assertIsNone(security.password_problem('correct-horse-battery', 'admin'))
        self.assertIsNone(security.password_problem('Snow fox 2026!', 'admin'))


class ModelAllowlistBypassTest(unittest.TestCase):
    """模型白名单不能因为 model 缺失而跳过。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'k.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _key(self, models: list[str]) -> dict:
        created = keysvc.create_key('t', models=models)
        return created

    def test_missing_model_rejected_when_allowlist_set(self) -> None:
        key = self._key(['glm-5.2'])
        row = keysvc.resolve(key['key'])
        # 不带 model（None）→ 必须拒绝，而不是绕过
        reason = keysvc.validate(row, '1.2.3.4', None)
        self.assertIsNotNone(reason, '不带 model 竟然绕过了白名单')
        # 空串与非字符串同样拒绝
        self.assertIsNotNone(keysvc.validate(row, '1.2.3.4', ''))
        self.assertIsNotNone(keysvc.validate(row, '1.2.3.4', '   '))

    def test_allowed_model_passes(self) -> None:
        key = self._key(['glm-5.2'])
        row = keysvc.resolve(key['key'])
        self.assertIsNone(keysvc.validate(row, '1.2.3.4', 'glm-5.2'))

    def test_other_model_rejected(self) -> None:
        key = self._key(['glm-5.2'])
        row = keysvc.resolve(key['key'])
        self.assertIsNotNone(keysvc.validate(row, '1.2.3.4', 'gpt-4o'))

    def test_no_allowlist_allows_anything(self) -> None:
        """没设白名单时不该拦（保持原行为）。"""
        key = self._key([])
        row = keysvc.resolve(key['key'])
        self.assertIsNone(keysvc.validate(row, '1.2.3.4', None))
        self.assertIsNone(keysvc.validate(row, '1.2.3.4', 'whatever'))


class CleanTextTest(unittest.TestCase):
    """日志/审计写入前的单行化：外部输入不能伪造出额外的行。"""

    def test_newline_injection_removed(self) -> None:
        NL = chr(10)
        CR = chr(13)
        out = db._clean(f'admin{NL}伪造: 改密码{CR}{NL}再来一行')
        self.assertNotIn(NL, out)
        self.assertNotIn(CR, out)

    def test_control_chars_removed(self) -> None:
        out = db._clean('a' + chr(0) + 'b' + chr(27) + 'c' + chr(7))
        self.assertEqual(out, 'abc')

    def test_truncates(self) -> None:
        self.assertEqual(len(db._clean('x' * 5000, 500)), 500)
        self.assertEqual(len(db._clean('x' * 5000, 64)), 64)

    def test_none_and_numbers(self) -> None:
        self.assertEqual(db._clean(None), '')
        self.assertEqual(db._clean(123), '123')


class AuditCoverageTest(unittest.TestCase):
    """敏感操作必须留痕（上次入侵时「建号」这一步没记录）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 'a.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_audit_strips_newlines(self) -> None:
        """用户名里的换行不能伪造出额外审计行。"""
        security.audit({'username': 'admin'}, 'login_failed',
                       'x' + chr(10) + 'y', 'detail' + chr(10) + 'injected')
        rows = db.list_audit_logs(10)
        self.assertEqual(len(rows), 1)
        self.assertNotIn(chr(10), rows[0]['target'])
        self.assertNotIn(chr(10), rows[0]['detail'])

    def test_audit_records_ip_field(self) -> None:
        security.audit({'username': 'admin'}, 'create_key', 'mykey',
                       '来源 10.0.0.1', ip='10.0.0.1')
        rows = db.list_audit_logs(10)
        self.assertEqual(rows[0]['action'], 'create_key')
        self.assertIn('10.0.0.1', rows[0]['detail'])
        self.assertEqual(rows[0]['ip'], '10.0.0.1')


if __name__ == '__main__':
    unittest.main()
