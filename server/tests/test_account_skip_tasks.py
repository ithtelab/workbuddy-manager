"""账号级「不参与定时任务」开关（issue #437）。

企业账号没有个人成长体系：签到端点恒返回 400 code=10001，自动任务会反复白打。
「临时停用」把账号改名退出号池，连转发一起停掉，不满足「保留转发、只停任务」。

这里钉住的是这个中间档的存储与 API 契约：

  1. 字段写在上游 auth 文件**顶层** `skip_tasks`（JSON bool），上游 scheduler 才能读到；
  2. 写入只动这一个键，**其余字段与未知键必须原样保留**（device_token / proxy /
     weight / realm / 自定义键都不能丢）——热加载会整份读回文件；
  3. 清除 = 删键（缺省 false），重复设置幂等；
  4. 支持按 file 或 uid 定位；非法文件 / 不存在 → 400/404；
  5. 列表接口把 skip_tasks 带回，前端才能画徽章；
  6. 重登 / 覆盖导入（write_auth_file 整份重建）要保留该值。
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

from server import config, db, security  # noqa: E402
from server.services import tencent, wb2api  # noqa: E402

UID = '437aaaaabbbbcccc'


def _auth_doc(uid: str = UID, *, skip: bool | None = None) -> dict:
    doc = {
        'account': {'uid': uid, 'nickname': '企业号', 'enterpriseId': 'ent-1'},
        'auth': {'accessToken': 'tok-' + uid, 'refreshToken': 'ref-' + uid,
                 'expiresAt': 4102444800, 'domain': 'copilot.tencent.com',
                 'realm': 'cn'},
        # 未知键：写入时一个都不能丢（上游/插件可能放自己的字段）
        'device_token': 'dev-token',
        'proxy': 'line-a',
        'weight': 2.5,
        'custom_plugin_key': {'nested': [1, 2, 3]},
    }
    if skip is not None:
        doc['skip_tasks'] = skip
    return doc


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._orig = config.AUTH_DIR
        config.AUTH_DIR = self.dir
        self.fname = f'workbuddy-{UID}.json'
        self.path = self.dir / self.fname
        self.path.write_text(json.dumps(_auth_doc(), ensure_ascii=False), encoding='utf-8')

    def tearDown(self) -> None:
        config.AUTH_DIR = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _raw(self, name: str | None = None) -> dict:
        return json.loads((self.dir / (name or self.fname)).read_text(encoding='utf-8'))

    def test_set_true_writes_bool_and_keeps_every_other_key(self) -> None:
        before = self._raw()
        r = wb2api.set_account_skip_tasks(self.fname, True)
        self.assertTrue(r['changed'])
        self.assertTrue(r['skip_tasks'])
        after = self._raw()
        self.assertIs(after['skip_tasks'], True)
        for key, value in before.items():
            self.assertEqual(after[key], value, f'{key} 被写操作改掉了')
        self.assertEqual(after['custom_plugin_key'], {'nested': [1, 2, 3]})
        self.assertEqual(after['auth'], before['auth'])
        self.assertEqual(after['account'], before['account'])

    def test_clear_removes_key_only(self) -> None:
        wb2api.set_account_skip_tasks(self.fname, True)
        r = wb2api.set_account_skip_tasks(self.fname, False)
        self.assertTrue(r['changed'])
        self.assertFalse(r['skip_tasks'])
        after = self._raw()
        self.assertNotIn('skip_tasks', after)
        self.assertEqual(after['device_token'], 'dev-token')
        self.assertEqual(after['weight'], 2.5)

    def test_idempotent(self) -> None:
        first = wb2api.set_account_skip_tasks(self.fname, True)
        second = wb2api.set_account_skip_tasks(self.fname, True)
        self.assertTrue(first['changed'])
        self.assertFalse(second['changed'], '重复设置不应重写文件')
        third = wb2api.set_account_skip_tasks(self.fname, False)
        fourth = wb2api.set_account_skip_tasks(self.fname, False)
        self.assertTrue(third['changed'])
        self.assertFalse(fourth['changed'])

    def test_uid_locates_existing_file(self) -> None:
        # 故意用非规范文件名：uid 定位必须能找到它，而不是按规范名新建副本
        odd = self.dir / 'workbuddy_new.json'
        self.path.rename(odd)
        r = wb2api.set_account_skip_tasks('', True, uid=UID)
        self.assertEqual(r['file'], 'workbuddy_new.json')
        self.assertIs(self._raw('workbuddy_new.json')['skip_tasks'], True)
        self.assertFalse(self.path.exists(), 'uid 定位不应新建规范名副本')

    def test_uid_missing_raises_file_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            wb2api.set_account_skip_tasks('', True, uid='nosuchuid')

    def test_missing_file_raises_file_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            wb2api.set_account_skip_tasks('workbuddy-nosuchuid.json', True)

    def test_illegal_filename_rejected(self) -> None:
        for bad in ('../workbuddy-x.json', 'workbuddy-x.json/../y',
                    '/etc/passwd', 'other.json', 'workbuddy-x.txt'):
            with self.assertRaises(ValueError, msg=repr(bad)):
                wb2api.set_account_skip_tasks(bad, True)

    def test_enabled_must_be_bool(self) -> None:
        for bad in (1, 'true', None, [True]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                wb2api.set_account_skip_tasks(self.fname, bad)  # type: ignore[arg-type]

    def test_disabled_suffix_supported(self) -> None:
        """面板停用（改名）的账号也能预设该开关，启用后直接生效。"""
        disabled = self.fname + '.disabled'
        self.path.rename(self.dir / disabled)
        r = wb2api.set_account_skip_tasks(disabled, True)
        self.assertEqual(r['file'], disabled)
        self.assertIs(self._raw(disabled)['skip_tasks'], True)

    def test_list_reports_skip_tasks(self) -> None:
        rows = {a['uid']: a for a in wb2api.list_auth_accounts()}
        self.assertIs(rows[UID]['skip_tasks'], False, '缺省必须是明确的 false')
        wb2api.set_account_skip_tasks(self.fname, True)
        rows = {a['uid']: a for a in wb2api.list_auth_accounts()}
        self.assertIs(rows[UID]['skip_tasks'], True)

    def test_list_ignores_non_bool_value(self) -> None:
        """手改成 "true" 字符串不算开启（上游 Parse 对 bool 是严格解析）。"""
        raw = self._raw()
        raw['skip_tasks'] = 'true'
        self.path.write_text(json.dumps(raw), encoding='utf-8')
        rows = {a['uid']: a for a in wb2api.list_auth_accounts()}
        self.assertIs(rows[UID]['skip_tasks'], False)

    def test_task_account_files_excludes_skip_tasks(self) -> None:
        wb2api.set_account_skip_tasks(self.fname, True)
        self.assertEqual(wb2api.task_account_files(), [])
        wb2api.set_account_skip_tasks(self.fname, False)
        self.assertEqual(wb2api.task_account_files(), [self.fname])


class WriteAuthFilePreserveTest(unittest.TestCase):
    """重登 / 覆盖导入会**整份重建** auth 文件：不显式保留就会清掉 skip_tasks。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._orig = config.AUTH_DIR
        config.AUTH_DIR = self.dir
        self.fname = f'workbuddy-{UID}.json'
        (self.dir / self.fname).write_text(
            json.dumps(_auth_doc(skip=True), ensure_ascii=False), encoding='utf-8')

    def tearDown(self) -> None:
        config.AUTH_DIR = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _write(self, account: dict) -> dict:
        tencent.write_auth_file({
            'uid': UID,
            'access_token': 'new-token',
            'refresh_token': 'new-ref',
            'expires_at': 4102444801,
            'domain': 'copilot.tencent.com',
            'realm': 'cn',
            'nickname': '企业号',
            'enterprise_id': 'ent-1',
            **account,
        }, self.dir)
        return json.loads((self.dir / self.fname).read_text(encoding='utf-8'))

    def test_relogin_keeps_existing_skip_tasks(self) -> None:
        after = self._write({})
        self.assertIs(after.get('skip_tasks'), True, '重登把「不参与任务」静默清掉了')

    def test_explicit_false_clears(self) -> None:
        after = self._write({'skip_tasks': False})
        self.assertNotIn('skip_tasks', after)


