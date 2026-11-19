"""未鉴权入口的入站日志要**采样**，不能被匿名者当成写放大通道。

`missing_key` / `invalid_key` 这两条路径在校验密钥**之前**就写库：任何人不带
token 或用随机 token 打 `/v1/chat/completions`、`/v1/usage`，每请求都会落一行。
表有两万行滚动上限，写满就把真正该看的泄露窗口挤掉，还外带持续的写入与清理。

所以按 (IP, 原因) 做窗口计数：前 N 条照常逐条记（路径、UA 各不相同的细节有
排查价值），超出的折叠，并在本窗口第一次折叠时写**一条** `key_auth_flood`
汇总行 —— 运维仍能看出「这个 IP 在刷」，但每分钟每 IP 的行数有上限。

这个文件钉住四件事：
  · 前 N 条逐条记、之后折叠成一条汇总行、再之后不再写；
  · 汇总行只写一次（否则等于把采样阈值挪了个位置，照样能放大）；
  · 采样只针对这两个匿名可达的原因 —— 密钥层的拒绝不能被藏掉；
  · 换 IP / 换原因 / 跨窗口都各自重新计数（不能误伤正常的不同来源）。
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import gateway  # noqa: E402


class AuthLogSamplingTest(unittest.TestCase):
    def setUp(self) -> None:
        gateway._auth_log_state.clear()
        self.calls: list[tuple] = []
        patcher = mock.patch.object(
            gateway.db, 'add_ip_access_log',
            side_effect=lambda *a, **kw: self.calls.append((a, kw)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(gateway._auth_log_state.clear)

    def _reasons(self) -> list[str | None]:
        return [kw.get('reason') if kw else (a[4] if len(a) > 4 else None)
                for a, kw in self.calls]

    def _log(self, ip: str = '9.9.9.9', reason: str = 'invalid_key') -> None:
        gateway._log_ip(ip, '/v1/chat/completions', True, 'UA', reason)

    def test_first_n_are_recorded_verbatim(self) -> None:
        n = gateway._AUTH_LOG_PER_WINDOW
        for _ in range(n):
            self._log()
        self.assertEqual(len(self.calls), n)
        self.assertEqual(self._reasons(), ['invalid_key'] * n)

    def test_excess_collapses_into_one_flood_row(self) -> None:
        n = gateway._AUTH_LOG_PER_WINDOW
        for _ in range(n + 40):
            self._log()
        self.assertEqual(len(self.calls), n + 1, '超出阈值后只该多一条汇总行')
        self.assertEqual(self._reasons()[-1], 'key_auth_flood')

    def test_flood_row_is_written_once_per_window(self) -> None:
        n = gateway._AUTH_LOG_PER_WINDOW
        for _ in range(n + 200):
            self._log()
        self.assertEqual(self._reasons().count('key_auth_flood'), 1)

    def test_window_expiry_resumes_logging(self) -> None:
        n = gateway._AUTH_LOG_PER_WINDOW
        for _ in range(n + 5):
            self._log()
        before = len(self.calls)
        # 把窗口起点往前拨，模拟一分钟过去
        for state in gateway._auth_log_state.values():
            state[0] -= gateway._AUTH_LOG_WINDOW + 1
        self._log()
        self.assertEqual(len(self.calls), before + 1)
        self.assertEqual(self._reasons()[-1], 'invalid_key')

    def test_other_ip_is_counted_separately(self) -> None:
        n = gateway._AUTH_LOG_PER_WINDOW
        for _ in range(n):
            self._log(ip='1.1.1.1')
        self._log(ip='2.2.2.2')
        self.assertEqual(len(self.calls), n + 1)
        self.assertEqual(self._reasons()[-1], 'invalid_key')

    def test_other_reason_is_counted_separately(self) -> None:
        n = gateway._AUTH_LOG_PER_WINDOW
        for _ in range(n):
            self._log(reason='invalid_key')
        self._log(reason='missing_key')
        self.assertEqual(len(self.calls), n + 1)
        self.assertEqual(self._reasons()[-1], 'missing_key')

    def test_key_level_rejections_are_never_sampled(self) -> None:
        """能拿出一把已知密钥的请求，写放大的成本由密钥自己承担。"""
        for _ in range(gateway._AUTH_LOG_PER_WINDOW * 4):
            self._log(reason='ip_blocked')
            self._log(reason='rate_limited')
        self.assertEqual(len(self.calls), gateway._AUTH_LOG_PER_WINDOW * 8)
        self.assertNotIn('key_auth_flood', self._reasons())

    def test_clean_ip_is_passed_through(self) -> None:
        """采样不改变落库内容：ip / path / ua / blocked 原样透传。"""
        self._log(ip='8.8.4.4')
        (args, kwargs), = self.calls
        self.assertEqual(args[:4], ('8.8.4.4', '/v1/chat/completions', True, 'UA'))

    def test_state_is_bounded_for_many_distinct_ips(self) -> None:
        """大量 IP 各打一次时，按 state 计数的过期清理永远轮不到，必须有硬上限。"""
        for i in range(gateway._AUTH_LOG_MAX_ENTRIES + 100):
            self._log(ip=f'10.0.{i // 256}.{i % 256}')
        self.assertLessEqual(len(gateway._auth_log_state),
                             gateway._AUTH_LOG_MAX_ENTRIES)


class AuthLogSamplingWindowTest(unittest.TestCase):
    """时间推进用假时钟，避免真的等一分钟。"""

    def setUp(self) -> None:
        gateway._auth_log_state.clear()
        self.addCleanup(gateway._auth_log_state.clear)

    def test_counts_reset_after_window(self) -> None:
        n = gateway._AUTH_LOG_PER_WINDOW
        with mock.patch.object(gateway, 'time') as clock, \
                mock.patch.object(gateway.db, 'add_ip_access_log') as written:
            clock.time.return_value = 1000.0
            for _ in range(n + 3):
                gateway._log_ip('7.7.7.7', '/v1/models', True, 'UA', 'invalid_key')
            self.assertEqual(written.call_count, n + 1)
            clock.time.return_value = 1000.0 + gateway._AUTH_LOG_WINDOW + 1
            gateway._log_ip('7.7.7.7', '/v1/models', True, 'UA', 'invalid_key')
            self.assertEqual(written.call_count, n + 2)
            reason = written.call_args.args[4]
            self.assertEqual(reason, 'invalid_key', '新窗口的第一条不能被标成汇总')


if __name__ == '__main__':
    unittest.main()
