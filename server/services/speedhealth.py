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

import re
import statistics
import time
from pathlib import Path

from .. import db
from . import wb2api

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
