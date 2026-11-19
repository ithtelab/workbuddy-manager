"""积分收支统计（`/api/stats/credits`）。

用户要的是「今日 / 近 7 天 / 每天的积分收入与支出」。这两个数来自**两套口径**，
本文件钉住的就是「口径不被写错」：

  1. **收入只取 `kind='credit'`** —— 它是 `credits.record_balance` 在余额增加时写的
     流水。task_logs 里还有把 credits 当布尔标记写的行（如 `keepalive` 成功记 1），
     所以收入**不能**直接 `SUM(credits)` 全部 kind，否则续期次数会被当成积分收入。
  2. **支出取 `usage_daily.credit`**（每次调用实际扣费），按天合计。
  3. 区间内**每一天都要出现**（含没有数据的日子），折线才不会断成两截。
  4. realm 过滤：支出直接筛列；收入没有 realm 列，按**完整 uid** 映射（`kind='credit'`
     的 uid 是完整 uid，可精确匹配）。
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import stats  # noqa: E402


def _day(offset: int) -> str:
    """相对今天的日期串（offset 为负表示往前）——与后端同一本地时区口径。"""
    return time.strftime('%Y-%m-%d', time.localtime(time.time() + offset * 86400))


class CreditsFlowShapingTest(unittest.TestCase):
    def _run(self, task_rows, usage_rows, *, days=7, realm=None, uids=None):
        seen: list[tuple[str, tuple]] = []

        def fake_query(sql, args=()):
            seen.append((sql, tuple(args)))
            if 'task_logs' in sql:
                return task_rows
            if 'usage_daily' in sql:
                return usage_rows
            return []

        with mock.patch.object(stats.db, 'query', fake_query), \
             mock.patch.object(stats, '_realm_uids', lambda r: uids):
            out = stats.credits_flow(days=days, realm=realm, user={'role': 'admin'})
        return out, seen

    def test_today_and_week_totals(self) -> None:
        today, yday = _day(0), _day(-1)
        out, _ = self._run(
            [{'day': today, 'income': 100}, {'day': yday, 'income': 50}],
            [{'day': today, 'credit': 20}],
        )
        self.assertEqual(out['today'], {'income': 100, 'expense': 20, 'net': 80})
        self.assertEqual(out['week']['income'], 150)
        self.assertEqual(out['week']['expense'], 20)
        self.assertEqual(out['week']['net'], 130)

    def test_daily_is_continuous_and_ends_today(self) -> None:
        """区间内每一天都要有（含无数据的日子），否则折线会断。"""
        out, _ = self._run([], [], days=7)
        self.assertEqual(len(out['daily']), 7)
        self.assertEqual(out['daily'][-1]['day'], _day(0))
        self.assertEqual(out['daily'][0]['day'], _day(-6))
        self.assertTrue(all(p['income'] == 0 and p['expense'] == 0 for p in out['daily']))

    def test_net_is_income_minus_expense(self) -> None:
        out, _ = self._run(
            [{'day': _day(0), 'income': 300}],
            [{'day': _day(0), 'credit': 120}],
        )
        self.assertEqual(out['daily'][-1]['net'], 180)

    def test_days_clamped(self) -> None:
        """days 走 _clamp_days：越界值被钳住，不能让超大整数打到数据库。"""
        out, _ = self._run([], [], days=999999)
        self.assertEqual(out['days'], stats._DAYS_MAX)

    def test_income_query_targets_credit_kind_only(self) -> None:
        """收入必须只取 kind='credit'——别的 kind 带着当标记用的 credits 值。"""
        _, seen = self._run([], [])
        income_sql = [s for s, _ in seen if 'task_logs' in s]
        self.assertTrue(income_sql, '没有对 task_logs 发起查询')
        self.assertIn("kind = 'credit'", income_sql[0])

    def test_expense_query_targets_usage_daily(self) -> None:
        _, seen = self._run([], [])
        usage_sql = [s for s, _ in seen if 'usage_daily' in s]
        self.assertTrue(usage_sql, '没有对 usage_daily 发起查询')
        self.assertIn('SUM(credit)', usage_sql[0])

    def test_realm_filters_income_by_full_uid(self) -> None:
        """收入按版本筛：uid 是完整 uid，应写成 IN (...) 并带上这些 uid。"""
        _, seen = self._run([], [], realm='cn', uids={'uid-a', 'uid-b'})
        income = [(s, a) for s, a in seen if 'task_logs' in s][0]
        self.assertIn('uid IN (?,?)', income[0])
        self.assertIn('uid-a', income[1])
        self.assertIn('uid-b', income[1])

    def test_realm_filters_expense_by_column(self) -> None:
        _, seen = self._run([], [], realm='global', uids={'uid-a'})
        usage = [(s, a) for s, a in seen if 'usage_daily' in s][0]
        self.assertIn('realm = ?', usage[0])
        self.assertEqual(usage[1][-1], 'global')

    def test_empty_realm_has_no_uid_filter(self) -> None:
        _, seen = self._run([], [], realm=None)
        income = [s for s, _ in seen if 'task_logs' in s][0]
        self.assertNotIn('uid IN', income)

    def test_unknown_realm_uid_set_yields_no_income(self) -> None:
        """该版本一个账号都没有时，收入直接为空 —— 不能退化成「不过滤」。"""
        out, seen = self._run([{'day': _day(0), 'income': 999}], [], realm='cn', uids=set())
        self.assertEqual(out['today']['income'], 0)
        self.assertFalse([s for s, _ in seen if 'task_logs' in s],
                         '空 uid 集合时不该再查 task_logs')


class CreditsFlowHourlyTest(unittest.TestCase):
    """days=1 时下探到小时（issue #285）。

    单日范围按天聚合只剩一个点，折线画不出来。粒度规则沿用用量趋势图
    （`/stats/hourly`）：固定 24 桶补零，支出取 `usage_hourly`、收入按
    `task_logs` 的余额流水按小时归档。
    """

    def _run(self, *, hours=(), expense_hours=(), days=1, realm=None, uids=None):
        seen: list[tuple[str, tuple]] = []
        today = _day(0)

        def fake_query(sql, args=()):
            seen.append((sql, tuple(args)))
            if 'task_logs' in sql:
                if ' AS bucket' in sql:
                    return [{'bucket': f'{today} {h:02d}', 'income': v} for h, v in hours]
                return []
            if 'usage_hourly' in sql:
                return [{'hour': h, 'credit': v} for h, v in expense_hours]
            if 'usage_daily' in sql:
                return [{'day': today, 'credit': sum(v for _, v in expense_hours)}]
            return []

        with mock.patch.object(stats.db, 'query', fake_query), \
             mock.patch.object(stats, '_realm_uids', lambda r: uids):
            out = stats.credits_flow(days=days, realm=realm, user={'role': 'admin'})
        return out, seen

    def test_single_day_returns_24_buckets(self) -> None:
        out, _ = self._run(hours=[(9, 300.0)], expense_hours=[(9, 50.0), (18, 20.0)])
        self.assertEqual(out['granularity'], 'hour')
        self.assertEqual(len(out['hourly']), 24, '固定 24 桶，X 轴不随时段抖动')
        self.assertEqual([p['hour'] for p in out['hourly']], list(range(24)))
        self.assertEqual(out['hourly'][9]['income'], 300.0)
        self.assertEqual(out['hourly'][9]['expense'], 50.0)
        self.assertEqual(out['hourly'][9]['net'], 250.0)
        self.assertEqual(out['hourly'][18]['expense'], 20.0)
        self.assertEqual(out['hourly'][0]['income'], 0.0, '无数据的小时补零')

    def test_multi_day_keeps_old_shape(self) -> None:
        out, _ = self._run(days=7)
        self.assertEqual(out['granularity'], 'day')
        self.assertNotIn('hourly', out, '多日范围不返回小时数据')

    def test_hourly_filters_realm(self) -> None:
        _, seen = self._run(realm='global', uids={'uid-a'})
        usage = [(s, a) for s, a in seen if 'usage_hourly' in s][0]
        self.assertIn('realm = ?', usage[0])
        self.assertEqual(usage[1][-1], 'global')
        income = [(s, a) for s, a in seen if 'task_logs' in s][0]
        self.assertIn('uid IN (?)', income[0])
        self.assertIn('uid-a', income[1])

    def test_hourly_empty_realm_uid_set_yields_no_income(self) -> None:
        out, seen = self._run(hours=[(9, 999.0)], realm='cn', uids=set())
        self.assertFalse([s for s, _ in seen if 'task_logs' in s],
                         '空 uid 集合时不该再查 task_logs')
        self.assertEqual(out['hourly'][9]['income'], 0.0)


if __name__ == '__main__':
    unittest.main()
