"""自动更新（设置 → 更新）的回归测试：配置校验、时间窗与「该不该动手」的判定。"""
from __future__ import annotations

import time
import unittest
from unittest import mock

from server.services import autoupdate


def _check(**manager) -> dict:
    base = {'latest': 'v9.9.9', 'has_update': True, 'assets_ready': True,
            'error': '', 'stale': False, 'upstream_changed': False}
    base.update(manager)
    return {'manager': base}


def _cfg(**over) -> dict:
    cfg = autoupdate.default_config()
    cfg.update(enabled=True, window_start=3, window_end=6)
    cfg.update(over)
    return cfg


class ConfigTest(unittest.TestCase):
    def test_default_is_off(self) -> None:
        self.assertFalse(autoupdate.default_config()['enabled'])

    def test_normalize_clamps_and_drops_unknown(self) -> None:
        cfg = autoupdate.normalize_config({
            'enabled': 1, 'interval_hours': 9999, 'window_start': -3,
            'window_end': 40, 'target': 'evil', 'extra': 'x'})
        self.assertEqual(cfg, {'enabled': True, 'interval_hours': 168,
                               'window_start': 0, 'window_end': 23, 'target': 'auto'})

    def test_string_false_does_not_turn_it_on(self) -> None:
        # 表单/HTTP 客户端把布尔写成字符串时，"false" 是 truthy；直接 bool() 会把
        # 「关闭自动更新」变成「开启」（它会让面板无人值守重启）。
        self.assertFalse(autoupdate.normalize_config({'enabled': 'false'})['enabled'])
        self.assertFalse(autoupdate.normalize_config({'enabled': '0'})['enabled'])
        self.assertFalse(autoupdate.normalize_config({'enabled': ''})['enabled'])
        self.assertTrue(autoupdate.normalize_config({'enabled': 'true'})['enabled'])
        self.assertTrue(autoupdate.normalize_config({'enabled': 1})['enabled'])
        # 认不出来的值退回默认（关），不是「非空即真」
        self.assertFalse(autoupdate.normalize_config({'enabled': 'maybe'})['enabled'])
        self.assertFalse(autoupdate.normalize_config({'enabled': 2})['enabled'])

    def test_validate_rejects_out_of_range(self) -> None:
        ok = {'enabled': True, 'interval_hours': 6, 'window_start': 3,
              'window_end': 6, 'target': 'manager'}
        self.assertIsNotNone(autoupdate.validate_config(ok)[0])
        for bad in ({**ok, 'interval_hours': 0}, {**ok, 'window_end': 24},
                    {**ok, 'target': 'upstream'}, 'nope'):
            cfg, err = autoupdate.validate_config(bad)
            self.assertIsNone(cfg)
            self.assertTrue(err)


class WindowTest(unittest.TestCase):
    def test_same_start_end_means_all_day(self) -> None:
        self.assertTrue(all(autoupdate.in_window(h, 4, 4) for h in range(24)))

    def test_plain_window_is_half_open(self) -> None:
        self.assertTrue(autoupdate.in_window(3, 3, 6))
        self.assertTrue(autoupdate.in_window(5, 3, 6))
        self.assertFalse(autoupdate.in_window(6, 3, 6))
        self.assertFalse(autoupdate.in_window(2, 3, 6))

    def test_window_across_midnight(self) -> None:
        self.assertTrue(autoupdate.in_window(23, 22, 2))
        self.assertTrue(autoupdate.in_window(1, 22, 2))
        self.assertFalse(autoupdate.in_window(2, 22, 2))
        self.assertFalse(autoupdate.in_window(12, 22, 2))


