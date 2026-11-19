"""侧表保留策略（2026-10-18 审计）。

`usage_hourly` / `task_logs` / `audit_logs` 以前只增不减：
  · usage_hourly 每天 × 每密钥 × 每模型 × 24 行，长期运行拖慢查询与备份；
  · audit_logs **未认证路径也能写**（失败令牌会留痕），没有上限等于给了一个
    填磁盘的入口（请求日志有 90 天保留，审计反而没有）。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402


class SideTableRetentionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'r.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        try:
            db._conn.close()  # type: ignore[union-attr]
        finally:
            db._conn = None
            config.DB_PATH = self._orig
            self._tmp.cleanup()

    def _day(self, offset_days: int) -> str:
        return time.strftime('%Y-%m-%d',
                             time.localtime(time.time() + offset_days * 86400))

    def test_hourly_and_task_logs_expire(self) -> None:
        old_day = self._day(-db._HOURLY_RETAIN_DAYS - 5)
        db.execute('INSERT INTO usage_hourly(day,hour,key_id,model) VALUES(?,?,?,?)',
                   (old_day, 1, 0, 'm'))
        db.execute('INSERT INTO usage_hourly(day,hour,key_id,model) VALUES(?,?,?,?)',
                   (self._day(0), 1, 0, 'm'))

        old_ts = int(time.time()) - (db._TASK_LOG_RETAIN_DAYS + 1) * 86400
        db.execute('INSERT INTO task_logs(ts,uid,kind,dedup_key) VALUES(?,?,?,?)',
                   (old_ts, 'u', 'checkin', 'old'))
        db.execute('INSERT INTO task_logs(ts,uid,kind,dedup_key) VALUES(?,?,?,?)',
                   (int(time.time()), 'u', 'checkin', 'new'))

        db._prune_side_tables()

        days = {r['day'] for r in db.query('SELECT day FROM usage_hourly')}
        self.assertNotIn(old_day, days, '过期的 usage_hourly 没被清理')
        self.assertIn(self._day(0), days)
        keys = {r['dedup_key'] for r in db.query('SELECT dedup_key FROM task_logs')}
        self.assertEqual(keys, {'new'})

    def test_audit_logs_are_capped_keeping_newest(self) -> None:
        for i in range(8):
            db.execute('INSERT INTO audit_logs(ts,actor,action) VALUES(?,?,?)',
                       (1700000000 + i, 'admin', f'act{i}'))
        with mock.patch.object(db, '_AUDIT_LOG_MAX_ROWS', 3):
            db._prune_side_tables()
        actions = [r['action'] for r in db.query(
            'SELECT action FROM audit_logs ORDER BY id ASC')]
        self.assertEqual(actions, ['act5', 'act6', 'act7'], '审计日志没有保留最新 N 条')

    def test_fewer_rows_than_cap_are_untouched(self) -> None:
        for i in range(3):
            db.execute('INSERT INTO audit_logs(ts,actor,action) VALUES(?,?,?)',
                       (1700000000 + i, 'admin', f'a{i}'))
        with mock.patch.object(db, '_AUDIT_LOG_MAX_ROWS', 10):
            db._prune_side_tables()
        self.assertEqual(db.query_one('SELECT COUNT(*) AS n FROM audit_logs')['n'], 3)


if __name__ == '__main__':
    unittest.main()
