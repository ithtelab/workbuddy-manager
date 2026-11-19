"""令牌刷新计划（计划任务：每 N 天全量刷一遍）。

需求：用户有账号 7 天后到期，希望「每 5 天自动把所有账号刷一遍」，不必手动点。
这与既有的自动续期是**两条判据不同**的路径：

  · `renew_once`：按剩余寿命，`< 3 天`才刷（覆盖长期闲置号，避免过期）；
  · 计划任务：按**日历间隔**，不看剩多少天，到点全量刷。

这里钉住的几条：

  1. 配置存 db（本端调度意图，不写进上游 config.json），脏值要能收敛；
  2. `set_plan` 校验类型/范围，且**保留 `last_run`**（改间隔不该触发立刻重刷）；
  3. 是否到点：disabled / 整点没对上 / 间隔没满 → 不跑；从未跑过 → 到下一个整点就跑；
  4. `force=True` 无条件刷所有账号，`force=False` 仍只刷快过期的；
  5. 面板改名停用的账号在 force 模式下也跳过。
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.services import renew  # noqa: E402


def _ts(y: int, m: int, d: int, h: int, mi: int = 0) -> int:
    return int(datetime.datetime(y, m, d, h, mi).timestamp())


class _PlanStore:
    """内存版 settings：不碰真实 manager.db。"""

    def __init__(self, initial: dict | None = None) -> None:
        self.store: dict = dict(initial or {})

    def get_setting(self, key: str, default=None):
        return self.store.get(key, default)

    def set_setting(self, key: str, value) -> None:
        self.store[key] = value


def _patch_db(initial: dict | None = None):
    store = _PlanStore(initial)
    return mock.patch.multiple(renew.db, get_setting=store.get_setting,
                               set_setting=store.set_setting), store


class PlanConfigTest(unittest.TestCase):
    def test_defaults_when_unset(self) -> None:
        patcher, _ = _patch_db()
        with patcher:
            plan = renew.get_plan()
        self.assertEqual(
            (plan['enabled'], plan['interval_days'], plan['hour'], plan['last_run']),
            (False, 5, 3, 0),
        )

    def test_corrupt_values_are_normalised(self) -> None:
        """库里的值可能被手改或来自旧版本：逐项兜底，不能让它把调度带崩。"""
        patcher, _ = _patch_db({'token_refresh_plan': {
            'enabled': 'yes', 'interval_days': 0, 'hour': 99, 'last_run': -5}})
        with patcher:
            plan = renew.get_plan()
        self.assertFalse(plan['enabled'])
        self.assertEqual(plan['interval_days'], 5)
        self.assertEqual(plan['hour'], 3)
        self.assertEqual(plan['last_run'], 0)

    def test_set_plan_validates_types(self) -> None:
        patcher, _ = _patch_db()
        with patcher:
            for args in ((1, 5, 3), (True, True, 3), (True, 5, '3')):
                with self.subTest(args=args), self.assertRaises(TypeError):
                    renew.set_plan(*args)

    def test_set_plan_validates_range(self) -> None:
        patcher, _ = _patch_db()
        with patcher:
            for args in ((True, 0, 3), (True, 366, 3), (True, 5, 24)):
                with self.subTest(args=args), self.assertRaises(ValueError):
                    renew.set_plan(*args)

    def test_set_plan_preserves_last_run(self) -> None:
        """改间隔不该把「上次跑过」抹掉，否则调完设置会被判成逾期、立刻重刷。"""
        patcher, _ = _patch_db()
        with patcher:
            renew.mark_plan_ran(now=_ts(2026, 1, 1, 3))
            plan = renew.set_plan(True, 5, 4)
        self.assertEqual(plan['last_run'], _ts(2026, 1, 1, 3))


class PlanDueTest(unittest.TestCase):
    def _plan(self, **kw) -> dict:
        base = {'enabled': True, 'interval_days': 5, 'hour': 3, 'last_run': 0}
        base.update(kw)
        return base

    def test_disabled_never_due(self) -> None:
        self.assertFalse(renew.plan_is_due(self._plan(enabled=False),
                                           now=_ts(2026, 1, 10, 3)))

    def test_only_at_configured_hour(self) -> None:
        self.assertFalse(renew.plan_is_due(self._plan(hour=3), now=_ts(2026, 1, 10, 2)))
        self.assertTrue(renew.plan_is_due(self._plan(hour=3), now=_ts(2026, 1, 10, 3)))

    def test_never_run_is_due_at_configured_hour(self) -> None:
        """从未跑过只等下一个整点，不立刻打一轮腾讯。"""
        self.assertFalse(renew.plan_is_due(self._plan(last_run=0), now=_ts(2026, 1, 10, 2)))
        self.assertTrue(renew.plan_is_due(self._plan(last_run=0), now=_ts(2026, 1, 10, 3)))

    def test_interval_not_elapsed(self) -> None:
        now = _ts(2026, 1, 10, 3)
        self.assertFalse(renew.plan_is_due(self._plan(last_run=now - 3 * 86400), now=now))

    def test_interval_elapsed(self) -> None:
        now = _ts(2026, 1, 10, 3)
        self.assertTrue(renew.plan_is_due(self._plan(last_run=now - 6 * 86400), now=now))


class PlanNextRunTest(unittest.TestCase):
    def _plan(self, **kw) -> dict:
        base = {'enabled': True, 'interval_days': 5, 'hour': 3, 'last_run': 0}
        base.update(kw)
        return base

    def test_disabled_returns_none(self) -> None:
        self.assertIsNone(renew.plan_next_run(self._plan(enabled=False)))

    def test_before_hour_is_today(self) -> None:
        got = renew.plan_next_run(self._plan(), now=_ts(2026, 1, 10, 1))
        self.assertEqual(got, _ts(2026, 1, 10, 3))

    def test_after_hour_is_tomorrow(self) -> None:
        got = renew.plan_next_run(self._plan(), now=_ts(2026, 1, 10, 5))
        self.assertEqual(got, _ts(2026, 1, 11, 3))

    def test_waits_full_interval_after_last_run(self) -> None:
        plan = self._plan(last_run=_ts(2026, 1, 10, 3))
        got = renew.plan_next_run(plan, now=_ts(2026, 1, 12, 0))
        self.assertEqual(got, _ts(2026, 1, 15, 3))


class RefreshAllForceTest(unittest.TestCase):
    """`force=True` 无条件刷新；默认仍只刷快过期的。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._orig = config.AUTH_DIR
        config.AUTH_DIR = self.dir

    def tearDown(self) -> None:
        config.AUTH_DIR = self._orig
        with contextlib.suppress(PermissionError):
            self._tmp.cleanup()

    def _write(self, uid: str, *, expires_in: int, refresh: str = 'rt',
               disabled: bool = False) -> str:
        name = f'workbuddy-{uid}.json' + ('.disabled' if disabled else '')
        (self.dir / name).write_text(json.dumps({
            'account': {'uid': uid, 'nickname': uid, 'enterpriseId': 'e'},
            'auth': {'accessToken': 'at', 'refreshToken': refresh,
                     'expiresAt': int(time.time()) + expires_in,
                     'domain': '', 'realm': 'cn'},
        }), encoding='utf-8')
        return name

    def _run(self, *, force: bool) -> dict:
        async def fake_refresh(payload):
            return True, '刷新成功', {
                'access_token': 'new', 'expires_at': int(time.time()) + 100}

        with mock.patch.object(renew.tencent, 'refresh_token', fake_refresh), \
             mock.patch.object(renew.tencent, 'update_auth_tokens'), \
             mock.patch.object(renew, '_record'):
            return asyncio.run(renew.refresh_all(force=force))

    def test_default_skips_fresh_accounts(self) -> None:
        self._write('fresh001', expires_in=50 * 86400)
        self.assertEqual(self._run(force=False)['checked'], 0)

    def test_force_refreshes_fresh_accounts(self) -> None:
        """计划任务的核心：剩 50 天也照刷（与「快过期才刷」判据不同）。"""
        self._write('fresh001', expires_in=50 * 86400)
        stats = self._run(force=True)
        self.assertEqual(stats['checked'], 1)
        self.assertEqual(stats['renewed'], 1)

    def test_force_still_skips_panel_disabled(self) -> None:
        self._write('paused01', expires_in=50 * 86400, disabled=True)
        stats = self._run(force=True)
        self.assertEqual(stats['skipped'], 1)
        self.assertEqual(stats['checked'], 0)

    def test_renew_once_delegates_with_force_false(self) -> None:
        """保留的 renew_once 语义不变：等价 `refresh_all(force=False)`。"""
        seen: dict = {}

        async def fake_all(force: bool = False, now=None) -> dict:
            seen['force'] = force
            return {'checked': 0, 'renewed': 0, 'failed': 0, 'skipped': 0}

        with mock.patch.object(renew, 'refresh_all', fake_all):
            asyncio.run(renew.renew_once())
        self.assertFalse(seen['force'])