class DecideTest(unittest.TestCase):
    NOW = 1_800_000_000.0

    def _decide(self, check: dict, *, cfg: dict | None = None, hour: int = 4,
                running: bool = False, state: dict | None = None,
                docker_ok: bool = True, busy: bool = False) -> tuple[str, str, str]:
        return autoupdate.decide(cfg or _cfg(), check, hour=hour, running=running,
                                 state=state or {}, now=self.NOW, docker_ok=docker_ok,
                                 busy=busy)

    def test_updates_manager_inside_window(self) -> None:
        self.assertEqual(self._decide(_check())[:2], ('update', 'manager'))

    def test_includes_upstream_only_when_changed_and_docker_ok(self) -> None:
        self.assertEqual(self._decide(_check(upstream_changed=True))[:2], ('update', 'both'))
        self.assertEqual(self._decide(_check(upstream_changed=True), docker_ok=False)[:2],
                         ('update', 'manager'))
        self.assertEqual(
            self._decide(_check(upstream_changed=True), cfg=_cfg(target='manager'))[:2],
            ('update', 'manager'))

    def test_waits_outside_window(self) -> None:
        self.assertEqual(self._decide(_check(), hour=12)[0], 'wait')

    def test_never_updates_on_untrusted_check(self) -> None:
        self.assertEqual(self._decide(_check(error='HTTP 403'))[0], 'none')
        self.assertEqual(self._decide(_check(stale=True))[0], 'none')
        self.assertEqual(self._decide(_check(has_update=False))[0], 'none')

    def test_waits_for_assets_and_running_update(self) -> None:
        self.assertEqual(self._decide(_check(assets_ready=False))[0], 'wait')
        self.assertEqual(self._decide(_check(), running=True)[0], 'wait')
        # 302 回退拿不到资产清单（assets_ready=None）不算「未就位」
        self.assertEqual(self._decide(_check(assets_ready=None))[0], 'update')

    def test_busy_tasks_defer_update_inside_window(self) -> None:
        # 更新会重启面板，正在跑的签到/领奖会被打断；先等它结束。
        self.assertEqual(self._decide(_check(), busy=True)[0], 'wait')
        self.assertEqual(self._decide(_check())[0], 'update')
        # 时间窗与忙碌同时成立时，先说「在等时间窗」（主判据）
        action, _, reason = self._decide(_check(), hour=12, busy=True)
        self.assertEqual(action, 'wait')
        self.assertIn('等待时间窗', reason)

    def test_no_rapid_retry_of_same_failed_version(self) -> None:
        recent = {'last_attempt_version': 'v9.9.9', 'last_attempt_at': self.NOW - 3600}
        self.assertEqual(self._decide(_check(), state=recent)[0], 'wait')
        old = {'last_attempt_version': 'v9.9.9', 'last_attempt_at': self.NOW - 25 * 3600}
        self.assertEqual(self._decide(_check(), state=old)[0], 'update')
        other = {'last_attempt_version': 'v9.9.8', 'last_attempt_at': self.NOW - 60}
        self.assertEqual(self._decide(_check(), state=other)[0], 'update')


class RunOnceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings: dict = {}
        patches = [
            mock.patch.object(autoupdate.db, 'get_setting',
                              side_effect=lambda k, d=None: self.settings.get(k, d)),
            mock.patch.object(autoupdate.db, 'set_setting',
                              side_effect=lambda k, v: self.settings.__setitem__(k, v)),
            mock.patch.object(autoupdate.security, 'audit'),
            mock.patch.object(autoupdate.updater, 'current_version', return_value='v1.0.0'),
            mock.patch.object(autoupdate.updater, 'read_status', return_value={}),
            mock.patch.object(autoupdate.updater, 'can_control_docker', return_value=False),
            # 账号任务状态来自 taskrun（真实实现会读库）；这里固定为「没有任务在跑」
            mock.patch.object(autoupdate, '_tasks_busy', return_value=False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.check = mock.patch.object(autoupdate.updater, 'check_updates',
                                       return_value=_check())
        self.start = mock.patch.object(autoupdate.updater, 'start_update',
                                       return_value=(True, 'started'))
        self.check_mock = self.check.start()
        self.start_mock = self.start.start()
        self.addCleanup(self.check.stop)
        self.addCleanup(self.start.stop)

    def _now_at_hour(self, hour: int) -> float:
        lt = time.localtime()
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 30, 0, 0, 0, -1))

    def test_disabled_does_nothing(self) -> None:
        autoupdate.run_once(self._now_at_hour(4))
        self.check_mock.assert_not_called()
        self.start_mock.assert_not_called()

    def test_enabled_in_window_starts_update_once(self) -> None:
        self.settings[autoupdate.CONFIG_KEY] = _cfg()
        now = self._now_at_hour(4)
        state = autoupdate.run_once(now)
        self.start_mock.assert_called_once_with('manager')
        self.assertEqual(state['last_action'], 'started')
        # 下一轮（未到检测间隔、也没有待更新版本）不应再打 GitHub
        self.check_mock.reset_mock()
        autoupdate.run_once(now + 300)
        self.check_mock.assert_not_called()

    def test_pending_version_waits_then_updates_when_window_opens(self) -> None:
        self.settings[autoupdate.CONFIG_KEY] = _cfg()
        state = autoupdate.run_once(self._now_at_hour(1))
        self.assertEqual(state['last_action'], 'wait')
        self.assertEqual(state['pending_version'], 'v9.9.9')
        self.start_mock.assert_not_called()
        # 时间窗打开后（检测间隔未到），用缓存结果即可动手
        state = autoupdate.run_once(self._now_at_hour(3))
        self.check_mock.assert_called_with(force=False)
        self.start_mock.assert_called_once_with('manager')

    def test_failed_check_retries_in_an_hour_not_after_interval(self) -> None:
        # 间隔最大 168 小时：一次网络抖动不能让自动更新停摆一周。
        self.settings[autoupdate.CONFIG_KEY] = _cfg(interval_hours=168)
        self.check_mock.return_value = _check(error='HTTP 403')
        now = self._now_at_hour(4)
        state = autoupdate.run_once(now)
        self.assertEqual(state['next_check_at'], now + autoupdate.ERROR_RETRY_SECONDS)
        # 一小时后重新强制检测（不是继续用 6 小时缓存）
        self.check_mock.reset_mock()
        autoupdate.run_once(now + autoupdate.ERROR_RETRY_SECONDS)
        self.check_mock.assert_called_with(force=True)

    def test_successful_check_schedules_by_interval(self) -> None:
        self.settings[autoupdate.CONFIG_KEY] = _cfg(interval_hours=6)
        now = self._now_at_hour(1)
        state = autoupdate.run_once(now)
        self.assertEqual(state['next_check_at'], now + 6 * 3600)

    def test_nothing_happens_while_account_tasks_run(self) -> None:
        self.settings[autoupdate.CONFIG_KEY] = _cfg()
        with mock.patch.object(autoupdate, '_tasks_busy', return_value=True):
            state = autoupdate.run_once(self._now_at_hour(4))
        self.start_mock.assert_not_called()
        self.assertEqual(state['last_action'], 'wait')
        self.assertEqual(state['pending_version'], 'v9.9.9')

    def test_start_failure_is_recorded(self) -> None:
        self.settings[autoupdate.CONFIG_KEY] = _cfg()
        self.start_mock.return_value = (False, '已有更新任务正在执行')
        state = autoupdate.run_once(self._now_at_hour(4))
        self.assertEqual(state['last_action'], 'error')
        self.assertEqual(state['last_message'], '已有更新任务正在执行')
        self.assertEqual(state['last_attempt_version'], 'v9.9.9')

    def test_windows_native_is_skipped_without_trying(self) -> None:
        self.settings[autoupdate.CONFIG_KEY] = _cfg()
        with mock.patch.object(autoupdate.updater.os, 'name', 'nt'), \
                mock.patch.object(autoupdate.updater.config, 'WB2API_MODE', 'native'):
            state = autoupdate.run_once(self._now_at_hour(4))
        self.assertEqual(state['last_action'], 'unsupported')
        self.check_mock.assert_not_called()
        self.start_mock.assert_not_called()


