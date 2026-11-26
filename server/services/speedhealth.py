"""按实测吞吐给账号降权（慢号自动退出轮换）。

## 需求现场

issue #494：同一模型、同一客户端、全 200 的情况下，一部分账号稳定在
110–150 tok/s，另一部分稳定在 35–45 tok/s，而且**同一账号会在两档之间迁移**
（上午快的号下午可能慢，重启后又恢复）。上下文长度不是变量：按 prompt 分桶，
3 万到 40 万 token 的速率中位数都在 117–148。

慢的成因在云端，本地改不了「它吐多快」；本地能决定的是「还派不派给它」。

## 为什么可以在管理器做（而不是只能改 wb2api）

两条本地机制刚好够用：

  · 数据：`request_logs` 每次调用都记了 `account / completion_tokens /
    latency_ms`，够算真实速率；
  · 执行器：`wb2api.set_account_weight()` 把 `weight` 写进账号文件，而上游
    **每 5 秒轮询 auths 目录热加载**（`internal/pool/watch.go`），
    `upsertLocked` 是整体替换 `*auth.Auth`，权重因此生效——**不需要重启**。
    重启会把在途请求全部打断，还会清掉上游内存里的状态，代价太大。

## 口径（与上游日志一致）

    rate = completion_tokens / (latency_ms / 1000)

只统计 `stream=1` + `status=200` + `completion_tokens >= 300`：
低于 300 时首帧占比过大，`tok/total` 会严重低估真实速率，把快号误判成慢号。

基准取**同一批账号的中位数**而不是绝对值：上游整体抖动时（晚高峰），
所有号一起慢，score 仍然接近 1，不会误伤全池。

## 安全阀

  · 默认只读：`evaluate()` 只出建议，不动文件；真正写盘要显式 `apply`；
  · 一次性最多降 `MAX_CHANGES` 个号，避免全池被打散；
  · 权重有下限 `FLOOR`（不写 0），给慢号留自证恢复的机会；
  · 池内超过一半账号同时偏慢 → 判定为全局，不处置。
"""
from __future__ import annotations

import asyncio
import logging
import re
import statistics
import time
from pathlib import Path

from .. import db, upstreamsvc
from . import wb2api

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 15 * 60
MIN_TOKENS = 300          # 低于此值的样本不进统计（首帧占比过大）
MIN_SAMPLES = 3           # 少于这个样本数不下结论
LOW_SCORE = 0.60          # 低于池内中位数的 60% → 判慢
HIGH_SCORE = 0.85         # 回到 85% 以上 → 判正常
FLOOR = 0.15              # 降权下限（不写 0：0 在上游表示「清除」）
MAX_CHANGES = 3           # 单次最多调整几个号

_UID_RE = re.compile(r'\(([0-9a-f]{8})\)\s*$')


def uid_of(account: object) -> str | None:
    """`昵称(1a2b3c4d)` → `1a2b3c4d`。取不到返回 None。"""
    if not isinstance(account, str):
        return None
    m = _UID_RE.search(account.strip())
    return m.group(1) if m else None


def collect(window_seconds: int = WINDOW_SECONDS, *, now: float | None = None) -> list[dict]:
    """按账号汇总窗口内的实测吞吐。只读。"""
    end = int(now if now is not None else time.time())
    start = end - int(window_seconds)
    rows = db.query(
        'SELECT account, completion_tokens, latency_ms FROM request_logs '
        'WHERE ts >= ? AND ts < ? AND stream = 1 AND status = 200 '
        'AND completion_tokens >= ? AND account IS NOT NULL',
        (start, end, MIN_TOKENS),
    )
    buckets: dict[str, list[float]] = {}
    for row in rows:
        acc = row['account']
        lat = row['latency_ms'] or 0
        ct = row['completion_tokens'] or 0
        if not acc or lat <= 0 or ct < MIN_TOKENS:
            continue
        buckets.setdefault(acc, []).append(ct / (lat / 1000.0))
    out = []
    for acc, rates in buckets.items():
        out.append({
            'account': acc,
            'uid': uid_of(acc),
            'samples': len(rates),
            'rate': round(statistics.median(rates), 1),
        })
    out.sort(key=lambda item: -item['rate'])
    return out


