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
             prompt: int = 100, completion: int = 20, realm: str = 'cn') -> None:
        db.add_request_log(ts=_ts(day), model='glm-5.2', account=account,
                           credit=credit, prompt_tokens=prompt,
                           completion_tokens=completion, realm=realm,
                           status=200, key_id=1)

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