class RunPlanEndpointTest(unittest.TestCase):
    """「立即执行一次」：force 全量 + 重置计时，返回统计。"""

    def test_run_now_marks_plan_ran(self) -> None:
        from server.routers import accounts

        seen: dict = {}

        async def fake_all(force: bool = False, now=None) -> dict:
            seen['force'] = force
            return {'checked': 2, 'renewed': 2, 'failed': 0, 'skipped': 0}

        def fake_mark(now=None) -> dict:
            seen['marked'] = True
            return {'enabled': True, 'interval_days': 5, 'hour': 3, 'last_run': 123}

        with mock.patch.object(accounts.renew, 'refresh_all', fake_all), \
             mock.patch.object(accounts.renew, 'mark_plan_ran', fake_mark), \
             mock.patch.object(accounts.renew, 'get_plan',
                               lambda: {'enabled': True, 'interval_days': 5,
                                        'hour': 3, 'last_run': 123}), \
             mock.patch.object(accounts.renew, 'plan_next_run',
                               lambda plan=None, now=None: 999):
            out = asyncio.run(accounts.run_refresh_plan_now(user={'role': 'admin'}))

        self.assertTrue(seen.get('force'), '立即执行必须是全量刷新（force=True）')
        self.assertTrue(seen.get('marked'), '跑完要重置计时，免得到点又跑一次')
        self.assertEqual(out['renewed'], 2)
        self.assertEqual(out['next_run'], 999)


if __name__ == '__main__':
    unittest.main()
