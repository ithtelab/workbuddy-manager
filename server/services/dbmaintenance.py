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
一次，之后每 6 小时跑一次。第一步只做「删旧 + checkpoint」；第二步补上
`VACUUM INTO` 压缩（删行不会缩文件）与设置页的数据维护卡片。

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
import os
import shutil
import sqlite3
import threading
import time
from pathlib import Path

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


# ── 第二步：VACUUM INTO 压缩（issue #492）──────────────────────────
#
# SQLite 的 DELETE 只把页放回 freelist，文件不会缩；`VACUUM INTO` 会按当前
# 内容重建一份新库。流程刻意模仿 pgsync 的恢复：先在旁边写一份完整副本，校验
# 通过后再用 `db.replace_database()` 原子替换。
#
# 与 pgsync 恢复的唯一差别是**这里不能丢写**：恢复的语义是「用镜像覆盖本地」，
# 而压缩只是换一种存放方式，压缩期间发生的写入必须保留。所以整个「复制 + 替换」
# 都在 `db._lock` 内完成 —— `_lock` 是 RLock，`replace_database()` 在同一线程
# 里再次进入不会死锁。代价是压缩期间所有数据库操作（含读）排队；这是一次
# 管理员显式触发的维护动作，界面会先给出二次确认。
#
# 注：`replace_database()` 的注释说「调用方不得持有 _lock」，那是针对 pgsync
# 恢复这种「耗时写入 staging」的场景；这里的 staging 本身就是主库的完整副本，
# 若在锁外复制，复制开始后提交的写入会在替换时被静默丢掉，因此必须持锁。
_COMPACT_MIN_FREE_FACTOR = 1.2
_TABLE_COUNTS = ('request_logs', 'tap_turns', 'checkin_logs',
                 'account_risk_events', 'task_logs')
_LAST_RESULT_KEY = 'db_maintenance_last'


class MaintenanceBusy(RuntimeError):
    """已有一轮维护 / 压缩在跑。"""


class InsufficientDiskSpace(RuntimeError):
    """临时磁盘空间不够放下压缩副本。"""


class CompactError(RuntimeError):
    """压缩失败（完整性校验不通过等），原库未被替换。"""


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _readonly_uri(path: Path) -> str:
    """SQLite 的只读 URI。

    用 `Path.as_uri()` 而不是手拼 `file:{path}`：Windows 盘符与中文/空格路径
    必须转成 `file:///D:/...` 并做百分号编码，手拼会在 `mode=ro` 打开时直接
    报 `unable to open database file`。
    """
    return f'{path.resolve().as_uri()}?mode=ro'


def _staging_path(target: Path) -> Path:
    """与正式库同目录的唯一副本名。

    同目录是硬性要求：替换走 `os.replace`，跨文件系统会抛 EXDEV。用 pid +
    纳秒时间戳，避免上次异常退出留下的残骸被复用时 `VACUUM INTO` 因目标已存在
    而失败。
    """
    return target.parent / f'{target.name}.compact-{os.getpid()}-{time.time_ns()}'


def _cleanup_stale_staging(target: Path, *, older_than: float = 3600) -> int:
    """删掉异常退出留下的整库副本（`*.compact-*`）。

    只删一小时前的：另一个进程可能正在压缩，它的 staging 是新的。
    这些文件是完整数据库副本（含请求日志、密钥哈希、审计），不能长期留在盘上。
    """
    removed = 0
    try:
        candidates = list(target.parent.glob(target.name + '.compact-*'))
    except OSError:
        return 0
    for path in candidates:
        try:
            if time.time() - path.stat().st_mtime < older_than:
                continue
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _remember_result(kind: str, payload: dict) -> None:
    """把最近一次维护结果写进 settings，供 GET 接口展示。

    旁路信息，写失败只记日志：维护本身已经完成，不能因为展示数据没落库而报错。
    """
    try:
        db.set_setting(_LAST_RESULT_KEY, {
            'kind': kind,
            'at': int(time.time()),
            **payload,
        })
    except Exception:  # noqa: BLE001 - 展示用元数据，失败不影响维护结果
        logger.warning('数据库维护：记录最近一次结果失败', exc_info=True)


def _table_count(table: str) -> int:
    if not _table_exists(table):
        return 0
    row = db.query_one(f'SELECT COUNT(*) AS n FROM "{table}"')
    return int(row['n']) if row else 0


def database_status() -> dict:
    """当前库/WAL 大小、主要表行数与最近一次维护结果（GET /api/maintenance/database）。"""
    target = Path(config.DB_PATH)
    last = db.get_setting(_LAST_RESULT_KEY)
    if not isinstance(last, dict):
        last = None
    return {
        'db_bytes': _file_size(target),
        'wal_bytes': _file_size(Path(str(target) + '-wal')),
        'shm_bytes': _file_size(Path(str(target) + '-shm')),
        'tables': {name: _table_count(name) for name in _TABLE_COUNTS},
        'last': last,
    }


