"""调度器与设置：默认关闭、坏值回落、预演不落盘。"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import speedhealth as S  # noqa: E402


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
    async def test_disabled_does_nothing(self) -> None:
        with mock.patch.object(S, 'load_settings', return_value={'enabled': False}), \
                mock.patch.object(S, 'evaluate_for_dir') as ev, \
                mock.patch.object(S, 'apply') as ap:
            out = await S.run_once(now=1)
        self.assertEqual(out, {'skipped': 'disabled'})
        ev.assert_not_called()
        ap.assert_not_called()

    async def test_enabled_dry_run_does_not_write(self) -> None:
        settings = {'enabled': True, 'interval_minutes': 15,
                    'window_minutes': 15, 'dry_run': True}
        group = {'name': '默认', 'enabled': True, 'auth_dir': str(Path('.'))}
        result = {'pool_median': 100.0, 'reason': '',
                  'changes': [{'uid': 'u1', 'file': 'f.json', 'from': 1.0, 'to': 0.15}]}
        with mock.patch.object(S, 'load_settings', return_value=settings), \
                mock.patch.object(S.upstreamsvc, 'list_upstreams', return_value=[group]), \
                mock.patch.object(S, 'evaluate_for_dir', return_value=result), \
                mock.patch.object(S, 'apply', return_value=[{'uid': 'u1', 'ok': True,
                                                             'dry_run': True}]) as ap:
            out = await S.run_once(now=2)
        ap.assert_called_once()
        self.assertEqual(ap.call_args[1]['dry_run'], True)
        self.assertEqual(out['groups'], 1)
        self.assertEqual(out['changed'], 0)   # 预演不算「已调整」

    async def test_group_error_does_not_stop_others(self) -> None:
        settings = {'enabled': True, 'interval_minutes': 15,
                    'window_minutes': 15, 'dry_run': False}
        groups = [
            {'name': 'A', 'enabled': True, 'auth_dir': str(Path('.'))},
            {'name': 'B', 'enabled': True, 'auth_dir': str(Path('.'))},
        ]
        calls = {'n': 0}

        def fake_eval(_dir, _window):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('boom')
            return {'pool_median': 1.0, 'reason': '', 'changes': []}

        with mock.patch.object(S, 'load_settings', return_value=settings), \
                mock.patch.object(S.upstreamsvc, 'list_upstreams', return_value=groups), \
                mock.patch.object(S, 'evaluate_for_dir', side_effect=fake_eval), \
                mock.patch.object(S, 'apply', return_value=[]):
            out = await S.run_once(now=3)
        self.assertEqual(out['groups'], 2)
        self.assertTrue(any('error' in d for d in out['details']))


if __name__ == '__main__':
    unittest.main()
