"""版本筛选不能筛掉「不属于任何账号」的日志行（issue #51）。

## 现象

定时签到实际跑成功了，但「签到记录」里看不到自动签到的**轮次汇总行**——国内版 /
国际版两个视图都看不到，页面看起来就像自动签到没跑。根因是那行的 uid 为空
（`checkin done: total=6 ok=4 ...` 不属于任何账号），而版本筛选把空 uid 判成
「不属于该版本」直接丢掉了。

同一个判据还影响 `/task-logs`：uid 为空的脚本行（school）在版本视图下同样不可见。

## 这里钉住的

  · **uid 为空 → 属于所有版本**：轮次汇总行、脚本行不属于任何账号，是「这一轮
    跑没跑」的唯一凭据，在哪个版本视图下都必须看得见；
  · **删号的历史日志仍按老规矩**（uid 非空但不在账号表里 → 不展示）：那时的顾虑
    是「宁可少显示，也不要把国际版账号的日志挂到国内版视图下」，与空 uid 不是
    一回事；
  · **total 与列表同口径**：此前 total 统计在版本过滤之前，界面显示「共 6 条 ·
    仅显示最近 2 条」而列表只有 2 条，加重了「记录丢了」的错觉；现在按过滤后
    统计，另外单独给出两版合计（`unfiltered_total`）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import db  # noqa: E402
from server.routers import accounts  # noqa: E402

CN_UID = 'cn000000-1111-2222-3333-444444444444'
GL_UID = 'gl000000-1111-2222-3333-444444444444'
REALM_MAP = {CN_UID: 'cn', GL_UID: 'global'}


class UidRealmMatchTest(unittest.TestCase):
    """判据本身：哪些行算「属于这个版本」。"""

    def test_empty_uid_belongs_to_every_realm(self) -> None:
        """轮次汇总 / 脚本行（uid 为空）在两个视图下都要显示。"""
        for realm in ('cn', 'global'):
            self.assertTrue(accounts._uid_matches_realm('', REALM_MAP, realm),
                            f'{realm} 视图把空 uid 的行筛掉了')

    def test_none_uid_behaves_like_empty(self) -> None:
        self.assertTrue(accounts._uid_matches_realm(None, REALM_MAP, 'cn'))

    def test_account_rows_still_filtered(self) -> None:
        self.assertTrue(accounts._uid_matches_realm(CN_UID, REALM_MAP, 'cn'))
        self.assertFalse(accounts._uid_matches_realm(CN_UID, REALM_MAP, 'global'))
        self.assertFalse(accounts._uid_matches_realm(GL_UID, REALM_MAP, 'cn'))

    def test_deleted_account_row_still_hidden(self) -> None:
        """uid 非空但账号已删除 → 宁可不显示（既有策略，不受本次改动影响）。"""
        for realm in ('cn', 'global'):
            self.assertFalse(accounts._uid_matches_realm('deadbeef-0000', REALM_MAP, realm))

    def test_upstream_short_uid_prefix(self) -> None:
        """上游日志只带前 8 位：前缀唯一命中才算。"""
        self.assertTrue(accounts._uid_matches_realm(CN_UID[:8], REALM_MAP, 'cn'))
        self.assertFalse(accounts._uid_matches_realm(CN_UID[:8], REALM_MAP, 'global'))

    def test_ambiguous_prefix_across_realms_hidden(self) -> None:
        """同一前缀同时命中两个版本 → 不确定，不展示（既有策略）。"""
        m = {'aaaaaaaa-1': 'cn', 'aaaaaaaa-2': 'global'}
        self.assertFalse(accounts._uid_matches_realm('aaaaaaaa', m, 'cn'))
        self.assertFalse(accounts._uid_matches_realm('aaaaaaaa', m, 'global'))

    def test_no_filter_means_everything(self) -> None:
        self.assertTrue(accounts._uid_matches_realm('whatever', None, 'cn'))


def _checkin_row(uid: str, ts: int, message: str = 'ok') -> dict:
    return {'id': ts, 'ts': ts, 'uid': uid, 'nickname': '', 'source': 'manual',
            'kind': 'checkin', 'success': True, 'code': None, 'message': message}


def _auto_row(uid: str, ts: int, message: str) -> dict:
    return {'id': ts, 'ts': ts, 'uid': uid, 'kind': 'checkin', 'level': 'ok',
            'credits': 0, 'message': message}


class CheckinLogsRealmViewTest(unittest.TestCase):
    """接口层：汇总行可见，且 total 与列表同一口径。"""

    def _call(self, *, realm: str | None, local: list[dict], auto: list[dict]) -> dict:
        def list_checkin_logs(limit=200, uid=None, offset=0, days=None):
            return list(local)

        def list_task_logs(limit=200, uid=None, kind=None, offset=0, days=None):
            return list(auto)

        with mock.patch.object(db, 'list_checkin_logs', list_checkin_logs), \
             mock.patch.object(db, 'count_checkin_logs', lambda uid=None, days=None: len(local)), \
             mock.patch.object(db, 'list_task_logs', list_task_logs), \
             mock.patch.object(db, 'count_task_logs',
                               lambda uid=None, kind=None, days=None: len(auto)), \
             mock.patch.object(accounts, '_realm_uid_filter', lambda r: (REALM_MAP if r else None)), \
             mock.patch.object(accounts, '_nickname_resolver', lambda: (lambda uid: '')):
            return accounts.checkin_logs(limit=20, realm=realm, user={'role': 'admin'})

    def test_summary_row_visible_in_both_realm_views(self) -> None:
        """报告者的场景：轮次汇总行（uid 为空）在两个版本视图下都要出现。"""
        local = [_checkin_row(CN_UID, 100)]
        auto = [_auto_row('', 200, '本轮签到完成：共 6 个，成功 4，已签到 0，失败 0，跳过 2'),
                _auto_row(GL_UID, 300, 'checkin done')]
        for realm in ('cn', 'global'):
            with self.subTest(realm=realm):
                res = self._call(realm=realm, local=local, auto=auto)
                uids = [it['uid'] for it in res['items']]
                self.assertIn('', uids, f'{realm} 视图里看不到自动签到的汇总行')

    def test_realm_view_keeps_its_own_account_rows(self) -> None:
        """本版自己的行留着、另一版的行滤掉；汇总行两边都在（顺序按时间倒序）。"""
        local = [_checkin_row(CN_UID, 100)]
        auto = [_auto_row('', 200, 'summary'), _auto_row(GL_UID, 300, 'gl')]
        cn = self._call(realm='cn', local=local, auto=auto)
        gl = self._call(realm='global', local=local, auto=auto)
        self.assertEqual(sorted(it['uid'] for it in cn['items']), sorted(['', CN_UID]))
        self.assertEqual(sorted(it['uid'] for it in gl['items']), sorted(['', GL_UID]))

    def test_total_follows_the_realm_filter(self) -> None:
        """total 必须等于当前视图里的条数，否则界面「共 N 条」与列表对不上。"""
        local = [_checkin_row(CN_UID, 100)]
        auto = [_auto_row('', 200, 'summary'), _auto_row(GL_UID, 300, 'gl')]
        for realm, want in (('cn', 2), ('global', 2), (None, 3)):
            with self.subTest(realm=realm):
                res = self._call(realm=realm, local=local, auto=auto)
                self.assertEqual(res['total'], want)
                self.assertEqual(len(res['items']), want)
        # 版本视图下另给一个两版合计，便于界面说明「共 N 条（两版合计）」
        self.assertEqual(self._call(realm='cn', local=local, auto=auto)['unfiltered_total'], 3)

    def test_unfiltered_view_unchanged(self) -> None:
        """不带 realm 的调用行为不变（total 仍是两表合计）。"""
        local = [_checkin_row(CN_UID, 100)]
        auto = [_auto_row('', 200, 'summary')]
        res = self._call(realm=None, local=local, auto=auto)
        self.assertEqual(res['total'], 2)
        self.assertEqual(res['local_total'], 1)
        self.assertEqual(res['auto_total'], 1)

    def test_realm_window_is_widened(self) -> None:
        """版本视图要把候选窗口放大到单表上限。

        否则另一版本的记录会把本版的挤出窗口——表现为「本版一条都没有」，
        而实际只是没被取出来。
        """
        seen: list[int] = []

        def list_checkin_logs(limit=200, uid=None, offset=0, days=None):
            seen.append(limit)
            return []

        with mock.patch.object(db, 'list_checkin_logs', list_checkin_logs), \
             mock.patch.object(db, 'count_checkin_logs', lambda uid=None, days=None: 0), \
             mock.patch.object(db, 'list_task_logs', lambda **k: []), \
             mock.patch.object(db, 'count_task_logs', lambda **k: 0), \
             mock.patch.object(accounts, '_realm_uid_filter', lambda r: (REALM_MAP if r else None)), \
             mock.patch.object(accounts, '_nickname_resolver', lambda: (lambda uid: '')):
            accounts.checkin_logs(limit=20, realm='cn', user={'role': 'admin'})
            cn_window = seen[-1]
            seen.clear()
            accounts.checkin_logs(limit=20, realm=None, user={'role': 'admin'})
            plain_window = seen[-1]
        self.assertGreater(cn_window, plain_window,
                           '版本视图没放大候选窗口，本版记录可能被另一版挤掉')
        self.assertEqual(plain_window, 20, '不带版本筛选时的窗口不该变')


class LegacyFileUidCompatTest(unittest.TestCase):
    """旧版续期失败行把 uid 落成了**账号文件名**，读端要兼容（PR #477）。

    旧实现 `_note_failure` 只传 `{'uid': '', 'file': name}`，而 `_record` 取
    `uid or file` —— 于是历史行的 uid 是 `workbuddy-xxx.json`。只修写入端的话，
    这些行在版本视图下仍会被过滤掉、或显示成文件名。
    """

    ROWS = [
        {'uid': 'g' * 32, 'nickname': '国际号', 'file': 'workbuddy-gl.json',
         'realm': 'global'},
        {'uid': 'c' * 32, 'nickname': '国内号', 'file': 'workbuddy-cn.json',
         'realm': 'cn'},
    ]

    def _patches(self):
        return (
            mock.patch.object(accounts.wb2api, 'list_auth_accounts',
                              lambda *a, **k: list(self.ROWS)),
            mock.patch.object(accounts.upstreamsvc, 'list_upstreams', lambda **k: []),
        )

    def test_filename_matches_realm(self) -> None:
        p1, p2 = self._patches()
        with p1, p2:
            gl = accounts._realm_uid_filter('global')
            cn = accounts._realm_uid_filter('cn')
        self.assertEqual('global', gl.get('workbuddy-gl.json'))
        self.assertEqual('cn', cn.get('workbuddy-cn.json'))
        self.assertTrue(accounts._uid_matches_realm(
            'workbuddy-gl.json', gl, 'global'))
        self.assertFalse(accounts._uid_matches_realm(
            'workbuddy-gl.json', gl, 'cn'))
        # 去掉 .json 的形态也要能对上
        self.assertTrue(accounts._uid_matches_realm(
            'workbuddy-gl', gl, 'global'))

    def test_filename_resolves_nickname(self) -> None:
        p1, p2 = self._patches()
        with p1, p2:
            resolve = accounts._nickname_resolver()
        self.assertEqual('国际号', resolve('workbuddy-gl.json'))
        self.assertEqual('国际号', resolve('workbuddy-gl'))
        self.assertEqual('国内号', resolve('c' * 32))


if __name__ == '__main__':
    unittest.main()