def _integrity_ok(path: Path) -> tuple[bool, str]:
    """对 staging 做 `PRAGMA integrity_check`；返回 (是否 ok, 详情)。"""
    conn = sqlite3.connect(_readonly_uri(path), uri=True)
    try:
        rows = [str(r[0]) for r in conn.execute('PRAGMA integrity_check').fetchall()]
    finally:
        conn.close()
    if rows == ['ok']:
        return True, 'ok'
    return False, '；'.join(rows[:5]) or '空结果'


def compact_database() -> dict[str, int]:
    """用 `VACUUM INTO` 重建数据库并原子替换，返回压缩前后字节数。

    * 只读源连接 + 同目录 staging，替换走 `db.replace_database()`；
    * 预检可用空间至少 `1.2 ×` 当前库（主库 + WAL）；
    * staging 必须通过 `PRAGMA integrity_check` 才允许替换；
    * 任何失败都会删掉 staging，原库保持不动。

    数据库很小（<1MB）时也照常执行：`VACUUM INTO` 是幂等的，结果只是文件
    几乎不变，不把「小」当错误。
    """
    if not _run_guard.acquire(blocking=False):
        raise MaintenanceBusy('数据库维护任务正在运行，请稍后再试')

    started = time.monotonic()
    staging: Path | None = None
    replaced = False
    probe: sqlite3.Connection | None = None
    try:
        config.ensure_dirs()
        target = Path(config.DB_PATH)
        _cleanup_stale_staging(target)

        # 先 checkpoint：把 WAL 里的已提交页并回主库，让「当前库大小」与
        # VACUUM INTO 看到的快照一致，也顺带缩小替换时的 WAL 残留。
        _checkpoint()

        before = _file_size(target)
        wal_bytes = _file_size(Path(str(target) + '-wal'))
        source_bytes = before + wal_bytes
        need = int(source_bytes * _COMPACT_MIN_FREE_FACTOR)
        free = int(shutil.disk_usage(target.parent).free)
        if free < need:
            raise InsufficientDiskSpace(
                f'磁盘空间不足：压缩至少需要 {need} 字节（当前库 {source_bytes} × '
                f'{_COMPACT_MIN_FREE_FACTOR:g}），可用 {free} 字节')

        staging = _staging_path(target)
        if staging.exists():  # 理论上不会命中；防御异常退出留下的同名文件
            staging.unlink()

        # 长耗时部分（快照 + 完整性检查）**不持 db._lock**：VACUUM INTO 读的是
        # 一致性快照，期间网关的读写照常进行；用另一条常驻只读连接记录
        # data_version，替换前若发现期间有提交就放弃本次（下次再压）。
        #
        # 不能读 db 共享连接上的 data_version：同一连接上的提交不会改变它。
        probe = sqlite3.connect(_readonly_uri(target), uri=True)
        version = int(probe.execute('PRAGMA data_version').fetchone()[0])
        source = sqlite3.connect(_readonly_uri(target), uri=True,
                                 isolation_level=None)
        try:
            source.execute('VACUUM INTO ?', (str(staging),))
        except sqlite3.Error as exc:
            raise CompactError(f'VACUUM INTO 失败：{exc}') from exc
        finally:
            source.close()

        # staging 是整库副本：先收紧权限，避免同机其它用户读到请求日志/密钥哈希。
        try:
            os.chmod(staging, 0o600)
        except OSError:
            pass

        ok, detail = _integrity_ok(staging)
        if not ok:
            raise CompactError(f'压缩副本完整性检查未通过：{detail}')

        # 只把「校验版本 + 两次 rename」放进锁内（毫秒级）。锁一旦拿到，
        # 本进程的写入都被挡住；probe 必须在 rename 前关闭（Windows 上打开着
        # 的句柄会让 os.replace 失败）。
        with db._lock:
            current = int(probe.execute('PRAGMA data_version').fetchone()[0])
            probe.close()
            probe = None
            if current != version:
                raise MaintenanceBusy(
                    '压缩期间有新的写入，已放弃本次压缩；请稍后重试')
            db.replace_database(staging)
            replaced = True
            after = _file_size(target)

        duration_ms = int((time.monotonic() - started) * 1000)
        result = {
            'before_bytes': before,
            'after_bytes': after,
            'saved_bytes': before - after,
            'duration_ms': duration_ms,
        }
        _remember_result('compact', {'ok': True, **result})
        logger.info('数据库压缩完成：%d → %d 字节（%d ms）',
                    before, after, duration_ms)
        return result
    except Exception as exc:
        _remember_result('compact', {'ok': False, 'error': str(exc)[:300]})
        raise
    finally:
        if probe is not None:
            try:
                probe.close()
            except sqlite3.Error:
                pass
        # 替换成功后 staging 已被 rename 走；失败时清掉副本，原库不动。
        if staging is not None and not replaced:
            try:
                if staging.exists():
                    staging.unlink()
            except OSError:
                logger.warning('数据库压缩：清理临时副本失败：%s', staging,
                               exc_info=True)
        _run_guard.release()


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
        # 设置页要展示「最近一次维护」，周期任务与手动压缩共用同一个键。
        _remember_result('prune', {'ok': True, 'removed': removed, **result})
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
