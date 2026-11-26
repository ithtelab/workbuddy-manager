"""调度器与设置：默认关闭、坏值回落、预演不落盘、互斥与多分组隔离。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import accounts  # noqa: E402
from server.services import speedhealth as S  # noqa: E402


def _entry(name: str, realms: set[str]) -> dict:
    return {'group': {'name': name, 'enabled': True},
            'dir': Path('.'), 'realms': realms, 'collisions': []}


class SettingsTest(unittest.TestCase):
    def test_defaults_are_off(self) -> None:
        s = S.normalize_settings(None)
        self.assertFalse(s['enabled'])
        self.assertTrue(s['dry_run'])
        self.assertEqual(s['interval_minutes'], 15)
        self.assertEqual(s['window_minutes'], 15)

    def test_bad_values_fall_back(self) -> None:
        s = S.normalize_settings({
            'enabled': True, 'interval_minutes': 'abc',
            'window_minutes': 10 ** 9, 'dry_run': 'off',
        })
        self.assertTrue(s['enabled'])
        self.assertEqual(s['interval_minutes'], 15)      # 坏值回默认
        self.assertEqual(s['window_minutes'], S._MAX_INTERVAL)  # 夹到上限
        self.assertFalse(s['dry_run'])
        self.assertGreaterEqual(s['interval_minutes'], S._MIN_INTERVAL)

    def test_string_booleans(self) -> None:
        self.assertTrue(S.normalize_settings({'enabled': 'true'})['enabled'])
        self.assertFalse(S.normalize_settings({'enabled': '0'})['enabled'])

    def test_save_merges(self) -> None:
        stored = {}

        def fake_get(key, default=None):
            return stored.get(key, default)

        def fake_set(key, value):
            stored[key] = value

        with mock.patch.object(S.db, 'get_setting', side_effect=fake_get), \
                mock.patch.object(S.db, 'set_setting', side_effect=fake_set):
            S.save_settings({'enabled': True})
            saved = S.load_settings()
        self.assertTrue(saved['enabled'])
        self.assertTrue(saved['dry_run'])   # 未提供的字段保留默认


class RunOnceTest(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        S._run_busy = False

    async def test_disabled_does_nothing(self) -> None:
        with mock.patch.object(S, 'load_settings',
                               return_value={'enabled': False}), \
                mock.patch.object(S, '_group_entries') as entries, \
                mock.patch.object(S, 'apply') as ap:
            out = await S.run_once(now=1)
        self.assertEqual(out, {'skipped': 'disabled'})
        entries.assert_not_called()
        ap.assert_not_called()

    async def test_busy_returns_skipped_without_work(self) -> None:
        S._run_busy = True
        with mock.patch.object(S, '_run_once_sync') as run:
            out = await S.run_once(now=1)
        self.assertEqual(out, {'skipped': 'busy'})
        run.assert_not_called()

    async def test_enabled_dry_run_does_not_write(self) -> None:
        settings = {'enabled': True, 'interval_minutes': 15,
                    'window_minutes': 15, 'dry_run': True}
        result = {'realm': 'cn', 'pool_median': 100.0, 'reason': '',
                  'rows': [], 'collisions': [], 'stale_demotions': [],
                  'changes': [{'uid': 'u1', 'file': 'f.json',
                               'from': 1.0, 'from_raw': 0, 'to': 0.15}],
                  'restores': []}
        with mock.patch.object(S, 'load_settings', return_value=settings), \
                mock.patch.object(S, '_group_entries',
                                  return_value=[_entry('A', {'cn'})]), \
                mock.patch.object(S, 'evaluate_for_dir', return_value=result), \
                mock.patch.object(S, 'restore', return_value=[]) as restore_mock, \
                mock.patch.object(S, 'apply',
                                  return_value=[{'uid': 'u1', 'ok': True,
                                                 'dry_run': True}]) as ap, \
                mock.patch.object(S, 'prune_demotions'):
            out = await S.run_once(now=2)
        ap.assert_called_once()
        self.assertEqual(ap.call_args.kwargs['dry_run'], True)
        restore_mock.assert_called_once()
        self.assertEqual(out['groups'], 1)
        self.assertEqual(out['changed'], 0)   # 预演不算「已调整」
        self.assertEqual(out['dry_run'], True)

    async def test_group_error_does_not_stop_others_and_keeps_last_error(self) -> None:
        settings = {'enabled': True, 'interval_minutes': 15,
                    'window_minutes': 15, 'dry_run': False}
        calls = {'n': 0}

        def fake_eval(_dir, _window, *, realm=None, now=None):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('boom')
            return {'realm': realm, 'pool_median': 1.0, 'reason': '',
                    'rows': [], 'collisions': [], 'stale_demotions': [],
                    'changes': [], 'restores': []}

        with mock.patch.object(S, 'load_settings', return_value=settings), \
                mock.patch.object(S, '_group_entries', return_value=[
                    _entry('A', {'cn'}), _entry('B', {'global'})]), \
                mock.patch.object(S, 'evaluate_for_dir', side_effect=fake_eval), \
                mock.patch.object(S, 'restore', return_value=[]), \
                mock.patch.object(S, 'apply', return_value=[]), \
                mock.patch.object(S, 'prune_demotions'):
            out = await S.run_once(now=3)
        self.assertEqual(out['groups'], 2)
        self.assertTrue(any('error' in d for d in out['details']))
        self.assertIn('A/cn', S._state['last_error'])

    async def test_successful_run_clears_last_error(self) -> None:
        settings = {'enabled': True, 'interval_minutes': 15,
                    'window_minutes': 15, 'dry_run': True}
        S._state['last_error'] = '旧错误'
        result = {'realm': 'cn', 'pool_median': None, 'reason': '',
                  'rows': [], 'collisions': [], 'stale_demotions': [],
                  'changes': [], 'restores': []}
        with mock.patch.object(S, 'load_settings', return_value=settings), \
                mock.patch.object(S, '_group_entries',
                                  return_value=[_entry('A', {'cn'})]), \
                mock.patch.object(S, 'evaluate_for_dir', return_value=result), \
                mock.patch.object(S, 'restore', return_value=[]), \
                mock.patch.object(S, 'apply', return_value=[]), \
                mock.patch.object(S, 'prune_demotions'):
            await S.run_once(now=4)
        self.assertEqual(S._state['last_error'], '')

    async def test_same_realm_across_groups_is_skipped(self) -> None:
        settings = {'enabled': True, 'interval_minutes': 15,
                    'window_minutes': 15, 'dry_run': True}
        with mock.patch.object(S, 'load_settings', return_value=settings), \
                mock.patch.object(S, '_group_entries', return_value=[
                    _entry('A', {'cn'}), _entry('B', {'cn'})]), \
                mock.patch.object(S, 'evaluate_for_dir') as ev:
            out = await S.run_once(now=5)
        ev.assert_not_called()
        self.assertEqual(out['groups'], 2)
        self.assertTrue(all('skipped' in d for d in out['details']))


class ApplyRouteTest(unittest.IsolatedAsyncioTestCase):
    """路由默认 dry_run=true，且 'false' 字符串不能当真。"""

    def tearDown(self) -> None:
        S._run_busy = False

    async def _call(self, body: dict) -> dict:
        fake = mock.AsyncMock(return_value={'dry_run': True, 'realms': []})
        with mock.patch.object(accounts, '_group', return_value={'name': 'A'}), \
                mock.patch.object(accounts, '_require_dir', return_value=Path('.')), \
                mock.patch.object(S, 'apply_for_group', fake):
            out = await accounts.account_speed_health_apply(
                body=body, upstream_id=None, user={'role': 'admin'})
        return {'out': out, 'mock': fake}

    async def test_defaults_to_dry_run(self) -> None:
        data = await self._call({})
        self.assertTrue(data['mock'].await_args.kwargs['dry_run'])

    async def test_string_false_is_honored(self) -> None:
        data = await self._call({'dry_run': 'false'})
        self.assertFalse(data['mock'].await_args.kwargs['dry_run'])

    async def test_window_is_clamped(self) -> None:
        data = await self._call({'window': 1})
        self.assertEqual(data['mock'].await_args.args[1], 60)


if __name__ == '__main__':
    unittest.main()
