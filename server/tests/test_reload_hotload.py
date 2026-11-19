"""加账号后的收尾：优先等上游热加载，超时才重启（用户反馈的「20-30 秒」）。

现象：国际版用链接 / GitHub 快速登录，界面提示登录成功之后，后台还要 20-30 秒
才看到账号。根因不是「导入慢」，而是**等容器重启**：

    落盘成功 → 直接重启上游 → 前端立刻刷新账号列表 → 那一刷里 get_status()
    请求上游 /status，而上游正在重启 → 面板侧上游调用超时默认 120 秒 → 挂到
    容器起来才返回。

上游 2026-09-19 起热加载 auths 目录（约 5 秒一轮），新版根本不需要重启。所以
改为：先等一段时间，账号出现在 /status 里就结束；只有「旧上游不热加载」或
「上游不可达」才回退重启（那两种情况下重启是唯一出路）。

这里钉住三条：热加载生效**不重启**、超时才重启、以及同时加多个账号**只重启一次**。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import reload as R  # noqa: E402


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._wait = R.HOT_RELOAD_WAIT_SECONDS
        self._poll = R.HOT_RELOAD_POLL_SECONDS
        # 测试里把窗口压到几十毫秒：判据是「等 / 不等」，不是等多久
        R.HOT_RELOAD_WAIT_SECONDS = 0.15
        R.HOT_RELOAD_POLL_SECONDS = 0.01
        R._pending_uids.clear()
        R._reload_worker = None
        self.restarts: list[int] = []

        async def _restart():
            self.restarts.append(1)
            return True, 'ok'

        self._restart_patch = mock.patch.object(R.wb2api, 'restart_container', _restart)
        self._restart_patch.start()
        self.addCleanup(self._restart_patch.stop)

    def tearDown(self) -> None:
        R.HOT_RELOAD_WAIT_SECONDS = self._wait
        R.HOT_RELOAD_POLL_SECONDS = self._poll
        R._pending_uids.clear()
        R._reload_worker = None

    def _status_patch(self, statuses: list[dict]) -> None:
        """按调用次序返回不同的 /status 快照；用完后一直用最后一个。"""
        seq = list(statuses)

        async def _get():
            return seq.pop(0) if len(seq) > 1 else seq[0]

        self._status = mock.patch.object(R.wb2api, 'get_status', _get)
        self._status.start()
        self.addCleanup(self._status.stop)

    async def _run(self) -> None:
        R.request_reload_or_restart('uid-new')
        # 等后台任务跑完（窗口 + 余量）
        if R._reload_worker is not None:
            await asyncio.wait_for(R._reload_worker, timeout=5)

    def test_hot_reload_appearing_skips_restart(self) -> None:
        """账号在窗口内被上游收录 → 不重启（这是新版上游的正常路径）。"""
        self._status_patch([
            {'connected': True, 'accounts': []},
            {'connected': True, 'accounts': [{'uid': 'uid-new'}]},
        ])
        asyncio.run(self._run())
        self.assertEqual(self.restarts, [], '热加载已生效却又重启了上游')

    def test_timeout_falls_back_to_restart(self) -> None:
        """老上游不热加载（账号一直不出现）→ 回退重启。"""
        self._status_patch([{'connected': True, 'accounts': [{'uid': 'other'}]}])
        asyncio.run(self._run())
        self.assertEqual(len(self.restarts), 1, '超时后应重启一次')

    def test_unreachable_upstream_falls_back_to_restart(self) -> None:
        """上游不可达 → 重启（此时它是唯一的出路）。"""
        self._status_patch([{'connected': False, 'error': 'connection refused'}])
        asyncio.run(self._run())
        self.assertEqual(len(self.restarts), 1, '上游不可达时应回退重启')

    def test_two_accounts_restart_at_most_once(self) -> None:
        """同时加两个账号且都不热加载 → **只重启一次**（不是两次）。"""
        self._status_patch([{'connected': True, 'accounts': [{'uid': 'other'}]}])

        async def _both() -> None:
            R.request_reload_or_restart('uid-a')
            R.request_reload_or_restart('uid-b')
            if R._reload_worker is not None:
                await asyncio.wait_for(R._reload_worker, timeout=5)

        asyncio.run(_both())
        self.assertEqual(len(self.restarts), 1, f'重启了 {len(self.restarts)} 次')

    def test_no_event_loop_reports_false(self) -> None:
        """没有事件循环时返回 False，让调用方自己决定（与 request_restart 同口径）。"""
        self.assertFalse(R.request_reload_or_restart('uid-x'))
