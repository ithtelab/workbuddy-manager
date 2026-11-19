"""issue #329：非 docker 部署下的「清除冷却」。

两条修复路径各自的回归：
  · 在线优先：11102 误标与面板强制冷却走上游 admin 接口，不停上游；
  · WB2API_MODE=systemd：停启 / 重启走 systemctl，日志走 journalctl。
"""
from __future__ import annotations

import asyncio
import subprocess
import unittest
from unittest import mock

from server import config
from server.services import wb2api


def _status(**account) -> dict:
    return {'connected': True, 'accounts': [{'uid': 'u1', **account}]}


_BLOCK_11102 = {'model': 'deepseek-v4.1-flash', 'until': '2099-01-01T00:00:00Z',
                'reason': '11102 model not available'}
_BLOCK_6004 = {'model': 'glm-5.3', 'until': '2099-01-01T00:00:00Z',
               'reason': '6004 model rate limit'}


class OnlineFirstTest(unittest.TestCase):
    def _run(self, statuses, *, clear=(True, '', 'ok'), enable=(True, '', 'ok')):
        with mock.patch.object(wb2api, 'get_status', new=mock.AsyncMock(side_effect=statuses)), \
                mock.patch.object(wb2api, 'clear_model_block',
                                  new=mock.AsyncMock(return_value=clear)) as cm, \
                mock.patch.object(wb2api, 'set_manual_disabled',
                                  new=mock.AsyncMock(return_value=enable)) as en, \
                mock.patch.object(wb2api, '_set_upstream_running',
                                  new=mock.AsyncMock(return_value=(False, 'no docker'))) as ctl:
            result = asyncio.run(wb2api.force_clear_account_cooling('u1'))
        return result, cm, en, ctl

    def test_11102_block_cleared_online_without_restart(self) -> None:
        (ok, msg, detail), cm, _en, ctl = self._run([
            _status(cooling=False, rate_limited_models=[_BLOCK_11102]),
            _status(cooling=False, rate_limited_models=[]),
        ])
        self.assertTrue(ok, msg)
        self.assertTrue(detail['online'])
        cm.assert_awaited_once_with('u1', 'deepseek-v4.1-flash')
        ctl.assert_not_awaited()

    def test_manual_freeze_released_online(self) -> None:
        reason = '强制冷却至 2099-01-01'
        with mock.patch.object(wb2api, '_is_manual_cooling_reason',
                               side_effect=lambda v: v == reason):
            (ok, msg, _), _cm, en, ctl = self._run([
                _status(cooling=False, rate_limited_models=[],
                        manual_disabled=True, manual_reason=reason),
                _status(cooling=False, rate_limited_models=[], manual_disabled=False),
            ])
        self.assertTrue(ok, msg)
        en.assert_awaited_once_with('u1', False)
        ctl.assert_not_awaited()

    def test_account_cooling_still_goes_offline(self) -> None:
        (ok, msg, _), cm, _en, ctl = self._run([
            _status(cooling=True, rate_limited_models=[_BLOCK_11102]),
        ])
        self.assertFalse(ok)
        self.assertIn('停止上游失败', msg)
        cm.assert_not_awaited()
        ctl.assert_awaited()

    def test_real_rate_limit_still_goes_offline(self) -> None:
        (ok, _msg, _), cm, _en, ctl = self._run([
            _status(cooling=False, rate_limited_models=[_BLOCK_11102, _BLOCK_6004]),
        ])
        self.assertFalse(ok)
        cm.assert_not_awaited()
        ctl.assert_awaited()

    def test_admin_api_unavailable_falls_back(self) -> None:
        (ok, _msg, _), cm, _en, ctl = self._run(
            [_status(cooling=False, rate_limited_models=[_BLOCK_11102])],
            clear=(False, '上游未启用管理接口', 'no_route'))
        self.assertFalse(ok)
        cm.assert_awaited_once()
        ctl.assert_awaited()

    def test_not_cleared_after_online_call_falls_back(self) -> None:
        (ok, _msg, _), _cm, _en, ctl = self._run([
            _status(cooling=False, rate_limited_models=[_BLOCK_11102]),
            _status(cooling=False, rate_limited_models=[_BLOCK_11102]),
        ])
        self.assertFalse(ok)
        ctl.assert_awaited()


class _Proc:
    def __init__(self, rc: int = 0, err: bytes = b'') -> None:
        self.returncode = rc
        self._err = err

    async def communicate(self):
        return b'', self._err


class SystemdModeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._mode = config.WB2API_MODE
        self._svc = config.WB2API_SERVICE
        config.WB2API_MODE = 'systemd'
        config.WB2API_SERVICE = 'wb2api-test'

    def tearDown(self) -> None:
        config.WB2API_MODE = self._mode
        config.WB2API_SERVICE = self._svc

    def _exec(self, proc):
        return mock.patch('asyncio.create_subprocess_exec', new=mock.AsyncMock(return_value=proc))

    def test_stop_and_start_use_systemctl(self) -> None:
        with self._exec(_Proc()) as ex:
            ok, msg = asyncio.run(wb2api._set_upstream_running(False))
            self.assertTrue(ok, msg)
            self.assertEqual(ex.await_args.args,
                             ('systemctl', '--no-ask-password', 'stop', 'wb2api-test'))
            ok, _ = asyncio.run(wb2api._set_upstream_running(True))
            self.assertTrue(ok)
            self.assertEqual(ex.await_args.args[2], 'start')

    def test_restart_uses_systemctl(self) -> None:
        with self._exec(_Proc()) as ex:
            ok, _ = asyncio.run(wb2api.restart_container())
        self.assertTrue(ok)
        self.assertEqual(ex.await_args.args,
                         ('systemctl', '--no-ask-password', 'restart', 'wb2api-test'))

    def test_group_container_restart_unchanged(self) -> None:
        with mock.patch.object(wb2api, '_docker_restart',
                               new=mock.AsyncMock(return_value=(True, 'ok'))) as dr:
            asyncio.run(wb2api.restart_container('other-container'))
        dr.assert_awaited_once_with('other-container')

    def test_permission_denied_points_to_polkit(self) -> None:
        err = b'Failed to stop wb2api-test.service: Access denied'
        with self._exec(_Proc(rc=4, err=err)):
            ok, msg = asyncio.run(wb2api._set_upstream_running(False))
        self.assertFalse(ok)
        self.assertIn('polkit', msg)

    def test_journal_lines_match_docker_timestamp_shape(self) -> None:
        out = ('-- Journal begins at Mon 2026-10-26 --\n'
               '2026-10-28T04:00:00.123456+0000 workbuddy2api[812]: [checkin] ok uid=abc\n'
               '2026-10-28T04:00:01.000001+0000 workbuddy2api[812]: \n')
        done = subprocess.CompletedProcess([], 0, stdout=out, stderr='')
        with mock.patch('subprocess.run', return_value=done) as run:
            lines = wb2api.read_container_logs(limit=10)
            bare = wb2api.read_container_logs(limit=10, timestamps=False)
            lines_m, mtime = wb2api.read_container_logs(limit=10, with_mtime=True)
        self.assertIn('wb2api-test', run.call_args.args[0])
        self.assertEqual(lines, ['2026-10-28T04:00:00.123456Z [checkin] ok uid=abc'])
        self.assertEqual(bare, ['[checkin] ok uid=abc'])
        self.assertEqual(lines_m, lines)
        self.assertIsNone(mtime)
        from server.services import tasklog
        self.assertTrue(tasklog._DOCKER_TS.match(lines[0]))


    def test_journal_offset_without_colon_is_the_real_format(self) -> None:
        """`short-iso-precise` 的偏移量是 `+0000`，不带冒号。

        只认带冒号的 `+00:00` 时，systemd 部署下日志会**全部**被丢掉并且没有任何
        报错 —— 账号回填与任务日志一起静默失效。这条钉住真实格式。
        """
        for raw in ('2026-10-28T04:00:00.123456+0000 workbuddy2api[812]: hi',
                    '2026-10-28T04:00:00+0000 workbuddy2api: hi'):
            self.assertTrue(wb2api._JOURNAL_LINE.match(raw), raw)
        self.assertIsNone(wb2api._JOURNAL_LINE.match('-- No entries --'))

class DockerMissingHintTest(unittest.TestCase):
    def test_missing_docker_explains_the_way_out(self) -> None:
        old = config.WB2API_MODE
        config.WB2API_MODE = 'docker'
        try:
            with mock.patch('asyncio.create_subprocess_exec',
                            new=mock.AsyncMock(side_effect=FileNotFoundError)):
                ok, msg = asyncio.run(wb2api._set_upstream_running(False))
        finally:
            config.WB2API_MODE = old
        self.assertFalse(ok)
        self.assertIn('WB2API_MODE=systemd', msg)


if __name__ == '__main__':
    unittest.main()
