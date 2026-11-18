"""实时指标端点：口径、桶对齐与上游不可达时的退化。

`/api/stats/realtime` 的三个数字各有口径，这里钉住容易算错的几处：

  · **并发峰值**来自区间重叠扫描（`[ts, ts + latency)`），不是「每分钟多少条」。
    首尾相接（A 在 t+2 结束、B 在 t+2 开始）算 1 个并发，不能算成 2；
  · **桶边界**对齐到 `step` 的整数倍。用 `now - window` 当起点的话，每次刷新窗口
    都会平移，同一批请求被切进不同的桶——曲线每轮都不一样；
  · **上游 `/status` 取不到**时不能整块返回失败：`rpm_*` / `peaks` / `series` 只
    依赖本库，上游挂了它们仍然有效，只有「在途 / 容量」变成未知。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.routers import stats  # noqa: E402
from server.services import wb2api  # noqa: E402


def _log(ts: int, latency_ms: int, status: int = 200, model: str = 'm1') -> None:
    db.execute(
        'INSERT INTO request_logs(ts,key_id,ip,model,mapped_model,status,'
        'prompt_tokens,completion_tokens,latency_ms,first_token_ms,ua,error,stream,realm) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (ts, None, '1.2.3.4', model, model, status, 10, 10,
         latency_ms, None, 'UA', None, 0, 'cn'),
    )


def _run(coro):
    return asyncio.run(coro)


class _DbCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 't.db'
        db._conn = None
        db.connect()
        stats._PEAK_CACHE.clear()

    def tearDown(self) -> None:
        try:
            db._conn.close()  # type: ignore[union-attr]
        finally:
            db._conn = None
            config.DB_PATH = self._orig_db
            stats._PEAK_CACHE.clear()
            self._tmp.cleanup()


class PeaksTest(_DbCase):
    def test_overlapping_requests_peak(self) -> None:
        now = int(time.time())
        _log(now - 30, 10_000)   # [-30s, -20s)
        _log(now - 25, 10_000)   # [-25s, -15s) → 与上一条重叠，峰值 2
        _log(now - 5, 1_000)     # 单独，不抬高峰值
        peaks = stats._peaks(1)
        self.assertEqual(peaks['concurrency'], 2)
        self.assertEqual(peaks['samples'], 3)

    def test_adjacent_requests_are_not_concurrent(self) -> None:
        # 半开区间：A 占 [t, t+10)、B 占 [t+10, t+20) —— 只算 1 个并发
        now = int(time.time())
        _log(now - 30, 10_000)
        _log(now - 20, 10_000)
        self.assertEqual(stats._peaks(1)['concurrency'], 1)

    def test_rpm_peak_is_per_minute_max(self) -> None:
        now = int(time.time())
        base = now - (now % 60)          # 当前这一分钟
        for i in range(5):
            _log(base + i * 10 + 1, 100)  # 同一分钟内、彼此不重叠
        _log(now - 120, 100)              # 落在另一分钟
        peaks = stats._peaks(1)
        self.assertEqual(peaks['rpm'], 5, 'RPM 峰值应当是「某一分钟最多几条」')
        self.assertEqual(peaks['concurrency'], 1)

    def test_cached_for_60_seconds(self) -> None:
        now = int(time.time())
        _log(now - 10, 100)
        first = stats._peaks(1)
        _log(now - 5, 100)   # 再加一条，但还在缓存窗口内
        self.assertEqual(stats._peaks(1), first)
        # 让缓存过期后应当重算
        stats._PEAK_CACHE[1] = (time.time() - 61, first)
        self.assertEqual(stats._peaks(1)['samples'], 2)


class SeriesTest(_DbCase):
    def test_buckets_align_to_absolute_grid(self) -> None:
        out = stats._series()
        self.assertEqual(out['step'], 15)
        self.assertEqual(out['window'], 180)
        self.assertEqual(out['start'] % 15, 0, '桶边界没对齐到 step 的整数倍')
        self.assertEqual(out['end'] - out['start'], 180)
        self.assertEqual(len(out['rpm']), 12)
        self.assertEqual(len(out['concurrency']), 12)

    def test_request_lands_in_expected_bucket(self) -> None:
        out = stats._series()
        start = out['start']
        _log(start + 7, 100)
        _log(start + 15 * 5 + 3, 100)
        out = stats._series()
        self.assertEqual(out['rpm'][0], 1)
        self.assertEqual(out['rpm'][5], 1)
        self.assertEqual(sum(out['rpm']), 2)

    def test_long_request_spans_concurrency_buckets(self) -> None:
        out = stats._series()
        start = out['start']
        _log(start + 1, 46_000)   # 覆盖 bucket 0..3
        out = stats._series()
        for i in range(4):
            self.assertGreaterEqual(out['concurrency'][i], 1, f'bucket {i} 没算到并发')
        self.assertEqual(out['concurrency'][4], 0)
        # RPM 只在开始的那个桶里计一次
        self.assertEqual(sum(out['rpm']), 1)

    def test_empty_window_is_all_zero(self) -> None:
        out = stats._series()
        self.assertEqual(set(out['rpm']), {0})
        self.assertEqual(set(out['concurrency']), {0})


class RealtimeEndpointTest(_DbCase):
    def _upstream(self, **overrides) -> dict:
        payload = {
            'connected': True,
            'accounts': [{'in_flight': 1}, {'in_flight': 0}, {'in_flight': 2}],
            'in_flight_full': 0,
            'healthy': 3,
            'sticky_sessions': 4,
        }
        payload.update(overrides)
        return payload

    def _call(self, payload: dict | None) -> dict:
        status = payload if payload is not None else {'connected': False,
                                                     'error': 'connect failed'}
        with mock.patch.object(wb2api, 'get_status',
                               new=mock.AsyncMock(return_value=status)):
            return _run(stats.realtime(days=1, user={'role': 'admin'}))

    def test_aggregates_inflight_and_capacity(self) -> None:
        cfg = Path(self._tmp.name) / 'config.json'
        cfg.write_text(json.dumps({'pool': {'max_in_flight': 3}}), encoding='utf-8')
        with mock.patch.object(config, 'UPSTREAM_CONFIG', cfg):
            out = self._call(self._upstream())
        self.assertTrue(out['available'])
        self.assertEqual(out['inflight'], 3)       # 1 + 0 + 2
        self.assertEqual(out['busy_accounts'], 2)
        self.assertEqual(out['accounts_total'], 3)
        self.assertEqual(out['per_account_limit'], 3)
        self.assertEqual(out['capacity'], 9)
        self.assertEqual(out['healthy'], 3)
        self.assertEqual(out['sticky'], 4)
        self.assertNotIn('error', out)

    def test_per_account_limit_falls_back_to_2(self) -> None:
        with mock.patch.object(config, 'UPSTREAM_CONFIG',
                               Path(self._tmp.name) / 'missing.json'):
            self.assertEqual(stats._per_account_limit(), 2)

    def test_rpm_windows(self) -> None:
        now = int(time.time())
        for _ in range(3):
            _log(now - 30, 100)     # 60 秒内 3 条
        _log(now - 200, 100)        # 3 分钟内、但在 60 秒外
        out = self._call(self._upstream())
        self.assertEqual(out['rpm_60'], 3)
        self.assertEqual(out['rpm_3m'], 1.0)      # 180 秒窗口：3 / 3（-200 秒那条不算）
        self.assertEqual(out['rpm_5m'], 0.8)      # 4 / 5

    def test_upstream_down_keeps_local_metrics(self) -> None:
        now = int(time.time())
        _log(now - 10, 100)
        out = self._call(None)
        self.assertFalse(out['available'])
        self.assertIn('error', out)
        self.assertNotIn('capacity', out)
        # 本地口径的数据必须还在：上游挂掉时用户正是靠它判断「刚才有没有断」
        self.assertEqual(out['rpm_60'], 1)
        self.assertGreaterEqual(out['peaks']['samples'], 1)
        self.assertEqual(len(out['series']['concurrency']), 12)

    def test_days_is_clamped(self) -> None:
        self.assertEqual(self._call(self._upstream())['days'], 1)
        with mock.patch.object(wb2api, 'get_status',
                               new=mock.AsyncMock(return_value=self._upstream())):
            self.assertEqual(_run(stats.realtime(days=9999,
                                                 user={'role': 'admin'}))['days'], 90)
            self.assertEqual(_run(stats.realtime(days=0,
                                                 user={'role': 'admin'}))['days'], 1)


if __name__ == '__main__':
    unittest.main()
