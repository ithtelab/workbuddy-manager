"""模型输出吞吐量（issue #214）。

「tok/s」很容易被算成好看但没意义的数，所以口径钉在这里：

  · 生成耗时 = latency − first_token（流式）；非流式没有中间过程，用整个 latency；
  · 只统计**成功**、有输出 token、耗时为正的记录 —— 失败请求的 latency 含重试与
    排队，算进去只会把速率压成噪声；
  · 数据源是 `request_logs`（`usage_daily` 没有耗时列），所以只覆盖日志保留期，
    返回值带样本量让界面如实标注。
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.routers import stats  # noqa: E402


def _log(model: str, status: int, ct: int, latency: int,
         first_token: int | None, realm: str = 'cn') -> None:
    db.execute(
        'INSERT INTO request_logs(ts,key_id,ip,model,mapped_model,status,'
        'prompt_tokens,completion_tokens,latency_ms,first_token_ms,ua,error,stream,realm) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (int(time.time()), None, '1.2.3.4', model, model, status, 10, ct,
         latency, first_token, 'UA', None, 1 if first_token is not None else 0, realm),
    )


class ThroughputTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 't.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        try:
            db._conn.close()  # type: ignore[union-attr]
        finally:
            db._conn = None
            config.DB_PATH = self._orig_db
            self._tmp.cleanup()

    def test_stream_and_non_stream_mix(self) -> None:
        # 流式：3000ms 总延迟 - 1000ms 首字 = 2000ms 生成 50 tokens → 25 tok/s
        _log('m1', 200, 50, 3000, 1000)
        # 非流式：没有首字，整个 2000ms 生成 100 tokens → 50 tok/s
        _log('m1', 200, 100, 2000, None)
        out = stats._throughput_by_model(1, None)
        got = out['m1']
        self.assertEqual(got['throughput_samples'], 2)
        # (50 + 100) tokens / 4.0 s = 37.5 tok/s
        self.assertAlmostEqual(got['tokens_per_sec'], 37.5, places=2)

    def test_failures_and_empty_outputs_are_excluded(self) -> None:
        _log('m2', 200, 40, 1000, None)              # ok：40 tok/s
        _log('m2', 500, 9999, 100, None)             # 失败：不参与
        _log('m2', 200, 0, 500, None)                # 无输出：不参与
        _log('m2', 200, 40, 0, None)                 # 耗时为 0：单调且会虚高 -> 不参与
        out = stats._throughput_by_model(1, None)
        self.assertAlmostEqual(out['m2']['tokens_per_sec'], 40.0, places=2)
        self.assertEqual(out['m2']['throughput_samples'], 1)

    def test_realm_filter(self) -> None:
        _log('cn-model', 200, 10, 1000, None, realm='cn')
        _log('gl-model', 200, 10, 1000, None, realm='global')
        only_cn = stats._throughput_by_model(1, 'cn')
        self.assertIn('cn-model', only_cn)
        self.assertNotIn('gl-model', only_cn)

    def test_by_model_merges_throughput(self) -> None:
        db.execute(
            'INSERT INTO usage_daily(day,model,realm,key_id,requests,prompt_tokens,'
            'completion_tokens,credit) VALUES(?,?,?,?,?,?,?,?)',
            (time.strftime('%Y-%m-%d'), 'm3', 'cn', '', 1, 10, 40, 0.5),
        )
        _log('m3', 200, 40, 1000, None)
        rows = stats.by_model(days=1, realm=None, user={'role': 'admin'})
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]['tokens_per_sec'], 40.0, places=2)
        self.assertEqual(rows[0]['throughput_samples'], 1)
        self.assertEqual(rows[0]['completion_tokens'], 40)

    def test_usage_model_without_logs_gets_null(self) -> None:
        """日志被清过 / 区间早于保留期：如实给 null（界面显示 —），不拿猜的数填。"""
        db.execute(
            'INSERT INTO usage_daily(day,model,realm,key_id,requests,prompt_tokens,'
            'completion_tokens,credit) VALUES(?,?,?,?,?,?,?,?)',
            (time.strftime('%Y-%m-%d'), 'old-model', 'cn', '', 5, 100, 200, 1.0),
        )
        rows = stats.by_model(days=30, realm=None, user={'role': 'admin'})
        self.assertIsNone(rows[0]['tokens_per_sec'])
        self.assertEqual(rows[0]['throughput_samples'], 0)


if __name__ == '__main__':
    unittest.main()
