"""账号分组（多账号池）：按分组管理账号 + 组间转移 + 分组上下文贯通。

## 背景

本服务是**转发型**反代：账号由上游挑。「让不同密钥用不同分组的账号」在本端
唯一能落地的形态是**多上游实例**——每个实例一套 `auths/` 目录、独立端口与
api_key，天然就是一个账号池分组（见 `upstreamsvc` 模块注释）。本文件钉住的是
**面板侧**的分组语义：

  1. 列表按分组读**目录**（读 A 组的文件、问 B 组的状态，是本功能最容易犯的
     错——界面会把别的组的账号标成在线/离线，判断全错）；
  2. 「移动账号」= 移动**账号文件本身**（凭证一个字节不改），同名冲突拒绝
     （覆盖等于丢掉目标那份凭据）；
  3. 没有本地账号目录的分组：列表只读（manageable=false）、任何文件操作明确
     409——绝不能静默落到默认目录，那会把 B 组的账号加到 A 组去；
  4. 默认分组（不传 upstream_id）行为与升级前逐字相同；
  5. 分组上下文要**贯通到收尾动作**：新账号落盘后触发的热加载等待、改名后的
     重载、重启，都必须指向该分组自己的上游实例，而不是默认那个。

分组上游的 `/status` 与重载是外部动作，测试里一律打桩；文件侧用真实临时目录。
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

from fastapi.testclient import TestClient  # noqa: E402

from server import config, db, security, upstreamsvc  # noqa: E402

UID_D = 'dddd4444-0000-0000-0000-00000000000d'
UID_G = 'eeee5555-0000-0000-0000-00000000000e'


def _acct(uid: str, nickname: str = '测试号', realm: str = 'cn') -> dict:
    return {
        'uid': uid,
        'nickname': nickname,
        'enterprise_id': '',
        'access_token': 'AT-' + uid,
        'refresh_token': 'RT-' + uid,
        'expires_at': int(time.time()) + 3600,
        'domain': 'copilot.tencent.com' if realm == 'cn' else 'www.workbuddy.ai',
        'realm': realm,
    }


class _GroupCase(unittest.TestCase):
    """临时目录 + 临时库 + 会话管理员 + 两个分组（一个有目录、一个没有）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (config.AUTH_DIR, config.DB_PATH, config.USERS_FILE)
        root = Path(self._tmp.name)
        config.AUTH_DIR = root / 'default-auths'
        config.AUTH_DIR.mkdir()
        self.g1_dir = root / 'g1-auths'
        self.g1_dir.mkdir()
        config.DB_PATH = root / 'm.db'
        config.USERS_FILE = root / 'users.json'
        db._conn = None
        db.connect()
        security.save_users({'secret': 'S', 'users': [
            {'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('pw')}],
            'api_keys': []})
        from server.main import app
        self.client = TestClient(app)
        r = self.client.post('/api/login', json={'username': 'admin', 'password': 'pw'})
        assert r.status_code == 200, r.text
        self.client.cookies.update(dict(r.cookies))

        self.g1 = self._create_group('分组甲', 'http://127.0.0.1:7991',
                                      auth_dir=str(self.g1_dir))
        self.g2 = self._create_group('分组乙', 'http://127.0.0.1:7992')  # 无本地目录

    def tearDown(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.AUTH_DIR, config.DB_PATH, config.USERS_FILE = self._orig
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _create_group(self, name: str, base: str, **extra) -> int:
        r = self.client.post('/api/upstreams',
                             json={'name': name, 'base_url': base, **extra})
        self.assertEqual(r.status_code, 200, r.text)
        return int(r.json()['id'])

    def _write(self, directory: Path, filename: str, uid: str, **kw) -> Path:
        p = directory / filename
        p.write_text(json.dumps({
            'account': {'uid': uid, 'nickname': kw.get('nickname', '测试号'),
                        'enterpriseId': ''},
            'auth': {'accessToken': 'AT-' + uid, 'refreshToken': 'RT-' + uid,
                     'expiresAt': int(time.time()) + 3600,
                     'domain': 'copilot.tencent.com', 'realm': 'cn'},
        }, ensure_ascii=False), encoding='utf-8')
        return p


# ── 1) 列表按分组 ──────────────────────────────────────────
class GroupScopeTest(_GroupCase):
    def setUp(self) -> None:
        super().setUp()
        self.f_default = f'workbuddy-{UID_D}.json'
        self.f_group = f'workbuddy-{UID_G}.json'
        self._write(config.AUTH_DIR, self.f_default, UID_D, nickname='默认号')
        self._write(self.g1_dir, self.f_group, UID_G, nickname='甲组号')

    def _status_stub(self):
        from server.services import wb2api
        return mock.patch.object(wb2api, 'get_status',
                                 new=mock.AsyncMock(return_value={'connected': False}))

    def test_default_group_lists_default_dir(self) -> None:
        with self._status_stub():
            r = self.client.get('/api/accounts')
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual([a['file'] for a in out['accounts']], [self.f_default])
        self.assertTrue(out['upstream']['is_default'])
        self.assertTrue(out['manageable'])
        self.assertEqual(out['auth_dir'], str(config.AUTH_DIR))

    def test_group_lists_only_its_own_dir(self) -> None:
        from server.services import wb2api
        with mock.patch.object(wb2api, 'get_status',
                               new=mock.AsyncMock(return_value={'connected': False})) as st:
            r = self.client.get('/api/accounts', params={'upstream_id': self.g1})
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual([a['file'] for a in out['accounts']], [self.f_group],
                         '分组列表读的必须是它自己的目录')
        self.assertEqual(out['upstream']['id'], self.g1)
        self.assertFalse(out['upstream']['is_default'])
        self.assertFalse(out['pool_available'])
        # 状态也必须是**问该分组**的上游实例（而不是默认那个）
        self.assertEqual(st.await_args.kwargs['base_url'], 'http://127.0.0.1:7991')

    def test_group_without_dir_is_readonly_empty(self) -> None:
        from server.services import wb2api
        with mock.patch.object(wb2api, 'get_status',
                               new=mock.AsyncMock(return_value={'connected': False})):
            r = self.client.get('/api/accounts', params={'upstream_id': self.g2})
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual(out['total'], 0)
        self.assertFalse(out['manageable'], '没有本地目录的分组不可管理账号')
        self.assertEqual(out['auth_dir'], '')

    def test_unknown_group_is_404(self) -> None:
        r = self.client.get('/api/accounts', params={'upstream_id': 999999})
        self.assertEqual(r.status_code, 404, r.text)


# ── 2) 移动账号 ────────────────────────────────────────────
class MoveAccountTest(_GroupCase):
    def setUp(self) -> None:
        super().setUp()
        self.fname = f'workbuddy-{UID_D}.json'
        self.src = self._write(config.AUTH_DIR, self.fname, UID_D, nickname='搬家号')

    def _move(self, *, src_group=None, filename=None, to):
        params = {} if src_group is None else {'upstream_id': src_group}
        body = {} if to is ... else {'to_upstream_id': to}
        return self.client.post(f'/api/accounts/{filename or self.fname}/move',
                                params=params, json=body)

    def test_move_default_to_group_keeps_bytes(self) -> None:
        before = self.src.read_bytes()
        with mock.patch.object(upstreamsvc.__class__, '__init__', lambda self: None) \
                if False else mock.patch('server.routers.accounts.reload') as rl:
            rl.request_reload_or_restart = mock.MagicMock(return_value=True)
            resp = self._move(to=self.g1)
        self.assertEqual(resp.status_code, 200, resp.text)
        out = resp.json()
        self.assertTrue(out['ok'])
        self.assertFalse((config.AUTH_DIR / self.fname).exists(), '源目录里应已不在')
        moved = self.g1_dir / self.fname
        self.assertTrue(moved.exists(), '目标目录里应出现')
        self.assertEqual(moved.read_bytes(), before, '凭证一个字节都不能改')
        self.assertEqual(out['to']['id'], self.g1)
        self.assertEqual(out['uid'], UID_D)
        # 收尾的重载等待必须指向**目标分组**
        _args, kwargs = rl.request_reload_or_restart.call_args
        self.assertEqual(kwargs['upstream']['id'], self.g1)

    def test_move_back_to_default_with_zero(self) -> None:
        (self.g1_dir / self.fname).write_bytes(self.src.read_bytes())
        self.src.unlink()
        with mock.patch('server.routers.accounts.reload') as rl:
            rl.request_reload_or_restart = mock.MagicMock(return_value=True)
            resp = self._move(src_group=self.g1, filename=self.fname, to=0)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue((config.AUTH_DIR / self.fname).exists(), '0 = 移回默认分组')
        self.assertFalse((self.g1_dir / self.fname).exists())

    def test_same_group_rejected(self) -> None:
        r = self._move(src_group=self.g1, to=self.g1)
        self.assertEqual(r.status_code, 400, r.text)

    def test_target_collision_rejected(self) -> None:
        (self.g1_dir / self.fname).write_text('{}', encoding='utf-8')
        r = self._move(to=self.g1)
        self.assertEqual(r.status_code, 409, r.text)
        # 冲突时**两边都不能被动过**
        self.assertTrue((self.g1_dir / self.fname).exists())
        self.assertTrue((config.AUTH_DIR / self.fname).exists())

    def test_target_without_dir_rejected(self) -> None:
        r = self._move(to=self.g2)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertTrue((config.AUTH_DIR / self.fname).exists(), '拒绝时账号不能丢')

    def test_source_without_dir_rejected(self) -> None:
        r = self._move(src_group=self.g2, to=self.g1)
        self.assertEqual(r.status_code, 409, r.text)

    def test_unknown_target_404_and_missing_param_400(self) -> None:
        self.assertEqual(self._move(to=999999).status_code, 404)
        self.assertEqual(self._move(to=...).status_code, 400, '必须显式给目标')
        r = self.client.post(f'/api/accounts/{self.fname}/move',
                             json={'to_upstream_id': 'abc'})
        self.assertEqual(r.status_code, 400, r.text)

    def test_missing_file_404(self) -> None:
        r = self._move(filename='workbuddy-nosuch.json', to=self.g1)
        self.assertEqual(r.status_code, 404, r.text)


class FileOpWithoutDirTest(_GroupCase):
    def test_file_op_without_dir_is_409(self) -> None:
        """无本地目录的分组做文件操作必须明确 409，绝不落到默认目录。"""
        r = self.client.post('/api/accounts/workbuddy-x.json/test',
                             params={'upstream_id': self.g2})
        self.assertEqual(r.status_code, 409, r.text)


# ── 3) 文件操作按分组 ──────────────────────────────────────
class GroupFileOpsTest(_GroupCase):
    def setUp(self) -> None:
        super().setUp()
        self.fname = f'workbuddy-{UID_G}.json'
        self._write(self.g1_dir, self.fname, UID_G, nickname='甲组号')
        self.f_default = f'workbuddy-{UID_D}.json'
        self._write(config.AUTH_DIR, self.f_default, UID_D, nickname='默认号')

    def test_disable_renames_in_group_dir_only(self) -> None:
        from server.services import wb2api
        with mock.patch.object(wb2api, 'set_manual_disabled',
                               new=mock.AsyncMock(return_value=(False, 'no route', 'no_route'))), \
                mock.patch('server.routers.accounts.reload') as rl:
            rl.request_restart = mock.MagicMock(return_value=True)
            resp = self.client.post(
                f'/api/accounts/{self.fname}/disabled',
                params={'upstream_id': self.g1}, json={'disabled': True})
        self.assertEqual(resp.status_code, 200, resp.text)
        out = resp.json()
        self.assertEqual(out['via'], 'rename')
        self.assertTrue((self.g1_dir / (self.fname + '.disabled')).exists())
        self.assertFalse((self.g1_dir / self.fname).exists())
        # 默认目录里的账号完全不受影响
        self.assertTrue((config.AUTH_DIR / self.f_default).exists())

    def test_delete_removes_from_group_dir_only(self) -> None:
        with mock.patch('server.routers.accounts.reload') as rl:
            rl.request_restart = mock.MagicMock(return_value=True)
            r = self.client.delete(f'/api/accounts/{self.fname}',
                                   params={'upstream_id': self.g1})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse((self.g1_dir / self.fname).exists())
        self.assertTrue((config.AUTH_DIR / self.f_default).exists(),
                        '删的必须是这一组的文件')


# ── 4) 批量签到/重启也带分组 ───────────────────────────────
class GroupBulkOpsTest(_GroupCase):
    def test_checkin_all_only_touches_group_accounts(self) -> None:
        from server.services import realm, tencent
        self._write(self.g1_dir, f'workbuddy-{UID_G}.json', UID_G, nickname='甲组号')
        self._write(config.AUTH_DIR, f'workbuddy-{UID_D}.json', UID_D, nickname='默认号')
        checkin = mock.AsyncMock(return_value=(0, 'ok'))
        with mock.patch.object(realm, '_read_global_config',
                               return_value={'enabled': True, 'chat_base': '',
                                             'billing_base': ''}), \
                mock.patch.object(realm, 'invalidate'), \
                mock.patch.object(tencent, 'checkin', checkin):
            r = self.client.post('/api/accounts/checkin-all',
                                 params={'upstream_id': self.g1})
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual(out['total'], 1, '只应签该分组的账号')
        self.assertEqual(out['succeeded'], 1)
        self.assertEqual(checkin.await_count, 1)
        self.assertEqual(checkin.await_args.args[0]['uid'], UID_G)

    def test_restart_without_container_reports_actionable(self) -> None:
        r = self.client.post('/api/restart', params={'upstream_id': self.g1})
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertFalse(out['ok'])
        self.assertIn('容器名', out['message'])

    def test_restart_with_container_restarts_that_container(self) -> None:
        from server.services import wb2api
        g3 = self._create_group('分组丙', 'http://127.0.0.1:7993', container='wb-g3')
        with mock.patch.object(wb2api, 'restart_container',
                               new=mock.AsyncMock(return_value=(True, '容器 wb-g3 已重启'))) as rc:
            r = self.client.post('/api/restart', params={'upstream_id': g3})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()['ok'])
        rc.assert_awaited_once_with('wb-g3')


# ── 5) 添加账号落进指定分组 + 分组贯通到收尾 ───────────────
class AddAccountIntoGroupTest(_GroupCase):
    def test_auth_poll_writes_into_group_dir(self) -> None:
        from server.services import tencent
        import server.routers.accounts as acct_mod
        ready = {'status': 'ready', 'uid': UID_G, 'nickname': '新号',
                 'enterprise_id': '', 'access_token': 'AT-new',
                 'refresh_token': 'RT-new', 'expires_at': int(time.time()) + 3600,
                 'domain': 'copilot.tencent.com', 'realm': 'cn'}
        with mock.patch.object(tencent, 'poll_login',
                               new=mock.AsyncMock(return_value=ready)), \
                mock.patch.object(tencent, 'checkin',
                                  new=mock.AsyncMock(return_value=(0, 'ok'))), \
                mock.patch.object(acct_mod.reload, 'request_reload_or_restart',
                                  mock.MagicMock(return_value=True)) as rl:
            r = self.client.get('/api/auth/poll',
                                params={'state': 's1', 'upstream_id': self.g1})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['status'], 'success')
        self.assertTrue((self.g1_dir / f'workbuddy-{UID_G}.json').exists(),
                        '新账号必须落在目标分组的目录里')
        self.assertFalse((config.AUTH_DIR / f'workbuddy-{UID_G}.json').exists())
        # 热加载等待同样要指向该分组
        _args, kwargs = rl.call_args
        self.assertEqual(kwargs['upstream']['id'], self.g1)

    def test_unknown_group_on_auth_start_404(self) -> None:
        r = self.client.post('/api/auth/start', params={'upstream_id': 999999})
        self.assertEqual(r.status_code, 404, r.text)


# ── 5.5) 删除分组：还有账号时拒绝 ───────────────────────────
class DeleteGroupTest(_GroupCase):
    """删除分组的两道闸之一：**分组里还有账号就拒绝**。

    为什么要有这道闸：删记录会让那个账号目录从面板里消失——文件仍在磁盘上，
    但面板不再有它的入口（移不走、删不掉、也看不见）。允许「先清空再删」，
    拒绝「带着账号删」。
    """

    def test_delete_refused_when_group_has_accounts(self) -> None:
        fname = f'workbuddy-{UID_G}.json'
        src = self._write(self.g1_dir, fname, UID_G, nickname='甲组号')
        r = self.client.delete(f'/api/upstreams/{self.g1}')
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn('账号', r.json()['detail'])
        # 拒绝时分组与账号都必须原样还在
        self.assertIsNotNone(upstreamsvc.get_upstream(self.g1))
        self.assertTrue(src.exists())

        # 清空账号后就能删；目录与文件不受影响（这里已经被测试自己删了）
        src.unlink()
        r2 = self.client.delete(f'/api/upstreams/{self.g1}')
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertIsNone(upstreamsvc.get_upstream(self.g1))

    def test_delete_group_without_dir_is_allowed(self) -> None:
        r = self.client.delete(f'/api/upstreams/{self.g2}')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(upstreamsvc.get_upstream(self.g2))

    def test_disabled_account_still_counts(self) -> None:
        """改名停用的账号（.disabled）还在目录里，同样算「还有账号」。"""
        fname = f'workbuddy-{UID_G}.json'
        src = self._write(self.g1_dir, fname, UID_G, nickname='甲组号')
        src.rename(self.g1_dir / (fname + '.disabled'))
        r = self.client.delete(f'/api/upstreams/{self.g1}')
        self.assertEqual(r.status_code, 409, r.text)


# ── 6) 配置语义（服务层） ──────────────────────────────────
class UpstreamGroupFieldsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 'g.db'
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

    def test_default_upstream_exposes_local_dir(self) -> None:
        d = upstreamsvc.default_upstream()
        self.assertEqual(d['auth_dir'], str(config.AUTH_DIR))
        self.assertEqual(d['container'], config.WB2API_CONTAINER)

    def test_auth_dir_must_be_absolute(self) -> None:
        with self.assertRaises(ValueError):
            upstreamsvc.create_upstream('X', 'http://x.example', auth_dir='relative/auths')

    def test_container_name_validated(self) -> None:
        with self.assertRaises(ValueError):
            upstreamsvc.create_upstream('X', 'http://x.example', container='bad name; rm -rf')

    def test_roundtrip_and_clear(self) -> None:
        d = str(Path(self._tmp.name) / 'auths')
        up = upstreamsvc.create_upstream('甲', 'http://a.example', auth_dir=d,
                                         container='wb-a')
        self.assertEqual(up['auth_dir'], d)
        self.assertEqual(up['container'], 'wb-a')
        got = upstreamsvc.get_upstream(up['id'])
        self.assertEqual(got['auth_dir'], d)
        cleared = upstreamsvc.update_upstream(up['id'], {'auth_dir': ''})
        self.assertEqual(cleared['auth_dir'], '')


if __name__ == '__main__':
    unittest.main()

# ── 评审补：评审时发现这一条没人测，而它决定「默认钥匙会不会被发去别的地址」──
class ForwardApiKeyTest(unittest.TestCase):
    """`upstreamsvc.forward_api_key` 的取值规则（评审补）。

    这是整个分组功能里**最该被测到**的一条：分组的 api_key 留空时，它决定要不要
    沿用默认分组那把钥匙。沿用错了方向就是「把默认上游的凭据发去另一个地址」——
    而分组地址是管理员填的，一旦误判，凭据就跑到了那台机器上。原 PR 没有为它写
    任何用例（新增的 1736 项里一条都没覆盖），所以这里逐档钉住。
    """

    def setUp(self) -> None:
        from unittest import mock
        from server import upstreamsvc
        self.svc = upstreamsvc
        default = {'id': None, 'is_default': True,
                   'base_url': 'http://default.example:7863', 'api_key': 'DEFAULT-KEY'}
        self._patch = mock.patch.object(upstreamsvc, 'default_upstream',
                                        return_value=default)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_own_key_wins(self) -> None:
        up = {'is_default': False, 'base_url': 'http://a.example', 'api_key': 'OWN'}
        self.assertEqual(self.svc.forward_api_key(up), 'OWN')

    def test_blank_key_same_address_borrows_default(self) -> None:
        """同址 = 同一套实例，钥匙本就是同一把 —— 不沿用的话「只填名称」的分组必吃 401。"""
        up = {'is_default': False, 'base_url': 'http://default.example:7863/', 'api_key': ''}
        self.assertEqual(self.svc.forward_api_key(up), 'DEFAULT-KEY')

    def test_blank_key_other_address_never_borrows(self) -> None:
        """**关键**：地址不同就必须不带鉴权头 —— 绝不把默认钥匙发去别的地址。

        （这是「少沿用」的方向：别名写法会退化成不带鉴权头、换来上游 401，
        那是可见的失败；反方向才是出事。）
        """
        for base in ('http://other.example:7863', 'http://default.example:7864',
                     'https://default.example:7863', 'http://default.example:7863/v1'):
            with self.subTest(base=base):
                up = {'is_default': False, 'base_url': base, 'api_key': ''}
                self.assertEqual(self.svc.forward_api_key(up), '',
                                 f'{base} 与默认上游不同址，不该沿用默认钥匙')

    def test_default_upstream_returns_its_own(self) -> None:
        self.assertEqual(self.svc.forward_api_key({'is_default': True, 'api_key': 'DEFAULT-KEY'}),
                         'DEFAULT-KEY')

    def test_missing_upstream_is_not_a_reason_to_hand_out_the_key(self) -> None:
        self.assertEqual(self.svc.forward_api_key(None), '')


class MoveFilenameTraversalTest(_GroupCase):
    """移动接口不能借文件名跳出分组目录（评审补）。

    文件名来自 URL。校验若只做「目标组有没有目录」，`../` 这类名字就有机会被搬到
    目录之外——而它对上层是 200（看起来成功了）。
    """

    def test_traversal_filename_is_rejected_and_nothing_is_moved(self) -> None:
        victim = self.g1_dir.parent / 'victim.json'
        victim.write_text('{}', encoding='utf-8')
        for bad in ('../victim.json', '..%2Fvictim.json', 'a/../../victim.json'):
            with self.subTest(filename=bad):
                r = self.client.post(f'/api/accounts/{bad}/move',
                                     json={'to_upstream_id': 0})
                self.assertNotEqual(r.status_code, 200, f'{bad} 被接受了：{r.text[:120]}')
        self.assertTrue(victim.is_file(), '目录之外的文件被动过')
        self.assertEqual(victim.read_text(encoding='utf-8'), '{}')



class SharedInstanceHintTest(unittest.TestCase):
    """与默认分组同址的分组，界面必须**就地说明**账号为什么一直「未加载」。

    用户反馈（issue #94 问题 1）：把账号移动到新分组后，切过去看见的状态是
    「未加载」，在该组里重新扫码加的号也一样。这不是故障，而是这种分组的必然
    结果 —— 上游只读它自己那份账号目录，面板把文件放进这一组，上游根本不会加载。
    界面若不解释，用户只会反复扫码、反复重试（报告者就是这么做的）。

    这里用源码级断言钉住「解释确实存在」，因为要真浏览器才跑得出的行为测试
    成本高：横幅本身在验收脚本里看得到（dev/verify_account_groups.mjs）。
    """

    PAGE = (Path(__file__).resolve().parents[2] / 'web' / 'app' / '(main)'
            / 'accounts' / 'page.tsx')

    def test_page_detects_a_shared_instance(self) -> None:
        src = self.PAGE.read_text(encoding='utf-8')
        self.assertIn('sharedInstance', src, '账号页没有检测「与默认分组同址」')
        # 只看地址：api_key 留空时后端本就沿用默认那把（见 upstreamsvc.forward_api_key），
        # 所以「地址相同」= 同一套实例，判据不能更宽也不能更窄。
        self.assertRegex(src, r'base_url[^\n]*===|=== [^\n]*base_url',
                         '判据没有比较 base_url —— 同址才是同一套实例')
        self.assertIn("g.is_default", src, '没有拿默认分组的地址做基准')

    def test_hint_is_rendered_with_its_own_copy(self) -> None:
        src = self.PAGE.read_text(encoding='utf-8')
        # 必须挂在条件上**真的渲染**：只断言「文件里有这句话」是空转的 ——
        # 把条件改成 `false &&` 时文案仍在文件里，界面上却一个像素都没有。
        self.assertRegex(
            src,
            r"\{sharedInstance && \([\s\S]{0,900}?t\('accounts\.sharedInstanceTitle'\)"
            r"[\s\S]{0,900}?t\('accounts\.sharedInstanceHint'\)"
            r"[\s\S]{0,900}?t\('accounts\.sharedInstanceAction'\)",
            '横幅没有挂在 sharedInstance 条件下（或三段文案不在一处）—— '
            '文案在文件里但不渲染，等于没提示',
        )

    def test_copy_exists_in_every_locale(self) -> None:
        import json
        loc = Path(__file__).resolve().parents[2] / 'web' / 'lib' / 'i18n' / 'locales'
        for f in sorted(loc.glob('*.json')):
            data = json.loads(f.read_text(encoding='utf-8'))
            for key in ('sharedInstanceTitle', 'sharedInstanceHint', 'sharedInstanceAction'):
                self.assertIn(key, data.get('accounts', {}),
                              f'{f.name} 缺 accounts.{key}')
