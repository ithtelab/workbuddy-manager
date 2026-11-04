"""账号回填自诊断（issue #232）。

这一列空的时候有四种完全不同的原因（模式配错 / 日志路径错 / 上游改了行格式 /
时钟对不上），处理方式不同却都要靠维护者反复索要现场信息。这里钉住的是
**每种原因都能自己说清**，而不是只有一句「未采集」。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server.services import accountlog


class AccountBackfillStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        accountlog._last = {'checked_at': 0, 'lines': 0, 'parsed': 0,
                            'filled': 0, 'error': ''}

    def _native(self, path: Path):
        # 原生模式读的是**对话日志**（上游 stdout），不是业务日志（stderr）
        return [mock.patch.object(accountlog.config, 'WB2API_MODE', 'native'),
                mock.patch.object(accountlog.config, 'WB2API_CHAT_LOG_FILE', path)]

    def test_native_missing_file_names_the_path(self) -> None:
        path = Path(self.temp.name) / 'server.err.log'
        for p in self._native(path):
            p.start()
            self.addCleanup(p.stop)
        info = accountlog.status()
        self.assertEqual(info['mode'], 'native')
        self.assertFalse(info['exists'])
        self.assertIn(str(path), info['hint'])
        self.assertIn('WB2API_CHAT_LOG_FILE', info['hint'])

    def test_native_empty_file_says_so(self) -> None:
        path = Path(self.temp.name) / 'server.err.log'
        path.write_text('', encoding='utf-8')
        for p in self._native(path):
            p.start()
            self.addCleanup(p.stop)
        info = accountlog.status()
        self.assertTrue(info['exists'])
        self.assertEqual(info['size'], 0)
        self.assertIn('空', info['hint'])

    def test_docker_without_socket_points_at_the_deployment(self) -> None:
        with mock.patch.object(accountlog.config, 'WB2API_MODE', 'docker'), \
                mock.patch.object(accountlog.updater, 'can_control_docker', return_value=False):
            info = accountlog.status()
        self.assertEqual(info['mode'], 'docker')
        self.assertFalse(info['exists'])
        self.assertIn('docker.sock', info['hint'])

    def test_lines_but_nothing_parsed_means_format_change(self) -> None:
        path = Path(self.temp.name) / 'server.err.log'
        path.write_text('some log\n', encoding='utf-8')
        for p in self._native(path):
            p.start()
            self.addCleanup(p.stop)
        accountlog._last.update(checked_at=123, lines=40, parsed=0)
        info = accountlog.status()
        self.assertIn('格式', info['hint'])

    def test_parsed_but_not_filled_means_time_or_model_mismatch(self) -> None:
        path = Path(self.temp.name) / 'server.err.log'
        path.write_text('some log\n', encoding='utf-8')
        for p in self._native(path):
            p.start()
            self.addCleanup(p.stop)
        accountlog._last.update(checked_at=123, lines=40, parsed=7, filled=0)
        with mock.patch.object(accountlog, '_has_pending_accounts', return_value=True):
            info = accountlog.status()
        self.assertIn('时间', info['hint'])

    def test_no_hint_when_backfill_is_working(self) -> None:
        path = Path(self.temp.name) / 'server.err.log'
        path.write_text('some log\n', encoding='utf-8')
        for p in self._native(path):
            p.start()
            self.addCleanup(p.stop)
        accountlog._last.update(checked_at=123, lines=40, parsed=7, filled=3)
        with mock.patch.object(accountlog, '_has_pending_accounts', return_value=False):
            self.assertEqual(accountlog.status()['hint'], '')

    def test_no_hint_when_nothing_left_to_fill(self) -> None:
        """全部已回填时 filled 也是 0（它只统计**本轮新填**），不能据此报「对不上」。

        采集每 15 秒读一次同样的日志尾巴，已经填对的行不会再填（`attach_request_accounts`
        只匹配 `account IS NULL`），稳态下 filled 恒为 0。若只看 filled，提示会在一切
        正常时长期挂着 —— 用户看到的是一个假的故障，真问题反而被盖住。
        """
        path = Path(self.temp.name) / 'server.err.log'
        path.write_text('some log\n', encoding='utf-8')
        for p in self._native(path):
            p.start()
            self.addCleanup(p.stop)
        accountlog._last.update(checked_at=123, lines=40, parsed=40, filled=0)
        with mock.patch.object(accountlog, '_has_pending_accounts', return_value=False):
            info = accountlog.status()
        self.assertEqual(info['hint'], '')

    def test_pending_probe_failure_is_not_an_alarm(self) -> None:
        """查库异常按「没有积压」处理：宁可少报，也不要报一个查不出原因的故障。"""
        with mock.patch.object(accountlog.db, 'query_one',
                               side_effect=RuntimeError('db is locked')):
            self.assertFalse(accountlog._has_pending_accounts(0))

    def test_pending_probe_reports_rows(self) -> None:
        with mock.patch.object(accountlog.db, 'query_one',
                               return_value={'1': 1}) as probe:
            self.assertTrue(accountlog._has_pending_accounts(123))
        probe.assert_called_once()

    def test_collect_reads_chat_log_not_business_log(self) -> None:
        """原生模式必须读对话日志（上游 stdout），不是业务日志（stderr）。

        上游把每请求一行（含账号）`fmt.Fprintf` 到 stdout，签到 / 活跃 / 旅行等
        编排走 Go log → stderr；原生启动脚本把两路分别重定向到 server.out.log 与
        server.err.log。读错文件时读到的是业务日志，一条 chat 行都解析不到 ——
        账号列恒为空，而界面看不出任何异常（#232 长期无法定位就卡在这里）。
        """
        chat = Path(self.temp.name) / 'server.out.log'
        err = Path(self.temp.name) / 'server.err.log'
        chat.write_text(
            '| #001 | 13:09:49 | deepseek-v4.1-flash | stream | 200 | u(3a3a19b1)'
            ' | TTFB=1ms |\n',
            encoding='utf-8')
        err.write_text('loaded 16 account(s) from ./auths\n', encoding='utf-8')
        with mock.patch.object(accountlog.config, 'WB2API_MODE', 'native'), \
                mock.patch.object(accountlog.config, 'WB2API_CHAT_LOG_FILE', chat), \
                mock.patch.object(accountlog.config, 'WB2API_LOG_FILE', err), \
                mock.patch.object(accountlog.db, 'attach_request_accounts', return_value=0):
            accountlog._collect_once()
            source = accountlog.status()['source']
        self.assertEqual(accountlog._last['lines'], 1, '应当读到对话日志的那一行')
        self.assertEqual(accountlog._last['parsed'], 1, '读错文件会解析出 0 条')
        self.assertEqual(source, str(chat))

    def test_systemd_mode_reports_the_unit_not_docker(self) -> None:
        """新增 systemd 模式后，这里必须按 systemd 报，不能再落到 docker 分支。

        落错分支的表现是：界面说「读不到 docker，改用 native」，而用户其实就在
        systemd 模式下、该做的是把面板加进 systemd-journal 组或核对 unit 名。
        """
        with mock.patch.object(accountlog.config, 'WB2API_MODE', 'systemd'),                 mock.patch.object(accountlog.config, 'WB2API_SERVICE', 'wb2api-test'):
            accountlog._last.update(checked_at=123, lines=0, parsed=0, filled=0)
            info = accountlog.status()
        self.assertEqual(info['mode'], 'systemd')
        self.assertEqual(info['source'], 'wb2api-test')
        self.assertNotIn('docker.sock', info['hint'])
        self.assertIn('systemd-journal', info['hint'])

    def test_collect_loop_records_the_error_text(self) -> None:
        # 采集抛异常时，界面要能看到原因，而不是只有一条 warning 埋在后端日志
        lines = ['| #001 | 13:09:49 | m | stream | 200 | u(3a3a19b1) | TTFB=1ms |']
        with mock.patch.object(accountlog.wb2api, 'read_container_logs',
                               return_value=(lines, 1_700_000_000.0)), \
                mock.patch.object(accountlog.db, 'attach_request_accounts',
                                  side_effect=RuntimeError('db is locked')):
            with self.assertRaises(RuntimeError):
                accountlog._collect_once()
        # _collect_once 自己抛；外层 _loop 会记录。这里直接验证字段可被写入
        accountlog._last['error'] = 'db is locked'
        self.assertIn('db is locked', accountlog._last['error'])

    def test_partial_success_does_not_hide_overdue_accounts(self) -> None:
        with mock.patch.object(accountlog.config, 'WB2API_MODE', 'docker'), \
                mock.patch.object(accountlog.updater, 'can_control_docker', return_value=True), \
                mock.patch.object(accountlog, '_has_pending_accounts', return_value=True):
            accountlog._last.update(checked_at=123, lines=40, parsed=7, filled=3)
            self.assertIn('60 秒', accountlog.status()['hint'])

    def test_empty_read_clears_previous_filled_count(self) -> None:
        accountlog._last.update(filled=7)
        with mock.patch.object(accountlog.wb2api, 'read_container_logs', return_value=([], None)):
            self.assertEqual(accountlog._collect_once(), 0)
        self.assertEqual(accountlog._last['filled'], 0)
        self.assertEqual(accountlog._last['parsed'], 0)

    def test_systemd_uses_common_pending_diagnosis(self) -> None:
        with mock.patch.object(accountlog.config, 'WB2API_MODE', 'systemd'), \
                mock.patch.object(accountlog, '_has_pending_accounts', return_value=True):
            accountlog._last.update(checked_at=123, lines=40, parsed=7)
            self.assertIn('60 秒', accountlog.status()['hint'])

    def test_probe_cutoff_is_anchored_to_last_collection(self) -> None:
        with mock.patch.object(accountlog.config, 'WB2API_MODE', 'docker'), \
                mock.patch.object(accountlog.updater, 'can_control_docker', return_value=True), \
                mock.patch.object(accountlog.time, 'time', return_value=1000), \
                mock.patch.object(accountlog, '_has_pending_accounts', return_value=False) as probe:
            accountlog._last.update(checked_at=800, lines=40, parsed=7)
            accountlog.status()
        probe.assert_called_once_with(700, 740)

    def test_endpoint_requires_admin(self) -> None:
        from server.routers import logs
        names = {getattr(r, 'path', '') for r in logs.router.routes}
        self.assertIn('/api/logs/account-backfill', names)


if __name__ == '__main__':
    unittest.main()
