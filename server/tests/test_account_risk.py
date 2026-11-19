import asyncio
import json
from pathlib import Path
import tempfile
import shutil
import subprocess
import time
import unittest
from unittest import mock

from server import config, db, upstreamsvc
from server.services import accountrisk, tencent, wb2api


class AccountRiskTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is required for the frontend status test')
    def test_frontend_safety_pause_status(self):
        node = shutil.which('node')
        version = subprocess.check_output([node, '--version'], text=True).strip().lstrip('v')
        if tuple(map(int, version.split('.')[:2])) < (22, 6):
            self.skipTest('Node >= 22.6 is required')
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run([node, '--experimental-strip-types', str(root/'web/lib/account-risk-status.test.mjs')],
                                cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original = config.DB_PATH, config.AUTH_DIR, config.USERS_FILE
        config.DB_PATH = self.root / 'manager.db'
        config.AUTH_DIR = self.root / 'auths'
        config.AUTH_DIR.mkdir()
        config.USERS_FILE = self.root / 'users.json'
        db._conn = None
        db.connect()
        self.now = int(time.time())
        self.uid = 'abcd1234-0000-0000-0000-000000000001'
        self.filename = 'workbuddy-test.json'
        (config.AUTH_DIR / self.filename).write_text(json.dumps({
            'account': {'uid': self.uid, 'nickname': 'risk-test'},
            'auth': {'accessToken': 'at', 'refreshToken': 'rt', 'expiresAt': self.now + 86400,
                     'realm': 'cn', 'domain': 'copilot.tencent.com'},
        }), encoding='utf-8')
        self.pool = {'connected': True, 'accounts': [{'uid': self.uid, 'realm': 'cn', 'disabled': False}]}
        self.status_patch = mock.patch.object(wb2api, 'get_status', new=mock.AsyncMock(side_effect=lambda **kw: self.pool))
        self.status_patch.start()
        accountrisk.initialize()
        db.set_setting('account_risk_enabled_at', self.now)

    def tearDown(self):
        self.status_patch.stop()
        if db._conn:
            db._conn.close()
        db._conn = None
        config.DB_PATH, config.AUTH_DIR, config.USERS_FILE = self.original
        self.temp.cleanup()

    def risk(self, **kwargs):
        return dict({'until':self.now+300, 'last_failure_at':self.now, 'failures':1,
                     'needs_review':False, 'retry_after_sec':300, 'paused':True}, **kwargs)

    def event(self, **kwargs):
        return dict({'ts': self.now, 'uid': self.uid[:8], 'kind': 'keepalive',
                     'message': 'upstream soft_rate (http 429): Too Many Requests'}, **kwargs)

    async def apply_pause(self, uid, wait, **kwargs):
        self.assertEqual(uid, self.uid)
        self.pool['accounts'][0]['refresh_risk'] = self.risk(until=self.now+wait)
        return True, '', 'ok', {'uid':uid, 'refresh_risk':self.pool['accounts'][0]['refresh_risk']}

    def test_429_pauses_one_account_without_manual_state_or_restart(self):
        with mock.patch.object(wb2api, 'set_refresh_risk', new=mock.AsyncMock(side_effect=self.apply_pause)) as action, \
             mock.patch.object(wb2api, 'set_manual_disabled') as manual, mock.patch.object(wb2api, 'restart_container') as restart:
            asyncio.run(accountrisk.handle_tasks([self.event()]))
            asyncio.run(accountrisk.handle_tasks([self.event()]))
            action.assert_awaited_once()
            manual.assert_not_called(); restart.assert_not_called()
        self.assertTrue(asyncio.run(accountrisk.warnings())['items'][0]['paused'])

    def test_native_callback_is_not_applied_again_from_logs(self):
        self.pool['accounts'][0]['refresh_risk'] = self.risk()
        with mock.patch.object(wb2api, 'set_refresh_risk', new=mock.AsyncMock()) as action:
            asyncio.run(accountrisk.handle_tasks([self.event()]))
            action.assert_not_awaited()

    def test_expired_native_event_replay_does_not_repause(self):
        self.pool['accounts'][0]['refresh_risk'] = self.risk(until=self.now-1)
        with mock.patch.object(wb2api, 'set_refresh_risk', new=mock.AsyncMock()) as action:
            asyncio.run(accountrisk.handle_tasks([self.event()]))
            action.assert_not_awaited()
        self.assertFalse(asyncio.run(accountrisk.paused(self.uid, config.AUTH_DIR)))

    def test_historical_logs_only_warn(self):
        with mock.patch.object(wb2api, 'set_refresh_risk', new=mock.AsyncMock()) as action:
            asyncio.run(accountrisk.handle_tasks([self.event(ts=self.now-86400)]))
            action.assert_not_awaited()
        self.assertTrue(asyncio.run(accountrisk.warnings())['items'][0]['historical'])

    def test_model_quota_and_500_only_warn(self):
        with mock.patch.object(wb2api, 'set_refresh_risk', new=mock.AsyncMock()) as action:
            asyncio.run(accountrisk.handle_tasks([self.event(kind='checkin', message='http 429 quota'),
                self.event(kind='travel', message='http 500 grant credits failed')]))
            action.assert_not_awaited()
        self.assertEqual(len(asyncio.run(accountrisk.warnings())['items']), 2)

    def test_prefix_is_not_risk_state(self):
        self.pool['accounts'][0].update(manual_disabled=True, manual_reason='account-risk: arbitrary operator reason')
        self.assertFalse(asyncio.run(accountrisk.paused(self.uid, config.AUTH_DIR)))
        self.assertEqual(asyncio.run(accountrisk.warnings())['items'], [])

    def test_cached_pause_expires_during_status_outage(self):
        self.pool['accounts'][0]['refresh_risk'] = self.risk()
        self.assertTrue(asyncio.run(accountrisk.paused(self.uid, config.AUTH_DIR)))
        self.pool = {'connected':False}
        with mock.patch.object(accountrisk.time, 'time', return_value=self.now+301):
            self.assertFalse(asyncio.run(accountrisk.paused(self.uid, config.AUTH_DIR)))

    def test_review_remains_during_outage_until_explicit_clear(self):
        self.pool['accounts'][0]['refresh_risk'] = self.risk(needs_review=True, failures=3, until=self.now-1)
        self.assertTrue(asyncio.run(accountrisk.paused(self.uid, config.AUTH_DIR)))
        self.pool = {'connected':False}
        self.assertTrue(asyncio.run(accountrisk.paused(self.uid, config.AUTH_DIR)))

    def test_acknowledge_does_not_resume_and_history_is_readable(self):
        self.pool['accounts'][0]['refresh_risk'] = self.risk()
        item = asyncio.run(accountrisk.warnings())['items'][0]
        self.assertTrue(accountrisk.acknowledge(item['id']))
        self.assertTrue(asyncio.run(accountrisk.warnings())['items'][0]['paused'])
        self.pool['accounts'][0]['refresh_risk']['until'] = self.now-1
        self.assertEqual(asyncio.run(accountrisk.warnings())['items'], [])
        self.assertEqual(asyncio.run(accountrisk.warnings(include_acknowledged=True))['items'][0]['id'], item['id'])

    def test_missing_route_warns_without_permanent_file_fallback(self):
        with mock.patch.object(wb2api, 'set_refresh_risk', new=mock.AsyncMock(return_value=(False, '', 'no_route', {}))), \
             mock.patch.object(wb2api, 'set_account_disabled') as rename:
            self.assertFalse(asyncio.run(accountrisk.pause_refresh(self.uid, config.AUTH_DIR)))
            rename.assert_not_called()
        self.assertTrue((config.AUTH_DIR/self.filename).exists())
        self.assertEqual(asyncio.run(accountrisk.warnings())['items'][0]['action_error'], 'no_route')

    def test_retry_after_boundaries(self):
        import httpx
        for header, body, expected in [('1',{},300),('900',{},900),('999999999',{},21600),('',{'retry_after':1200},1200),('',{'retry_policy':{'retry_after':1800}},1800),('',{},300)]:
            response = httpx.Response(429,headers={'retry-after':header},json=body)
            self.assertEqual(tencent._refresh_retry_after(response),expected)

    def test_direct_failure_same_second_is_not_hidden_by_ack(self):
        with mock.patch.object(accountrisk.time, 'time', return_value=self.now), \
             mock.patch.object(wb2api, 'set_refresh_risk', new=mock.AsyncMock(side_effect=self.apply_pause)) as action:
            asyncio.run(accountrisk.pause_refresh(self.uid,config.AUTH_DIR))
            item=asyncio.run(accountrisk.warnings())['items'][0]
            accountrisk.acknowledge(item['id'])
            self.pool['accounts'][0]['refresh_risk'] = self.risk(until=0,failures=0)
            asyncio.run(accountrisk.pause_refresh(self.uid,config.AUTH_DIR))
            self.assertEqual(action.await_count,2)
            self.assertFalse(asyncio.run(accountrisk.warnings())['items'][0]['acknowledged'])

    def test_review_recovery_uses_dedicated_api_only(self):
        self.pool['accounts'][0].update(refresh_risk=self.risk(needs_review=True,failures=3),manual_disabled=True,disabled=True)
        item=asyncio.run(accountrisk.warnings())['items'][0]
        with mock.patch.object(wb2api, 'clear_refresh_risk', new=mock.AsyncMock(return_value=(True,'','ok',{'refresh_risk':self.risk(until=0,needs_review=False,failures=0),'manual_disabled':True,'disabled':True}))) as clear, \
             mock.patch.object(wb2api, 'set_manual_disabled') as manual, mock.patch.object(wb2api, 'revive_account') as revive:
            self.assertTrue(asyncio.run(accountrisk.resume(item['id']))[0])
            clear.assert_awaited_once();manual.assert_not_called();revive.assert_not_called()

    def test_scope_does_not_leak(self):
        accountrisk.record(config.AUTH_DIR,wb2api.list_auth_accounts()[0],'token_refresh',429,self.now)
        other={**upstreamsvc.default_upstream(),'id':9,'name':'other','auth_dir':str(self.root/'other')}
        Path(other['auth_dir']).mkdir()
        with mock.patch.object(upstreamsvc,'get_upstream',return_value=other):
            self.assertEqual(asyncio.run(accountrisk.warnings(group_id=9))['items'],[])

    def test_refresh_guard_prevents_network(self):
        payload={'uid':self.uid,'refresh_token':'rt','_risk_auth_dir':str(config.AUTH_DIR)}
        with mock.patch.object(accountrisk,'paused',new=mock.AsyncMock(return_value=True)), mock.patch.object(tencent,'_account_http_client') as network:
            self.assertFalse(asyncio.run(tencent.refresh_token(payload))[0]);network.assert_not_called()

    def test_warning_and_recovery_permissions(self):
        from fastapi.testclient import TestClient
        from server.main import app
        from server import security
        with TestClient(app) as client:
            with mock.patch.object(security,'load_users',return_value={'secret':'test-only','users':[]}):
                self.assertEqual(client.get('/api/account-risk').status_code,401)
            app.dependency_overrides[security.current_user]=lambda:{'username':'viewer','role':'viewer'}
            try:
                self.assertEqual(client.get('/api/account-risk').status_code,200)
                for action in ('acknowledge','resume'):
                    self.assertEqual(client.post('/api/account-risk/id/'+action).status_code,403)
            finally:app.dependency_overrides.pop(security.current_user,None)

    def test_keepalive_summary_denominator(self):
        from server.services import tasklog
        event=tasklog.parse_line('2026-11-01T01:00:00Z keepalive done: checked=49 ok=35 failed=14 skipped=0')
        self.assertIn('尝试 49，成功 35，失败 14',event['message'])
