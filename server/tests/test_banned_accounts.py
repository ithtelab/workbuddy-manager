import asyncio
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest import mock

from server.services import wb2api


class BannedAccountsTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), '未安装 node，跳过前端逻辑测试')
    def test_frontend_credit_availability(self):
        node = shutil.which('node')
        version = subprocess.check_output([node, '--version'], text=True).strip().lstrip('v')
        if tuple(map(int, version.split('.')[:2])) < (22, 6):
            self.skipTest('需要 node >= 22.6 的 type stripping')
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run(
            [node, '--experimental-strip-types', str(root / 'web/lib/credit-availability.test.mjs')],
            cwd=root, text=True, encoding='utf-8', capture_output=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ban_requires_semantics_not_11140_alone(self):
        for reason in ('account banned by upstream (request illegal, code 11140, http 403), re-login required',
                       '11140 request illegal', '账号存在异常行为，已被限制使用'):
            self.assertTrue(wb2api.is_account_banned({'disabled': True, 'disabled_reason': reason}))
        for reason in ('11140 The model provider is rate-limiting requests', '12153 session dead', '',
                       'account banned by upstream (request illegal, code 11128, http 400), re-login required'):
            self.assertFalse(wb2api.is_account_banned({'disabled': True, 'disabled_reason': reason}))
        self.assertFalse(wb2api.is_account_banned({'disabled': False, 'disabled_reason': 'request illegal'}))

    def test_revive_refuses_ban_and_unknown_status(self):
        for status in ({'connected': True, 'accounts': [{'uid': 'u', 'disabled': True,
                       'disabled_reason': 'request illegal, code 11140, http 403'}]},
                       {'connected': False}, {'connected': True, 'accounts': []}):
            with self.subTest(status=status), \
                 mock.patch.object(wb2api, 'get_status', new=mock.AsyncMock(return_value=status)), \
                 mock.patch.object(wb2api, '_admin_account_call', new=mock.AsyncMock()) as post:
                ok, message, _ = asyncio.run(wb2api.revive_account('u', base_url='http://group', api_key='key'))
                self.assertFalse(ok)
                self.assertTrue(message)
                post.assert_not_awaited()

    def test_session_failure_still_recoverable(self):
        status = {'connected': True, 'accounts': [{'uid': 'u', 'disabled': True, 'disabled_reason': '12153 session dead'}]}
        with mock.patch.object(wb2api, 'get_status', new=mock.AsyncMock(return_value=status)) as get, \
             mock.patch.object(wb2api, '_admin_account_call', new=mock.AsyncMock(return_value=(True, '', 'ok', {'changed': True, 'disabled': False}))) as post:
            self.assertTrue(asyncio.run(wb2api.revive_account('u', base_url='http://group', api_key='key'))[0])
            get.assert_awaited_once_with(base_url='http://group', api_key='key')
            post.assert_awaited_once_with('u', 'revive', base_url='http://group', api_key='key')

    def test_login_cannot_auto_revive_banned_account(self):
        from server.routers import accounts
        status = {'connected': True, 'accounts': [{'uid': 'u', 'disabled': True, 'disabled_reason': '11140 request illegal'}]}
        with mock.patch.object(wb2api, 'get_status', new=mock.AsyncMock(return_value=status)), \
             mock.patch.object(wb2api, '_admin_account_call', new=mock.AsyncMock()) as post:
            asyncio.run(accounts._revive_after_login('u', {'base_url': 'http://group'}))
            post.assert_not_awaited()
