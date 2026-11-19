"""批量签到（`POST /api/accounts/checkin-all`）的统计口径。

## 钉住的问题

界面提示「5/6 个账号成功」，但账号池里 6 个有 1 个是**国际版**。国际版没有
签到体系（上游对 global 账号直接过滤、不发请求），`checkin_all` 对它返回
「已跳过」——既不会成功、也不是失败。可它此前仍然占着 `total` 的分母，
于是用户看到「5/6」会以为有一个号漏签了，反复去点；而那个号无论点多少次
都是「已跳过」。同理，`succeeded != total` 会让前端走「部分失败」分支，
弹一个其实并不存在的告警。

## 正确口径

`total` / `succeeded` 只统计**可签到**的账号；跳过的用 `skipped` 单独报出来，
让界面能说清「另有 N 个账号不适用」。

这个口径与前端已有的两处判断必须一致——否则同一个国际版账号会在三个地方
得到三种待遇：

  · 账号页**隐藏**它的签到按钮（注释：对国际版只会返回「已跳过」，属误导）
  · 国际版视图**隐藏**「全部签到」按钮（理由同上）
  · 批量统计**把它算进分母并计为不成功** ← 本文件修掉的就是这条
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402

CN_UID = 'aaaa1111-0000-0000-0000-000000000001'
CN2_UID = 'aaaa1111-0000-0000-0000-000000000002'
GL_UID = 'bbbb2222-0000-0000-0000-000000000001'


def _acc(file: str, uid: str, nickname: str) -> dict:
    return {'file': file, 'uid': uid, 'nickname': nickname}


def _raw(uid: str, realm: str) -> dict:
    return {
        'auth': {
            'accessToken': 'AT',
            'domain': 'www.workbuddy.ai' if realm == 'global' else 'www.codebuddy.cn',
            'realm': realm,
        },
        'account': {'uid': uid, 'enterpriseId': ''},
        'device_token': 'dt',
    }


class CheckinAllScopeTest(unittest.TestCase):
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

    def _run(self, accounts: list, realm_by_uid: dict) -> dict:
        """跑一次批量签到，出站请求全部换成本地桩。"""
        from server.services import realm, tencent, wb2api

        uid_by_file = {a['file']: a['uid'] for a in accounts}

        def _read(f: str, auth_dir=None) -> dict:
            uid = uid_by_file[f]
            return _raw(uid, realm_by_uid[uid])

        checkin = mock.AsyncMock(return_value=(0, 'ok'))
        # 逃生门（global.enabled）的默认值取决于运行环境的 config.json，
        # 显式打开才能确定性地走到「国际版」分支
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
        out['__checkin_calls'] = checkin.call_count
        return out

    def test_global_account_excluded_from_denominator(self) -> None:
        """2 个国内 + 1 个国际 → 2/2 成功，1 个不适用（而不是 2/3）。"""
        accounts = [_acc('workbuddy-a.json', CN_UID, '甲'),
                    _acc('workbuddy-b.json', CN2_UID, '乙'),
                    _acc('workbuddy-g.json', GL_UID, '国际号')]
        out = self._run(accounts, {CN_UID: 'cn', CN2_UID: 'cn', GL_UID: 'global'})

        self.assertEqual(out['total'], 2, '分母不应包含国际版账号')
        self.assertEqual(out['succeeded'], 2)
        self.assertEqual(out['skipped'], 1)
        # 跳过不能变成「失败」——否则前端会弹一个并不存在的告警
        self.assertEqual(out['total'] - out['succeeded'], 0,
                         '国际版账号被算成了失败，前端会误报「部分失败」')
        # 国际版不发上游请求（风控），所以只应有 2 次签到调用
        self.assertEqual(out['__checkin_calls'], 2,
                         '对国际版账号发起了签到请求（应直接跳过）')

    def test_skipped_flag_marks_the_global_account(self) -> None:
        """逐条结果里也要能认出「跳过」，前端才能分类展示。"""
        accounts = [_acc('workbuddy-a.json', CN_UID, '甲'),
                    _acc('workbuddy-g.json', GL_UID, '国际号')]
        out = self._run(accounts, {CN_UID: 'cn', GL_UID: 'global'})

        by_nick = {r['nickname']: r for r in out['results']}
        self.assertTrue(by_nick['甲']['ok'])
        self.assertFalse(by_nick['国际号']['ok'])
        self.assertTrue(by_nick['国际号'].get('skipped'),
                        '国际版账号没有 skipped 标记，前端无法与「真失败」区分')
        self.assertEqual(by_nick['国际号']['code'], -2)

    def test_all_global_reports_zero_targets(self) -> None:
        """整池都是国际版 → 0 个可签到，而不是 N 个失败。"""
        accounts = [_acc('workbuddy-g.json', GL_UID, '国际号')]
        out = self._run(accounts, {GL_UID: 'global'})

        self.assertEqual(out['total'], 0)
        self.assertEqual(out['succeeded'], 0)
        self.assertEqual(out['skipped'], 1)

    def test_cn_only_unchanged(self) -> None:
        """全是国内版时口径不变（回归保护）。"""
        accounts = [_acc('workbuddy-a.json', CN_UID, '甲'),
                    _acc('workbuddy-b.json', CN2_UID, '乙')]
        out = self._run(accounts, {CN_UID: 'cn', CN2_UID: 'cn'})

        self.assertEqual(out['total'], 2)
        self.assertEqual(out['succeeded'], 2)
        self.assertEqual(out['skipped'], 0)


if __name__ == '__main__':
    unittest.main()
