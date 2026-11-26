"""按实测吞吐给账号降权（慢号自动退出轮换）。

口径与上游日志一致：`rate = completion_tokens / (latency_ms / 1000)`，只统计
流式 + 200 + `completion_tokens >= 300`。低于 300 时首帧占比过大，会把快号
误判成慢号——这正是 issue #494 里「首字慢 ≠ 吐字慢」的同一个坑。

这里钉住的：
  · 相对基准（池内中位数）而不是绝对值：晚高峰整体变慢时不能全池降权；
  · 样本不足（<3）不下结论，标 `insufficient` 而不是当慢号；
  · 池内过半偏慢 → 判全局，不处置（否则会把整个池子打散）；
  · 权重是**乘性**降权且有下限，绝不把本来就低于下限的号「提」上来；
  · `dry_run` 默认只回显，不碰账号文件。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import db  # noqa: E402
from server.services import speedhealth as S  # noqa: E402


def _row(account: str, ct: int, lat_ms: int) -> dict:
    return {'account': account, 'completion_tokens': ct, 'latency_ms': lat_ms}


class UidTest(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(S.uid_of('Echo(248f8d4f)'), '248f8d4f')
        self.assertEqual(S.uid_of('130****3363(f3f3e0a2)'), 'f3f3e0a2')
        self.assertIsNone(S.uid_of('没有括号'))
        self.assertIsNone(S.uid_of(None))


class CollectTest(unittest.TestCase):
    def test_median_per_account(self) -> None:
        rows = [
            _row('A(11112222)', 1000, 10000),   # 100 tok/s
            _row('A(11112222)', 3000, 10000),   # 300
            _row('A(11112222)', 500, 10000),    # 50
            _row('B(33334444)', 900, 10000),    # 90
        ]
        with mock.patch.object(db, 'query', return_value=rows):
            stats = S.collect(900, now=1000)
        self.assertEqual(len(stats), 2)
        first = stats[0]
        self.assertEqual(first['account'], 'A(11112222)')
        self.assertEqual(first['uid'], '11112222')
        self.assertEqual(first['rate'], 100.0)   # median(100,300,50)
        self.assertEqual(first['samples'], 3)

    def test_skips_zero_latency(self) -> None:
        rows = [_row('A(11112222)', 1000, 0)]
        with mock.patch.object(db, 'query', return_value=rows):
            self.assertEqual(S.collect(900, now=1000), [])


class EvaluateTest(unittest.TestCase):
    def _stats(self, *pairs):
        return [{'account': a, 'uid': a[:8], 'samples': 5, 'rate': r} for a, r in pairs]

    def test_slow_and_ok(self) -> None:
        ev = S.evaluate(self._stats(
            ('fast0001', 140.0), ('fast0002', 130.0),
            ('slow0003', 45.0), ('slow0004', 40.0),
        ))
        self.assertEqual(ev['pool_median'], 87.5)
        actions = {r['account']: r['action'] for r in ev['rows']}
        self.assertEqual(actions['fast0001'], 'ok')
        self.assertEqual(actions['slow0004'], 'slow')
        self.assertFalse(ev['global_slow'])

    def test_insufficient_samples(self) -> None:
        # 至少要有 2 个可用账号才出结论；样本不足的那个标 insufficient，不当慢号
        stats = [
            {'account': 'a', 'uid': 'a', 'samples': 2, 'rate': 10.0},
            {'account': 'b', 'uid': 'b', 'samples': 9, 'rate': 100.0},
            {'account': 'c', 'uid': 'c', 'samples': 9, 'rate': 120.0},
        ]
        ev = S.evaluate(stats)
        actions = {r['account']: r['action'] for r in ev['rows']}
        self.assertEqual(actions['a'], 'insufficient')
        self.assertEqual(actions['b'], 'ok')

    def test_majority_slow_changes_nothing(self) -> None:
        # 过半账号偏慢时中位数本身就被拖低，没人跌破 60% —— 结果正好是「不处置」，
        # 这层保护靠相对基准天然成立，不需要真去改权重。
        stats = self._stats(
            ('a1', 100.0), ('b2', 40.0), ('c3', 38.0), ('d4', 36.0),
        )
        plan = S.plan(stats)
        self.assertEqual(plan['changes'], [])

    def test_two_of_four_slow_degrades_only_them(self) -> None:
        stats = self._stats(
            ('a1', 140.0), ('b2', 130.0), ('c3', 45.0), ('d4', 40.0),
        )
        plan = S.plan(stats)
        self.assertEqual({c['uid'] for c in plan['changes']}, {'c3', 'd4'})

    def test_too_few_accounts(self) -> None:
        ev = S.evaluate(self._stats(('a1', 100.0)))
        self.assertEqual(ev['rows'], [])


class PlanTest(unittest.TestCase):
    def _stats(self, *pairs):
        return [{'account': a, 'uid': a, 'samples': 5, 'rate': r} for a, r in pairs]

    def test_weight_floor_and_cap(self) -> None:
        stats = self._stats(
            ('aaa1', 140.0), ('bbb2', 130.0), ('ccc3', 120.0),
            ('slow4', 40.0), ('slow5', 35.0), ('slow6', 30.0), ('slow7', 25.0),
        )
        plan = S.plan(stats, current_weight={})
        self.assertLessEqual(len(plan['changes']), S.MAX_CHANGES)
        for ch in plan['changes']:
            self.assertGreaterEqual(ch['to'], S.FLOOR)
            self.assertLess(ch['to'], ch['from'])

    def test_already_below_floor_not_raised(self) -> None:
        stats = self._stats(('aaa1', 140.0), ('slow4', 20.0))
        plan = S.plan(stats, current_weight={'slow4': 0.05})
        for ch in plan['changes']:
            self.assertLessEqual(ch['to'], 0.05)


class ApplyTest(unittest.TestCase):
    def test_dry_run_does_not_write(self) -> None:
        changes = [{'uid': 'u1', 'file': 'workbuddy-u1.json', 'from': 1.0, 'to': 0.15}]
        with mock.patch.object(S.wb2api, 'set_account_weight') as m:
            out = S.apply(changes, Path('.'), dry_run=True)
        m.assert_not_called()
        self.assertTrue(out[0]['ok'])
        self.assertTrue(out[0]['dry_run'])

    def test_apply_writes_expected_weight(self) -> None:
        changes = [{'uid': 'u1', 'file': 'workbuddy-u1.json', 'from': 1.0, 'to': 0.15}]
        with mock.patch.object(S.wb2api, 'set_account_weight') as m:
            out = S.apply(changes, Path('.'), dry_run=False)
        m.assert_called_once()
        args = m.call_args[0]
        self.assertEqual(args[0], 'workbuddy-u1.json')
        self.assertAlmostEqual(args[1], 0.15)
        self.assertTrue(out[0]['ok'])

    def test_missing_file_is_reported_not_raised(self) -> None:
        out = S.apply([{'uid': 'u1', 'from': 1.0, 'to': 0.15}], Path('.'), dry_run=False)
        self.assertFalse(out[0]['ok'])
        self.assertIn('账号文件', out[0]['error'])


if __name__ == '__main__':
    unittest.main()
