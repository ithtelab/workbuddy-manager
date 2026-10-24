"""强制冷却到期清理：让定时冷却真的会「到期后自动恢复」。

`set_account_cooling_state` 会把 `manual_disabled` 一起置位，防止上游每日签到
的 `ReenableIfCredits` 把 `until` 重置后账号又被选中。这样做的副作用是
`manual_disabled` 不会随 `until` 到期自动消失——如果没人清，定时冷却会变成
事实上的永久停用。

这个模块只做一件事：每 5 分钟检查一次 state.json，发现「前缀标记 + 标记里的
到期时间已过」的强制冷却就停上游一次、批量清掉、再启动。没有到期条目时不碰
上游，开销只是读一个小 JSON。

两项保护：更新进行中不碰上游（避免和 updater 抢停/启），上游未连接时也不碰
（可能正在被其他运维动作停着，贸然启动会打乱现场）。
"""
from __future__ import annotations

import asyncio
import logging

from . import updater, wb2api

logger = logging.getLogger(__name__)

_CHECK_INTERVAL_SECONDS = 300
_FIRST_DELAY_SECONDS = 60

_task: asyncio.Task | None = None


async def _loop() -> None:
    await asyncio.sleep(_FIRST_DELAY_SECONDS)
    while True:
        try:
            # 先做便宜的两道检查：更新中 / 没有到期条目都不碰上游。
            if not updater.read_status().get('running') and \
                    wb2api.expired_manual_cooling_uids():
                status = await wb2api.get_status()
                if status.get('connected'):
                    cleared = await wb2api.clear_expired_manual_cooling()
                    if cleared:
                        logger.info('强制冷却到期清理完成：%d 个账号', cleared)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning('强制冷却到期清理异常: %s', exc)
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)


def start_scheduler() -> bool:
    """启动到期清理巡检（幂等）。无事件循环时返回 False（如 CLI 场景）。"""
    global _task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if _task is None or _task.done():
        _task = loop.create_task(_loop())
    return True


def stop_scheduler() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