def evaluate(stats: list[dict]) -> dict:
    """算 score 与建议动作。纯函数，不碰数据库也不碰文件。"""
    usable = [s for s in stats if s['samples'] >= MIN_SAMPLES and s['rate'] > 0]
    if len(usable) < 2:
        return {'pool_median': None, 'rows': [], 'global_slow': False,
                'note': '样本不足（可用账号少于 2 个）'}
    pool = statistics.median([s['rate'] for s in usable])
    rows = []
    for s in stats:
        if s['samples'] < MIN_SAMPLES or s['rate'] <= 0:
            rows.append({**s, 'score': None, 'action': 'insufficient'})
            continue
        score = round(s['rate'] / pool, 3) if pool else None
        action = 'slow' if score is not None and score < LOW_SCORE else 'ok'
        rows.append({**s, 'score': score, 'action': action})
    slow = [r for r in rows if r['action'] == 'slow']
    return {
        'pool_median': round(pool, 1),
        'rows': rows,
        'global_slow': len(slow) * 2 > len(usable),
        'note': '',
    }


def plan(stats: list[dict], *, current_weight: dict[str, float] | None = None) -> dict:
    """把评估结果翻成「要写哪些账号、写成多少」。

    `current_weight` 以 uid 为键；缺省按 1.0（上游默认）。
    """
    ev = evaluate(stats)
    current_weight = current_weight or {}
    if ev['global_slow']:
        return {**ev, 'changes': [], 'reason': '池内过半偏慢，判定为全局，不处置'}
    changes = []
    for row in ev['rows']:
        if row['action'] != 'slow' or not row.get('uid'):
            continue
        base = float(current_weight.get(row['uid'], 1.0) or 1.0)
        # 乘性降权，但不越过下限，也不把本来就低于下限的号「提」上来
        target = round(min(base, max(FLOOR, base * FLOOR)), 3)
        if abs(target - base) < 1e-6:
            continue
        changes.append({**row, 'from': base, 'to': target})
    changes.sort(key=lambda c: c['score'] or 1)
    changes = changes[:MAX_CHANGES]
    return {**ev, 'changes': changes, 'reason': ''}


def apply(changes: list[dict], auth_dir: Path, *, dry_run: bool = True) -> list[dict]:
    """把权重写进账号文件。dry_run=True 只回显不落盘。

    调用方负责把 uid 映射成文件名（见 `index_accounts`）；这里只认
    `file` 字段，避免猜文件名。
    """
    results = []
    for item in changes:
        filename = item.get('file')
        if not filename:
            results.append({**item, 'ok': False, 'error': '没有对应账号文件'})
            continue
        if dry_run:
            results.append({**item, 'ok': True, 'dry_run': True})
            continue
        try:
            wb2api.set_account_weight(filename, item['to'], auth_dir)
            results.append({**item, 'ok': True, 'dry_run': False})
        except Exception as exc:  # noqa: BLE001 —— 单个账号失败不影响其余
            results.append({**item, 'ok': False, 'error': str(exc)})
    return results


def index_accounts(auth_dir: Path) -> dict[str, dict]:
    """uid8 → 账号文件条目（含 filename / weight）。"""
    out: dict[str, dict] = {}
    try:
        accounts = wb2api.list_auth_accounts(auth_dir)
    except Exception:  # noqa: BLE001
        return out
    for acc in accounts:
        uid = str(acc.get('uid') or '')
        if not uid:
            continue
        out[uid[:8]] = acc
    return out


# ---------------------------------------------------------------- 调度与设置

SETTINGS_KEY = 'speed_health'
TICK_SECONDS = 60

DEFAULT_SETTINGS = {
    'enabled': False,          # 默认关闭：不替用户擅自改权重
    'interval_minutes': 15,    # 多久评估一次
    'window_minutes': 15,      # 用多长的窗口算速率
    'dry_run': True,           # 默认只出建议，不落盘
}

_MIN_INTERVAL = 5
_MAX_INTERVAL = 24 * 60

_task: asyncio.Task | None = None
_state: dict = {'last_at': 0.0, 'last_result': None, 'last_error': ''}


