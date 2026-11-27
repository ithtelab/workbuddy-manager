"""数据库维护任务（issue #492）。

覆盖第一步的五项要求：
  · 启动维护清掉过期的 request_logs / checkin_logs / account_risk_events；
  · 抓包关闭时 tap_turns 仍按保留期清理；
  · 保留期 0 = 永久保留，不删任何行；
  · 维护任务不可重入（上一轮未结束时本轮返回 skipped）；
  · `connect()` 设置的 journal_size_limit 能读回。

不真的等 6 小时：直接调用 `run_maintenance_once()`，它与后台循环执行的是同一段
逻辑。
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
from server.services import accountrisk, dbmaintenance, tap  # noqa: E402


class DbMaintenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_retention = (
            config.REQUEST_LOG_RETENTION_DAYS,
            config.CHECKIN_LOG_RETENTION_DAYS,
            config.RISK_EVENT_RETENTION_DAYS,
        )
        config.DB_PATH = Path(self._tmp.name) / 'maintenance.db'
        db._conn = None
        db.connect()
        # account_risk_events 由 accountrisk 惰性建表；生产上维护任务也排在
        # accountrisk.initialize() 之后启动。
        accountrisk.initialize()
        self.now = int(time.time())

    def tearDown(self) -> None:
        (config.REQUEST_LOG_RETENTION_DAYS,
         config.CHECKIN_LOG_RETENTION_DAYS,
         config.RISK_EVENT_RETENTION_DAYS) = self._orig_retention
        try:
            if db._conn is not None:
                db._conn.close()
        finally:
            db._conn = None
            config.DB_PATH = self._orig_db
            self._tmp.cleanup()

    def _old(self, days: int = 91) -> int:
        return self.now - days * 86400

    def _count(self, table: str) -> int:
        return int(db.query_one(f'SELECT COUNT(*) AS n FROM {table}')['n'])

    def _insert_request_log(self, ts: int) -> None:
        db.execute('INSERT INTO request_logs(ts, model) VALUES(?, ?)', (ts, 'm'))

    def _insert_checkin_log(self, ts: int) -> None:
        db.execute('INSERT INTO checkin_logs(ts, uid, kind) VALUES(?, ?, ?)',
                   (ts, 'u', 'checkin'))

    def _insert_risk_event(self, ident: str, ts: int) -> None:
        db.execute(
            'INSERT INTO account_risk_events(id, scope, uid, nickname, realm, '
            'operation, http_status, ts, historical) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (ident, '/scope', 'u', 'nick', 'cn', 'keepalive', 429, ts, 0),
        )

    # ── 启动维护的清理范围 ───────────────────────────────
    def test_maintenance_prunes_expired_retention_tables(self) -> None:
        old, fresh = self._old(), self.now
        self._insert_request_log(old)
        self._insert_request_log(fresh)
        self._insert_checkin_log(old)
        self._insert_checkin_log(fresh)
        self._insert_risk_event('old', old)
        self._insert_risk_event('new', fresh)

        result = dbmaintenance.run_maintenance_once()

        self.assertEqual(result['skipped'], 0)
        self.assertGreaterEqual(result['request_logs'], 1)
        self.assertGreaterEqual(result['checkin_logs'], 1)
        self.assertGreaterEqual(result['account_risk_events'], 1)
        self.assertEqual(self._count('request_logs'), 1, '过期请求日志没被清理')
        self.assertEqual(self._count('checkin_logs'), 1, '过期签到记录没被清理')
        self.assertEqual(
            [r['id'] for r in db.query('SELECT id FROM account_risk_events')],
            ['new'], '过期风控事件没被清理 / 新事件被误删')
        self.assertEqual(result['checkpoint'], 1, 'WAL checkpoint 未成功')

    def test_maintenance_still_prunes_tap_when_disabled(self) -> None:
        self.assertFalse(tap.settings()['enabled'], '测试前提：抓包应处于关闭状态')
        old, fresh = self._old(8), self.now
        db.execute(
            'INSERT INTO tap_sessions(id, fingerprint, protocol, started_at, updated_at) '
            'VALUES(?, ?, ?, ?, ?)', (1, 'fp-old', 'chat', old, old))
        db.execute('INSERT INTO tap_turns(session_id, seq, ts) VALUES(?, ?, ?)',
                   (1, 1, old))
        db.execute(
            'INSERT INTO tap_sessions(id, fingerprint, protocol, started_at, updated_at) '
            'VALUES(?, ?, ?, ?, ?)', (2, 'fp-new', 'chat', fresh, fresh))
        db.execute('INSERT INTO tap_turns(session_id, seq, ts) VALUES(?, ?, ?)',
                   (2, 1, fresh))

        result = dbmaintenance.run_maintenance_once()

        self.assertGreaterEqual(result['tap_turns'], 1, '抓包关闭时不应跳过 prune')
        self.assertEqual(
            [r['id'] for r in db.query('SELECT id FROM tap_turns ORDER BY id')], [2],
            '过期抓包轮次没被清理 / 新轮次被误删')

    # ── 0 = 永久保留 ─────────────────────────────────────
    def test_zero_retention_keeps_rows(self) -> None:
        self._insert_request_log(self._old(3650))
        self._insert_checkin_log(self._old(3650))
        self._insert_risk_event('ancient', self._old(3650))
        config.REQUEST_LOG_RETENTION_DAYS = 0
        config.CHECKIN_LOG_RETENTION_DAYS = 0
        config.RISK_EVENT_RETENTION_DAYS = 0

        result = dbmaintenance.run_maintenance_once()

        self.assertEqual(result['request_logs'], 0)
        self.assertEqual(result['checkin_logs'], 0)
        self.assertEqual(result['account_risk_events'], 0)
        self.assertEqual(self._count('request_logs'), 1, '0 应表示永久保留')
        self.assertEqual(self._count('checkin_logs'), 1, '0 应表示永久保留')
        self.assertEqual(self._count('account_risk_events'), 1, '0 应表示永久保留')

    # ── 防重入 ───────────────────────────────────────────
    def test_reentrant_run_is_skipped(self) -> None:
        inner: dict[str, dict[str, int]] = {}

        def fake_prune() -> dict[str, int]:
            # 模拟上一轮仍在执行时又来一次：必须立即返回 skipped，而不是排队
            # 再删一遍。
            inner['result'] = dbmaintenance.run_maintenance_once()
            return {'turns': 0}

        with mock.patch.object(tap, 'prune', side_effect=fake_prune):
            outer = dbmaintenance.run_maintenance_once()

        self.assertEqual(inner['result'], {'skipped': 1})
        self.assertEqual(outer['skipped'], 0)

    # ── journal_size_limit ───────────────────────────────
    def test_journal_size_limit_is_applied(self) -> None:
        self.assertEqual(config.JOURNAL_SIZE_LIMIT, 256 * 1024 * 1024)
        self.assertEqual(int(db.query_one('PRAGMA journal_size_limit')[0]),
                         config.JOURNAL_SIZE_LIMIT)

        # 改配置后重开连接应生效（验证 connect() 是读 config 而不是写死常量）。
        with mock.patch.object(config, 'JOURNAL_SIZE_LIMIT', 1024 * 1024):
            db._conn.close()  # type: ignore[union-attr]
            db._conn = None
            db.connect()
            self.assertEqual(int(db.query_one('PRAGMA journal_size_limit')[0]),
                             1024 * 1024)


if __name__ == '__main__':
    unittest.main()
