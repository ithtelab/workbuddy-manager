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
    latency_ms / first_token_ms`，够算真实速率；
  · 执行器：`wb2api.set_account_weight()` 把 `weight` 写进账号文件，而上游
    **每 5 秒轮询 auths 目录热加载**（`internal/pool/watch.go`），
    `upsertLocked` 是整体替换 `*auth.Auth`，权重因此生效——**不需要重启**。
    重启会把在途请求全部打断，还会清掉上游内存里的状态，代价太大。

## 口径（与统计页 issue #214 一致，本仓库唯一权威口径）

    gen_ms = latency_ms - COALESCE(first_token_ms, 0)
    rate   = completion_tokens / (gen_ms / 1000)

`first_token_ms` 是首字延迟：从请求发出到上游开始吐字；`latency_ms` 是整次
请求的总耗时。生成耗时必须把首字延迟减掉——用总耗时当分母会把「上游排队慢」
误算成「吐字慢」，正是 issue #494 里首字 100+ 秒的账号被误判的根因。
非流式请求 first_token 为 NULL，按 0 兜底（等于用整个 latency，略低估，
但不会凭空造数）。

只统计 `stream=1` + `status=200` + `completion_tokens >= 300` + 生成耗时为正：
样本太短时中位数噪声大；`latency <= first_token` 的记录算不出生成速率，
留在分母外。

基准取**同一批账号的中位数**而不是绝对值：上游整体抖动时（晚高峰），
所有号一起慢，score 仍然接近 1，不会误伤全池。

## 池边界（realm / 多分组）

`request_logs` 有 `realm` 列但没有 `upstream_id`：日志无法归属到某个上游
分组。所以：

  · 每个分组按账号的 realm 分别评估（cn / global 的池基准不混在一起）；
  · 多分组时，只有 realm 集合互不相交的分组才能安全评估；
    同一 realm 被两个分组共用 → 无法区分请求归属，**跳过**并写明原因
    （GET /apply 也会 409，而不是给出被污染的结论）。

## 恢复路径（降权不是永久的）

`HIGH_SCORE` 不是装饰：降权记录里保存原始权重与到期时间。

  · 到期自动恢复：默认 6 小时（`RESTORE_SECONDS`）；
  · 提前恢复：窗口内样本 >= `MIN_SAMPLES` 且 score >= `HIGH_SCORE`；
  · 恢复前核对文件里的当前权重仍等于当初写入的降权值——若用户手动改过，
    视为人工接管，**不覆盖**，只丢弃记录；
  · 原始权重 0 表示「未设置」，恢复时写 0（清除键），不会给文件塞一个
    `weight: 1.0`。

这同时解决「降权后样本变少 → 永远 insufficient → 永不恢复」：到期恢复
不依赖样本量。

## 安全阀

  · 默认只读：`evaluate*()` 只出建议，不动文件；真正写盘要显式 apply；
  · 一次性最多降 `MAX_CHANGES` 个号，避免全池被打散；
  · 权重有下限 `FLOOR`（不写 0），给慢号留自证恢复的机会；
  · `global_slow`（过半偏慢）已删除：相对中位数下该条件数学上恒假，
    真正的保护来自「相对基准 + MAX_CHANGES + 恢复路径」。
"""
from __future__ import annotations

import asyncio
import logging
import re
import statistics
import time
from collections import defaultdict
from pathlib import Path

from .. import db, upstreamsvc
from . import wb2api

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 15 * 60
MIN_TOKENS = 300          # 低于此值的样本不进统计（短回答的速率噪声太大）
MIN_SAMPLES = 3           # 少于这个样本数不下结论
LOW_SCORE = 0.60          # 低于池内中位数的 60% → 判慢
HIGH_SCORE = 0.85         # 已降权账号回到 85% 以上 → 提前恢复
FLOOR = 0.15              # 降权下限（不写 0：0 在上游表示「清除」）
MAX_CHANGES = 3           # 单次最多调整几个号
RESTORE_SECONDS = 6 * 3600  # 降权有效期；到期自动恢复原始权重

# 账号标签的两种形态（见上游 internal/logfmt/logfmt.go）：
#   · 聊天日志：`昵称(1a2b3c4d)`，uid 是 8 位小写 hex；
#   · 少数构建/错误串：`uid=1A2B3C4D` 或裸 uid8。
# 两种都收，hex 大小写都认，统一返回小写。
_UID_PAREN_RE = re.compile(r'\(\s*([0-9a-fA-F]{8})\s*\)\s*$')
_UID_BARE_RE = re.compile(r'^(?:uid\s*=\s*)?([0-9a-fA-F]{8})$', re.IGNORECASE)


def uid_of(account: object) -> str | None:
    """`昵称(1a2b3c4d)` / 裸 `1A2B3C4D` / `uid=1a2b3c4d` → 小写 uid8。

    取不到返回 None。
    """
    if not isinstance(account, str):
        return None
    text = account.strip()
    m = _UID_PAREN_RE.search(text)
    if m:
        return m.group(1).lower()
    m = _UID_BARE_RE.match(text)
    return m.group(1).lower() if m else None


def _realm_of_account(acc: dict) -> str:
    """账号条目里的 realm（wb2api 已按 domain 兜底归一）；异常值按 cn。"""
    value = str(acc.get('realm') or '').strip().lower()
    return value if value in ('cn', 'global') else 'cn'


def collect(window_seconds: int = WINDOW_SECONDS, *,
            realm: str | None = None, now: float | None = None) -> list[dict]:
    """按账号汇总窗口内的实测吞吐。只读。

    realm 非空时只统计该版本：`request_logs.realm` 为 NULL/空串的历史记录
    按 cn 归类（与 `db.realm_of_model` / 统计页口径一致）。
    """
    end = int(now if now is not None else time.time())
    start = end - int(window_seconds)
    realm_sql = ''
    args: tuple = (start, end, MIN_TOKENS)
    if realm in ('cn', 'global'):
        realm_sql = " AND COALESCE(NULLIF(realm, ''), 'cn') = ?"
        args = (*args, realm)
    rows = db.query(
        'SELECT account, completion_tokens, latency_ms, first_token_ms '
        'FROM request_logs '
        'WHERE ts >= ? AND ts < ? AND stream = 1 AND status = 200 '
        'AND completion_tokens >= ? AND account IS NOT NULL '
        # 生成耗时必须为正：latency=0 或 latency<=first_token 的记录算不出速率，
        # 留在分母外（与 server/routers/stats.py 的口径逐字一致）。
        'AND latency_ms > COALESCE(first_token_ms, 0)'
        f'{realm_sql}',
        args,
    )
    buckets: dict[str, list[float]] = {}
    for row in rows:
        acc = row['account']
        lat = int(row['latency_ms'] or 0)
        first = int(row['first_token_ms'] or 0)
        ct = int(row['completion_tokens'] or 0)
        gen_ms = lat - first
        if not acc or gen_ms <= 0 or ct < MIN_TOKENS:
            continue
        buckets.setdefault(acc, []).append(ct / (gen_ms / 1000.0))
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
        return {'pool_median': None, 'rows': [], 'note': '样本不足（可用账号少于 2 个）'}
    pool = statistics.median([s['rate'] for s in usable])
    rows = []
    for s in stats:
        if s['samples'] < MIN_SAMPLES or s['rate'] <= 0:
            rows.append({**s, 'score': None, 'action': 'insufficient'})
            continue
        score = round(s['rate'] / pool, 3) if pool else None
        action = 'slow' if score is not None and score < LOW_SCORE else 'ok'
        rows.append({**s, 'score': score, 'action': action})
    return {'pool_median': round(pool, 1), 'rows': rows, 'note': ''}


def plan(stats: list[dict], *, current_weight: dict[str, float] | None = None) -> dict:
    """把评估结果翻成「要写哪些账号、写成多少」。

    `current_weight` 以 uid8 为键，存的是**文件里的原始值**：0/缺省 = 未设置
    （上游按 1.0 处理）。`from_raw` 要原样带进降权记录，恢复时才能写回
    「清除」而不是 `1.0`。
    """
    ev = evaluate(stats)
    current_weight = current_weight or {}
    changes = []
    for row in ev['rows']:
        if row['action'] != 'slow' or not row.get('uid'):
            continue
        try:
            raw = float(current_weight.get(row['uid'], 0) or 0)
        except (TypeError, ValueError):
            raw = 0.0
        if raw < 0:
            raw = 0.0
        base = raw if raw > 0 else 1.0
        # 乘性降权，但不越过下限，也不把本来就低于下限的号「提」上来
        target = round(min(base, max(FLOOR, base * FLOOR)), 3)
        if abs(target - base) < 1e-6:
            continue
        changes.append({**row, 'from': base, 'from_raw': raw, 'to': target})
    changes.sort(key=lambda c: c['score'] or 1)
    changes = changes[:MAX_CHANGES]
    return {**ev, 'changes': changes}


def index_accounts(auth_dir: Path, *,
                   collisions: list[dict] | None = None) -> dict[str, dict]:
    """uid8 → 账号文件条目（含 filename / weight / realm）。

    uid8 是日志里的账号标签，只取 uid 前 8 位，理论上可能碰撞。碰撞时
    **两个账号都不进索引**（宁可不评估，也不能把权重写到错误的账号上），
    并把碰撞对交给调用方展示/告警。同一 uid 的 `.json` 与 `.json.disabled`
    同时存在时保留先扫到的 enabled 那份，不算碰撞。
    """
    out: dict[str, dict] = {}
    collided: set[str] = set()
    try:
        accounts = wb2api.list_auth_accounts(auth_dir)
    except Exception as exc:  # noqa: BLE001 —— 读目录失败不能让整轮评估炸掉
        logger.warning('读取账号目录失败（%s）: %s', auth_dir, exc)
        return out
    for acc in accounts:
        uid = str(acc.get('uid') or '').strip()
        if not uid:
            continue
        key = uid[:8].lower()
        if key in collided:
            continue
        existing = out.get(key)
        if existing is not None:
            if str(existing.get('uid') or '') == uid:
                continue        # 同一账号的两份文件（enabled 优先）
            collided.add(key)
            out.pop(key, None)
            pair = {'prefix': key,
                    'uids': [str(existing.get('uid') or ''), uid]}
            if collisions is not None:
                collisions.append(pair)
            logger.warning('账号 uid8 碰撞，已跳过这两个账号：%s / %s',
                           pair['uids'][0][:16], uid[:16])
            continue
        out[key] = acc
    return out


# ---------------------------------------------------------------- 降权记录

DEMOTIONS_KEY = 'speed_health_demotions'


def _group_key(auth_dir: Path) -> str:
    """降权记录的键：绝对路径（同一目录的相对/绝对写法要归并）。"""
    try:
        return str(Path(auth_dir).resolve())
    except OSError:
        return str(auth_dir)


def _load_demotions() -> dict:
    raw = db.get_setting(DEMOTIONS_KEY, {})
    return raw if isinstance(raw, dict) else {}


def _save_demotions(data: dict) -> None:
    db.set_setting(DEMOTIONS_KEY, data)


def demotions(auth_dir: Path) -> dict[str, dict]:
    """该账号目录当前的降权记录（uid8 → 记录）。只读。"""
    bucket = _load_demotions().get(_group_key(auth_dir))
    return bucket if isinstance(bucket, dict) else {}


def _remove_demotions(auth_dir: Path, uids: list[str]) -> None:
    if not uids:
        return
    data = _load_demotions()
    key = _group_key(auth_dir)
    bucket = data.get(key)
    if not isinstance(bucket, dict):
        return
    for uid in uids:
        bucket.pop(uid, None)
    if not bucket:
        data.pop(key, None)
    _save_demotions(data)


def _pending_restores(auth_dir: Path, index: dict[str, dict],
                      rows: list[dict], *, now: float) -> tuple[list[dict], list[str]]:
    """列出该恢复的降权账号；同时返回已失效的记录（账号没了 / 人工改过权重）。

    纯读取，不写库也不写文件——是否真的恢复由调用方按 dry_run 决定。
    """
    bucket = demotions(auth_dir)
    if not bucket:
        return [], []
    scored = {row.get('uid'): row for row in rows if row.get('uid')}
    restores: list[dict] = []
    stale: list[str] = []
    for uid, record in bucket.items():
        if not isinstance(record, dict):
            stale.append(uid)
            continue
        acc = index.get(uid)
        if acc is None:
            stale.append(uid)       # 账号文件已不在这个分组
            continue
        try:
            current = float(acc.get('weight') or 0)
        except (TypeError, ValueError):
            current = 0.0
        try:
            demoted_to = float(record.get('demoted_to') or 0)
            expires_at = float(record.get('expires_at') or 0)
        except (TypeError, ValueError):
            stale.append(uid)
            continue
        if abs(current - demoted_to) > 1e-6:
            # 文件里的权重不是我们写的那一个：用户手动改过，尊重人工接管。
            stale.append(uid)
            continue
        row = scored.get(uid) or {}
        recovered = bool(
            row and row.get('score') is not None
            and int(row.get('samples') or 0) >= MIN_SAMPLES
            and float(row.get('score') or 0) >= HIGH_SCORE
        )
        expired = expires_at <= now
        if not expired and not recovered:
            continue
        restores.append({
            'uid': uid,
            'file': str(record.get('file') or acc.get('file') or ''),
            'nickname': str(acc.get('nickname') or ''),
            'original_weight': float(record.get('original_weight') or 0),
            'demoted_to': demoted_to,
            'expires_at': int(expires_at),
            'reason': 'expired' if expired else 'recovered',
        })
    return restores, stale


def restore(restores: list[dict], auth_dir: Path, *,
            dry_run: bool = True) -> list[dict]:
    """按记录恢复原始权重。dry_run=True 只回显。

    `original_weight` 传 0 表示「原来没设置」→ set_account_weight(0) 清除键，
    不会给账号文件留下一个默认值。
    """
    results: list[dict] = []
    restored: list[str] = []
    for item in restores or []:
        filename = str(item.get('file') or '')
        if not filename:
            results.append({**item, 'ok': False, 'error': '没有对应账号文件'})
            continue
        if dry_run:
            results.append({**item, 'ok': True, 'dry_run': True})
            continue
        try:
            wb2api.set_account_weight(
                filename, float(item.get('original_weight') or 0), auth_dir)
        except Exception as exc:  # noqa: BLE001 —— 单个账号失败不影响其余
            results.append({**item, 'ok': False, 'error': str(exc)})
            continue
        restored.append(str(item.get('uid') or ''))
        results.append({**item, 'ok': True, 'dry_run': False})
    if restored and not dry_run:
        _remove_demotions(auth_dir, [u for u in restored if u])
    return results


def prune_demotions(auth_dir: Path, stale: list[str], *, dry_run: bool = True) -> None:
    """丢弃失效记录（账号已删 / 用户手动改过权重）。dry_run 时不动。"""
    if not dry_run and stale:
        _remove_demotions(auth_dir, stale)


def apply(changes: list[dict], auth_dir: Path, *,
          dry_run: bool = True, now: float | None = None) -> list[dict]:
    """把权重写进账号文件，并记下原始权重与恢复时间。dry_run 只回显。

    调用方负责把 uid 映射成文件名（见 `index_accounts`）；这里只认
    `file` 字段，避免猜文件名。
    """
    now = time.time() if now is None else now
    results: list[dict] = []
    records: dict | None = None
    for item in changes or []:
        filename = item.get('file')
        if not filename:
            results.append({**item, 'ok': False, 'error': '没有对应账号文件'})
            continue
        if dry_run:
            results.append({**item, 'ok': True, 'dry_run': True})
            continue
        try:
            wb2api.set_account_weight(filename, item['to'], auth_dir)
        except Exception as exc:  # noqa: BLE001 —— 单个账号失败不影响其余
            results.append({**item, 'ok': False, 'error': str(exc)})
            continue
        results.append({**item, 'ok': True, 'dry_run': False})
        uid = str(item.get('uid') or '')
        if not uid:
            continue
        if records is None:
            records = _load_demotions()
        bucket = records.setdefault(_group_key(auth_dir), {})
        if not isinstance(bucket, dict):
            bucket = {}
            records[_group_key(auth_dir)] = bucket
        previous = bucket.get(uid) if isinstance(bucket.get(uid), dict) else {}
        # 已经有一条降权记录时保留**最初**的原始权重：连续降权不能把
        # 「1.0 → 0.15」的原始值覆盖成 0.15，否则恢复时等于没恢复。
        original = previous.get('original_weight')
        if original is None:
            original = item.get('from_raw')
            if original is None:
                original = item.get('from', 0)
        bucket[uid] = {
            'uid': uid,
            'file': filename,
            'original_weight': float(original or 0),
            'demoted_to': float(item['to']),
            'applied_at': int(previous.get('applied_at') or now),
            'expires_at': int(now + RESTORE_SECONDS),
        }
    if records is not None:
        _save_demotions(records)
    return results


# ---------------------------------------------------------------- 评估入口


def _evaluate_with_index(auth_dir: Path, window_seconds: int, realm: str | None,
                         index: dict[str, dict], collisions: list[dict], *,
                         now: float) -> dict:
    if realm is not None:
        index = {uid: acc for uid, acc in index.items()
                 if _realm_of_account(acc) == realm}
    if not index:
        return {
            'realm': realm, 'pool_median': None, 'rows': [], 'changes': [],
            'restores': [], 'collisions': collisions, 'stale_demotions': [],
            'note': '该分组没有可评估的本地账号',
        }
    weights: dict[str, float] = {}
    for uid, acc in index.items():
        try:
            weights[uid] = float(acc.get('weight') or 0)
        except (TypeError, ValueError):
            weights[uid] = 0.0
    # 只保留本分组、本版本的账号：窗口内可能还有已移走/已禁用账号的历史日志，
    # 或别的分组同 realm 的残留；它们不能参与本池的中位数，更不能被写权重。
    stats = [s for s in collect(window_seconds, realm=realm, now=now)
             if s.get('uid') in index]
    result = plan(stats, current_weight=weights)
    restores, stale = _pending_restores(auth_dir, index, result['rows'], now=now)
    restore_uids = {item['uid'] for item in restores}
    changes = [c for c in result['changes']
               if c['uid'] in index and c['uid'] not in restore_uids]
    for change in changes:
        acc = index.get(change['uid']) or {}
        change['file'] = acc.get('file') or ''
        change['nickname'] = acc.get('nickname') or ''
    for item in restores:
        acc = index.get(item['uid']) or {}
        if not item.get('file'):
            item['file'] = acc.get('file') or ''
        if not item.get('nickname'):
            item['nickname'] = acc.get('nickname') or ''
    bucket = demotions(auth_dir)
    for row in result['rows']:
        record = bucket.get(row.get('uid') or '')
        row['demoted_until'] = (int(record.get('expires_at') or 0)
                                if isinstance(record, dict) else 0)
    result.update({
        'realm': realm, 'changes': changes, 'restores': restores,
        'collisions': collisions, 'stale_demotions': stale,
    })
    return result


def evaluate_for_dir(auth_dir: Path, window_seconds: int, *,
                     realm: str | None = None, now: float | None = None) -> dict:
    """评估一个分组的某个版本。只读。

    realm 缺省时从账号文件推导；分组内同时含 cn/global 时要求显式指定，
    避免把两个版本的池基准混在一起。
    """
    now = time.time() if now is not None else now
    collisions: list[dict] = []
    index = index_accounts(auth_dir, collisions=collisions)
    realms = sorted({_realm_of_account(acc) for acc in index.values()})
    if realm is None:
        if len(realms) > 1:
            raise ValueError('该分组的账号同时属于国内版与国际版，必须按 realm 分别评估')
        realm = realms[0] if realms else None
    elif realm not in ('cn', 'global'):
        raise ValueError('realm 只能是 cn 或 global')
    return _evaluate_with_index(auth_dir, window_seconds, realm, index,
                                collisions, now=now)


def _group_entries() -> list[dict]:
    """所有可评估分组：`{group, dir, realms}`。只读账号文件。"""
    entries: list[dict] = []
    for group in upstreamsvc.list_upstreams():
        if not group.get('enabled'):
            continue
        raw = str(group.get('auth_dir') or '').strip()
        if not raw:
            continue
        directory = Path(raw)
        if not directory.is_dir():
            continue
        index, collisions = index_accounts(directory)
        entries.append({
            'group': group,
            'dir': directory,
            'realms': {_realm_of_account(acc) for acc in index.values()},
            'collisions': collisions,
        })
    return entries


def pool_conflict(auth_dir: Path) -> list[str]:
    """与目标分组共用 realm 的其它分组名。

    `request_logs` 没有 `upstream_id` 列：同一 realm 被多个分组共用时，日志
    无法区分归属，任何「池内中位数」都会被别的分组污染。返回空列表 = 安全。
    """
    target = _group_key(auth_dir)
    entries = _group_entries()
    own = next((e for e in entries if _group_key(e['dir']) == target), None)
    if own is None or not own['realms']:
        return []
    return [str(e['group'].get('name') or '默认')
            for e in entries
            if _group_key(e['dir']) != target and e['realms'] & own['realms']]


def evaluate_group(auth_dir: Path, window_seconds: int, *,
                   now: float | None = None, check_conflict: bool = True) -> dict:
    """评估一个分组的全部版本（cn / global 各一份基准）。只读。"""
    now = time.time() if now is not None else now
    conflicts = pool_conflict(auth_dir) if check_conflict else []
    if conflicts:
        return {'conflicts': conflicts, 'collisions': [], 'realms': []}
    collisions: list[dict] = []
    index = index_accounts(auth_dir, collisions=collisions)
    realms = sorted({_realm_of_account(acc) for acc in index.values()})
    return {
        'conflicts': [],
        'collisions': collisions,
        'realms': [
            _evaluate_with_index(auth_dir, window_seconds, realm, index,
                                 collisions, now=now)
            for realm in realms
        ],
    }


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
# 同一时刻只允许一轮评估/落盘。asyncio 单线程里「检查 + 置位」是原子的，
# 不需要 asyncio.Lock（后者绑定事件循环，跨测试的事件循环会报错）。
_run_busy = False


def _begin_run() -> bool:
    global _run_busy
    if _run_busy:
        return False
    _run_busy = True
    return True


def _end_run() -> None:
    global _run_busy
    _run_busy = False


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
        'dry_run': bool(dry) if isinstance(dry, bool) else str(dry).lower() not in ('0', 'false', 'off', 'no'),
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


def state() -> dict:
    return {**_state, 'settings': load_settings()}


def _run_once_sync(now: float | None) -> dict:
    now = time.time() if now is None else now
    settings = load_settings()
    if not settings['enabled']:
        return {'skipped': 'disabled'}
    window = settings['window_minutes'] * 60
    dry = bool(settings['dry_run'])

    entries = _group_entries()
    if not entries:
        result = {'groups': 0, 'changed': 0, 'restored': 0, 'dry_run': dry,
                  'details': [], 'skipped': 'no-group'}
        _state.update({'last_at': now, 'last_result': result, 'last_error': ''})
        return result

    # 同一 realm 被多个分组共用 → 日志无法归属，跳过相关分组。
    owners: dict[str, list[int]] = defaultdict(list)
    for i, entry in enumerate(entries):
        for realm in entry['realms']:
            owners[realm].append(i)
    conflicted = {realm for realm, ids in owners.items() if len(ids) > 1}

    changed = restored = 0
    details: list[dict] = []
    errors: list[str] = []
    for entry in entries:
        group = entry['group']
        name = str(group.get('name') or '默认')
        safe_realms = entry['realms'] - conflicted
        if entry['realms'] & conflicted:
            details.append({
                'group': name,
                'skipped': '多分组共用同一版本（request_logs 无 upstream_id），跳过',
                'realms': sorted(entry['realms'] & conflicted),
            })
            if not safe_realms:
                continue
        if not entry['realms']:
            details.append({'group': name, 'skipped': '该分组没有本地账号'})
            continue
        stale: list[str] = []
        for realm in sorted(safe_realms):
            try:
                result = evaluate_for_dir(entry['dir'], window, realm=realm, now=now)
                restored_items = restore(result['restores'], entry['dir'],
                                         dry_run=dry)
                applied = apply(result['changes'], entry['dir'],
                                dry_run=dry, now=now)
                restored += sum(1 for item in restored_items
                                if item.get('ok') and not item.get('dry_run'))
                changed += sum(1 for item in applied
                               if item.get('ok') and not item.get('dry_run'))
                stale.extend(result.get('stale_demotions') or [])
                details.append({
                    'group': name,
                    'realm': realm,
                    'pool_median': result['pool_median'],
                    'reason': result['reason'],
                    'collisions': result['collisions'],
                    'restored': restored_items,
                    'changes': applied,
                })
            except Exception as exc:  # noqa: BLE001 —— 单个分组失败不影响其它
                logger.warning('吞吐降权失败（%s/%s）: %s', name, realm, exc)
                errors.append(f'{name}/{realm}: {exc}')
                details.append({'group': name, 'realm': realm, 'error': str(exc)})
        prune_demotions(entry['dir'], stale, dry_run=dry)

    result = {'groups': len(entries), 'changed': changed, 'restored': restored,
              'dry_run': dry, 'details': details}
    # 不要每轮无条件清空：有任何分组失败时保留错误，全部成功才清。
    _state.update({'last_at': now, 'last_result': result,
                   'last_error': '; '.join(errors)[:500]})
    if entries:
        logger.info('吞吐降权：%d 个分组，%s %d 个账号，恢复 %d 个',
                    len(entries), '预演' if dry else '实际调整', changed, restored)
    return result


async def run_once(now: float | None = None) -> dict:
    """跑一轮：评估每个上游分组的账号池，按设置决定是否落盘。

    与 POST /run、手动 apply 共用 `_run_busy`：同一时刻只跑一轮，避免
    调度器与手动触发同时写同一批账号文件。
    """
    if not _begin_run():
        return {'skipped': 'busy'}
    try:
        return await asyncio.to_thread(_run_once_sync, now)
    finally:
        _end_run()


def _apply_sync(auth_dir: Path, window_seconds: int, dry_run: bool,
                now: float | None) -> dict:
    now = time.time() if now is None else now
    grouped = evaluate_group(auth_dir, window_seconds, now=now, check_conflict=True)
    if grouped['conflicts']:
        return {'skipped': 'group-conflict',
                'conflict_groups': grouped['conflicts'],
                'dry_run': dry_run, 'window_seconds': window_seconds,
                'realms': []}
    out = []
    stale: list[str] = []
    for result in grouped['realms']:
        restored_items = restore(result['restores'], auth_dir, dry_run=dry_run)
        applied = apply(result['changes'], auth_dir, dry_run=dry_run, now=now)
        stale.extend(result.get('stale_demotions') or [])
        out.append({
            'realm': result['realm'],
            'pool_median': result['pool_median'],
            'reason': result['reason'],
            'note': result.get('note', ''),
            'collisions': result['collisions'],
            'restored': restored_items,
            'applied': applied,
        })
    prune_demotions(auth_dir, stale, dry_run=dry_run)
    return {'dry_run': dry_run, 'window_seconds': window_seconds,
            'conflicts': grouped['conflicts'], 'realms': out}


async def apply_for_group(auth_dir: Path, window_seconds: int, *,
                          dry_run: bool = True, now: float | None = None) -> dict:
    """手动 apply / 预览：与调度器共用互斥标记，SQLite 与文件 I/O 在线程里跑。"""
    if not _begin_run():
        return {'skipped': 'busy'}
    try:
        return await asyncio.to_thread(_apply_sync, auth_dir, window_seconds,
                                       dry_run, now)
    finally:
        _end_run()


async def _loop() -> None:
    while True:
        try:
            # 读设置也是同步 SQLite：挪到线程，别让它卡住事件循环。
            settings = await asyncio.to_thread(load_settings)
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
