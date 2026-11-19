"""出站并发：刷积分要有上限、交互操作要有独立连接池（issue #161）。

## 现场

报告人的账号页出现大批「已禁用（需重新登录）」，单号测活显示
`请求异常: PoolTimeout`。两件事其实是两层：

  · 禁用是上游对 `11140 request illegal` 的硬判定（面板从不写禁用位，
    测活只回 ok/message），我们能做的是把原因说清（另见 #201）。
  · `PoolTimeout` 是**面板自己**的连接池被占满：`refresh-credits` 对全部账号
    无上限 `asyncio.gather`，而这些请求共用同一个 httpx 客户端（同一个池，
    默认上限 100）；排队超过 pool 超时（`WB_TENCENT_TIMEOUT`）就抛 PoolTimeout。
    单号测活借的是同一个池 —— 它只是排在队尾的那个人，于是「测活报错」先被看见。

这里钉三条：
  1. 刷积分的并发峰值不超过 `config.CREDITS_CONCURRENCY`（仍然并发，不是退回串行）；
  2. 带 `lane` 的客户端**不与默认车道共用连接池**（否则隔离形同虚设）；
  3. `PoolTimeout` 翻译成「本机并发过多」的人话，而不是原样甩类名。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.routers import accounts  # noqa: E402
from server.services import errtext  # noqa: E402


def _fake_accounts(n: int) -> list[dict]:
    return [{'file': f'workbuddy_{i}.json', 'uid': f'uid{i:04d}', 'nickname': f'n{i}'}
            for i in range(n)]


class CreditsConcurrencyTest(unittest.TestCase):
    """`refresh-credits` 的扇出上限。"""

    def setUp(self) -> None:
        self.peak = 0
        self.inflight = 0
        self.calls = 0
        self._patches = [
            mock.patch.object(accounts, '_require_dir', return_value=Path('.')),
            mock.patch.object(accounts, '_group', return_value=None),
            mock.patch.object(accounts.wb2api, 'list_auth_accounts',
                              return_value=_fake_accounts(24)),
            mock.patch.object(accounts.wb2api, 'read_account_file',
                              return_value={'auth': {'accessToken': 't'}}),
            mock.patch.object(accounts.creditsvc, 'get_credits', side_effect=self._slow),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()

    async def _slow(self, auth, *, force, nickname):  # noqa: ANN001, ANN202
        self.calls += 1
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(0.01)  # 模拟一次真实出站
        finally:
            self.inflight -= 1
        return True, 100, '', False, None, []

    def test_peak_is_capped_but_still_concurrent(self) -> None:
        result = asyncio.run(accounts.refresh_all_credits(
            force=False, upstream_id=None, user={'role': 'admin'}))

        self.assertEqual(result['total'], 24)
        self.assertEqual(result['succeeded'], 24)
        self.assertEqual(self.calls, 24)
        self.assertLessEqual(self.peak, config.CREDITS_CONCURRENCY,
                            '刷新积分必须受 WB_CREDITS_CONCURRENCY 约束')
        self.assertGreater(self.peak, 1, '限流不能退化成串行（慢到前端超时）')

    def test_env_knob_is_read(self) -> None:
        """上限来自环境变量（部署方可调），默认 8。"""
        self.assertEqual(config.CREDITS_CONCURRENCY,
                         max(1, int(__import__('os').environ.get(
                             'WB_CREDITS_CONCURRENCY', 8))))


class ClientLaneTest(unittest.TestCase):
    """带 lane 的客户端必须独占连接池。"""

    def tearDown(self) -> None:
        asyncio.run(config.close_clients())

    def test_lane_isolates_pool(self) -> None:
        async def go():  # noqa: ANN202
            probe1 = config.http_client(1, connect=1, lane='probe')
            probe2 = config.http_client(1, connect=1, lane='probe')
            plain = config.http_client(1, connect=1)
            return probe1, probe2, plain

        probe1, probe2, plain = asyncio.run(go())
        # 同一 lane 复用同一个客户端（池缓存仍然有效）
        self.assertIs(probe1._client, probe2._client)
        # 不同 lane 必须是不同的池，否则「独立车道」只是说法
        self.assertIsNot(probe1._client, plain._client)


class PoolTimeoutTextTest(unittest.TestCase):
    def test_pool_timeout_explains_local_congestion(self) -> None:
        class PoolTimeout(Exception):
            def __str__(self) -> str:  # httpx 的原文就是这么贫瘠
                return 'timed out'

        text = errtext.err_text(PoolTimeout())
        self.assertIn('PoolTimeout', text)
        self.assertIn('连接池', text)
        self.assertIn('WB_CREDITS_CONCURRENCY', text)

    def test_other_errors_keep_type_and_detail(self) -> None:
        self.assertEqual(errtext.err_text(ValueError('boom')), 'ValueError: boom')


if __name__ == '__main__':
    unittest.main()
