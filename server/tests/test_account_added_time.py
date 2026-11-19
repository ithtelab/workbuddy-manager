"""Recorded additions survive token rewrites, relogin, moves and restarts."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from server import config, db, security, upstreamsvc
from server.routers import accounts
from server.services import tencent, wb2api


class AccountAddedTimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.original = config.DB_PATH, config.AUTH_DIR
        config.DB_PATH = self.root/'manager.db'
        config.AUTH_DIR = self.root/'auths'
        config.AUTH_DIR.mkdir()
        db._conn = None
        db.connect()
        self.group = upstreamsvc.default_upstream()
        self.result = {'uid':'added-test', 'nickname':'new account', 'access_token':'at',
                       'refresh_token':'rt', 'expires_at':4102444800, 'realm':'cn',
                       'domain':'copilot.tencent.com'}
        self.patches = [
            mock.patch.object(accounts.reload, 'request_reload_or_restart'),
            mock.patch.object(accounts, '_schedule_revive_after_login'),
            mock.patch.object(wb2api, 'get_status', new=mock.AsyncMock(return_value={'connected':False})),
        ]
        for p in self.patches:p.start()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        if db._conn:db._conn.close()
        db._conn = None
        config.DB_PATH, config.AUTH_DIR = self.original
        self.temp.cleanup()

    def login(self, result=None):
        return accounts._save_and_finish(result or self.result, 'cn', '', 'test-state', self.group)

    def listing(self):
        return asyncio.run(accounts.list_accounts(upstream_id=None, user={'role':'viewer'}))['accounts']

    def test_login_records_time_and_relogin_preserves_it(self):
        with mock.patch.object(db.time, 'time', return_value=1000.125):self.login()
        self.assertEqual(self.listing()[0]['added_at'],1000.125)
        with mock.patch.object(db.time, 'time', return_value=2000):self.login()
        self.assertEqual(self.listing()[0]['added_at'],1000.125)

    def test_token_rewrite_and_disable_rename_do_not_change_addition(self):
        self.login()
        at=self.listing()[0]['added_at']
        filename=self.listing()[0]['file']
        tencent.update_auth_tokens(filename,{'access_token':'refreshed'},config.AUTH_DIR)
        wb2api.set_account_disabled(filename,True,config.AUTH_DIR)
        self.assertEqual(self.listing()[0]['added_at'],at)

    def test_legacy_and_direct_files_stay_unknown(self):
        tencent.write_auth_file(self.result,config.AUTH_DIR)
        self.assertIsNone(self.listing()[0]['added_at'])
        self.login()  # Re-login is not a new addition.
        self.assertIsNone(self.listing()[0]['added_at'])

    def test_delete_then_readd_gets_new_time(self):
        with mock.patch.object(db.time,'time',return_value=1000):self.login()
        wb2api.delete_auth_account(self.listing()[0]['file'],config.AUTH_DIR)
        with mock.patch.object(db.time,'time',return_value=2000):self.login()
        self.assertEqual(self.listing()[0]['added_at'],2000)

    def test_scoped_identity_and_move_keep_original_time(self):
        self.login();at=self.listing()[0]['added_at']
        target=self.root/'other'
        self.assertEqual(db.account_added_times(target),{})
        db.move_account_added(self.result['uid'],config.AUTH_DIR,target)
        self.assertEqual(db.account_added_times(target),{self.result['uid']:at})
        self.assertEqual(db.account_added_times(config.AUTH_DIR),{})

    def test_database_reopen_preserves_time(self):
        self.login();at=self.listing()[0]['added_at']
        db._conn.close();db._conn=None
        self.assertEqual(self.listing()[0]['added_at'],at)

    def test_upload_records_new_account_but_not_overwrite(self):
        app=FastAPI();app.include_router(accounts.router)
        app.dependency_overrides[security.require_session_admin]=lambda:{'username':'admin','role':'admin'}
        with TestClient(app) as client, mock.patch.object(security,'audit'):
            def upload(overwrite=False):
                return client.post('/api/accounts/upload?overwrite='+str(overwrite).lower(),
                    files={'files':('new.json',json.dumps(self.result),'application/json')})
            with mock.patch.object(db.time,'time',return_value=1000):
                response=upload();self.assertEqual(response.status_code,200);self.assertTrue(response.json()['ok'])
            with mock.patch.object(db.time,'time',return_value=2000):
                response=upload(True);self.assertTrue(response.json()['ok'])
            self.assertEqual(self.listing()[0]['added_at'],1000)