class UnsupportedReasonTest(unittest.TestCase):
    def test_windows_native_unsupported(self) -> None:
        with mock.patch.object(autoupdate.updater.os, 'name', 'nt'), \
                mock.patch.object(autoupdate.updater.config, 'WB2API_MODE', 'native'):
            self.assertTrue(autoupdate.unsupported_reason())

    def test_windows_docker_and_linux_supported(self) -> None:
        with mock.patch.object(autoupdate.updater.os, 'name', 'nt'), \
                mock.patch.object(autoupdate.updater.config, 'WB2API_MODE', 'docker'):
            self.assertEqual(autoupdate.unsupported_reason(), '')
        with mock.patch.object(autoupdate.updater.os, 'name', 'posix'), \
                mock.patch.object(autoupdate.updater.config, 'WB2API_MODE', 'native'):
            self.assertEqual(autoupdate.unsupported_reason(), '')


    def test_same_predicate_as_one_click_update(self) -> None:
        """自动更新与一键更新必须读同一个判据，避免新增部署形态时两处漂移。"""
        from server.services import updater
        for os_name, mode, expected in (('nt', 'native', True), ('nt', 'docker', False),
                                        ('posix', 'native', False), ('posix', 'systemd', False)):
            with mock.patch.object(updater.os, 'name', os_name),                     mock.patch.object(updater.config, 'WB2API_MODE', mode):
                self.assertEqual(bool(autoupdate.unsupported_reason()), expected,
                                 f'{os_name}/{mode}')


class SaveEndpointTest(unittest.TestCase):
    def test_enabling_is_rejected_when_unsupported(self) -> None:
        from fastapi import HTTPException
        from server.routers import settings as settings_router
        with mock.patch.object(autoupdate, 'unsupported_reason', return_value='不支持'), \
                mock.patch.object(autoupdate, 'save_config') as save:
            with self.assertRaises(HTTPException) as ctx:
                settings_router.save_auto_update(_cfg(), user={'username': 'admin'})
        self.assertEqual(ctx.exception.status_code, 409)
        save.assert_not_called()

    def test_disabling_still_allowed_when_unsupported(self) -> None:
        from server.routers import settings as settings_router
        cfg = {**_cfg(), 'enabled': False}
        with mock.patch.object(autoupdate, 'unsupported_reason', return_value='不支持'), \
                mock.patch.object(autoupdate, 'save_config', return_value=cfg), \
                mock.patch.object(settings_router.security, 'audit'), \
                mock.patch.object(settings_router, '_auto_update_payload', return_value={}):
            settings_router.save_auto_update(cfg, user={'username': 'admin'})


if __name__ == '__main__':
    unittest.main()
