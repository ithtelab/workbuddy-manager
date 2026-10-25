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
  5. days=1（当天）时下探到小时：固定 24 个小时桶（补零）。支出取 `usage_hourly`
     （与 `usage_daily` 同一条写入路径，合计一致），收入按 `hour_sql(ts)` 分桶、
     归到余额查询发生的那个小时。
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
    def _run(self, task_rows, usage_rows, *, days=7, realm=None, uids=None, hour_rows=None):
        seen: list[tuple[str, tuple]] = []

        def fake_query(sql, args=()):
            seen.append((sql, tuple(args)))
            if 'task_logs' in sql:
                return task_rows
            if 'usage_hourly' in sql:
                return hour_rows or []
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
    """days=1 的单日下探（issue #285）：24 个小时桶、两套口径各自的取数来源。"""

    def _run(self, task_rows, usage_rows, *, hour_rows=None, realm=None, uids=None):
        def fake_query(sql, args=()):
            if 'task_logs' in sql:
                # task_logs 会被查两次：按小时（含 %Y-%m-%d %H）与按天。
                # 按天查询拿到小时形态的行会在 r['day'] 上炸，这里只喂给按小时的。
                return task_rows if "%Y-%m-%d %H'" in sql else []
            if 'usage_hourly' in sql:
                return hour_rows or []
            if 'usage_daily' in sql:
                return usage_rows
            return []

        with mock.patch.object(stats.db, 'query', fake_query), \
             mock.patch.object(stats, '_realm_uids', lambda r: uids):
            return stats.credits_flow(days=1, realm=realm, user={'role': 'admin'})

    def test_single_day_gives_24_hourly_buckets(self) -> None:
        """固定 24 桶（补零，含还没到的小时），收支与净额逐桶对得上。"""
        today = _day(0)
        out = self._run(
            [{'bucket': f'{today} 09', 'income': 40}],
            [],
            hour_rows=[{'hour': 14, 'credit': 3.5}],
        )
        self.assertEqual(out['granularity'], 'hour')
        hourly = out['hourly']
        self.assertEqual(len(hourly), 24)
        self.assertEqual([p['hour'] for p in hourly], list(range(24)))
        self.assertEqual(hourly[9],
                         {'day': today, 'hour': 9, 'income': 40, 'expense': 0, 'net': 40})
        self.assertEqual(hourly[14]['expense'], 3.5)
        self.assertEqual(hourly[14]['net'], -3.5)
        self.assertTrue(all(p['income'] == 0 and p['expense'] == 0
                            for p in hourly if p['hour'] not in (9, 14)),
                        '没有数据的小时补零，不是缺格')

    def test_hourly_expense_targets_usage_hourly(self) -> None:
        """支出按小时取 usage_hourly（与 usage_daily 同一条写入路径，合计一致）。"""
        seen: list[tuple[str, tuple]] = []

        def fake_query(sql, args=()):
            seen.append((sql, tuple(args)))
            return []

        with mock.patch.object(stats.db, 'query', fake_query), \
             mock.patch.object(stats, '_realm_uids', lambda r: None):
            stats.credits_flow(days=1, realm='global', user={'role': 'admin'})
        hourly_sql = [(s, a) for s, a in seen if 'usage_hourly' in s]
        self.assertTrue(hourly_sql, '没有对 usage_hourly 发起查询')
        sql, args = hourly_sql[0]
        self.assertIn('SUM(credit)', sql)
        self.assertIn('GROUP BY hour', sql)
        self.assertIn('realm = ?', sql)
        self.assertEqual(args, (_day(0), 'global'))

    def test_hourly_income_groups_by_hour_with_ts_range(self) -> None:
        """收入按小时分桶：kind 仍只取 credit，过滤用 ts 范围（走索引），
        只有 GROUP BY 用 hour_sql 表达式。"""
        seen: list[tuple[str, tuple]] = []

        def fake_query(sql, args=()):
            seen.append((sql, tuple(args)))
            return []

        with mock.patch.object(stats.db, 'query', fake_query), \
             mock.patch.object(stats, '_realm_uids', lambda r: None):
            stats.credits_flow(days=1, user={'role': 'admin'})
        # task_logs 会被查两次（按天 + 按小时）；按天用的 day_sql 同样含 strftime，
        # 必须用小时格式 `%Y-%m-%d %H` 区分，不能笼统找 strftime。
        income_hour = [(s, a) for s, a in seen
                       if 'task_logs' in s and "%Y-%m-%d %H'" in s]
        self.assertTrue(income_hour, '没有按小时聚合 task_logs')
        sql, args = income_hour[0]
        self.assertIn("kind = 'credit'", sql)
        self.assertIn('GROUP BY bucket', sql)
        start = stats.db.day_start_ts(_day(0))
        self.assertEqual(args[:2], (start, start + 86400),
                         '过滤条件必须是当天零点起的 ts 范围')

    def test_multi_day_has_no_hourly_keys(self) -> None:
        """多日范围响应与原先一致，不带 granularity 与 hourly。"""

        def fake_query(sql, args=()):
            return []

        with mock.patch.object(stats.db, 'query', fake_query), \
             mock.patch.object(stats, '_realm_uids', lambda r: None):
            out = stats.credits_flow(days=7, user={'role': 'admin'})
        self.assertNotIn('hourly', out)
        self.assertNotIn('granularity', out)


if __name__ == '__main__':
    unittest.main()
