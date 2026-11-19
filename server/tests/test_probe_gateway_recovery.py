import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from server.routers import accounts


class ProbeGatewayRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def run_probe(self, status):
        group = {'base_url': 'http://gateway:7863', 'api_key': 'test'}
        raw = {'account': {'uid': 'u1'}, 'auth': {'realm': 'cn'}}
        with patch.object(accounts, '_group', return_value=group), \
                patch.object(accounts, '_require_dir', return_value=Path('/test')), \
                patch.object(accounts, '_load', return_value=raw), \
                patch.object(accounts.tencent, 'probe_model', new=AsyncMock(return_value={'status': status})), \
                patch.object(accounts.wb2api, 'clear_model_block', new=AsyncMock(return_value=(True, '', 'ok'))) as sync:
            result = await accounts.probe_models(
                {'accounts': ['workbuddy-u1.json'], 'models': ['cn:deepseek-v4.1-flash']},
                upstream_id=None, user={'role': 'admin'})
        return result, sync

    async def test_success_synchronizes_exact_account_model(self):
        result, sync = await self.run_probe('ok')
        sync.assert_awaited_once_with('u1', 'deepseek-v4.1-flash',
                                     base_url='http://gateway:7863', api_key='test')
        self.assertTrue(result['results'][0]['gateway_synced'])
        self.assertNotIn('gateway_sync_skipped', result['results'][0])

    async def test_rate_limit_does_not_clear_gateway(self):
        _, sync = await self.run_probe('rate')
        sync.assert_not_awaited()

    async def test_missing_admin_route_is_not_reported_as_failure(self):
        with patch.object(accounts, '_group', return_value={'base_url': 'http://gateway:7863', 'api_key': 'test'}),                 patch.object(accounts, '_require_dir', return_value=Path('/test')),                 patch.object(accounts, '_load', return_value={'account': {'uid': 'u1'}, 'auth': {'realm': 'cn'}}),                 patch.object(accounts.tencent, 'probe_model', new=AsyncMock(return_value={'status': 'ok'})),                 patch.object(accounts.wb2api, 'clear_model_block',
                             new=AsyncMock(return_value=(False, '上游未启用管理接口', 'no_route'))):
            result = await accounts.probe_models(
                {'accounts': ['workbuddy-u1.json'], 'models': ['cn:deepseek-v4.1-flash']},
                upstream_id=None, user={'role': 'admin'})
        detail = result['results'][0]
        self.assertTrue(detail['gateway_sync_skipped'])
        self.assertNotIn('gateway_synced', detail)
