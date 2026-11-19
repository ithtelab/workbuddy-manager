"""筛选到空类型时，筛选栏必须还在（issue #35）。

现场：任务页「自动任务与积分记录」下面那条筛选栏，点到一个**没有记录**的类型
后整条消失，用户再也没法切回「全部」，只能刷新整个页面。

根因不在前端组件，而在接口的 `stats` 口径：`stats` 本意是「各类型的汇总」
（筛选栏用它显示每个类型的条数与总条数），但带 `realm` 参数时（页面总会带）
服务端用了 `list_task_logs(kind=kind, ...)` 去重算它，于是 `stats.total` 变成
「**当前筛选下**的条数」。前端用 `stats.total > 0` 决定筛选栏是否渲染，点进空
类型时它就成了 0 → 筛选栏消失。

一句话：**筛选栏的可见性不能依赖筛选结果本身。** 本文件锁住这条性质。

（前端那侧其实已经做对了：`hasAnyTask` 读的正是 `stats.total`，注释里也写明
「不能按筛选结果判断，否则会消失」——是服务端把口径喂错了。）
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.services import wb2api  # noqa: E402
from server.routers import accounts as accounts_router  # noqa: E402


class StatsScopeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 't.db'
        db._conn = None
        db.connect()
        db.clear_task_logs()
        # 造两类记录，其中 credit 类型**没有**记录（正是用户点进去的那种）
        db.add_task_logs([
            {'ts': 100, 'uid': 'u-cn-1', 'kind': 'travel', 'level': 'ok',
             'credits': 100, 'message': 'travel done', 'dedup_key': 'k1'},
            {'ts': 200, 'uid': 'u-cn-1', 'kind': 'active', 'level': 'ok',
             'credits': 50, 'message': 'active done', 'dedup_key': 'k2'},
            {'ts': 300, 'uid': 'u-glob-1', 'kind': 'travel', 'level': 'ok',
             'credits': 7, 'message': 'global travel', 'dedup_key': 'k3'},
        ])
        # 账号表：cn 有一个号，global 有一个号（realm 过滤据此生效）
        self._accounts = [
            {'uid': 'u-cn-1', 'nickname': 'cn号', 'realm': 'cn', 'file': 'a.json'},
            {'uid': 'u-glob-1', 'nickname': 'global号', 'realm': 'global', 'file': 'b.json'},
        ]
        self._p = mock.patch.object(
            wb2api, 'list_auth_accounts', lambda auth_dir=None: self._accounts)
        self._p.start()

    def tearDown(self) -> None:
        self._p.stop()
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig_db
        self._tmp.cleanup()

    def _call(self, **kw) -> dict:
        return accounts_router.task_logs(limit=50, offset=0, uid=None,
                                         days=None, user={}, **kw)

    def test_stats_total_is_not_filtered_by_kind(self) -> None:
        """**核心断言**：筛一个没有记录的类型，`stats.total` 仍是该版本的总条数。

        前端就是拿它判断「筛选栏要不要渲染」。它一旦跟着 kind 归零，
        筛选栏消失，用户只能刷新页面（issue #35）。
        """
        res = self._call(kind='credit', realm='cn')
        self.assertEqual(res['logs'], [], '前提：credit 类型确实没有记录')
        self.assertEqual(res['total'], 0, 'total 是「当前筛选下」的条数，应为 0')
        self.assertGreater(
            res['stats']['total'], 0,
            'stats.total 跟着筛选归零了 —— 前端据此判断时会隐藏整条筛选栏',
        )
        self.assertEqual(res['stats']['total'], 2, 'cn 版本下应有 2 条记录')

    def test_stats_lists_every_kind_with_counts(self) -> None:
        """筛选栏要逐个类型显示条数（含 0），所以 by_kind 必须是全量的。"""
        res = self._call(kind='credit', realm='cn')
        by_kind = res['stats']['by_kind']
        self.assertEqual(by_kind.get('travel', {}).get('count'), 1)
        self.assertEqual(by_kind.get('active', {}).get('count'), 1)
        # 0 条的类型不出现也没关系（前端按 undefined 当 0），但绝不能因为
        # 当前筛选是 credit 就只剩 credit 一项
        self.assertNotEqual(list(by_kind.keys()), ['credit'],
                            'by_kind 被当前筛选裁剪了')

    def test_stats_respects_realm(self) -> None:
        """stats 要按版本算：国际版只有 1 条，不能把国内版的也数进去。"""
        self.assertEqual(self._call(kind='credit', realm='cn')['stats']['total'], 2)
        self.assertEqual(self._call(kind='credit', realm='global')['stats']['total'], 1)

    def test_total_still_filtered_by_kind(self) -> None:
        """total 保持「当前筛选下」的语义（列表分页要靠它）。"""
        self.assertEqual(self._call(kind='travel', realm='cn')['total'], 1)
        self.assertEqual(self._call(kind='active', realm='cn')['total'], 1)
        self.assertEqual(self._call(kind='credit', realm='cn')['total'], 0)
        self.assertEqual(self._call(realm='cn')['total'], 2)

    def test_total_respects_realm_and_kind_together(self) -> None:
        """版本与类型同时生效：global 的 travel 只有 1 条。"""
        self.assertEqual(self._call(kind='travel', realm='global')['total'], 1)
        # cn 视图下看不到 global 的那条 travel
        self.assertEqual(self._call(kind='travel', realm='cn')['total'], 1)

    def test_no_realm_behaves_the_same(self) -> None:
        """不带 realm（不过滤版本）时也要给出全量 stats。"""
        res = self._call(kind='credit')
        self.assertEqual(res['total'], 0)
        self.assertEqual(res['stats']['total'], 3, '不带版本过滤时应是全部 3 条')


class FrontendVisibilityRuleTest(unittest.TestCase):
    """前端据以判断的那个量必须来自 stats.total（而不是筛选结果）。"""

    def _page(self) -> str:
        return (Path(__file__).resolve().parents[2] / 'web' / 'app' / '(main)'
                / 'tasks' / 'page.tsx').read_text(encoding='utf-8')

    def test_filter_bar_uses_stats_total(self) -> None:
        page = self._page()
        self.assertIn('const hasAnyTask = (taskStats?.total ?? 0) > 0;', page,
                      'hasAnyTask 不再基于 stats.total —— 筛选栏又会被筛选结果左右')
        self.assertIn('{hasAnyTask && (', page, '筛选栏的渲染条件不是 hasAnyTask')

    def test_filter_bar_is_not_conditioned_on_filtered_rows(self) -> None:
        """反证：筛选栏**不得**依赖 taskLogs / filteredTasks。"""
        page = self._page()
        # 找筛选栏那段渲染条件
        idx = page.index('筛选栏的渲染条件')
        seg = page[idx:idx + 400]
        self.assertNotIn('taskLogs.length', seg,
                         '筛选栏的渲染条件用到了筛选后的行数，空筛选时会消失')
        self.assertNotIn('filteredTasks.length', seg, '同上')


if __name__ == '__main__':
    unittest.main()
