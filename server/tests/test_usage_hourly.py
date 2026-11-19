"""「今日」按小时聚合（usage_hourly）的回归测试。

背景：用量统计原来的趋势图**粒度固定按天**，范围选「今日」时只有一个数据点 ——
画出来就是一根孤零零的柱子，看不出「今天什么时候忙」。修法是让粒度跟着范围走
（今日→小时），并为此新增 `usage_hourly` 表。

为什么不直接在查询时聚合请求日志：日志会被保留期裁剪、也能被用户从「日志」页
清空，而页头卡片读的是 `usage_daily` —— 两处一旦不同源，清完日志就会出现
「卡片 338、图上一片空」的自相矛盾。所以小时维度与按天**同一写入路径、同一口径**。

本文件钉住的就是这条口径：
  1. 一次调用同时写两张表；
  2. **同一天按小时求和 == 按天那一行**（两种粒度不许漂移）——这是最要紧的一条；
  3. `/api/stats/hourly` 固定 24 个桶（补零），失败数按小时来自请求日志；
  4. 重建 / 回填两条路径都把小时表一起处理；
  5. 升级兜底：存量库升级后今天的小时表能自动补上（否则「今日」图是空的）。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        self._orig_auth = config.AUTH_DIR
        self._orig_users = config.USERS_FILE
        config.DB_PATH = Path(self._tmp.name) / 'm.db'
        config.AUTH_DIR = Path(self._tmp.name) / 'auths'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        from server import db
        db._conn = None
        db.connect()
        self.db = db

    def tearDown(self) -> None:
        if self.db._conn is not None:
            self.db._conn.close()
        self.db._conn = None
        config.DB_PATH = self._orig_db
        config.AUTH_DIR = self._orig_auth
        config.USERS_FILE = self._orig_users
        self._tmp.cleanup()

    def _log(self, ts: int, key_id: int = 1, model: str = 'glm-5.2',
             pt: int = 10, ct: int = 5, status: int = 200, realm: str | None = 'cn') -> None:
        self.db.add_request_log(ts=ts, key_id=key_id, model=model, status=status,
                                prompt_tokens=pt, completion_tokens=ct, realm=realm)


class HourlyWriteTest(_Case):
    def test_bump_usage_writes_both_granularities(self) -> None:
        self.db.bump_usage(1, 'glm-5.2', 100, 50, credit=0.5, realm='cn')
        daily = self.db.query_one('SELECT * FROM usage_daily')
        hourly = self.db.query_one('SELECT * FROM usage_hourly')
        self.assertIsNotNone(daily)
        self.assertIsNotNone(hourly, 'bump_usage 没有写小时表 —— 今日趋势图会是空的')
        self.assertEqual(int(hourly['prompt_tokens']), int(daily['prompt_tokens']))
        self.assertEqual(int(hourly['completion_tokens']), int(daily['completion_tokens']))
        self.assertEqual(float(hourly['credit']), float(daily['credit']))
        self.assertEqual(int(hourly['hour']), self.db.hour_of())
        self.assertEqual(str(hourly['day']), str(daily['day']))

    def test_hourly_sum_equals_daily_row(self) -> None:
        """最要紧的一条：同一天按小时求和 == 按天那一行。

        两种粒度分开维护，最容易出的错就是漂移（改了写路径只改一处、
        回填只补一边）。这条断言让任何一侧的漏写立刻现形。
        """
        for _ in range(3):
            self.db.bump_usage(1, 'glm-5.2', 100, 50, credit=0.25, realm='cn')
        self.db.bump_usage(2, 'global:claude-sonnet-4', 7, 3, credit=None, realm='global')
        day = self.db.day_of()
        d_rows = self.db.query(
            'SELECT key_id, model, realm, SUM(requests) AS r, SUM(prompt_tokens) AS pt, '
            'SUM(completion_tokens) AS ct, SUM(credit) AS cr FROM usage_daily '
            'WHERE day = ? GROUP BY key_id, model, realm', (day,))
        h_rows = self.db.query(
            'SELECT key_id, model, realm, SUM(requests) AS r, SUM(prompt_tokens) AS pt, '
            'SUM(completion_tokens) AS ct, SUM(credit) AS cr FROM usage_hourly '
            'WHERE day = ? GROUP BY key_id, model, realm', (day,))
        norm = lambda rows: sorted(  # noqa: E731
            (int(x['key_id']), str(x['model']), str(x['realm']), int(x['r']),
             int(x['pt']), int(x['ct']), round(float(x['cr']), 6)) for x in rows)
        self.assertEqual(norm(h_rows), norm(d_rows),
                         '按小时与按天的合计对不上 —— 两种粒度的写入/回填出现了漂移')


class HourlyRebuildTest(_Case):
    def test_rebuild_fills_hourly_from_logs(self) -> None:
        now = int(time.time())
        self._log(now - 3600, pt=10, ct=5)
        self._log(now - 60, key_id=2, pt=20, ct=7)
        out = self.db.rebuild_usage_from_logs()
        self.assertGreaterEqual(out['rows_after'], 1)
        rows = self.db.query('SELECT * FROM usage_hourly')
        self.assertTrue(rows, '重建没有回填小时表')
        total_p = sum(int(r['prompt_tokens']) for r in rows)
        self.assertEqual(total_p, 30)
        # 重建是「以日志为准」：再跑一次不该翻倍
        self.db.rebuild_usage_from_logs()
        self.assertEqual(sum(int(r['prompt_tokens'])
                             for r in self.db.query('SELECT * FROM usage_hourly')), 30)

    def test_backfill_is_idempotent_for_hourly(self) -> None:
        now = int(time.time())
        self._log(now - 120, pt=11, ct=4)
        first = self.db.backfill_usage_from_logs()
        self.assertEqual(first['repaired_hourly'], 1, first)
        again = self.db.backfill_usage_from_logs()
        self.assertEqual(again['repaired_hourly'], 0, '回填重复计数了')
        rows = self.db.query('SELECT * FROM usage_hourly')
        self.assertEqual(sum(int(r['prompt_tokens']) for r in rows), 11)

    def test_ensure_hourly_today_backfills_upgrade_gap(self) -> None:
        """存量库升级场景：小时表空着，但今天有请求日志 → 自动补上。"""
        now = int(time.time())
        self._log(now - 300, pt=42, ct=8)
        self.db.execute('DELETE FROM usage_hourly')      # 模拟「升级后这张表是空的」
        self.assertTrue(self.db.ensure_hourly_today(), '升级兜底没有回填')
        rows = self.db.query('SELECT * FROM usage_hourly')
        self.assertEqual(sum(int(r['prompt_tokens']) for r in rows), 42)
        # 已经补过了：再调用不该重复补
        self.assertFalse(self.db.ensure_hourly_today())

    def test_ensure_hourly_today_noop_without_logs(self) -> None:
        self.db.execute('DELETE FROM usage_hourly')
        self.assertFalse(self.db.ensure_hourly_today())
        self.assertEqual(self.db.query('SELECT * FROM usage_hourly'), [])


class HourlyEndpointTest(_Case):
    def setUp(self) -> None:
        super().setUp()
        from fastapi.testclient import TestClient
        from server import security
        from server.main import app
        security.save_users({'secret': 'S', 'users': [
            {'username': 'admin', 'role': 'admin',
             'pwd_hash': security.make_hash('p')}], 'api_keys': []})
        self.client = TestClient(app)
        assert self.client.post('/api/login',
                                json={'username': 'admin', 'password': 'p'}).status_code == 200

    def test_returns_24_zero_filled_buckets(self) -> None:
        r = self.client.get('/api/stats/hourly')
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(len(data), 24, '小时桶不是 24 个 —— X 轴会随数据抖动')
        self.assertEqual([d['hour'] for d in data], list(range(24)))
        self.assertTrue(all(d['requests'] == 0 and d['failed'] == 0 for d in data))

    def test_totals_match_the_daily_endpoint(self) -> None:
        """小时端点与按天端点必须给出同一份用量（同源同口径）。"""
        for _ in range(4):
            self.db.bump_usage(1, 'glm-5.2', 100, 50, credit=0.5, realm='cn')
        hourly = self.client.get('/api/stats/hourly').json()
        daily = self.client.get('/api/stats/daily', params={'days': 1}).json()
        self.assertEqual(sum(d['requests'] for d in hourly),
                         sum(d['requests'] for d in daily))
        self.assertEqual(sum(d['prompt_tokens'] + d['completion_tokens'] for d in hourly),
                         sum(d['prompt_tokens'] + d['completion_tokens'] for d in daily))

    def test_failures_come_from_request_logs_per_hour(self) -> None:
        """失败数按小时来自请求日志：用量表里根本没有被拒绝的调用。

        时间戳钉在**当前小时的开头 + 1/2 秒**，而不是「现在减一分钟」：整套
        测试跑到这里时若恰逢整点前后一分钟，`now - 60` 会落进**上一个小时**，
        而断言找的是「当前小时」的桶 —— 每小时边界上都可能偶发失败一次。
        钉进当前小时后，无论什么时候跑都落在同一个桶里。
        """
        day = time.strftime('%Y-%m-%d')
        hour = self.db.hour_of()
        start = self.db.hour_start_ts(day=day, hour=hour)
        self._log(start + 1, status=429, pt=0, ct=0)
        self._log(start + 2, status=500, pt=0, ct=0)
        data = self.client.get('/api/stats/hourly', params={'day': day}).json()
        bucket = next(d for d in data if d['hour'] == hour)
        self.assertEqual(bucket['failed'], 2, '失败数没有从请求日志按小时统计')
        self.assertEqual(bucket['requests'], 0, '失败的调用不该进用量表')

    def test_realm_filter(self) -> None:
        self.db.bump_usage(1, 'cn:glm-5.2', 10, 5, realm='cn')
        self.db.bump_usage(1, 'global:claude-sonnet-4', 20, 9, realm='global')
        cn = self.client.get('/api/stats/hourly', params={'realm': 'cn'}).json()
        gl = self.client.get('/api/stats/hourly', params={'realm': 'global'}).json()
        self.assertEqual(sum(d['prompt_tokens'] for d in cn), 10)
        self.assertEqual(sum(d['prompt_tokens'] for d in gl), 20)

    def test_bad_day_param_falls_back_to_today(self) -> None:
        """手改 URL 不该把趋势图变成红色错误态。"""
        for bad in ('2026-9-1', 'yesterday', '2026/09/01'):
            r = self.client.get('/api/stats/hourly', params={'day': bad})
            self.assertEqual(r.status_code, 200, bad)
            self.assertEqual(len(r.json()), 24)


if __name__ == '__main__':
    unittest.main()
