"""按「日期 × 账号」的用量聚合与自定义日期范围（issue #402 / #257）。"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.routers import stats  # noqa: E402

TODAY = time.strftime('%Y-%m-%d')
USER = {'role': 'admin'}


def _ts(day: str, hour: int = 12) -> int:
    return int(time.mktime(time.strptime(f'{day} {hour:02d}:00:00', '%Y-%m-%d %H:%M:%S')))


class ByAccountTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config.DB_PATH = Path(self._tmp.name) / 's.db'
        db._conn = None
        db.connect()
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        self._tmp.cleanup()

    def _log(self, *, day: str, account: str, credit: float = 1.0,
             prompt: int = 100, completion: int = 20, realm: str = 'cn',
             model: str = 'glm-5.2', mapped: str = '', status: int = 200) -> None:
        db.add_request_log(ts=_ts(day), model=model, mapped_model=mapped,
                           account=account, credit=credit, prompt_tokens=prompt,
                           completion_tokens=completion, realm=realm,
                           status=status, key_id=1)

    def test_groups_by_day_and_account(self) -> None:
        self._log(day=TODAY, account='甲(aaaaaaaa)', credit=1.5)
        self._log(day=TODAY, account='甲(aaaaaaaa)', credit=2.5)
        self._log(day=TODAY, account='乙(bbbbbbbb)', credit=4.0)
        self._log(day=TODAY, account='')  # 未回填：不计

        out = stats.by_account(days=1, user=USER)
        by_acct = {i['account']: i for i in out['items']}
        self.assertEqual(by_acct['甲(aaaaaaaa)']['requests'], 2)
        self.assertAlmostEqual(by_acct['甲(aaaaaaaa)']['credit'], 4.0)
        self.assertEqual(by_acct['甲(aaaaaaaa)']['nickname'], '甲')
        self.assertEqual(by_acct['甲(aaaaaaaa)']['uid8'], 'aaaaaaaa')
        self.assertEqual(by_acct['乙(bbbbbbbb)']['requests'], 1)
        self.assertNotIn('', by_acct)

    def test_custom_range_filters_days(self) -> None:
        self._log(day='2026-01-01', account='旧(xxxxxxxx)')
        self._log(day=TODAY, account='新(yyyyyyyy)')
        out = stats.by_account(start='2026-01-01', end='2026-01-01', user=USER)
        self.assertEqual([i['account'] for i in out['items']], ['旧(xxxxxxxx)'])
        self.assertEqual(out['from'], '2026-01-01')
        self.assertEqual(out['to'], '2026-01-01')

    def test_realm_filter(self) -> None:
        self._log(day=TODAY, account='国(cncncncn)', realm='cn')
        self._log(day=TODAY, account='际(glglglgl)', realm='global')
        out = stats.by_account(days=1, realm='global', user=USER)
        self.assertEqual([i['account'] for i in out['items']], ['际(glglglgl)'])

    def test_by_account_model_groups_and_prefers_mapped(self) -> None:
        """issue #369 第 2 点：账号 × 模型。映射后的模型名优先。"""
        self._log(day=TODAY, account='甲(aaaaaaaa)', model='alias-x',
                  mapped='glm-5.2', prompt=100, completion=10)
        self._log(day=TODAY, account='甲(aaaaaaaa)', model='glm-5.2', prompt=50, completion=5)
        self._log(day=TODAY, account='甲(aaaaaaaa)', model='kimi-k3', prompt=200, completion=20)
        self._log(day=TODAY, account='乙(bbbbbbbb)', model='glm-5.2', prompt=30, completion=3)

        out = stats.by_account_model(days=1, user=USER)
        rows = {(r['account'], r['model']): r for r in out['items']}
        self.assertEqual(rows[('甲(aaaaaaaa)', 'glm-5.2')]['requests'], 2)
        self.assertEqual(rows[('甲(aaaaaaaa)', 'glm-5.2')]['tokens'], 165)
        self.assertEqual(rows[('甲(aaaaaaaa)', 'kimi-k3')]['tokens'], 220)
        self.assertEqual(rows[('乙(bbbbbbbb)', 'glm-5.2')]['requests'], 1)

    def test_daily_reports_success_and_failed(self) -> None:
        """issue #369 第 3 点：趋势图要能同时看到成功与失败。"""
        self._log(day=TODAY, account='甲(aaaaaaaa)', status=200)
        self._log(day=TODAY, account='甲(aaaaaaaa)', status=201)
        self._log(day=TODAY, account='甲(aaaaaaaa)', status=500)
        self._log(day=TODAY, account='甲(aaaaaaaa)', status=429)
        # daily 的行来自 usage_daily；request_logs 只提供成功/失败计数。
        db.bump_usage(1, 'glm-5.2', 10, 5, credit=1.0, ts=time.time())
        rows = stats.daily(days=1, user=USER)
        row = next(r for r in rows if r['day'] == TODAY)
        self.assertEqual(row['success'], 2)
        self.assertEqual(row['failed'], 2)

    def test_range_bounds_swaps_and_clamps(self) -> None:
        lo, hi, span = stats._range_bounds(30, '2026-03-10', '2026-03-01')
        self.assertEqual((lo, hi, span), ('2026-03-01', '2026-03-10', 10))
        lo, hi, span = stats._range_bounds(30, '2000-01-01', '2026-01-01')
        self.assertEqual(span, stats._DAYS_MAX)
        self.assertEqual(hi, '2026-01-01')

    def test_bad_dates_fall_back_to_days(self) -> None:
        lo, hi, span = stats._range_bounds(7, 'not-a-date', 'x')
        self.assertEqual(span, 7)
        self.assertEqual(hi, TODAY)


if __name__ == '__main__':
    unittest.main()
