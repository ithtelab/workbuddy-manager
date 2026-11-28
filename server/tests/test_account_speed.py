"""按实测吞吐给账号降权（慢号自动退出轮换）。

口径与统计页 issue #214 一致：`gen_ms = latency_ms - COALESCE(first_token_ms, 0)`，
`rate = completion_tokens / (gen_ms / 1000)`；只统计流式 + 200 +
`completion_tokens >= 300` + 生成耗时为正的样本。

这里钉住的：
  · 用生成耗时而不是总耗时：首字慢 ≠ 吐字慢（issue #494 的根因）；
  · 相对基准（池内中位数）而不是绝对值：晚高峰整体变慢时不能全池降权；
  · 样本不足（<3）不下结论，标 `insufficient` 而不是当慢号；
  · 权重是**乘性**降权且有下限，绝不把本来就低于下限的号「提」上来；
  · 降权有有效期，到期/回升后恢复原始权重；
  · uid8 碰撞、realm 混池都不静默评估；
  · `dry_run` 默认只回显，不碰账号文件。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import db  # noqa: E402
from server.services import speedhealth as S  # noqa: E402


def _row(account: str, ct: int, lat_ms: int, first_ms: int | None = None) -> dict:
    return {'account': account, 'completion_tokens': ct,
            'latency_ms': lat_ms, 'first_token_ms': first_ms}


class UidTest(unittest.TestCase):
    def test_parse_parenthesized(self) -> None:
        self.assertEqual(S.uid_of('Echo(248f8d4f)'), '248f8d4f')
        self.assertEqual(S.uid_of('130****3363(f3f3e0a2)'), 'f3f3e0a2')

    def test_uppercase_hex(self) -> None:
        self.assertEqual(S.uid_of('Echo(248F8D4F)'), '248f8d4f')
        self.assertEqual(S.uid_of('UID=248F8D4F'), '248f8d4f')

    def test_bare_uid8(self) -> None:
        self.assertEqual(S.uid_of('248f8d4f'), '248f8d4f')
        self.assertEqual(S.uid_of('  248F8D4F  '), '248f8d4f')
        self.assertEqual(S.uid_of('uid=248f8d4f'), '248f8d4f')

    def test_invalid(self) -> None:
        self.assertIsNone(S.uid_of('没有括号'))
        self.assertIsNone(S.uid_of('248f8d4'))        # 7 位
        self.assertIsNone(S.uid_of('248f8d4f0'))      # 9 位
        self.assertIsNone(S.uid_of(None))


class CollectTest(unittest.TestCase):
    def test_median_per_account(self) -> None:
        rows = [
            _row('A(11112222)', 1000, 10000),
            _row('A(11112222)', 3000, 10000),
            _row('A(11112222)', 500, 10000),
            _row('B(33334444)', 900, 10000),
        ]
        with mock.patch.object(db, 'query', return_value=rows):
            stats = S.collect(900, now=1000)
        self.assertEqual(len(stats), 2)
        first = stats[0]
        self.assertEqual(first['account'], 'A(11112222)')
        self.assertEqual(first['uid'], '11112222')
        self.assertEqual(first['rate'], 100.0)   # median(100,300,50)
        self.assertEqual(first['samples'], 3)

    def test_uses_generation_time(self) -> None:
        # 总耗时 10s、首字 2s → 生成耗时 8s，速率 125 而不是 100
        rows = [_row('A(11112222)', 1000, 10000, 2000)]
        with mock.patch.object(db, 'query', return_value=rows):
            stats = S.collect(900, now=1000)
        self.assertEqual(stats[0]['rate'], 125.0)

    def test_null_first_token_falls_back_to_full_latency(self) -> None:
        rows = [_row('A(11112222)', 1000, 10000, None)]
        with mock.patch.object(db, 'query', return_value=rows):
            stats = S.collect(900, now=1000)
        self.assertEqual(stats[0]['rate'], 100.0)

    def test_skips_non_positive_generation_time(self) -> None:
        for ft in (10000, 12000):
            with mock.patch.object(db, 'query',
                                   return_value=[_row('A(11112222)', 1000, 10000, ft)]):
                self.assertEqual(S.collect(900, now=1000), [])

    def test_skips_short_requests(self) -> None:
        rows = [_row('A(11112222)', S.MIN_TOKENS - 1, 10000, 1000)]
        with mock.patch.object(db, 'query', return_value=rows):
            self.assertEqual(S.collect(900, now=1000), [])

    def test_query_uses_authoritative_formula_and_realm(self) -> None:
        with mock.patch.object(db, 'query', return_value=[]) as query:
            S.collect(900, realm='global', now=1000)
        sql, args = query.call_args[0]
        self.assertIn('latency_ms > COALESCE(first_token_ms, 0)', sql)
        self.assertIn('first_token_ms', sql)
        self.assertIn("COALESCE(NULLIF(realm, ''), 'cn') = ?", sql)
        self.assertEqual(args[-1], 'global')


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
        # global_slow 已删除：相对基准下它数学上恒假，不能留下名不副实的字段。
        self.assertNotIn('global_slow', ev)

    def test_insufficient_samples(self) -> None:
        stats = [
            {'account': 'a', 'uid': 'a', 'samples': 2, 'rate': 10.0},
            {'account': 'b', 'uid': 'b', 'samples': 9, 'rate': 100.0},
            {'account': 'c', 'uid': 'c', 'samples': 9, 'rate': 120.0},
        ]
        ev = S.evaluate(stats)
        actions = {r['account']: r['action'] for r in ev['rows']}
        self.assertEqual(actions['a'], 'insufficient')
        self.assertEqual(actions['b'], 'ok')

    def test_relative_baseline_spares_pool_when_all_slow(self) -> None:
        # 过半账号偏慢时中位数本身被拖低，没人跌破 60% —— 相对基准天然不处置。
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
            self.assertEqual(ch['from_raw'], 0)   # 未设置 → 恢复时写 0（清除）

    def test_already_below_floor_not_raised(self) -> None:
        stats = self._stats(('aaa1', 140.0), ('slow4', 20.0))
        plan = S.plan(stats, current_weight={'slow4': 0.05})
        for ch in plan['changes']:
            self.assertLessEqual(ch['to'], 0.05)

    def test_change_carries_raw_weight(self) -> None:
        plan = S.plan(self._stats(('aaa1', 140.0), ('slow4', 20.0)),
                      current_weight={'slow4': 2.0})
        change = plan['changes'][0]
        self.assertEqual(change['from'], 2.0)
        self.assertEqual(change['from_raw'], 2.0)
        self.assertAlmostEqual(change['to'], 0.3)


class IndexAccountsTest(unittest.TestCase):
    def test_collision_skips_both(self) -> None:
        accounts = [
            {'uid': 'aaaaaaaa1111', 'file': 'a.json', 'realm': 'cn', 'weight': 0},
            {'uid': 'aaaaaaaa2222', 'file': 'b.json', 'realm': 'cn', 'weight': 0},
        ]
        collisions: list[dict] = []
        with mock.patch.object(S.wb2api, 'list_auth_accounts', return_value=accounts):
            index = S.index_accounts(Path('.'), collisions=collisions)
        self.assertEqual(index, {})
        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0]['prefix'], 'aaaaaaaa')
        self.assertEqual(len(collisions[0]['uids']), 2)

    def test_same_uid_two_files_is_not_collision(self) -> None:
        accounts = [
            {'uid': 'abcd1234ffff', 'file': 'a.json', 'realm': 'cn', 'weight': 0},
            {'uid': 'abcd1234ffff', 'file': 'a.json.disabled', 'realm': 'cn', 'weight': 0},
        ]
        collisions: list[dict] = []
        with mock.patch.object(S.wb2api, 'list_auth_accounts', return_value=accounts):
            index = S.index_accounts(Path('.'), collisions=collisions)
        self.assertEqual(list(index), ['abcd1234'])
        self.assertEqual(index['abcd1234']['file'], 'a.json')
        self.assertEqual(collisions, [])


class RealmScopeTest(unittest.TestCase):
    def _index(self) -> dict:
        return {
            'aaaaaaaa': {'uid': 'aaaaaaaa1111', 'file': 'a.json',
                         'realm': 'cn', 'weight': 0},
            'bbbbbbbb': {'uid': 'bbbbbbbb2222', 'file': 'b.json',
                         'realm': 'global', 'weight': 0},
        }

    def test_mixed_group_requires_explicit_realm(self) -> None:
        with mock.patch.object(S, 'index_accounts', return_value=self._index()):
            with self.assertRaises(ValueError):
                S.evaluate_for_dir(Path('.'), 900)

    def test_realm_is_passed_to_collect_and_index(self) -> None:
        with mock.patch.object(S, 'index_accounts', return_value=self._index()), \
                mock.patch.object(S, 'collect', return_value=[]) as collect, \
                mock.patch.object(S, 'demotions', return_value={}):
            out = S.evaluate_for_dir(Path('.'), 900, realm='cn', now=1000)
        self.assertEqual(out['realm'], 'cn')
        self.assertEqual(collect.call_args.kwargs['realm'], 'cn')
        self.assertEqual({c['uid'] for c in out['changes']}, set())
        self.assertEqual(out['collisions'], [])

    def test_accounts_outside_the_group_do_not_pollute_the_pool(self) -> None:
        index = {
            'aaaaaaaa': {'uid': 'aaaaaaaa', 'file': 'a.json',
                         'realm': 'cn', 'weight': 0},
            'bbbbbbbb': {'uid': 'bbbbbbbb', 'file': 'b.json',
                         'realm': 'cn', 'weight': 0},
        }
        stats = [
            {'account': 'A(aaaaaaaa)', 'uid': 'aaaaaaaa', 'samples': 5, 'rate': 100.0},
            {'account': 'B(bbbbbbbb)', 'uid': 'bbbbbbbb', 'samples': 5, 'rate': 90.0},
            # 已移走/已禁用账号的残留日志：不能拉低本池中位数
            {'account': 'X(deadbeef)', 'uid': 'deadbeef', 'samples': 5, 'rate': 1.0},
        ]
        with mock.patch.object(S, 'index_accounts', return_value=index), \
                mock.patch.object(S, 'collect', return_value=stats), \
                mock.patch.object(S, 'demotions', return_value={}):
            out = S.evaluate_for_dir(Path('.'), 900, realm='cn', now=1000)
        self.assertEqual(out['pool_median'], 95.0)
        self.assertEqual({r['uid'] for r in out['rows']}, {'aaaaaaaa', 'bbbbbbbb'})


class DemotionRecoveryTest(unittest.TestCase):
    """降权记录：原始权重、到期恢复、HIGH_SCORE 提前恢复、人工接管不覆盖。"""

    def setUp(self) -> None:
        self.stored: dict = {}
        patch = mock.patch.object(S.db, 'get_setting',
                                  side_effect=lambda key, default=None:
                                  self.stored.get(key, default))
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(S.db, 'set_setting',
                                  side_effect=lambda key, value:
                                  self.stored.__setitem__(key, value))
        patch.start()
        self.addCleanup(patch.stop)

    def _seed(self, *, expires_at: int, original: float = 2.0) -> None:
        self.stored[S.DEMOTIONS_KEY] = {
            S._group_key(Path('auth')): {
                'u1': {'uid': 'u1', 'file': 'f.json',
                       'original_weight': original, 'demoted_to': 0.3,
                       'applied_at': 0, 'expires_at': expires_at},
            },
        }

    def test_apply_records_original_weight_and_expiry(self) -> None:
        change = {'uid': 'u1', 'file': 'f.json', 'from': 2.0,
                  'from_raw': 2.0, 'to': 0.3, 'samples': 5, 'score': 0.4}
        with mock.patch.object(S.wb2api, 'set_account_weight') as write:
            out = S.apply([change], Path('auth'), dry_run=False, now=1000)
        write.assert_called_once_with('f.json', 0.3, Path('auth'))
        self.assertTrue(out[0]['ok'])
        record = S.demotions(Path('auth'))['u1']
        self.assertEqual(record['original_weight'], 2.0)
        self.assertEqual(record['demoted_to'], 0.3)
        self.assertEqual(record['expires_at'], 1000 + S.RESTORE_SECONDS)

    def test_expired_restores_original_weight(self) -> None:
        self._seed(expires_at=500)
        index = {'u1': {'uid': 'u1', 'file': 'f.json', 'weight': 0.3}}
        restores, stale = S._pending_restores(Path('auth'), index, [], now=1000)
        self.assertEqual(stale, [])
        self.assertEqual(restores[0]['reason'], 'expired')
        with mock.patch.object(S.wb2api, 'set_account_weight') as write:
            out = S.restore(restores, Path('auth'), dry_run=False)
        write.assert_called_once_with('f.json', 2.0, Path('auth'))
        self.assertTrue(out[0]['ok'])
        self.assertEqual(S.demotions(Path('auth')), {})

    def test_zero_original_clears_weight(self) -> None:
        self._seed(expires_at=500, original=0.0)
        index = {'u1': {'uid': 'u1', 'file': 'f.json', 'weight': 0.3}}
        restores, _ = S._pending_restores(Path('auth'), index, [], now=1000)
        with mock.patch.object(S.wb2api, 'set_account_weight') as write:
            S.restore(restores, Path('auth'), dry_run=False)
        write.assert_called_once_with('f.json', 0.0, Path('auth'))

    def test_high_score_recovers_before_expiry(self) -> None:
        self._seed(expires_at=99999)
        index = {'u1': {'uid': 'u1', 'file': 'f.json', 'weight': 0.3}}
        rows = [{'uid': 'u1', 'samples': S.MIN_SAMPLES, 'score': S.HIGH_SCORE}]
        restores, stale = S._pending_restores(Path('auth'), index, rows, now=1000)
        self.assertEqual(stale, [])
        self.assertEqual(restores[0]['reason'], 'recovered')

    def test_low_score_keeps_demotion(self) -> None:
        self._seed(expires_at=99999)
        index = {'u1': {'uid': 'u1', 'file': 'f.json', 'weight': 0.3}}
        rows = [{'uid': 'u1', 'samples': S.MIN_SAMPLES, 'score': 0.4}]
        restores, _ = S._pending_restores(Path('auth'), index, rows, now=1000)
        self.assertEqual(restores, [])

    def test_manual_weight_change_is_not_overwritten(self) -> None:
        self._seed(expires_at=500)
        index = {'u1': {'uid': 'u1', 'file': 'f.json', 'weight': 0.8}}
        restores, stale = S._pending_restores(Path('auth'), index, [], now=1000)
        self.assertEqual(restores, [])
        self.assertEqual(stale, ['u1'])

    def test_dry_run_restore_does_not_write_or_clear(self) -> None:
        self._seed(expires_at=500)
        index = {'u1': {'uid': 'u1', 'file': 'f.json', 'weight': 0.3}}
        restores, _ = S._pending_restores(Path('auth'), index, [], now=1000)
        with mock.patch.object(S.wb2api, 'set_account_weight') as write:
            out = S.restore(restores, Path('auth'), dry_run=True)
        write.assert_not_called()
        self.assertTrue(out[0]['dry_run'])
        self.assertIn('u1', S.demotions(Path('auth')))


class ApplyTest(unittest.TestCase):
    def test_dry_run_does_not_write(self) -> None:
        changes = [{'uid': 'u1', 'file': 'workbuddy-u1.json', 'from': 1.0, 'to': 0.15}]
        with mock.patch.object(S.wb2api, 'set_account_weight') as m:
            out = S.apply(changes, Path('.'), dry_run=True)
        m.assert_not_called()
        self.assertTrue(out[0]['ok'])
        self.assertTrue(out[0]['dry_run'])

    def test_apply_writes_expected_weight(self) -> None:
        changes = [{'uid': 'u1', 'file': 'workbuddy-u1.json',
                    'from': 1.0, 'from_raw': 0, 'to': 0.15}]
        with mock.patch.object(S.wb2api, 'set_account_weight') as m, \
                mock.patch.object(S, '_load_demotions', return_value={}), \
                mock.patch.object(S, '_save_demotions'):
            out = S.apply(changes, Path('.'), dry_run=False, now=1)
        m.assert_called_once()
        args = m.call_args[0]
        self.assertEqual(args[0], 'workbuddy-u1.json')
        self.assertAlmostEqual(args[1], 0.15)
        self.assertTrue(out[0]['ok'])

    def test_missing_file_is_reported_not_raised(self) -> None:
        out = S.apply([{'uid': 'u1', 'from': 1.0, 'to': 0.15}], Path('.'), dry_run=False)
        self.assertFalse(out[0]['ok'])
        self.assertIn('账号文件', out[0]['error'])


class MixedRealmDemotionTest(unittest.TestCase):
    """混合 realm 分组：评估 cn 时不能把 global 的有效降权记录判成失效。

    安全审查 2026-11-28：`_evaluate_with_index` 先把 index 过滤成单 realm，
    再交给 `_pending_restores`，另一个 realm 的账号必然「不在 index 里」→
    stale → 被 prune 删除，权重永久停在下限。
    """

    def test_other_realm_record_is_not_stale(self) -> None:
        auth_dir = Path('auth')
        index = {
            'aaaa1111': {'uid': 'aaaa1111', 'file': 'workbuddy-a.json',
                         'realm': 'cn', 'weight': 0.15},
            'bbbb2222': {'uid': 'bbbb2222', 'file': 'workbuddy-b.json',
                         'realm': 'global', 'weight': 0.15},
        }
        record = {'uid': 'bbbb2222', 'file': 'workbuddy-b.json',
                  'original_weight': 0, 'demoted_to': 0.15,
                  'expires_at': 10_000_000, 'applied_at': 1}
        with mock.patch.object(S, 'collect', return_value=[]), \
                mock.patch.object(S, 'demotions', return_value={'bbbb2222': record}):
            result = S._evaluate_with_index(auth_dir, 900, 'cn', index, [], now=1000)
        self.assertEqual(result['stale_demotions'], [],
                         'global 的记录不能在 cn 评估时被判失效')
        self.assertEqual(result['restores'], [], '未到期的记录也不该提前恢复')

    def test_apply_sync_does_not_require_reason_key(self) -> None:
        """`evaluate_group` 的真实返回没有 `reason` 键；`_apply_sync` 必须能跑完。

        旧代码直接 `result['reason']` → KeyError：手动 apply 与自动调度都会 500，
        而单测用 mock 塞了 reason，所以一直绿。
        """
        realm_result = {
            'realm': 'cn', 'pool_median': None, 'rows': [], 'changes': [],
            'restores': [], 'stale_demotions': [], 'collisions': [],
            'note': '样本不足（可用账号少于 2 个）',
        }
        grouped = {'conflicts': [], 'realms': [realm_result]}
        with mock.patch.object(S, 'evaluate_group', return_value=grouped), \
                mock.patch.object(S, 'prune_demotions'):
            out = S._apply_sync(Path('auth'), 900, True, None)
        self.assertEqual(out['realms'][0]['reason'], '样本不足（可用账号少于 2 个）')
        self.assertEqual(out['realms'][0]['note'], '样本不足（可用账号少于 2 个）')


if __name__ == '__main__':
    unittest.main()
