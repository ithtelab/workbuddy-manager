"""签到收入被同日到期抵消时的记账口径（issue #393）。

余额净增会把「签到 +100、同日旧包到期 -100」记成 0；实际收入是**新出现的积分包**。
这里钉住：新包记收入、旧包不重复、拿不到到期列表时仍回退余额净增。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.services import credits  # noqa: E402


class PackageIncomeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        config.DB_PATH = Path(self._tmp.name) / 'c.db'
        db._conn = None
        db.connect()
        credits.invalidate()
        db.set_setting(credits._SNAP_KEY, '{}')
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        self._tmp.cleanup()

    def _logs(self) -> list[dict]:
        return db.query('SELECT ts, credits, message, dedup_key FROM task_logs ORDER BY id')

    def test_new_package_counts_even_when_balance_flat(self) -> None:
        # 基线：3910，包 A 到期
        credits.record_balance('u1', '一号', 3910, [{'at': 1000, 'amount': 100}])
        # 签到 +100（新包 B），同日包 A 到期 → 余额仍是 3910
        income = credits.record_balance(
            'u1', '一号', 3910,
            [{'at': 1000, 'amount': 100}, {'at': 2000, 'amount': 100}])
        self.assertEqual(income, 100, '新积分包应记为收入')
        logs = self._logs()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['credits'], 100)
        self.assertIn('积分包', logs[0]['message'])

    def test_same_package_not_double_counted(self) -> None:
        credits.record_balance('u2', '二号', 100, [{'at': 1, 'amount': 50}])
        credits.record_balance('u2', '二号', 100, [{'at': 1, 'amount': 50}])
        credits.record_balance('u2', '二号', 100, [{'at': 1, 'amount': 50}])
        self.assertEqual(self._logs(), [], '同一积分包不重复记账')

    def test_falls_back_to_balance_delta_without_expiries(self) -> None:
        credits.record_balance('u3', '三号', 100)
        income = credits.record_balance('u3', '三号', 130)
        self.assertEqual(income, 30, '拿不到到期列表时仍按余额净增记账')

    def test_no_income_when_nothing_changed(self) -> None:
        credits.record_balance('u4', '四号', 500, [{'at': 9, 'amount': 10}])
        self.assertIsNone(credits.record_balance('u4', '四号', 500, [{'at': 9, 'amount': 10}]))
        self.assertEqual(self._logs(), [])

    def test_consuming_package_does_not_count_remaining_balance_as_income(self) -> None:
        credits.record_balance('u5', '', 1367, [{'at': 3000, 'amount': 1367}])
        self.assertIsNone(credits.record_balance('u5', '', 1366, [{'at': 3000, 'amount': 1366}]))
        self.assertIsNone(credits.record_balance('u5', '', 1365, [{'at': 3000, 'amount': 1365}]))
        self.assertEqual(self._logs(), [])

    def test_same_expiry_topup_only_counts_positive_difference(self) -> None:
        credits.record_balance('u6', '', 90, [{'at': 3000, 'amount': 90}])
        self.assertEqual(credits.record_balance('u6', '', 100, [{'at': 3000, 'amount': 100}]), 10)
        self.assertEqual([row['credits'] for row in self._logs()], [10])

    def test_new_package_and_consumed_old_package_are_separate(self) -> None:
        credits.record_balance('u7', '', 1000, [{'at': 3000, 'amount': 1000}])
        income = credits.record_balance('u7', '', 1000,
            [{'at': 3000, 'amount': 900}, {'at': 4000, 'amount': 100}])
        self.assertEqual(income, 100)

    def test_multiple_packages_at_same_expiry_are_aggregated(self) -> None:
        credits.record_balance('u8', '', 100,
            [{'at': 3000, 'amount': 50}, {'at': 3000, 'amount': 50}])
        self.assertIsNone(credits.record_balance('u8', '', 90,
            [{'at': 3000, 'amount': 40}, {'at': 3000, 'amount': 50}]))
        self.assertEqual(credits.record_balance('u8', '', 120,
            [{'at': 3000, 'amount': 50}, {'at': 3000, 'amount': 70}]), 30)
        self.assertEqual([row['credits'] for row in self._logs()], [30])

    def test_missing_expiries_does_not_create_an_empty_package_baseline(self) -> None:
        credits.record_balance('u9', '', 1000, [{'at': 3000, 'amount': 1000}])
        credits.record_balance('u9', '', 900, [])
        self.assertIsNone(credits.record_balance('u9', '', 899, [{'at': 3000, 'amount': 899}]))
        self.assertEqual(self._logs(), [])

    def test_legacy_empty_package_snapshot_only_establishes_package_baseline(self) -> None:
        db.set_setting(credits._SNAP_KEY, {'old': {'credits': 1000, 'at': 123, 'packages': []}})
        self.assertIsNone(credits.record_balance('old', '', 999, [{'at': 3000, 'amount': 999}]))
        self.assertEqual(self._logs(), [])

    def test_zero_balance_empty_packages_can_receive_real_income(self) -> None:
        credits.record_balance('empty', '', 0, [])
        self.assertEqual(credits.record_balance('empty', '', 20, [{'at': 3000, 'amount': 20}]), 20)

    def test_package_income_uses_created_timestamp(self) -> None:
        credits.record_balance('u10', '', 100, [{'at': 3000, 'amount': 100,
                                                  'created': 1000}])
        with mock.patch.object(credits.time, 'time', return_value=5000):
            income = credits.record_balance(
                'u10', '', 200,
                [{'at': 3000, 'amount': 100, 'created': 1000},
                 {'at': 4000, 'amount': 100, 'created': 2000}])
        self.assertEqual(income, 100)
        self.assertEqual(self._logs()[0]['ts'], 2000,
                         '流水时间用上游 CreateTime，而不是采集时刻')

    def test_missing_created_falls_back_to_collection_time(self) -> None:
        credits.record_balance('u11', '', 100, [{'at': 3000, 'amount': 100}])
        with mock.patch.object(credits.time, 'time', return_value=5000):
            credits.record_balance('u11', '', 200,
                                   [{'at': 4000, 'amount': 100}])
        self.assertEqual(self._logs()[0]['ts'], 5000)

    def test_legacy_two_tuple_snapshot_is_readable(self) -> None:
        db.set_setting(credits._SNAP_KEY, {'old2': {
            'credits': 100, 'at': 123, 'packages': [[3000, 100]]}})
        self.assertIsNone(credits.record_balance(
            'old2', '', 100, [{'at': 3000, 'amount': 100, 'created': 1000}]))
        self.assertEqual(self._logs(), [])

    def test_repeat_topups_in_same_second_are_not_lost(self) -> None:
        with mock.patch.object(credits.time, 'time', return_value=123):
            for value in (90, 100, 90, 100):
                credits.record_balance('repeat', '', value, [{'at': 3000, 'amount': value}])
        self.assertEqual([row['credits'] for row in self._logs()], [10, 10])


if __name__ == '__main__':
    unittest.main()
