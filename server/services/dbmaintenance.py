"""数据库维护：按保留期清理大表，并收缩 WAL（issue #492）。

## 为什么需要独立任务

`manager.db` 的膨胀不是单一入口造成的：

  · `tap_turns` 的四个大文本列（请求体 / 响应 / 客户端响应 / 重建结果）单列上限
    512 KB，默认留 7 天 / 300 会话 —— 但**抓包关闭后谁也触发不了 `prune()`**，
    而且它只限会话数、不限单会话轮数与总字节；
  · `request_logs` 的 90 天清理挂在「每 2000 次写入」上，低流量部署可能几个月
    都不触发一次；
  · `checkin_logs` 与 `account_risk_events` 此前**没有任何保留策略**，只增不减；
  · `auto_vacuum=0` 且没有周期 checkpoint，DELETE 之后主库不缩、`-wal` 也可能
    一直很大。

所以这里把清理从「写入侧顺带触发」改成**与抓包开关无关的周期任务**：启动时跑
一次，之后每 6 小时跑一次。第一步只做「删旧 + checkpoint」，不做清空改分批、
VACUUM INTO 或数据管理页（那些属于第二步）。

## 不变量

1. **不能阻塞事件循环**：所有 DB / 文件操作都在 `asyncio.to_thread` 里跑。
2. **不能重入**：上一轮还没跑完（例如大表 DELETE 较慢）时，本轮直接跳过，
   不叠加第二个线程。
3. **单项失败不影响其它项**：某张表不存在或锁冲突时只记日志，继续做后面的
   清理与 checkpoint；维护是旁路，绝不能拖垮主流程。
4. **0 = 永久保留**：保留期为 0 时跳过时间清理，但仍执行条数上限与 checkpoint。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

from .. import config, db
from . import tap

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
# 防重入标记：后台循环、启动任务与手动调用共用同一把锁。非阻塞获取，拿不到
# 就说明上一轮还在跑，本轮直接跳过（不排队，避免堆积）。
_run_guard = threading.Lock()


def _cutoff(days: int, now: int | None = None) -> int | None:
    """把「保留天数」换算成截止时间戳；0 / 负数表示永久保留（返回 None）。"""
    try:
        days = int(days)
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    return (int(time.time()) if now is None else int(now)) - days * 86400


def _table_exists(name: str) -> bool:
    """`account_risk_events` 由 accountrisk 在启动时惰性建表，不能假定它存在。"""
    return db.query_one(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ) is not None


def _prune_table(table: str, days: int) -> int:
    """按 ts 删除过期行；表不存在或清理失败都只记日志并返回 0。"""
    try:
        if not _table_exists(table):
            return 0
        cutoff = _cutoff(days)
        if cutoff is None:
            return 0
        return db.prune_older_than(table, cutoff)
    except Exception:  # noqa: BLE001 - 维护失败不能影响主流程
        logger.warning('数据库维护：清理 %s 失败', table, exc_info=True)
        return 0


def _checkpoint() -> bool:
    """TRUNCATE checkpoint：把 WAL 合并回主库并尽量截断伴生文件。"""
    try:
        rows = db.query('PRAGMA wal_checkpoint(TRUNCATE)')
    except Exception:  # noqa: BLE001 - 有活跃读者时可能 busy，下次再试
        logger.warning('数据库维护：WAL checkpoint 失败', exc_info=True)
        return False
    busy = int(rows[0][0]) if rows else 0
    if busy:
        logger.info('数据库维护：WAL checkpoint 未完成（仍有读者/写者占用）')
        return False
    return True


def run_maintenance_once() -> dict[str, int]:
    """同步执行一轮维护，返回各项删除条数。

    可直接调用（测试、运维脚本），也可由 `_loop` 放进线程执行。
    `skipped=1` 表示已有另一轮在跑，本次什么都没做。
    """
    if not _run_guard.acquire(blocking=False):
        return {'skipped': 1}

    result = {
        'skipped': 0,
        'tap_turns': 0,
        'request_logs': 0,
        'checkin_logs': 0,
        'account_risk_events': 0,
        'checkpoint': 0,
    }
    try:
        # 抓包清理与开关无关：关闭抓包只是停止**写入**，历史记录仍要按保留期消失。
        try:
            result['tap_turns'] = int(tap.prune().get('turns') or 0)
        except Exception:  # noqa: BLE001
            logger.warning('数据库维护：抓包记录清理失败', exc_info=True)

        # 请求日志：复用写入侧的滚动清理（同时顺带清理 usage_hourly / task_logs
        # / request_account_matches / audit_logs 上限）。
        try:
            result['request_logs'] = db._prune_request_logs()
        except Exception:  # noqa: BLE001 - 单项失败不能跳过后面的表与 checkpoint
            logger.warning('数据库维护：请求日志清理失败', exc_info=True)

        result['checkin_logs'] = _prune_table(
            'checkin_logs', config.CHECKIN_LOG_RETENTION_DAYS)
        result['account_risk_events'] = _prune_table(
            'account_risk_events', config.RISK_EVENT_RETENTION_DAYS)

        result['checkpoint'] = int(_checkpoint())

        removed = (result['tap_turns'] + result['request_logs']
                   + result['checkin_logs'] + result['account_risk_events'])
        if removed:
            logger.info(
                '数据库维护完成：tap_turns=%d request_logs=%d checkin_logs=%d '
                'account_risk_events=%d checkpoint=%s',
                result['tap_turns'], result['request_logs'], result['checkin_logs'],
                result['account_risk_events'], 'ok' if result['checkpoint'] else 'busy',
            )
        return result
    finally:
        _run_guard.release()


async def _loop() -> None:
    interval = max(1, int(config.DB_MAINTENANCE_INTERVAL_HOURS)) * 3600
    while True:
        try:
            # 阻塞的 SQLite 操作全部丢给线程，避免清理大表时卡住网关。
            result = await asyncio.to_thread(run_maintenance_once)
            if result.get('skipped'):
                logger.info('数据库维护：上一轮尚未结束，跳过本轮')
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 周期任务不能因单次异常退出
            logger.exception('数据库维护异常')
        await asyncio.sleep(interval)


def start_scheduler() -> bool:
    """启动周期维护（幂等）。无事件循环时返回 False（如 CLI 场景）。"""
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
