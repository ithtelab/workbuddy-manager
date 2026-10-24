"""账号页「强制设置冷却时间」的回归测试。"""
from __future__ import annotations

import asyncio
import datetime
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from server import config
from server.services import wb2api


class ForceSetCoolingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self._old_config = config.UPSTREAM_CONFIG
        self._old_dir = config.UPSTREAM_DIR
        config.UPSTREAM_DIR = root
        config.UPSTREAM_CONFIG = root / 'config.json'
        config.UPSTREAM_CONFIG.write_text(
            json.dumps({'state_file': './data/state.json'}), encoding='utf-8',
        )
        self.state_path = root / 'data' / 'state.json'
        self.state_path.parent.mkdir(parents=True)
        self.state_path.write_text(json.dumps({
            'accounts': {
                'target': {
                    'credits': 123,
                    'disabled': False,
                    'until': '0001-01-01T00:00:00Z',
                    'cool_kind': 0,
                    'soft_streak': 0,
                },
                'other': {'credits': 9, 'until': '2099-02-01T04:00:00+08:00'},
            },
        }, ensure_ascii=False), encoding='utf-8')

    def tearDown(self) -> None:
        config.UPSTREAM_CONFIG = self._old_config
        config.UPSTREAM_DIR = self._old_dir
        self._tmp.cleanup()

    def test_set_cooling_state_updates_target(self) -> None:
        until = '2099-01-01T12:00:00Z'
        result = wb2api.set_account_cooling_state('target', until, '防刷保护')
        data = json.loads(self.state_path.read_text(encoding='utf-8'))
        target = data['accounts']['target']

        self.assertEqual(result['uid'], 'target')
        self.assertEqual(target['until'], until)
        self.assertEqual(target['reason'], '防刷保护')
        self.assertEqual(target['cool_kind'], 0)
        self.assertEqual(target['soft_streak'], 0)
        self.assertTrue(target.get('manual_disabled'))
        self.assertTrue(str(target.get('manual_reason') or '').startswith('手动冷却'))
        self.assertIn('防刷保护', target.get('manual_reason') or '')
        self.assertIn(until, target.get('manual_reason') or '')
        self.assertEqual(target['credits'], 123)
        self.assertEqual(data['accounts']['other']['credits'], 9)
        self.assertTrue(pathlib.Path(result['backup']).is_file())

    def test_empty_uid_raises(self) -> None:
        with self.assertRaises(ValueError):
            wb2api.set_account_cooling_state('', '2099-01-01T00:00:00Z')

    def test_expired_manual_cooling_is_detected_and_cleared(self) -> None:
        wb2api.set_account_cooling_state(
            'target', '2020-01-01T00:00:00Z', '临时保护')
        self.assertEqual(wb2api.expired_manual_cooling_uids(), ['target'])

        with mock.patch.object(
            wb2api, '_set_upstream_running',
            new=mock.AsyncMock(side_effect=[(True, 'stopped'), (True, 'started')]),
        ) as control:
            cleared = asyncio.run(wb2api.clear_expired_manual_cooling())

        self.assertEqual(cleared, 1)
        target = json.loads(self.state_path.read_text(encoding='utf-8'))['accounts']['target']
        self.assertFalse(target.get('manual_disabled'))
        self.assertNotIn('manual_reason', target)
        self.assertEqual(control.await_args_list[0].args, (False,))
        self.assertEqual(control.await_args_list[1].args, (True,))

    def test_permanent_or_user_manual_disable_is_not_cleared(self) -> None:
        wb2api.set_account_cooling_state(
            'target', '2099-01-01T00:00:00Z', '永久保护')
        self.assertEqual(wb2api.expired_manual_cooling_uids(), [])

        data = json.loads(self.state_path.read_text(encoding='utf-8'))
        data['accounts']['target']['manual_reason'] = '我主动摘的'
        data['accounts']['target']['until'] = '2020-01-01T00:00:00Z'
        self.state_path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        self.assertEqual(wb2api.expired_manual_cooling_uids(), [])

    def test_upstream_checkin_resetting_until_does_not_expire_cooling(self) -> None:
        """签到会把 until 重置成零值；到期判断必须看 reason 里的标记。"""
        wb2api.set_account_cooling_state(
            'target', '2099-01-01T00:00:00Z', '长时间保护')
        data = json.loads(self.state_path.read_text(encoding='utf-8'))
        data['accounts']['target']['until'] = '0001-01-01T00:00:00Z'
        self.state_path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        self.assertEqual(wb2api.expired_manual_cooling_uids(), [],
                         'until 被签到重置后，未来到期的冷却不能被当成已过期')

    def test_clear_does_not_unset_user_manual_disable_with_cooling_word(self) -> None:
        """用户手动停用的 reason 即使含“冷却”，也不该被清除冷却顺手解掉。"""
        data = json.loads(self.state_path.read_text(encoding='utf-8'))
        data['accounts']['target']['manual_disabled'] = True
        data['accounts']['target']['manual_reason'] = '上游冷却中，先摘掉'
        data['accounts']['target']['until'] = '0001-01-01T00:00:00Z'
        self.state_path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

        wb2api.clear_account_cooling_state('target')
        target = json.loads(self.state_path.read_text(encoding='utf-8'))['accounts']['target']
        self.assertTrue(target.get('manual_disabled'))
        self.assertEqual(target.get('manual_reason'), '上游冷却中，先摘掉')

    def test_force_set_cooling_stops_and_starts_upstream(self) -> None:
        until = '2099-01-01T12:00:00Z'
        status_after = {'connected': True, 'accounts': [{'uid': 'target', 'cooling': True, 'until': until}]}

        with (
            mock.patch.object(wb2api, 'get_status', new=mock.AsyncMock(return_value=status_after)),
            mock.patch.object(
                wb2api,
                '_set_upstream_running',
                new=mock.AsyncMock(side_effect=[(True, 'stopped'), (True, 'started')]),
            ) as control,
        ):
            ok, message, detail = asyncio.run(wb2api.force_set_account_cooling('target', until, '测试冷却'))

        self.assertTrue(ok, message)
        self.assertIn('已强制设定', message)
        self.assertEqual(detail['uid'], 'target')
        self.assertEqual(control.await_args_list[0].args, (False,))
        self.assertEqual(control.await_args_list[1].args, (True,))
        target = json.loads(self.state_path.read_text(encoding='utf-8'))['accounts']['target']
        self.assertEqual(target['until'], until)
        self.assertEqual(target['reason'], '测试冷却')


class ForceCoolingEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self._orig = (config.AUTH_DIR, config.DB_PATH, config.USERS_FILE,
                      config.UPSTREAM_CONFIG, config.UPSTREAM_DIR)
        config.AUTH_DIR = root / 'auths'
        config.AUTH_DIR.mkdir()
        config.DB_PATH = root / 'manager.db'
        config.USERS_FILE = root / 'users.json'
        config.UPSTREAM_DIR = root
        config.UPSTREAM_CONFIG = root / 'config.json'
        config.UPSTREAM_CONFIG.write_text(json.dumps({'state_file': './data/state.json'}), encoding='utf-8')
        state_dir = root / 'data'
        state_dir.mkdir()
        (state_dir / 'state.json').write_text(json.dumps({
            'accounts': {'target-uid': {'credits': 100, 'until': '0001-01-01T00:00:00Z'}},
        }), encoding='utf-8')

        (config.AUTH_DIR / 'workbuddy-target.json').write_text(json.dumps({
            'account': {'uid': 'target-uid', 'nickname': '目标账号'},
            'auth': {'accessToken': 'AT', 'refreshToken': 'RT', 'expiresAt': 9999999999, 'realm': 'cn'},
        }), encoding='utf-8')

        from server import db, security
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        db.connect()
        security.save_users({'secret': 'S', 'users': [
            {'username': 'admin', 'role': 'admin',
             'pwd_hash': security.make_hash('pw')}], 'api_keys': []})

        from server.main import app
        from fastapi.testclient import TestClient
        self.client = TestClient(app)
        r = self.client.post('/api/login', json={'username': 'admin', 'password': 'pw'})
        self.assertEqual(r.status_code, 200, r.text)
        self.client.cookies.update(dict(r.cookies))

    def tearDown(self) -> None:
        from server import db
        if db._conn is not None:
            db._conn.close()
        db._conn = None
        config.AUTH_DIR, config.DB_PATH, config.USERS_FILE, config.UPSTREAM_CONFIG, config.UPSTREAM_DIR = self._orig
        self._tmp.cleanup()

    def test_endpoint_with_duration(self) -> None:
        with mock.patch.object(
            wb2api, 'force_set_account_cooling',
            new=mock.AsyncMock(return_value=(True, '已成功设定', {'uid': 'target-uid'})),
        ) as mock_set:
            resp = self.client.post(
                '/api/accounts/workbuddy-target.json/force-cooling',
                json={'duration_seconds': 3600, 'reason': '测试冷却'},
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertIn('until', data)
        self.assertEqual(data['uid'], 'target-uid')
        mock_set.assert_awaited_once()

    def test_endpoint_permanent(self) -> None:
        with mock.patch.object(
            wb2api, 'force_set_account_cooling',
            new=mock.AsyncMock(return_value=(True, '已成功设定', {'uid': 'target-uid'})),
        ) as mock_set:
            resp = self.client.post(
                '/api/accounts/workbuddy-target.json/force-cooling',
                json={'permanent': True},
            )
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        self.assertTrue(data['ok'])
        self.assertIn('2099', data['until'])
        mock_set.assert_awaited_once()

    def test_endpoint_invalid_duration(self) -> None:
        resp = self.client.post(
            '/api/accounts/workbuddy-target.json/force-cooling',
            json={'duration_seconds': -10},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('冷却时长必须为正整数', resp.json()['detail'])

    def test_endpoint_past_until(self) -> None:
        resp = self.client.post(
            '/api/accounts/workbuddy-target.json/force-cooling',
            json={'until': '2000-01-01T00:00:00Z'},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn('冷却截止时间必须在未来', resp.json()['detail'])

    def test_endpoint_missing_account(self) -> None:
        resp = self.client.post(
            '/api/accounts/workbuddy-nonexistent.json/force-cooling',
            json={'duration_seconds': 3600},
        )
        self.assertEqual(resp.status_code, 404)


if __name__ == '__main__':
    unittest.main()
