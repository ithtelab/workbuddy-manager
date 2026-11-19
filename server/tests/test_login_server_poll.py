"""扫码检测搬到服务端后，必须**不依赖前端轮询**（用户反馈 2026-09-26）。

现场（管理端 1.0.71 / Chrome / Windows）：点授权链接后把面板窗口盖住，回来发现
弹窗还停在「等待…」，要等 20-60 秒才更新。取证显示前端那个 `setInterval(tick, 2000)`
在 64 秒里只跑了 2 次 —— Chrome 对**被遮挡**的窗口会节流定时器，而此时
`document.hidden` 仍是 false、`visibilitychange` 一次都不触发，代码里那条
「切回来立即补一次」的兜底因此完全失效。

修法是把「问腾讯」搬到服务端：auth/start 之后由后台任务每 2 秒轮询一次，跑出终态
就存住，前端 /api/auth/poll 只来读结果。本文件钉住这份契约：

  1. 没有任何前端参与也能跑到终态（这正是整个修法的意义）；
  2. 终态结果直接返回，**不再问腾讯第二次**；
  3. 只对**终态**短路 —— 失败态仍走原路径，「重试即重新落盘」的语义不能丢；
  4. 同一个 state 只有一个后台轮询；用户重新发码时旧轮询要收掉；
  5. 国际版的地区（auth/start 带的或前端轮询补的）必须传进地区登记。
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.services import tencent  # noqa: E402

_USER = {'username': 'admin', 'role': 'admin'}

_READY = {
    'status': 'ready', 'uid': 'u-bg', 'nickname': '背景账号', 'enterprise_id': '',
    'access_token': 'AT', 'refresh_token': 'RT',
    'expires_at': 9999999999, 'domain': 'copilot.tencent.com', 'realm': 'cn',
}
_READY_GLOBAL = {**_READY, 'uid': 'u-gl', 'realm': 'global',
                 'domain': 'www.workbuddy.ai'}


class LoginPollTestCase(unittest.TestCase):
    """公共装置：临时 auths/db/users 路径 + 清掉模块级轮询状态。"""

    def setUp(self) -> None:
        from server.routers import accounts as accounts_mod

        self.accounts = accounts_mod
        tencent._state_cache.clear()
        accounts_mod._login_polls.clear()
        accounts_mod._login_tasks.clear()
        accounts_mod._login_regions.clear()
        accounts_mod._login_owner.clear()
        accounts_mod._provisioned_states.clear()

        self._tmp = tempfile.TemporaryDirectory()
        self._orig_auth = config.AUTH_DIR
        self._orig_db = config.DB_PATH
        self._orig_users = config.USERS_FILE
        config.AUTH_DIR = Path(self._tmp.name) / 'auths'
        config.DB_PATH = Path(self._tmp.name) / 'm.db'
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        from server import db
        db._conn = None
        db.connect()
        self._db = db

    def tearDown(self) -> None:
        for task in list(self.accounts._login_tasks.values()):
            task.cancel()
        self.accounts._login_tasks.clear()
        self.accounts._login_polls.clear()
        self.accounts._login_regions.clear()
        self.accounts._login_owner.clear()
        self.accounts._provisioned_states.clear()
        self.accounts._login_ready.clear()
        if self._db._conn is not None:
            self._db._conn.close()
        self._db._conn = None
        config.AUTH_DIR = self._orig_auth
        config.DB_PATH = self._orig_db
        config.USERS_FILE = self._orig_users
        self._tmp.cleanup()

    # ── 小工具 ────────────────────────────────────────────────────────
    def _run_poller(self, state: str, realm: str = 'cn',
                    region: str | None = None) -> mock.Mock:
        """跑一遍真实的后台轮询（间隔压到 10ms），返回 reload 的 patch。"""
        with mock.patch.object(self.accounts, '_LOGIN_POLL_SECONDS', 0.01), \
             mock.patch.object(tencent, 'checkin',
                               mock.AsyncMock(return_value=(0, 'ok'))), \
             mock.patch.object(tencent, 'write_auth_file',
                               return_value=('workbuddy-u-bg.json', False)), \
             mock.patch.object(self.accounts.reload,
                               'request_reload_or_restart') as rr:
            asyncio.run(self.accounts._poll_login_background(
                state, realm, region, None, _USER))
        return rr

    def _client(self):
        from fastapi.testclient import TestClient
        from server import security
        from server.main import app

        security.save_users({'secret': 'S', 'users': [
            {'username': 'admin', 'role': 'admin',
             'pwd_hash': security.make_hash('p')}], 'api_keys': []})
        c = TestClient(app)
        assert c.post('/api/login', json={'username': 'admin',
                                          'password': 'p'}).status_code == 200
        return c


class BackgroundPollerTest(LoginPollTestCase):
    def test_reaches_terminal_state_without_any_frontend_poll(self) -> None:
        """后端自己盯到终点 —— 前端一次都不轮询也要能检测出来。

        这是整个修法的核心：用户被遮挡时前端定时器几乎不跑，检测节奏不能再
        依赖它。这里把 poll_login 设为「两次 waiting 后 ready」，只跑后台任务。
        """
        calls: list[str] = []

        async def fake_poll(state, realm=None):
            calls.append(state)
            return dict(_READY) if len(calls) >= 3 else {'status': 'waiting'}

        with mock.patch.object(tencent, 'poll_login', fake_poll):
            rr = self._run_poller('s-bg')

        self.assertEqual(len(calls), 3, f'后台轮询次数异常：{len(calls)}')
        cached = self.accounts._login_polls.get('s-bg')
        self.assertIsNotNone(cached, '后台跑出了终态却什么都没存 —— 前端读不到结果')
        assert cached is not None
        self.assertEqual(cached['status'], 'success')
        self.assertEqual(cached['nickname'], '背景账号')
        self.assertAlmostEqual(float(cached['_at']), time.time(), delta=30,
                               msg='终态要带时间戳，TTL 清理靠它')
        rr.assert_called_once_with(
            'u-bg', upstream=mock.ANY)      # 落盘后的收尾（等热加载、超时才重启）

    def test_terminal_result_does_not_ask_tencent_again(self) -> None:
        """前端来读结果时不能再问腾讯第二次（这是「避免重复调用」的兑现）。

        用 TestClient 走真实路由：前端在 1.0.71 里每 2 秒打一次，若每次都转发给
        腾讯，多窗口叠加起来就是重复调用。
        """
        self.accounts._login_polls['s-done'] = {
            'status': 'success', 'uid': 'u-bg', 'nickname': '背景账号',
            'realm': 'cn', 'updated': False, 'file': 'workbuddy-u-bg.json',
            'region_note': '', '_at': time.time(),
        }

        async def boom(state, realm=None):        # pragma: no cover - 调用即失败
            raise AssertionError('终态已缓存，不该再问腾讯')

        with mock.patch.object(tencent, 'poll_login', boom):
            r = self._client().get('/api/auth/poll', params={'state': 's-done'})

        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body['status'], 'success')
        self.assertEqual(body['uid'], 'u-bg')
        self.assertNotIn('_at', body, '内部记账字段不该漏给前端')

    def test_only_terminal_state_short_circuits(self) -> None:
        """非终态照样走原路径 —— 否则「等待」会被当成结果返回。

        与上一条配成一对：只对终态短路，是这次改动的边界。
        """
        self.accounts._login_polls['s-wait'] = {'status': 'waiting',
                                                '_at': time.time()}
        calls: list[str] = []

        async def fake_poll(state, realm=None):
            calls.append(state)
            return dict(_READY)

        with mock.patch.object(tencent, 'poll_login', fake_poll), \
             mock.patch.object(tencent, 'checkin',
                               mock.AsyncMock(return_value=(0, 'ok'))), \
             mock.patch.object(tencent, 'write_auth_file',
                               return_value=('workbuddy-u-bg.json', False)), \
             mock.patch.object(self.accounts.reload,
                               'request_reload_or_restart'):
            out = asyncio.run(self.accounts.auth_poll(
                state='s-wait', realm='cn', region=None, upstream_id=None,
                user=_USER))

        self.assertEqual(calls, ['s-wait'], '非终态的缓存把腾讯查询短路了')
        self.assertEqual(out['status'], 'success')

    def test_expired_results_are_pruned(self) -> None:
        """终态结果有 TTL：state 是短命的，别让这几张表无限涨。"""
        self.accounts._login_polls['s-old'] = {
            'status': 'success', '_at': time.time() - self.accounts._LOGIN_RESULT_TTL - 1,
        }
        self.accounts._login_owner['s-old'] = 'admin|'
        self.accounts._login_regions['s-old'] = 'HK'
        self.accounts._prune_login_polls()
        self.assertNotIn('s-old', self.accounts._login_polls)
        self.assertNotIn('s-old', self.accounts._login_owner)
        self.assertNotIn('s-old', self.accounts._login_regions)


class SaveFailureRetryTest(LoginPollTestCase):
    def test_save_failure_is_not_cached_so_retry_still_saves(self) -> None:
        """落盘失败：不存终态，state 不丢，用户重试**只补落盘**。

        后台轮询复用 auth_poll，因此默认继承了 issue #26 的两条语义：失败时
        保留 state（重试不该被引导去重新扫码）、且重试不重跑签到等副作用。
        """
        tencent._state_cache['s-fail'] = (time.time(), 'cn')

        async def fake_poll(state, realm=None):
            return dict(_READY)

        checkin = mock.AsyncMock(return_value=(0, 'ok'))
        with mock.patch.object(self.accounts, '_LOGIN_POLL_SECONDS', 0.01), \
             mock.patch.object(tencent, 'poll_login', fake_poll), \
             mock.patch.object(tencent, 'checkin', checkin), \
             mock.patch.object(tencent, 'write_auth_file',
                               side_effect=PermissionError('permission denied')), \
             mock.patch.object(self.accounts.reload,
                               'request_reload_or_restart'):
            asyncio.run(self.accounts._poll_login_background(
                's-fail', 'cn', None, None, _USER))

        self.assertNotIn('s-fail', self.accounts._login_polls,
                         '落盘失败被当成终态存住了 —— 前端会看到成功，账号却没进来')
        self.assertTrue(tencent.is_pending('s-fail'),
                        '落盘失败后 state 被丢了 —— 用户重试会看到「二维码已失效」')

        # 用户点「重试」：前端再轮询一次，这次落盘成功
        with mock.patch.object(tencent, 'poll_login', fake_poll), \
             mock.patch.object(tencent, 'checkin', checkin), \
             mock.patch.object(tencent, 'write_auth_file',
                               return_value=('workbuddy-u-bg.json', False)), \
             mock.patch.object(self.accounts.reload,
                               'request_reload_or_restart'):
            out = asyncio.run(self.accounts.auth_poll(
                state='s-fail', realm='cn', region=None, upstream_id=None,
                user=_USER))

        self.assertEqual(out['status'], 'success', out)
        self.assertEqual(checkin.call_count, 1, '重试又签到了一次（会重复写记录）')
        self.assertFalse(tencent.is_pending('s-fail'), '成功落盘后 state 应被丢弃')


class DuplicateStartTest(LoginPollTestCase):
    def test_one_poller_per_state_and_old_ones_are_reaped(self) -> None:
        """同一个 state 只起一个后台轮询；重新发码时把旧轮询收掉。

        前端在弹窗里改地区会重新申请（旧的 state 就此作废），不收的话每个废码
        都有一个轮询替它问腾讯 5 分钟 —— 多窗口叠加更糟。
        """
        seen: list[tuple] = []
        # 真实 start_login 每次都会给出**新的** state；这里手动切换，用来模拟
        # 「前端 effect 重跑拿到同一张码」与「用户改地区后重新发码」两种情形。
        current = {'state': 'state-a'}

        async def fake_start(realm='cn'):
            return {'state': current['state'], 'authUrl': 'https://x/y', 'realm': realm}

        async def fake_poller(state, realm, region, upstream_id, user):
            seen.append((state, realm, region, upstream_id))
            await asyncio.Event().wait()          # 永不返回：模拟还在盯着

        async def scenario() -> None:
            with mock.patch.object(tencent, 'start_login', fake_start), \
                 mock.patch.object(self.accounts, '_poll_login_background',
                                   fake_poller):
                # 第一次：国内版，没选地区
                await self.accounts.auth_start(realm='cn', region=None,
                                               upstream_id=None, body=None,
                                               user=_USER)
                # 让刚起的后台任务真正跑起来（交给事件循环一次）——
                # 「还在跑」才是下面那条去重判断要覆盖的状态
                await asyncio.sleep(0)
                self.assertEqual(len(self.accounts._login_tasks), 1)
                self.assertIn('state-a', self.accounts._login_tasks)

                # 再来一次一模一样的（前端 effect 重跑/用户连点）：仍只有一个
                await self.accounts.auth_start(realm='cn', region=None,
                                               upstream_id=None,
                                               body={'realm': 'cn'}, user=_USER)
                await asyncio.sleep(0)
                self.assertEqual(len(self.accounts._login_tasks), 1,
                                 '同一个 state 起了两个后台轮询')

                # 用户改成国际版 + 地区 HK：旧码作废，旧轮询要被收掉
                current['state'] = 'state-b'
                await self.accounts.auth_start(
                    realm='global', region=None, upstream_id=None,
                    body={'realm': 'global', 'region': 'HK'}, user=_USER)
                self.assertNotIn('state-a', self.accounts._login_tasks,
                                 '旧码的后台轮询没收掉（会一直问腾讯到超时）')
                self.assertIn('state-b', self.accounts._login_tasks)
                await asyncio.sleep(0)
                for task in list(self.accounts._login_tasks.values()):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

        asyncio.run(scenario())
        self.assertEqual(seen, [('state-a', 'cn', None, None),
                                ('state-b', 'global', 'HK', None)],
                         '后台轮询拿到的参数不对（realm/地区要传对）')


class RegionPlumbingTest(LoginPollTestCase):
    """国际版：地区登记必须在**落盘前**完成，否则聊天报 14017。

    地区有两个来源，两条都要能到后台轮询手里：
      · auth/start 的 body（用户在弹窗里选好后会重新发码，这条是主路径）；
      · /auth/poll 的 query（另一扇窗口/更早的前端版本会带上来）。
    """

    def test_poller_uses_region_from_start(self) -> None:
        submit = mock.AsyncMock(return_value=(True, '地区已提交'))
        with mock.patch.object(tencent, 'poll_login',
                               mock.AsyncMock(return_value=dict(_READY_GLOBAL))), \
             mock.patch.object(tencent, 'submit_region', submit), \
             mock.patch.object(tencent, 'claim_trial',
                               mock.AsyncMock(return_value=(True, '已领取'))), \
             mock.patch.object(self.accounts, '_LOGIN_POLL_SECONDS', 0.01), \
             mock.patch.object(tencent, 'checkin',
                               mock.AsyncMock(return_value=(0, 'ok'))), \
             mock.patch.object(tencent, 'write_auth_file',
                               return_value=('workbuddy-u-gl.json', False)), \
             mock.patch.object(self.accounts.reload,
                               'request_reload_or_restart'):
            asyncio.run(self.accounts._poll_login_background(
                's-gl', 'global', 'HK', None, _USER))

        self.assertTrue(submit.called, '国际版没做地区登记 —— 新号聊天会报 14017')
        self.assertEqual(submit.call_args[0][1], 'HK')

    def test_poller_picks_up_region_that_arrives_later(self) -> None:
        """/auth/poll 带来的地区也要记下来给后台用（auth_start 时可能还没选）。"""
        self.accounts._login_regions['s-gl2'] = 'SG'
        submit = mock.AsyncMock(return_value=(True, '地区已提交'))
        with mock.patch.object(tencent, 'poll_login',
                               mock.AsyncMock(return_value=dict(_READY_GLOBAL))), \
             mock.patch.object(tencent, 'submit_region', submit), \
             mock.patch.object(tencent, 'claim_trial',
                               mock.AsyncMock(return_value=(True, '已领取'))), \
             mock.patch.object(self.accounts, '_LOGIN_POLL_SECONDS', 0.01), \
             mock.patch.object(tencent, 'checkin',
                               mock.AsyncMock(return_value=(0, 'ok'))), \
             mock.patch.object(tencent, 'write_auth_file',
                               return_value=('workbuddy-u-gl.json', False)), \
             mock.patch.object(self.accounts.reload,
                               'request_reload_or_restart'):
            asyncio.run(self.accounts._poll_login_background(
                's-gl2', 'global', None, None, _USER))

        self.assertEqual(submit.call_args[0][1], 'SG',
                         '后台轮询没读后来的地区（国际版会漏登记）')

    def test_poll_route_remembers_region(self) -> None:
        """前端轮询带的地区要落到 state 上，供后台轮询下一轮读取。"""
        async def fake_poll(state, realm=None):
            return {'status': 'waiting'}

        with mock.patch.object(tencent, 'poll_login', fake_poll):
            out = asyncio.run(self.accounts.auth_poll(
                state='s-gl3', realm='global', region='TH', upstream_id=None,
                user=_USER))

        self.assertEqual(out['status'], 'waiting')
        self.assertEqual(self.accounts._login_regions.get('s-gl3'), 'TH')


class SaveFailureRetryTest(LoginPollTestCase):
    """落盘失败后的重试**不能再依赖腾讯**（issue #146）。

    报障：国际版扫码后弹窗一直「等待中」，日志里却在刷 200 OK。根因是后台轮询
    拿到 ready 之后落盘失败（auths 目录权限不对），而「重试」这条路径又回去问
    腾讯 —— 那边这次拿到的是「等待中」，于是前端永远等不到结果，也看不到那句
    「去修目录权限、修好直接重试」的可执行提示。

    修法：拿到 ready 就把结果记下来，重试只重做落盘。这三条分别钉住：
    失败仍如实报错、重试不再问腾讯、权限修好后能自己走通。
    """

    _BOOM = PermissionError(1, 'Operation not permitted', '/opt/workbuddy2api/auths/.x.json.ab.tmp')

    def _poll(self, state: str = 's-savefail'):
        return self._client().get('/api/auth/poll', params={'state': state})

    def test_retry_after_save_failure_still_reports_it(self) -> None:
        calls = {'n': 0}

        async def fake_poll(state, realm=None):
            calls['n'] += 1
            # 第一次给 ready；之后模拟「腾讯不再报 ready」——这正是报障现场
            return dict(_READY) if calls['n'] == 1 else {'status': 'waiting'}

        with mock.patch.object(tencent, 'poll_login', fake_poll),              mock.patch.object(tencent, 'checkin',
                               mock.AsyncMock(return_value=(0, 'ok'))),              mock.patch.object(tencent, 'write_auth_file', side_effect=self._BOOM):
            first = self._poll()
            second = self._poll()

        self.assertEqual(first.status_code, 500, first.text)
        self.assertIn('目录权限', first.json()['detail'])
        self.assertEqual(second.status_code, 500,
                         '第二次掉回了「等待中」——用户会一直等下去（#146 的现象）')
        self.assertIn('目录权限', second.json()['detail'])
        self.assertEqual(calls['n'], 1, '重试不该再问腾讯第二次')

    def test_recovery_once_permissions_are_fixed(self) -> None:
        """修好权限后，前端再轮询一次就应当成功（提示里承诺的就是这个）。"""
        state = 's-recover'
        boom_then_ok = [self._BOOM]

        def write(account, auth_dir=None):  # noqa: ANN001
            if boom_then_ok:
                boom_then_ok.pop()
                raise self._BOOM
            return ('workbuddy-u-bg.json', False)

        with mock.patch.object(tencent, 'poll_login',
                               mock.AsyncMock(return_value=dict(_READY))),              mock.patch.object(tencent, 'checkin',
                               mock.AsyncMock(return_value=(0, 'ok'))),              mock.patch.object(tencent, 'write_auth_file', side_effect=write),              mock.patch.object(self.accounts.reload, 'request_reload_or_restart'):
            failed = self._poll(state)
            recovered = self._poll(state)

        self.assertEqual(failed.status_code, 500)
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()['status'], 'success')


if __name__ == '__main__':
    unittest.main()
