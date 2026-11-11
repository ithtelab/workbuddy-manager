"""「重登」必须是**针对某个账号**的动作（而不是又一次「添加账号」）。

用户报的现象：账号被上游标成已停用（凭证 401），从「重登队列」里点重登、或从
账号页点「添加账户」，扫完码以后那个账号**仍然留在重登队列里**，怎么重登都清不掉。

根因不是扫码本身，而是两条路各自少了一半：

  1. **版本取自界面、不取自账号。** 扫码端点和落盘的 `auth.realm` 都由 realm 决定，
     而 realm 以前只来自全站那个「国内版 / 国际版」开关。在国际版账号上重登、而
     界面停在国内版时，拿到的是国内版的码，写回去的 realm 也被改写成 cn —— 这个
     账号随后拿国内版令牌去请求，聊天 401、继续被系统禁用。重复多少次都一样。
  2. **扫到的账号没人核对。** 扫了另一个微信号等于「又添加了一个账号」：目标账号
     的旧凭证一个字节没变，队列里那一行当然纹丝不动。
  3. **解除系统禁用的结果只写日志。** 上游对已存在账号的 upsert **保留** `disabled`
     （`upsertLocked` 只换凭证），所以重登只能修好「凭证」，那一位必须靠 `revive`。
     失败（管理接口没开 / api_key 不对 / 账号被封禁）时界面上什么都没有，用户没有
     任何线索知道自己卡在哪一步。

本文件钉住修好后的三条契约，外加「同一个 uid 只写一份文件」——否则上游按文件名
排序后只认其中一份，新写的那份可能被旧文件覆盖（凭证看着刷新了，实际没生效）。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import HTTPException  # noqa: E402

from server import config, upstreamsvc  # noqa: E402
from server.routers import accounts  # noqa: E402
from server.services import tencent, wb2api  # noqa: E402

USER = {'username': 'admin', 'role': 'admin'}
UID = 'aa11bb22-cc33-44dd'
OTHER = 'ff00ee99-8877-6655'


def _doc(uid: str, realm: str = 'cn', nickname: str = '目标号') -> dict:
    return {
        'account': {'uid': uid, 'nickname': nickname, 'enterpriseId': ''},
        'auth': {'accessToken': 'old-' + uid, 'refreshToken': 'ref-' + uid,
                 'expiresAt': 4102444800, 'domain': 'copilot.tencent.com',
                 'realm': realm},
    }


def _ready(uid: str, realm: str = 'cn', nickname: str = '扫到的号') -> dict:
    return {'status': 'ready', 'uid': uid, 'nickname': nickname,
            'access_token': 'new-' + uid, 'refresh_token': 'r2',
            'expires_at': 4102444800, 'realm': realm,
            'domain': 'copilot.tencent.com'}


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.auth_dir = self.root / 'auths'
        self.auth_dir.mkdir()
        self._orig = config.AUTH_DIR
        config.AUTH_DIR = self.auth_dir
        self.group = upstreamsvc.default_upstream()
        accounts._login_targets.clear()
        accounts._login_polls.clear()
        accounts._login_ready.clear()
        accounts._provisioned_states.clear()

    def tearDown(self) -> None:
        accounts._login_targets.clear()
        accounts._login_polls.clear()
        accounts._login_ready.clear()
        accounts._provisioned_states.clear()
        config.AUTH_DIR = self._orig
        self.tmp.cleanup()


class ReloginTargetTest(_Base):
    def test_finds_account_and_its_realm(self) -> None:
        """目标账号按 **uid** 找，版本取它自己的 —— 不是界面上那个开关。"""
        (self.auth_dir / f'workbuddy-{UID}.json').write_text(
            json.dumps(_doc(UID, realm='global')), encoding='utf-8')
        t = accounts._relogin_target(UID, self.group)
        self.assertIsNotNone(t)
        self.assertEqual(t['realm'], 'global')
        self.assertEqual(t['file'], f'workbuddy-{UID}.json')
        self.assertFalse(t['panel_disabled'])

    def test_panel_disabled_account_is_still_a_target(self) -> None:
        """面板「临时停用」只是改名：仍是同一个账号，可以重登，但要标出来。"""
        name = f'workbuddy-{UID}.json.disabled'
        (self.auth_dir / name).write_text(json.dumps(_doc(UID)), encoding='utf-8')
        t = accounts._relogin_target(UID, self.group)
        self.assertEqual(t['file'], name)
        self.assertTrue(t['panel_disabled'])

    def test_unknown_uid_returns_none(self) -> None:
        self.assertIsNone(accounts._relogin_target('nobody', self.group))


class AuthStartTargetTest(_Base):
    """发码阶段：版本跟随账号，未知 uid 直接拒绝。"""

    def _start(self, body: dict) -> dict:
        calls: list[str] = []

        async def fake_start(realm, proxy_name=''):
            calls.append(realm)
            return {'state': f'st-{realm}', 'authUrl': 'https://x', 'realm': realm}

        def no_task(coro):              # 不真的起后台轮询
            coro.close()
            return None

        with mock.patch.object(tencent, 'start_login', fake_start), \
             mock.patch.object(accounts.asyncio, 'create_task', no_task):
            out = asyncio.run(accounts.auth_start(
                realm='cn', region=None, upstream_id=None, uid=None,
                body=body, user=USER))
        return out, calls

    def test_realm_follows_target_account_not_the_ui(self) -> None:
        (self.auth_dir / f'workbuddy-{UID}.json').write_text(
            json.dumps(_doc(UID, realm='global')), encoding='utf-8')
        # 界面停在国内版（realm=cn），目标账号是国际版
        out, calls = self._start({'uid': UID, 'realm': 'cn'})
        self.assertEqual(calls, ['global'], '重登没跟着账号的版本走')
        self.assertEqual(out['target']['uid'], UID)
        self.assertEqual(out['target']['realm'], 'global')

    def test_unknown_target_is_rejected_before_showing_a_qr(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            self._start({'uid': 'not-here'})
        self.assertEqual(ctx.exception.status_code, 404)


class AuthPollMismatchTest(_Base):
    """轮询阶段：扫到别的账号时**不落盘、不供给**，并如实报出来。"""

    def _poll(self, ready: dict, *, state: str = 'st-1') -> dict:
        (self.auth_dir / f'workbuddy-{UID}.json').write_text(
            json.dumps(_doc(UID)), encoding='utf-8')
        accounts._login_targets[state] = UID
        checkin = mock.AsyncMock(return_value=(0, 'ok'))

        async def fake_poll(state, realm=None):
            return ready

        with mock.patch.object(tencent, 'poll_login', fake_poll), \
             mock.patch.object(tencent, 'checkin', checkin), \
             mock.patch.object(accounts, '_save_and_finish') as save:
            out = asyncio.run(accounts.auth_poll(
                state=state, realm=None, region=None, upstream_id=None, user=USER))
        return out, save, checkin

    def test_other_account_is_reported_and_not_written(self) -> None:
        out, save, checkin = self._poll(_ready(OTHER))
        self.assertEqual(out['status'], 'account_mismatch')
        self.assertEqual(out['expected_uid'], UID)
        self.assertEqual(out['uid'], OTHER)
        save.assert_not_called()
        checkin.assert_not_called()
        # 原文件一个字节都没动
        raw = json.loads((self.auth_dir / f'workbuddy-{UID}.json').read_text(encoding='utf-8'))
        self.assertEqual(raw['auth']['accessToken'], 'old-' + UID)
        # state 上的记账也清干净了（这张码已被用掉，留着只会让前端继续轮询）
        self.assertNotIn('st-1', accounts._login_targets)

    def test_matching_account_proceeds(self) -> None:
        out, save, checkin = self._poll(_ready(UID))
        self.assertNotEqual(out.get('status'), 'account_mismatch')
        save.assert_called_once()
        checkin.assert_called_once()


class AuthPollReloginReviveTest(_Base):
    """重登成功后：解除禁用的结果必须回到界面（`revive` 字段）。"""

    def _poll(self, revive: dict) -> dict:
        (self.auth_dir / f'workbuddy-{UID}.json').write_text(
            json.dumps(_doc(UID)), encoding='utf-8')
        accounts._login_targets['st-2'] = UID

        async def fake_poll(state, realm=None):
            return _ready(UID)

        async def fake_revive(uid, group):
            return revive

        with mock.patch.object(tencent, 'poll_login', fake_poll), \
             mock.patch.object(tencent, 'checkin', mock.AsyncMock(return_value=(0, 'ok'))), \
             mock.patch.object(accounts, '_save_and_finish',
                               return_value={'status': 'success', 'uid': UID}), \
             mock.patch.object(accounts, '_revive_inline', fake_revive):
            return asyncio.run(accounts.auth_poll(
                state='st-2', realm=None, region=None, upstream_id=None, user=USER))

    def test_revive_result_is_returned_to_the_ui(self) -> None:
        payload = {'attempted': True, 'ok': False, 'still_disabled': True,
                   'message': '上游拒绝了鉴权（api_key 不一致）'}
        out = self._poll(payload)
        self.assertEqual(out['revive'], payload,
                         '解除禁用的结果没回给界面 —— 用户就只会看到「还在队列里」')


class ReviveInlineTest(_Base):
    """`_revive_inline` 的三态：已恢复 / 解不掉（带原因）/ 等不到（转后台）。"""

    def _run(self, disabled_seq: list, revive_result=(True, '已解除系统禁用', 'ok')) -> dict:
        seq = list(disabled_seq)
        revive = mock.AsyncMock(return_value=revive_result)

        async def fake_disabled(uid, **kw):
            return seq.pop(0) if len(seq) > 1 else seq[0]

        clock = {'t': 0.0}

        def fake_monotonic() -> float:
            return clock['t']

        async def fast_sleep(_s):
            clock['t'] += 10.0
            return None

        with mock.patch.object(wb2api, 'account_disabled', fake_disabled), \
             mock.patch.object(wb2api, 'revive_account', revive), \
             mock.patch.object(accounts.time, 'monotonic', fake_monotonic), \
             mock.patch.object(asyncio, 'sleep', fast_sleep), \
             mock.patch.object(accounts, '_schedule_revive_after_login') as bg:
            out = asyncio.run(accounts._revive_inline(UID, self.group))
        out['_bg'] = bg.called
        return out

    def test_healthy_account_is_a_success_no_op(self) -> None:
        out = self._run([False])
        self.assertTrue(out['ok'])
        self.assertFalse(out['still_disabled'])
        self.assertFalse(out['_bg'])

    def test_revive_success_clears_the_bit(self) -> None:
        # 三次读数：本函数看到「仍禁用」→ _revive_if_disabled 复查仍禁用 →
        # revive 之后再查一次，这次已回到池子。
        out = self._run([True, True, False])
        self.assertTrue(out['ok'])
        self.assertFalse(out['still_disabled'])

    def test_failed_revive_returns_the_reason(self) -> None:
        """解不掉时要带上原因（api_key / 管理接口 / 封禁），不能只说「失败」。"""
        out = self._run([True], revive_result=(False, '上游拒绝了鉴权（api_key 不一致）', 'error'))
        self.assertFalse(out['ok'])
        self.assertTrue(out['still_disabled'])
        self.assertIn('api_key', out['message'])

    def test_unknown_status_falls_back_to_background(self) -> None:
        out = self._run([None])
        self.assertFalse(out['ok'])
        self.assertTrue(out.get('pending'))
        self.assertTrue(out['_bg'], '等不到入池时必须交回后台继续，而不是直接放弃')


class SingleFilePerUidTest(_Base):
    """同一个 uid 只写一份文件：否则上游按文件名排序可能读到旧的那份。"""

    def test_relogin_writes_into_the_existing_file(self) -> None:
        legacy = self.auth_dir / 'workbuddy_new.json'
        legacy.write_text(json.dumps(_doc(UID)), encoding='utf-8')
        name, existed = tencent.write_auth_file(
            {'uid': UID, 'access_token': 'fresh', 'expires_at': 4102444800,
             'realm': 'cn', 'domain': 'copilot.tencent.com', 'nickname': '目标号'},
            self.auth_dir)
        self.assertTrue(existed)
        self.assertEqual(name, 'workbuddy_new.json', '又在旁边建了一份规范名的新文件')
        self.assertFalse((self.auth_dir / f'workbuddy-{UID}.json').exists())
        raw = json.loads(legacy.read_text(encoding='utf-8'))
        self.assertEqual(raw['auth']['accessToken'], 'fresh')

    def test_new_account_still_uses_the_canonical_name(self) -> None:
        name, existed = tencent.write_auth_file(
            {'uid': UID, 'access_token': 'fresh', 'expires_at': 4102444800,
             'realm': 'cn', 'domain': 'copilot.tencent.com'},
            self.auth_dir)
        self.assertFalse(existed)
        self.assertEqual(name, f'workbuddy-{UID}.json')

    def test_disabled_file_with_noncanonical_name_is_still_used(self) -> None:
        """非规范名 + 面板停用：仍然就地写它，不在旁边另建一份「在线」副本。"""
        name = 'workbuddy_new.json.disabled'
        (self.auth_dir / name).write_text(json.dumps(_doc(UID)), encoding='utf-8')
        got, existed = tencent.write_auth_file(
            {'uid': UID, 'access_token': 'fresh', 'expires_at': 4102444800,
             'realm': 'cn', 'domain': 'copilot.tencent.com'},
            self.auth_dir)
        self.assertTrue(existed)
        self.assertEqual(got, name)
        self.assertFalse((self.auth_dir / f'workbuddy-{UID}.json').exists())

    def test_disabled_sibling_is_still_used(self) -> None:
        """面板停用后的 `.disabled` 文件：重登就地写它，不另建一个「在线」副本。"""
        name = f'workbuddy-{UID}.json.disabled'
        (self.auth_dir / name).write_text(json.dumps(_doc(UID)), encoding='utf-8')
        got, existed = tencent.write_auth_file(
            {'uid': UID, 'access_token': 'fresh', 'expires_at': 4102444800,
             'realm': 'cn', 'domain': 'copilot.tencent.com'},
            self.auth_dir)
        self.assertTrue(existed)
        self.assertEqual(got, name)
        self.assertFalse((self.auth_dir / f'workbuddy-{UID}.json').exists())


if __name__ == '__main__':
    unittest.main()
