"""低频复查 11102 模型误标；成功后同步运行中网关，真实 6004 等待上游重置。"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
from pathlib import Path
import re
import time

from .. import config, upstreamsvc
from . import updater, wb2api

logger = logging.getLogger(__name__)
INTERVAL = 30
BATCH_SIZE = 8
_task: asyncio.Task | None = None
_entries: dict[str, dict] = {}


def enabled() -> bool:
    return os.environ.get('WB_MODEL_RECOVERY_ENABLED', '1').lower() not in ('0', 'false', 'off')


def _save() -> None:
    path = config.DATA_DIR / 'model-recovery.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(_entries), encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(path)


def _load() -> None:
    global _entries
    try:
        data = json.loads((config.DATA_DIR / 'model-recovery.json').read_text(encoding='utf-8'))
        _entries = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        _entries = {}


async def _probe(group: dict, filename: str, model: str) -> dict:
    # 与手动探测共用分类、并发保护和成功同步，不另写一套上游请求。
    from ..routers.accounts import probe_models
    result = await probe_models({'accounts': [filename], 'models': [model]},
                                upstream_id=group['id'], user={'role': 'admin'})
    return result['results'][0]


async def run_once(now: float | None = None) -> dict:
    now = time.time() if now is None else now
    if not enabled() or updater.read_status().get('running'):
        return {'checked': 0, 'recovered': 0}
    # 巡检每 INTERVAL 秒跑一次；没有待复查项时不该反复重写状态文件。
    before = json.dumps(_entries, sort_keys=True)
    active: set[str] = set()
    due: list[tuple] = []
    for group in upstreamsvc.list_upstreams():
        if not group.get('enabled') or not group.get('auth_dir'):
            continue
        try:
            status = await wb2api.get_status(base_url=group['base_url'], api_key=group['api_key'])
            if not status.get('connected'):
                # 未连接不是模型恢复；保留退避记录，避免重新连接后立刻重复探测。
                active.update(k for k in _entries if k.startswith(str(group['id']) + '|'))
                continue
            files = {a['uid']: a['file'] for a in wb2api.list_auth_accounts(Path(group['auth_dir']))}
            for account in status.get('accounts', []):
                uid = account.get('uid')
                if account.get('disabled') or account.get('manual_disabled') or account.get('cooling') or uid not in files:
                    continue
                for block in account.get('rate_limited_models', []):
                    if not str(block.get('reason', '')).startswith('11102'):
                        continue
                    model = block['model']
                    key = f"{group['id']}|{uid}|{model}"
                    active.add(key)
                    entry = _entries.setdefault(key, {'next_at': now + INTERVAL, 'failures': 0})
                    if entry.get('next_at', 0) <= now:
                        due.append((entry.get('next_at', 0), key, group, uid, files[uid], model))
        except Exception as exc:
            logger.warning('模型恢复巡检读取分组失败: %s', exc)
            active.update(k for k in _entries if k.startswith(str(group['id']) + '|'))
    for key in set(_entries) - active:
        del _entries[key]
    checked = recovered = 0
    seen_accounts: set[tuple] = set()
    for _, key, group, uid, filename, model in sorted(due, key=lambda item: item[:2]):
        identity = (group['id'], uid)
        if identity in seen_accounts:
            continue
        if checked >= BATCH_SIZE:
            break
        seen_accounts.add(identity)
        checked += 1
        entry = _entries[key]
        try:
            result = await _probe(group, filename, model)
            if result.get('status') == 'ok' and result.get('gateway_sync_skipped'):
                # 上游没注册管理接口（admin.enabled=false）：探测成功也无从解除
                # 网关负缓存，继续复查只会白打上游请求。
                del _entries[key]
            elif result.get('status') == 'ok' and result.get('gateway_synced'):
                recovered += 1
                del _entries[key]
            else:
                failures = min(int(entry.get('failures', 0)) + 1, 5)
                delay = min(60 * 2 ** (failures - 1), 600)
                entry.update(failures=failures, next_at=now + delay)
                if result.get('status') == 'rate':
                    match = re.search(r'20\d\d-\d\d-\d\d \d\d:\d\d:\d\d', result.get('message', ''))
                    if match:
                        reset = datetime.datetime.fromisoformat(match[0]).replace(
                            tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp()
                        entry['next_at'] = max(now + delay, reset)
        except Exception as exc:
            entry['next_at'] = now + 60
            logger.warning('模型恢复探测失败: %s', exc)
        # 后台单路、每个账号每轮最多一个模型，不与用户流量抢并发。
        await asyncio.sleep(0.6)
    if json.dumps(_entries, sort_keys=True) != before:
        _save()
    if checked:
        logger.info('模型自动恢复：检查 %d 项，恢复 %d 项，待复查 %d 项', checked, recovered, len(_entries))
    return {'checked': checked, 'recovered': recovered, 'pending': len(_entries)}


async def _loop() -> None:
    await asyncio.sleep(INTERVAL)
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning('模型自动恢复异常: %s', exc)
        await asyncio.sleep(INTERVAL)


def start_scheduler() -> None:
    global _task
    if enabled() and (_task is None or _task.done()):
        _load()
        _task = asyncio.get_running_loop().create_task(_loop())


def stop_scheduler() -> None:
    if _task and not _task.done():
        _task.cancel()