def normalize_settings(raw: object) -> dict:
    """把存进 settings 的 JSON 收敛成可用配置；坏值一律回默认。"""
    src = raw if isinstance(raw, dict) else {}

    def _int(key: str, lo: int, hi: int) -> int:
        value = src.get(key, DEFAULT_SETTINGS[key])
        try:
            value = int(value)
        except (TypeError, ValueError):
            return int(DEFAULT_SETTINGS[key])
        return max(lo, min(hi, value))

    enabled = src.get('enabled', DEFAULT_SETTINGS['enabled'])
    dry = src.get('dry_run', DEFAULT_SETTINGS['dry_run'])
    return {
        'enabled': bool(enabled) if isinstance(enabled, bool) else str(enabled).lower() in ('1', 'true', 'on'),
        'interval_minutes': _int('interval_minutes', _MIN_INTERVAL, _MAX_INTERVAL),
        'window_minutes': _int('window_minutes', _MIN_INTERVAL, _MAX_INTERVAL),
        'dry_run': bool(dry) if isinstance(dry, bool) else str(dry).lower() not in ('0', 'false', 'off'),
    }


def load_settings() -> dict:
    try:
        return normalize_settings(db.get_setting(SETTINGS_KEY, None))
    except Exception:  # noqa: BLE001 —— 读设置失败不该让调度器炸
        return dict(DEFAULT_SETTINGS)


def save_settings(patch: dict) -> dict:
    merged = {**load_settings(), **(patch or {})}
    settings = normalize_settings(merged)
    db.set_setting(SETTINGS_KEY, settings)
    return settings


def evaluate_for_dir(auth_dir: Path, window_seconds: int) -> dict:
    """收集 + 评估 + 把 uid 映射到账号文件。只读。"""
    stats = collect(window_seconds)
    index = index_accounts(auth_dir)
    weights: dict[str, float] = {}
    for uid, acc in index.items():
        try:
            weights[uid] = float(acc.get('weight') or 1.0)
        except (TypeError, ValueError):
            weights[uid] = 1.0
    result = plan(stats, current_weight=weights)
    for change in result['changes']:
        acc = index.get(change['uid']) or {}
        change['file'] = acc.get('file') or ''
        change['nickname'] = acc.get('nickname') or ''
    return result


def state() -> dict:
    return {**_state, 'settings': load_settings()}


async def run_once(now: float | None = None) -> dict:
    """跑一轮：评估每个上游分组的账号池，按设置决定是否落盘。"""
    now = time.time() if now is None else now
    settings = load_settings()
    if not settings['enabled']:
        return {'skipped': 'disabled'}
    window = settings['window_minutes'] * 60
    groups = 0
    changed = 0
    details = []
    for group in upstreamsvc.list_upstreams():
        if not group.get('enabled'):
            continue
        auth_dir = str(group.get('auth_dir') or '').strip()
        if not auth_dir:
            continue
        directory = Path(auth_dir)
        if not directory.is_dir():
            continue
        groups += 1
        try:
            result = evaluate_for_dir(directory, window)
            applied = apply(result['changes'], directory, dry_run=settings['dry_run'])
            changed += sum(1 for item in applied if item.get('ok') and not item.get('dry_run'))
            details.append({
                'group': group.get('name') or '默认',
                'pool_median': result['pool_median'],
                'reason': result['reason'],
                'changes': applied,
            })
        except Exception as exc:  # noqa: BLE001 —— 单个分组失败不影响其它
            logger.warning('吞吐降权失败（%s）: %s', group.get('name'), exc)
            details.append({'group': group.get('name') or '默认', 'error': str(exc)})
    _state.update({'last_at': now, 'last_result': {'groups': groups, 'changed': changed,
                                                   'dry_run': settings['dry_run'],
                                                   'details': details}, 'last_error': ''})
    if groups:
        logger.info('吞吐降权：%d 个分组，%s %d 个账号',
                    groups, '预演' if settings['dry_run'] else '实际调整', changed)
    return _state['last_result'] or {'skipped': 'no-group'}


async def _loop() -> None:
    while True:
        try:
            settings = load_settings()
            await asyncio.sleep(max(_MIN_INTERVAL, settings['interval_minutes']) * 60)
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _state['last_error'] = str(exc)
            logger.warning('吞吐降权调度异常: %s', exc)
            await asyncio.sleep(TICK_SECONDS)


def start_scheduler() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_loop())


def stop_scheduler() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None