class RouteAuthTest(unittest.TestCase):
    """路由层：body 里的 enabled/file 必须真的生效；权限与错误码要正确。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._orig_auth = config.AUTH_DIR
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.AUTH_DIR = self.dir / 'auths'
        config.AUTH_DIR.mkdir()
        config.DB_PATH = self.dir / 'test.db'
        config.USERS_FILE = self.dir / 'users.json'
        db._conn = None
        db.connect()
        security.save_users({
            'secret': 'S',
            'users': [
                {'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('p')},
                {'username': 'viewer', 'role': 'viewer', 'pwd_hash': security.make_hash('p')},
            ],
            'api_keys': [],
        })
        self.fname = f'workbuddy-{UID}.json'
        self.path = config.AUTH_DIR / self.fname
        self.path.write_text(json.dumps(_auth_doc(), ensure_ascii=False), encoding='utf-8')
        from server.main import app
        from fastapi.testclient import TestClient
        self.client = TestClient(app)

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.AUTH_DIR = self._orig_auth
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _login(self, username: str) -> None:
        r = self.client.post('/api/login', json={'username': username, 'password': 'p'})
        self.assertEqual(r.status_code, 200, r.text)

    def _raw(self) -> dict:
        return json.loads(self.path.read_text(encoding='utf-8'))

    def test_requires_admin(self) -> None:
        self._login('viewer')
        r = self.client.post('/api/accounts/skip-tasks',
                             json={'file': self.fname, 'enabled': True})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertNotIn('skip_tasks', self._raw())

    def test_anonymous_requires_auth(self) -> None:
        r = self.client.post('/api/accounts/skip-tasks',
                             json={'file': self.fname, 'enabled': True})
        self.assertEqual(r.status_code, 401, r.text)

    def test_body_binding_sets_and_clears(self) -> None:
        """前端用 post() 把参数放 JSON body —— 绑错会 200 但字段不生效。"""
        self._login('admin')
        with mock.patch('server.routers.accounts.reload.request_restart',
                        lambda upstream=None: True):
            r = self.client.post('/api/accounts/skip-tasks',
                                  json={'file': self.fname, 'enabled': True})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIs(r.json()['skip_tasks'], True)
            self.assertIs(self._raw()['skip_tasks'], True)

            r = self.client.post('/api/accounts/skip-tasks',
                                  json={'uid': UID, 'enabled': False})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIs(r.json()['skip_tasks'], False)
            self.assertNotIn('skip_tasks', self._raw())

    def test_enabled_must_be_json_bool(self) -> None:
        self._login('admin')
        for bad in ('true', 1, None):
            r = self.client.post('/api/accounts/skip-tasks',
                                  json={'file': self.fname, 'enabled': bad})
            self.assertEqual(r.status_code, 400, f'{bad!r}: {r.text}')

    def test_missing_identifier_and_file(self) -> None:
        self._login('admin')
        r = self.client.post('/api/accounts/skip-tasks', json={'enabled': True})
        self.assertEqual(r.status_code, 400, r.text)
        r = self.client.post('/api/accounts/skip-tasks',
                             json={'file': 'workbuddy-nosuchuid.json', 'enabled': True})
        self.assertEqual(r.status_code, 404, r.text)

    def test_account_list_returns_skip_tasks(self) -> None:
        self._login('admin')
        wb2api.set_account_skip_tasks(self.fname, True)

        async def fake_status(**kwargs: object) -> dict:
            return {'connected': False, 'accounts': []}

        with mock.patch.object(wb2api, 'get_status', fake_status):
            r = self.client.get('/api/accounts')
        self.assertEqual(r.status_code, 200, r.text)
        rows = {a['uid']: a for a in r.json()['accounts']}
        self.assertIs(rows[UID]['skip_tasks'], True)
        self.assertTrue(rows[UID]['in_pool'] is not None)


class TaskRunFilterTest(unittest.TestCase):
    """面板「一键任务」（claim / full）以 ALL 调脚本时也要剔除 skip_tasks 账号。

    上游脚本只按名单逐个加载，自己不认 skip_tasks；过滤必须在面板构造 argv 时做。
    这里直接验 `_prepare` 生成的 argv（不真起子进程）。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        (self.dir / 'workbuddy-normal.json').write_text(
            json.dumps(_auth_doc('437normaluid')), encoding='utf-8')
        (self.dir / 'workbuddy-skip.json').write_text(
            json.dumps(_auth_doc('437skipuid', skip=True)), encoding='utf-8')

    def tearDown(self) -> None:
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def test_all_target_passes_only_task_accounts(self) -> None:
        from server.services import taskrun
        with mock.patch.object(taskrun, 'available', lambda d: (True, '')), \
             mock.patch.object(taskrun, '_script_path', lambda: Path('task_runner.py')):
            ok, payload = taskrun._prepare('preview', 'ALL', self.dir)
        self.assertTrue(ok, payload)
        argv = payload['argv']
        self.assertIn('workbuddy-normal.json', argv)
        self.assertNotIn('workbuddy-skip.json', argv)

    def test_all_accounts_skipped_is_rejected(self) -> None:
        from server.services import taskrun
        for path in self.dir.glob('workbuddy-*.json'):
            raw = json.loads(path.read_text(encoding='utf-8'))
            raw['skip_tasks'] = True
            path.write_text(json.dumps(raw), encoding='utf-8')
        with mock.patch.object(taskrun, 'available', lambda d: (True, '')), \
             mock.patch.object(taskrun, '_script_path', lambda: Path('task_runner.py')):
            ok, why = taskrun._prepare('preview', 'ALL', self.dir)
        self.assertFalse(ok)
        self.assertIn('不参与定时任务', str(why))


if __name__ == '__main__':
    unittest.main()
