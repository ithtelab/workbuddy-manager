import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from server.services import modelrecovery as recovery


class ModelRecoverySchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.group = {'id': None, 'enabled': True, 'auth_dir': '/auths',
                      'base_url': 'http://gateway', 'api_key': 'test'}
        self.account = {'uid': 'u1', 'rate_limited_models': [
            {'model': 'deepseek-v4.1-flash', 'reason': '11102 model not available'}]}
        self.patches = [
            patch.object(recovery.config, 'DATA_DIR', Path(self.temp.name)),
            patch.object(recovery.updater, 'read_status', return_value={}),
            patch.object(recovery.upstreamsvc, 'list_upstreams', return_value=[self.group]),
            patch.object(recovery.wb2api, 'get_status', new=AsyncMock(return_value={'connected': True, 'accounts': [self.account]})),
            patch.object(recovery.wb2api, 'list_auth_accounts', return_value=[{'uid': 'u1', 'file': 'workbuddy-u1.json'}]),
            patch.object(recovery.asyncio, 'sleep', new=AsyncMock()),
        ]
        for p in self.patches:
            p.start(); self.addCleanup(p.stop)
        recovery._entries = {}

    async def test_automatic_success_after_first_delay(self):
        with patch.object(recovery, '_probe', new=AsyncMock(return_value={'status': 'ok', 'gateway_synced': True})) as probe:
            self.assertEqual((await recovery.run_once(1000))['checked'], 0)
            self.assertEqual((await recovery.run_once(1030))['recovered'], 1)
            probe.assert_awaited_once_with(self.group, 'workbuddy-u1.json', 'deepseek-v4.1-flash')
            self.assertEqual(recovery._entries, {})

    async def test_known_real_quota_is_never_probed_early(self):
        self.account['rate_limited_models'][0]['reason'] = '6004 model rate limit'
        with patch.object(recovery, '_probe', new=AsyncMock()) as probe:
            await recovery.run_once(1000)
            await recovery.run_once(5000)
            probe.assert_not_awaited()

    async def test_failed_probe_backs_off_and_persists(self):
        with patch.object(recovery, '_probe', new=AsyncMock(return_value={'status': 'no_model'})) as probe:
            await recovery.run_once(1000)
            await recovery.run_once(1030)
            await recovery.run_once(1040)
            self.assertEqual(probe.await_count, 1)
            recovery._entries = {}
            recovery._load()
            self.assertEqual(next(iter(recovery._entries.values()))['next_at'], 1090)

    async def test_small_probe_failure_respects_reported_reset(self):
        with patch.object(recovery, '_probe', new=AsyncMock(return_value={
            'status': 'rate', 'message': '将在 2026-10-28 10:56:44 UTC+8 重置'})):
            await recovery.run_once(1000)
            await recovery.run_once(1030)
            self.assertGreater(next(iter(recovery._entries.values()))['next_at'], 1030 + 600)

    async def test_disabled_account_is_not_probed(self):
        self.account['manual_disabled'] = True
        with patch.object(recovery, '_probe', new=AsyncMock()) as probe:
            await recovery.run_once(1000)
            await recovery.run_once(5000)
            probe.assert_not_awaited()

    async def test_missing_admin_route_stops_rechecking(self):
        with patch.object(recovery, '_probe', new=AsyncMock(return_value={'status': 'ok', 'gateway_sync_skipped': True})):
            await recovery.run_once(1000)
            self.assertEqual((await recovery.run_once(1030))['checked'], 1)
            self.assertEqual(recovery._entries, {})

    async def test_one_account_is_checked_once_per_scan(self):
        self.account['rate_limited_models'].append({'model': 'glm-5.3', 'reason': '11102 model not available'})
        with patch.object(recovery, '_probe', new=AsyncMock(return_value={'status': 'ok', 'gateway_synced': True})) as probe:
            await recovery.run_once(1000)
            self.assertEqual((await recovery.run_once(1030))['checked'], 1)
            self.assertEqual(probe.await_count, 1)
