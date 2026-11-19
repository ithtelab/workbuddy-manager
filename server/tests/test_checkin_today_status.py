"""签到状态（「今日已签到」）的判定与据此产生的跳过行为。

## 钉住的问题

线上实测：同一个账号一天之内被记了 7~11 条签到记录，每条的返回码都是
`10001`「今日已签到」——用户看到「全部签到」按钮就一直点，每次都是一次真实
的腾讯 RPC，而签到记录被这一串「今日已签到」刷满，真正的失败记录被挤到看不见。

根因不是签到本身出错，而是**没人知道今天签过没有**：界面不显示状态、接口也不拦，
所以「已经签到的账号还可以去签到」。

## 正确口径

  · 账号列表带 `checkin_today`（今天成功签到的时刻，没有则为 null）；
  · 单账号签到：今天已签到 → **不打上游、不写日志**，直接回 `already: true`；
  · 批量签到：跳过今天已签到的账号，用 `already` 单独报数（不进 `total` 分母，
    否则界面会把「无需重复」说成「刚签成功」）。

判定依据是本端签到记录：腾讯对「今天已签过」回 10001，我们照记成功，所以
「本端签过」与「今天已签到」在这里是同一件事。边界是**自然日**——昨天的记录
不能算进今天（否则跨天后界面还说「已签到」，用户就永远签不上了）。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402

CN_UID = 'aaaa1111-0000-0000-0000-000000000001'
CN2_UID = 'aaaa1111-0000-0000-0000-000000000002'


def _acc(file: str, uid: str, nickname: str) -> dict:
    return {'file': file, 'uid': uid, 'nickname': nickname}


def _raw(uid: str) -> dict:
    return {
        'auth': {'accessToken': 'AT', 'domain': 'www.codebuddy.cn', 'realm': 'cn'},
        'account': {'uid': uid, 'enterpriseId': ''},
        'device_token': 'dt',
    }


class CheckinTodayStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (config.AUTH_DIR, config.DB_PATH, config.USERS_FILE)
        root = Path(self._tmp.name)
        config.AUTH_DIR = root / 'auths'
        config.DB_PATH = root / 'm.db'
        config.USERS_FILE = root / 'users.json'

        from server import db
        db._conn = None
        db.connect()
        self._db = db

        from server import security
        security.save_users({'secret': 'S', 'users': [
            {'username': 'admin', 'role': 'admin', 'pwd_hash': security.make_hash('p')}],
            'api_keys': []})

        from fastapi.testclient import TestClient
        from server.main import app
        self.c = TestClient(app)
        self.assertEqual(
            self.c.post('/api/login',
                        json={'username': 'admin', 'password': 'p'}).status_code, 200)

    def tearDown(self) -> None:
        if self._db._conn is not None:
            self._db._conn.close()
        self._db._conn = None
        config.AUTH_DIR, config.DB_PATH, config.USERS_FILE = self._orig
        self._tmp.cleanup()

    # ── 造数据 ──────────────────────────────────────────────

    def _seed(self, uid: str, *, ts: int, success: bool = True,
              code: int = 0, source: str = 'manual-batch') -> None:
        """直接写库：`add_checkin_log` 只接受「现在」，而本文件要造昨天/今天。"""
        self._db.execute(
            'INSERT INTO checkin_logs(ts, uid, nickname, source, kind, success, code, message) '
            'VALUES(?, ?, ?, ?, ?, ?, ?, ?)',
            (int(ts), uid, '甲', source, 'checkin', 1 if success else 0, code, ''),
        )

    @staticmethod
    def _today() -> int:
        from server.routers.accounts import _today_start
        return _today_start()

    # ── db 层：判定本身 ─────────────────────────────────────

    def test_done_since_keeps_latest_per_uid(self) -> None:
        """同一账号今天签过多次 → 只留最近一次（界面显示的时间要说得通）。"""
        base = self._today()
        self._seed(CN_UID, ts=base + 60)
        self._seed(CN_UID, ts=base + 600)
        self._seed(CN2_UID, ts=base + 120)

        out = self._db.checkin_done_since(base)
        self.assertEqual(out[CN_UID], base + 600)
        self.assertEqual(out[CN2_UID], base + 120)

    def test_failed_attempt_is_not_done(self) -> None:
        """失败记录不算签到（否则一次失败会让界面显示「已签到」，再也签不上）。"""
        base = self._today()
        self._seed(CN_UID, ts=base + 60, success=False, code=-1)
        self.assertNotIn(CN_UID, self._db.checkin_done_since(base))

    def test_yesterday_does_not_count(self) -> None:
        """跨天必须重新可签：昨天的记录不能算今天的状态。"""
        base = self._today()
        self._seed(CN_UID, ts=base - 3600)
        self.assertNotIn(CN_UID, self._db.checkin_done_since(base))

    def test_empty_uid_never_recorded(self) -> None:
        """无 uid 的账号（文件异常）不能互相串状态。"""
        base = self._today()
        self._seed('', ts=base + 60)
        self.assertEqual(self._db.checkin_done_since(base), {})

    # ── 接口层 ─────────────────────────────────────────────

    def _patch_accounts(self, accounts: list):
        from server.services import wb2api
        return (
            mock.patch.object(wb2api, 'list_auth_accounts', return_value=accounts),
            mock.patch.object(wb2api, 'get_status', new=mock.AsyncMock(return_value={})),
            mock.patch.object(wb2api, 'merge_pool_status', return_value=None),
        )

    def test_list_accounts_exposes_checkin_today(self) -> None:
        base = self._today()
        self._seed(CN_UID, ts=base + 60)
        accounts = [_acc('workbuddy-a.json', CN_UID, '甲'),
                    _acc('workbuddy-b.json', CN2_UID, '乙')]

        p1, p2, p3 = self._patch_accounts(accounts)
        with p1, p2, p3:
            r = self.c.get('/api/accounts')
        self.assertEqual(r.status_code, 200, r.text)
        by_uid = {a['uid']: a for a in r.json()['accounts']}
        self.assertEqual(by_uid[CN_UID]['checkin_today'], base + 60)
        self.assertIsNone(by_uid[CN2_UID]['checkin_today'],
                          '没签过的账号必须给出明确的 null，界面才能判断')

    def test_single_checkin_skips_upstream_when_already_done(self) -> None:
        """今天签过 → 一次上游请求都不发（这是「不要重复签到」的核心）。"""
        from server.services import tencent, wb2api
        base = self._today()
        self._seed(CN_UID, ts=base + 60)
        checkin = mock.AsyncMock(return_value=(0, 'ok'))

        with mock.patch.object(wb2api, 'read_account_file_any',
                              return_value=_raw(CN_UID)), \
             mock.patch.object(tencent, 'checkin', checkin):
            r = self.c.post('/api/accounts/workbuddy-a.json/checkin')

        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body.get('already'), '没有标记 already，前端无法区分「已签到」')
        self.assertEqual(body['code'], 10001)
        self.assertEqual(checkin.call_count, 0, '已签到的账号仍然打了上游请求')

    def test_single_checkin_reports_today_when_already(self) -> None:
        """重复点击不写日志：否则签到记录会被一串「今日已签到」刷满。"""
        from server.services import tencent, wb2api
        base = self._today()
        self._seed(CN_UID, ts=base + 60)
        before = len(self._db.list_checkin_logs(limit=200))

        with mock.patch.object(wb2api, 'read_account_file_any',
                              return_value=_raw(CN_UID)), \
             mock.patch.object(tencent, 'checkin', mock.AsyncMock(return_value=(0, 'ok'))):
            self.c.post('/api/accounts/workbuddy-a.json/checkin')

        after = len(self._db.list_checkin_logs(limit=200))
        self.assertEqual(before, after, '重复签到不该再写一条记录')

    def test_single_checkin_hits_upstream_when_not_done(self) -> None:
        """回归保护：没签过的账号照常签到，且照常记一条日志。"""
        from server.services import tencent, wb2api
        checkin = mock.AsyncMock(return_value=(0, 'ok'))

        with mock.patch.object(wb2api, 'read_account_file_any',
                              return_value=_raw(CN_UID)), \
             mock.patch.object(tencent, 'checkin', checkin), \
             mock.patch('server.routers.accounts.creditsvc.get_credits',
                        new=mock.AsyncMock(return_value=(True, 10, 'ok', False, None, []))):
            r = self.c.post('/api/accounts/workbuddy-a.json/checkin')

        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(checkin.call_count, 1)
        self.assertFalse(r.json().get('already'))

    def test_checkin_all_skips_already_done(self) -> None:
        """批量：已签到的账号不发请求、计入 already，剩余账号照常签。"""
        from server.services import realm, tencent, wb2api
        base = self._today()
        self._seed(CN_UID, ts=base + 60)
        accounts = [_acc('workbuddy-a.json', CN_UID, '甲'),
                    _acc('workbuddy-b.json', CN2_UID, '乙')]
        uid_by_file = {a['file']: a['uid'] for a in accounts}

        def _read(f: str, auth_dir=None) -> dict:
            return _raw(uid_by_file[f])

        checkin = mock.AsyncMock(return_value=(0, 'ok'))
        with mock.patch.object(realm, '_read_global_config',
                               return_value={'enabled': True, 'chat_base': '',
                                             'billing_base': ''}), \
             mock.patch.object(realm, 'invalidate'), \
             mock.patch.object(wb2api, 'list_auth_accounts', return_value=accounts), \
             mock.patch.object(wb2api, 'read_account_file', side_effect=_read), \
             mock.patch.object(tencent, 'checkin', checkin):
            r = self.c.post('/api/accounts/checkin-all')

        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual(out['already'], 1, '已签到账号没有单独报数')
        self.assertEqual(out['total'], 1, 'total 只应包含本次真正发起签到的账号')
        self.assertEqual(out['succeeded'], 1)
        self.assertEqual(checkin.call_count, 1, '对已签到的账号也发了请求')
        by_nick = {x['nickname']: x for x in out['results']}
        self.assertTrue(by_nick['甲'].get('already'))

    def test_checkin_all_all_done_reports_zero_targets(self) -> None:
        """全部已签到 → total 为 0 且 already = N（界面据此说「今日都已签到」）。"""
        from server.services import realm, tencent, wb2api
        base = self._today()
        self._seed(CN_UID, ts=base + 60)
        self._seed(CN2_UID, ts=base + 60)
        accounts = [_acc('workbuddy-a.json', CN_UID, '甲'),
                    _acc('workbuddy-b.json', CN2_UID, '乙')]
        uid_by_file = {a['file']: a['uid'] for a in accounts}
        checkin = mock.AsyncMock(return_value=(0, 'ok'))

        with mock.patch.object(realm, '_read_global_config',
                               return_value={'enabled': True, 'chat_base': '',
                                             'billing_base': ''}), \
             mock.patch.object(realm, 'invalidate'), \
             mock.patch.object(wb2api, 'list_auth_accounts', return_value=accounts), \
             mock.patch.object(wb2api, 'read_account_file',
                               side_effect=lambda f, auth_dir=None: _raw(uid_by_file[f])), \
             mock.patch.object(tencent, 'checkin', checkin):
            out = self.c.post('/api/accounts/checkin-all').json()

        self.assertEqual(out['total'], 0)
        self.assertEqual(out['succeeded'], 0)
        self.assertEqual(out['already'], 2)
        self.assertEqual(checkin.call_count, 0)


if __name__ == '__main__':
    unittest.main()
