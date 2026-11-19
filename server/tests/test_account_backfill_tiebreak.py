"""账号回填：同秒并发时的等距消歧（issue #394）。

背景：同秒到达两条同模型请求时，秒级时间距离都为 0。旧逻辑「等距即放弃」
让这批记录永久留空，界面常驻黄条。修复后等距分支改用上游 `total=`
与本地 `latency_ms` 的差值消歧，唯一胜出才填。
"""
from __future__ import annotations

import sqlite3
import unittest
from unittest import mock

from server import db
from server.services import accountlog


SCHEMA = (
    'CREATE TABLE request_logs ('
    'id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, model TEXT, mapped_model TEXT, '
    'status INTEGER, latency_ms INTEGER, account TEXT, account_expected INTEGER)'
)

MATCHES = (
    'CREATE TABLE request_account_matches ('
    'event_id TEXT PRIMARY KEY, request_log_id INTEGER, ts INTEGER)'
)


class TotalFieldParseTest(unittest.TestCase):
    """上游行尾 total= 要能解析成毫秒，缺失时返回 None。"""

    def test_parses_seconds_to_ms(self) -> None:
        line = ('| #145 | 13:36:37 | hy4free | stream | 200 | 158****1430(43c54954) '
                '| TTFB=10795ms | tok=611 | 37.4tok/s | total=16.3s |')
        self.assertEqual(accountlog._total_ms(line), 16300)

    def test_handles_integer_seconds(self) -> None:
        self.assertEqual(accountlog._total_ms('... | total=25s |'), 25000)

    def test_missing_total_returns_none(self) -> None:
        self.assertIsNone(accountlog._total_ms('| #1 | 00:00:00 | m | stream | 200 | a(b) |'))

    def test_parse_request_lines_carries_total_ms(self) -> None:
        line = ('2026-11-05T05:36:37.123456789Z | #145 | 13:36:37 | hy4free | stream '
                '| 200 | 158****1430(43c54954) | TTFB=10795ms | tok=611 '
                '| 37.4tok/s | total=16.3s |')
        entries = accountlog.parse_request_lines([line])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['total_ms'], 16300)


class PickAccountRowTest(unittest.TestCase):
    """_pick_account_row 的三种分支：不等距照旧、等距消歧、仍不唯一则不猜。"""

    def row(self, rid: int, distance: int, latency: int | None, account=None) -> dict:
        return {'id': rid, 'distance': distance, 'latency_ms': latency, 'account': account}

    def test_single_candidate_returned(self) -> None:
        only = self.row(1, 0, 5000)
        self.assertIs(db._pick_account_row([only], 5000), only)

    def test_unequal_distance_keeps_nearest(self) -> None:
        near = self.row(1, 0, 9000)
        far = self.row(2, 2, 5000)
        # 距离不等时不做耗时比较，仍取最近者（原语义）
        self.assertIs(db._pick_account_row([near, far], 5000), near)

    def test_equidistant_resolved_by_latency(self) -> None:
        a = self.row(1, 0, 16352)
        b = self.row(2, 0, 21309)
        self.assertIs(db._pick_account_row([a, b], 16300), a)
        self.assertIs(db._pick_account_row([a, b], 21300), b)

    def test_equidistant_without_total_gives_up(self) -> None:
        a = self.row(1, 0, 16352)
        b = self.row(2, 0, 21309)
        self.assertIsNone(db._pick_account_row([a, b], None))

    def test_equidistant_without_latency_gives_up(self) -> None:
        a = self.row(1, 0, None)
        b = self.row(2, 0, None)
        self.assertIsNone(db._pick_account_row([a, b], 16300))

    def test_equidistant_ambiguous_latency_gives_up(self) -> None:
        # 两条耗时同样接近目标：仍不猜，避免串填
        a = self.row(1, 0, 10000)
        b = self.row(2, 0, 10000)
        self.assertIsNone(db._pick_account_row([a, b], 10000))

    def test_three_way_tie_resolved(self) -> None:
        a = self.row(1, 0, 6582)
        b = self.row(2, 0, 18765)
        c = self.row(3, 0, 23677)
        self.assertIs(db._pick_account_row([a, b, c], 18700), b)


class AttachRequestAccountsTest(unittest.TestCase):
    """端到端：同秒两条同模型请求，各自回填到正确账号且不串填。"""

    def setUp(self) -> None:
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(SCHEMA)
        self.conn.execute(MATCHES)
        self.addCleanup(self.conn.close)
        patch = mock.patch.object(db, '_conn', self.conn)
        patch.start()
        self.addCleanup(patch.stop)
        lock = mock.patch.object(db, '_lock', __import__('threading').RLock())
        lock.start()
        self.addCleanup(lock.stop)

    def insert(self, ts: int, latency: int, account=None) -> int:
        cur = self.conn.execute(
            'INSERT INTO request_logs(ts, model, mapped_model, status, latency_ms, account, '
            'account_expected) VALUES(?, ?, ?, ?, ?, ?, ?)',
            (ts, 'hy4free', 'hy4free', 200, latency, account, 1))
        self.conn.commit()
        return int(cur.lastrowid)

    def test_same_second_pair_filled_by_latency(self) -> None:
        ts = 1793856997
        first = self.insert(ts, 16352)
        second = self.insert(ts, 21309)

        filled = db.attach_request_accounts([
            {'ts': ts, 'model': 'hy4free', 'account': 'A(11111111)',
             'event_id': 'e1', 'total_ms': 16300},
            {'ts': ts, 'model': 'hy4free', 'account': 'B(22222222)',
             'event_id': 'e2', 'total_ms': 21300},
        ])

        self.assertEqual(filled, 2)
        rows = {r['id']: r['account'] for r in self.conn.execute(
            'SELECT id, account FROM request_logs ORDER BY id')}
        self.assertEqual(rows[first], 'A(11111111)')
        self.assertEqual(rows[second], 'B(22222222)')

    def test_same_second_without_total_stays_empty(self) -> None:
        ts = 1793856998
        self.insert(ts, 16352)
        self.insert(ts, 21309)

        filled = db.attach_request_accounts([
            {'ts': ts, 'model': 'hy4free', 'account': 'A(11111111)', 'event_id': 'e1'},
            {'ts': ts, 'model': 'hy4free', 'account': 'B(22222222)', 'event_id': 'e2'},
        ])

        self.assertEqual(filled, 0)
        rows = self.conn.execute('SELECT account FROM request_logs').fetchall()
        self.assertTrue(all(r['account'] is None for r in rows))
        # 事件未被消费：下一轮日志行更完整时仍可匹配
        matches = self.conn.execute(
            'SELECT COUNT(*) FROM request_account_matches').fetchone()[0]
        self.assertEqual(matches, 0)

    def test_replay_does_not_double_fill(self) -> None:
        ts = 1793856999
        self.insert(ts, 16352)
        events = [{'ts': ts, 'model': 'hy4free', 'account': 'A(11111111)',
                   'event_id': 'e1', 'total_ms': 16300}]
        self.assertEqual(db.attach_request_accounts(events), 1)
        self.assertEqual(db.attach_request_accounts(events), 0)


if __name__ == '__main__':
    unittest.main()
