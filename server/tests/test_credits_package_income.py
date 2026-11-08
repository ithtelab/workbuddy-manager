"""签到收入被同日到期抵消时的记账口径（issue #393）。

余额净增会把「签到 +100、同日旧包到期 -100」记成 0；实际收入是**新出现的积分包**。
这里钉住：新包记收入、旧包不重复、拿不到到期列表时仍回退余额净增。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
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
        return db.query('SELECT credits, message, dedup_key FROM task_logs ORDER BY id')

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


if __name__ == '__main__':
    unittest.main()
