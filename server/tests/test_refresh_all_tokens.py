"""「一键刷新令牌」批量端点的编排守卫（issue #40 的批量版）。

单账号「刷新 Token」已在真续期（见 `account_refresh` / `_refresh_one_token`），
这里钉住批量版**独有的**行为——逐条聚合、以及最容易被写错的那条：

  · 只重载**一次**上游。单账号端点每次刷新都重载一遍，批量若照着它逐个调用，
    14 个号就会把上游重启 14 遍——既慢，又会把在途请求全打断；
  · 一个都没成功就**不重载**：重载既不续期，也治不了 refreshToken 失效，
    白重启一次只会打断在途请求；
  · 单条失败不中断整批，结果里能看出是哪个号、失败原因是什么。

权限沿用 `security.require_admin`（改的是凭证，只有管理员能动），单独钉一条。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import HTTPException  # noqa: E402

from server import security  # noqa: E402
from server.routers import accounts  # noqa: E402


def _account(name: str) -> dict:
    return {'file': f'{name}.json', 'uid': f'uid-{name}', 'nickname': name}


class RefreshAllTokensOrchestrationTest(unittest.TestCase):
    """批量端点：聚合结果 + 末尾只重载一次。"""

    def _run(self, account_list: list[dict], results: dict[str, dict],
             reload_result: tuple[bool, str] = (True, '')) -> tuple[dict, list]:
        async def fake_refresh(filename: str, base_dir):
            return results[filename]

        reload_calls: list = []

        async def fake_reload(upstream=None):
            reload_calls.append(upstream)
            return reload_result

        with mock.patch.object(accounts.wb2api, 'list_auth_accounts',
                               lambda auth_dir=None: account_list), \
             mock.patch.object(accounts, '_refresh_one_token', fake_refresh), \
             mock.patch.object(accounts.reload, 'restart_now', fake_reload):
            out = asyncio.run(accounts.refresh_all_tokens(user={'role': 'admin'}))
        return out, reload_calls

    def test_aggregates_and_reloads_once(self) -> None:
        """3 个号里 2 成功 1 失败：分母/成功数/失败清单正确，且只重载一次。"""
        accts = [_account('a'), _account('b'), _account('c')]
        results = {
            'a.json': {'ok': True, 'message': '刷新成功', 'nickname': 'a',
                       'uid': 'uid-a', 'expires_at': 123},
            'b.json': {'ok': True, 'message': '刷新成功', 'nickname': 'b',
                       'uid': 'uid-b', 'expires_at': 456},
            'c.json': {'ok': False, 'message': '该账号没有 refreshToken',
                       'nickname': 'c', 'uid': 'uid-c', 'expires_at': None},
        }
        out, reload_calls = self._run(accts, results)

        self.assertEqual(out['total'], 3)
        self.assertEqual(out['succeeded'], 2)
        self.assertEqual(len(out['failed']), 1)
        self.assertIn('c', out['failed'][0])
        self.assertIn('refreshToken', out['failed'][0])
        self.assertTrue(out['reload_triggered'])
        self.assertEqual(len(reload_calls), 1, '批量刷新必须只重载一次上游')

    def test_all_failed_does_not_reload(self) -> None:
        """一个都没成功就不重载：重载治不了失效的 refreshToken。"""
        accts = [_account('a'), _account('b')]
        results = {
            f: {'ok': False, 'message': '刷新失败', 'nickname': f[0],
                'uid': f'uid-{f[0]}', 'expires_at': None}
            for f in ('a.json', 'b.json')
        }
        out, reload_calls = self._run(accts, results)

        self.assertEqual(out['succeeded'], 0)
        self.assertEqual(len(out['failed']), 2)
        self.assertFalse(out['reload_triggered'])
        self.assertEqual(reload_calls, [], '全部失败时不该重载上游')

    def test_empty_pool(self) -> None:
        """没有账号：返回空结果，不报错、不重载。"""
        out, reload_calls = self._run([], {})
        self.assertEqual(out['total'], 0)
        self.assertEqual(out['succeeded'], 0)
        self.assertEqual(reload_calls, [])

    def test_reload_failure_is_reported(self) -> None:
        """续期成功但重载失败：照实说出来，不冒充「已生效」。"""
        accts = [_account('a')]
        results = {'a.json': {'ok': True, 'message': '刷新成功', 'nickname': 'a',
                              'uid': 'uid-a', 'expires_at': 1}}
        out, _ = self._run(accts, results, reload_result=(False, '容器未运行'))
        self.assertTrue(out['succeeded'])
        self.assertFalse(out['reload_triggered'])
        self.assertIn('容器未运行', out['reload_error'])


class RefreshOneTokenTest(unittest.TestCase):
    """单账号刷新助手：错误文案与写回口径（批量与单账号共用这一份）。"""

    def _refresh(self, raw: dict, refresh_result: tuple,
                 write_side_effect=None) -> tuple[dict, mock.MagicMock]:
        async def fake_refresh(auth):
            return refresh_result

        with mock.patch.object(accounts, '_load', lambda filename, base_dir=None: raw), \
             mock.patch.object(accounts.tencent, 'refresh_token', fake_refresh), \
             mock.patch.object(accounts.tencent, 'update_auth_tokens',
                               side_effect=write_side_effect) as write:
            # base_dir 只被两个已 mock 的调用用到，占位即可，不碰真实文件系统
            out = asyncio.run(accounts._refresh_one_token('x.json', Path('.')))
        return out, write

    def test_missing_refresh_token_says_rescan(self) -> None:
        """没有 refreshToken：明确说「只能重新扫码」，且不打上游。"""
        raw = {'auth': {'accessToken': 'at'}, 'account': {'uid': 'u1'}}
        out, write = self._refresh(raw, (True, '不应被调用', {}))
        self.assertFalse(out['ok'])
        self.assertIn('扫码', out['message'])
        write.assert_not_called()

    def test_success_writes_back(self) -> None:
        """成功：调写回，返回 ok + 到期时间。"""
        raw = {'auth': {'accessToken': 'at', 'refreshToken': 'rt'},
               'account': {'uid': 'u1', 'nickname': 'n1'}, 'device_token': 'dt'}
        out, write = self._refresh(raw, (True, '刷新成功', {'access_token': 'new', 'expires_at': 999}))
        self.assertTrue(out['ok'])
        self.assertEqual(out['expires_at'], 999)
        write.assert_called_once()

    def test_write_failure_is_not_reported_as_success(self) -> None:
        """刷新成功但写盘失败：必须报失败，否则重启后又变回过期、更难查。"""
        raw = {'auth': {'accessToken': 'at', 'refreshToken': 'rt'},
               'account': {'uid': 'u1'}}
        out, _ = self._refresh(raw, (True, '刷新成功', {'access_token': 'new'}),
                               write_side_effect=OSError('disk full'))
        self.assertFalse(out['ok'])
        self.assertIn('写入账号文件失败', out['message'])


class RefreshAllTokensPermissionTest(unittest.TestCase):
    """批量刷新会改凭证，必须是管理员。"""

    def test_non_admin_rejected(self) -> None:
        for role in ('viewer', '', None):
            with self.subTest(role=role):
                with self.assertRaises(HTTPException) as ctx:
                    security.require_admin({'role': role})
                self.assertEqual(ctx.exception.status_code, 403)

    def test_admin_passes(self) -> None:
        user = {'role': 'admin'}
        self.assertIs(security.require_admin(user), user)


if __name__ == '__main__':
    unittest.main()
