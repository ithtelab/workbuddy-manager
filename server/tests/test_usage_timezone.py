"""用量统计的时区口径与重建（P0 回归）。

真实缺陷：写入用量用本地日期（time.strftime），回填却用 UTC
（SQLite 的 date(ts,'unixepoch')）。在 UTC+8 的机器上，凌晨 00:00-08:00
的调用会被算进两个不同的 day，回填时把同一次调用重复计数。

注意：CI 通常跑在 UTC 时区，那里「本地日 == UTC 日」，这个 bug 不会显现。
因此本文件既有时区无关的断言，也在 test_timezone_forcing.py 里用子进程
强制 TZ=Asia/Shanghai 做真复现——否则 CI 永远抓不到它。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402


_HASH_SEQ = 0


def _uniq_hash() -> str:
    """每次调用返回唯一 hash。

    `api_keys.key_hash` 有唯一索引（issue #447 防并发重复），而下面这些测试辅助
    会被同一用例调用多次、每次都插一行 —— 写死同一个字面量会撞索引。测试并不
    关心 hash 取什么值，唯一即可。
    """
    global _HASH_SEQ
    _HASH_SEQ += 1
    return f'h{int(__import__("time").time() * 1e6)}-{_HASH_SEQ}'



def local_midnight_plus(minutes: int) -> int:
    """本地今天 00:minutes（在 UTC+8 下其 UTC 日期是前一天）。"""
    lt = time.localtime()
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, minutes, 0, 0, 0, -1)))


class UsageTimezone(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 'u.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig
        self._tmp.cleanup()

    def _seed(self, ts: int, pt: int = 100, ct: int = 50) -> None:
        db.execute(
            'INSERT INTO api_keys(name, key_hash, prefix, enabled, expires_at, max_ips, '
            'ip_allowlist, models, quota, used_tokens, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('k', _uniq_hash(), 'wbk_abc', 1, None, 0, '[]', '[]', 0, 0, ts),
        )
        db.execute(
            'INSERT INTO request_logs(ts, key_id, ip, model, mapped_model, status, '
            'prompt_tokens, completion_tokens, latency_ms, ua, error, stream) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            (ts, 1, '1.2.3.4', 'glm-5.2', 'glm-5.2', 200, pt, ct, 10, 'ua', '', 0),
        )

    def _daily(self, ts: int, requests: int = 1, pt: int = 100, ct: int = 50) -> None:
        db.execute(
            'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, completion_tokens) '
            'VALUES(?,?,?,?,?,?)',
            (db.day_of(ts), 1, 'glm-5.2', requests, pt, ct),
        )

    # ── 口径一致性（时区无关）────────────────────────────
    def test_day_of_and_sql_agree(self) -> None:
        ts = local_midnight_plus(30)
        via_py = db.day_of(ts)
        via_sql = db.query_one(
            f'SELECT {db.day_sql("ts")} AS d FROM (SELECT {ts} AS ts)'
        )['d']
        self.assertEqual(via_py, via_sql, 'Python 与 SQL 的取日口径必须一致')

    def test_day_of_follows_local_timezone(self) -> None:
        """helper 必须跟随本地时区，而不是固定 UTC。"""
        ts = local_midnight_plus(30)
        self.assertEqual(db.day_of(ts), time.strftime('%Y-%m-%d', time.localtime(ts)))

    def test_day_sql_is_not_naive_utc(self) -> None:
        """SQL 表达式必须带 localtime 修饰符（这正是当初出错的地方）。"""
        self.assertIn('localtime', db.day_sql('ts'))
        ts = int(time.time())
        naive_utc = db.query_one(
            f"SELECT date({ts}, 'unixepoch') AS d"
        )['d']
        ours = db.query_one(f'SELECT {db.day_sql(str(ts))} AS d')['d']
        # 两者可能相等（UTC 时区），但我们的口径必须跟随本地时间
        self.assertEqual(ours, time.strftime('%Y-%m-%d', time.localtime(ts)))
        self.assertIsInstance(naive_utc, str)

    # ── 不再重复计数 ────────────────────────────────────
    def test_backfill_after_bump_has_no_gap(self) -> None:
        """按 bump_usage 语义写入本地日后，回填应无缺口。"""
        ts = local_midnight_plus(30)
        self._seed(ts)
        self._daily(ts)
        res = db.backfill_usage_from_logs()
        self.assertEqual(res['repaired'], 0, '口径一致时不应产生回填')
        self.assertEqual(len(db.query('SELECT day FROM usage_daily')), 1)

    def test_backfill_does_not_create_second_day(self) -> None:
        ts = local_midnight_plus(30)
        self._seed(ts)
        db.backfill_usage_from_logs()
        days = [r['day'] for r in db.query('SELECT day FROM usage_daily')]
        self.assertEqual(days, [db.day_of(ts)])

    # ── 重建清理污染（不依赖机器时区）────────────────────
    def test_rebuild_removes_polluted_rows(self) -> None:
        """污染用一个「多出来的 day 行」模拟，任何时区都能构造。"""
        ts = local_midnight_plus(30)
        self._seed(ts)
        real_day = db.day_of(ts)
        # 造一个额外的（错误的）日行，模拟历史上被算到另一天
        polluting_day = '1999-01-01'
        for d in (real_day, polluting_day):
            db.execute(
                'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, completion_tokens) '
                'VALUES(?,?,?,?,?,?)',
                (d, 1, 'glm-5.2', 1, 100, 50),
            )
        before = db.query_one('SELECT SUM(requests) r FROM usage_daily')['r']
        self.assertEqual(before, 2, '应有两行（含一条污染）')

        res = db.rebuild_usage_from_logs()
        after = db.query_one('SELECT COUNT(*) c, SUM(requests) r FROM usage_daily')
        self.assertEqual(after['c'], 1, '重建后应只剩一行')
        self.assertEqual(after['r'], 1, '重建后请求数应回落到原始值 1')
        self.assertEqual(res['requests_delta'], -1)
        self.assertEqual(
            db.query_one('SELECT day FROM usage_daily')['day'], real_day
        )

    def test_rebuild_is_idempotent(self) -> None:
        ts = local_midnight_plus(30)
        self._seed(ts)
        db.rebuild_usage_from_logs()
        first = db.query_one('SELECT COUNT(*) c, SUM(requests) r FROM usage_daily')
        res2 = db.rebuild_usage_from_logs()
        second = db.query_one('SELECT COUNT(*) c, SUM(requests) r FROM usage_daily')
        self.assertEqual((first['c'], first['r']), (second['c'], second['r']))
        self.assertEqual(res2['requests_delta'], 0)

    def test_rebuild_keeps_multiple_days_separate(self) -> None:
        """不同两天的数据重建后仍应各占一行。"""
        now = int(time.time())
        for ts, pt in ((now, 100), (now - 86400, 200)):
            self._seed(ts, pt=pt)
        db.rebuild_usage_from_logs()
        rows = db.query('SELECT day, prompt_tokens FROM usage_daily ORDER BY day')
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['prompt_tokens'] for r in rows}, {100, 200})


if __name__ == '__main__':
    unittest.main()
