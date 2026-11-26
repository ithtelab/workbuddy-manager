"""实时积分的短期缓存 + 积分变动留痕。

为什么要缓存：积分需要直接调腾讯接口（上游 /status 的 credits 只在它自己的
定时任务里刷新，滞后可达数小时）。但每次打开页面都全量查询会给腾讯带来
不必要的请求，账号多时更明显，也可能触发限流。

因此加一层 TTL 缓存：TTL 内的重复查询直接命中缓存，超过 TTL 才真正请求。
手动「刷新积分」可传 force=True 绕过缓存。命中缓存时会明确标记，
前端据此区分「实时」与「缓存」，不让滞后的数字伪装成刚查到的。

积分变动留痕：签到 / 活跃上报 / 猫猫旅行等都会让余额增加，但上游只在
旅行领奖时打印具体数额，签到与活跃都不打。因此这里在**每次真实查询成功后**
比对上一次的余额，只要增加就记一条流水，从而覆盖所有获取渠道。
（只记增加：减少是消耗，不属于"积分获取"。）
"""
from __future__ import annotations

import datetime
import json
import re
import time
import uuid

from .. import db
from . import tencent

# uid -> (查询时间戳, 是否成功, 积分值, 消息, 到期列表)
# 到期列表跟着积分一起缓存：两者出自同一次账单请求，分开放会出现「余额是刚查的、
# 到期时间还是上一分钟那次查询的」这种自相矛盾的展示。
_cache: dict[str, tuple[float, bool, int | float | None, str, list[dict]]] = {}
TTL_SECONDS = 60

# 余额快照持久化在 settings 表，重启后仍能继续比对
_SNAP_KEY = 'credits_snapshot'

# 单次余额增加的记录上限，避免异常值刷屏（正常任务收益都远小于此）
_MAX_DELTA = 1_000_000


def cached_credits(uid: str) -> tuple[bool, int | float | None, str, list[dict]] | None:
    """命中未过期的缓存则返回，否则 None。"""
    item = _cache.get(uid)
    if not item:
        return None
    ts, ok, credits, message, expiries = item
    if time.time() - ts > TTL_SECONDS:
        return None
    return ok, credits, message, expiries


def cache_age(uid: str) -> int | None:
    """缓存已存在多少秒（未命中返回 None），用于界面标注「x 秒前」。"""
    item = _cache.get(uid)
    if not item:
        return None
    return max(0, int(time.time() - item[0]))


# ── 余额快照与变动流水 ───────────────────────────────────
def _load_snapshot() -> dict:
    raw = db.get_setting(_SNAP_KEY, None)
    if not raw:
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_snapshot(snap: dict) -> None:
    try:
        db.set_setting(_SNAP_KEY, json.dumps(snap, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass


def _package_keys(expiries: list[dict] | None) -> list[list[int]] | None:
    """把到期积分包列表转成可持久化的 `[到期时刻, 额度, 到账时刻]` 键。

    第三个字段是上游 CreateTime（epoch 秒）；缺失/旧补丁给 0，记账时回退采集
    时刻。拿不到到期列表返回 None（与「空列表」区分）。
    """
    if not isinstance(expiries, list):
        return None
    out: list[list[int]] = []
    for e in expiries:
        if not isinstance(e, dict):
            continue
        try:
            at = int(e.get('at'))
            amount = int(e.get('amount'))
        except (TypeError, ValueError):
            continue
        try:
            created = int(e.get('created') or 0)
        except (TypeError, ValueError):
            created = 0
        out.append([at, amount, created])
    return sorted(out, key=lambda item: (item[0], item[2]))


def _package_balances(packages: list[list[int]]) -> dict[int, int]:
    """按到期时刻汇总剩余额度；兼容旧快照的两元组。"""
    balances: dict[int, int] = {}
    for item in packages:
        at, amount = int(item[0]), int(item[1])
        balances[at] = balances.get(at, 0) + amount
    return balances


def record_balance(uid: str, nickname: str, credits: int | float | None,
                   expiries: list[dict] | None = None) -> int | None:
    """记录一次余额；写一条积分获取流水并返回本次入账额度。

    收入判据（issue #393）优先用**积分包出现**而不是余额净增：签到发 100、同一天
    有 100 的旧包到期时，余额净增为 0，只按净增记账会把这次签到收入完全吞掉。
    `expiries.amount` 是套餐剩余额度，会随消费减少。按到期时间汇总后，只记
    剩余额度的正向差额。新增到期桶仍能识别同日旧包到期抵消的签到收入。
    到期列表不可用时回退余额净增。

    首次见到的账号只建立基线、不记流水，否则会把历史余额误报成「刚获得」。
    """
    if not uid or credits is None:
        return None
    try:
        value = int(credits)
    except (TypeError, ValueError):
        return None

    now = int(time.time())
    snap = _load_snapshot()
    prev_raw = snap.get(uid)
    prev = _snapshot_value(prev_raw)
    prev_packages = None
    if isinstance(prev_raw, dict):
        raw_packages = prev_raw.get('packages')
        if isinstance(raw_packages, list):
            prev_packages = []
            for item in raw_packages:
                if isinstance(item, list) and len(item) >= 2:
                    try:
                        prev_packages.append([int(item[0]), int(item[1]),
                                              int(item[2]) if len(item) >= 3 else 0])
                    except (TypeError, ValueError):
                        continue
    packages = _package_keys(expiries)
    # 正余额却没有到期数据，不代表已知的空包基线。
    if packages == [] and value > 0:
        packages = None
    if prev_packages == [] and prev is not None and prev > 0:
        prev_packages = None
    previous_observation = prev_raw.get('observation_id', prev_raw.get('at', 0)) if isinstance(prev_raw, dict) else 0
    entry = {'credits': value, 'at': now, 'observation_id': uuid.uuid4().hex}
    if packages is not None:
        entry['packages'] = packages
    snap[uid] = entry
    _save_snapshot(snap)

    if prev is None:
        return None

    # 1) 同一到期桶的余额下降是消费。补充额度只计增加的部分。
    income = 0
    logs: list[dict] = []
    if packages is not None and prev_packages is not None:
        before = _package_balances(prev_packages)
        # 同一到期桶可能有多条包，额度先汇总；到账时刻取该桶里最晚的 CreateTime。
        created_by_at: dict[int, int] = {}
        for at, _amount, created in packages:
            created_by_at[at] = max(created_by_at.get(at, 0), created)
        for at, amount in _package_balances(packages).items():
            increase = amount - before.get(at, 0)
            if increase <= 0 or increase > _MAX_DELTA:
                continue
            income += increase
            created = created_by_at.get(at, 0)
            # 用上游到账时刻，而不是采集时刻（issue #414）；异常/未来值回退 now。
            deliver_at = created if 0 < created <= now else now
            logs.append({
                'ts': deliver_at,
                'uid': uid,
                'kind': 'credit',
                'level': 'credit',
                'credits': increase,
                'message': f'积分包 +{increase}（{datetime.datetime.fromtimestamp(at).strftime("%m-%d %H:%M")} 到期）'
                           + (f' · {nickname}' if nickname else ''),
                # 同一快照的并发观察去重，消费后再次补充相同额度可再次入账。
                'dedup_key': f'credit|{uid}|pkg-delta|{previous_observation}|{at}|{amount}',
            })
    # 2) 到期列表拿不到/没有新包时，退回余额净增（存量行为）。
    if not logs:
        delta = value - prev
        if delta <= 0 or delta > _MAX_DELTA:
            return None
        income = delta
        logs.append({
            'ts': now,
            'uid': uid,
            'kind': 'credit',
            'level': 'credit',
            'credits': delta,
            'message': f'余额 +{delta}（{prev} → {value}）'
                       + (f' · {nickname}' if nickname else ''),
            # 用余额值做去重键：同一余额只会留下一条流水
            'dedup_key': f'credit|{uid}|{value}',
        })
    db.add_task_logs(logs)
    return income or None


# ── 登录态失效标记（issue #490）────────────────────────────────────────
#
# 现场：账号的登录态被上游作废后，billing 接口返回 401（APISIX 拒在网关层，
# 响应体常常是一段 HTML），面板拿不到实时积分，只能回退展示 /status 里的旧
# 快照 —— 那个数字会误导用户以为账号还能用。
#
# 判定克制是这里最重要的部分：
#   · 只认**明确的 401**（`查询失败 code=401`）。非 401 的失败一律不动标记，
#     否则一次超时就把有效告警抹掉了；
#   · 连续 `_SESSION_DEAD_MIN_FAILS` 次才置位。上游自己的分类里，纯 401 可能
#     是网关抖动或 token 轮换竞态（`internal/upstream/client.go` 的
#     `ErrAuthRejected` 注释），一次就判死太重；
#   · 置位后 7 天有效；期间任一次查询成功（或重登成功）立即清除。
_SESSION_DEAD_KEY = 'credits_session_dead'
_SESSION_DEAD_TTL = 7 * 86400
_SESSION_DEAD_MIN_FAILS = 2
_401_RE = re.compile(r'code=401\b')


def _is_upstream_401(message: str) -> bool:
    """错误文案是否是**上游**的 401。

    `tencent.fetch_credits` 的失败文案有两种形态：
      · `查询失败 code=<业务码>` —— 拿到了响应，业务码来自信封或 HTTP 状态；
      · `查询异常: ...` —— 网络/代理层异常，不算登录态失效。

    代理认证失败通常以 `查询异常` 出现（httpx 的 ProxyError），不会命中这里；
    这里再排除文案里带「代理/proxy」的兜底形态。
    """
    text = str(message or '')
    if '查询异常' in text:
        return False
    if '代理' in text or 'proxy' in text.lower():
        return False
    return bool(_401_RE.search(text))


def _session_dead_raw() -> dict:
    """原始标记表：{uid: {'n': 连续失败次数, 'at': 首次置位时刻}}。"""
    try:
        raw = db.get_setting(_SESSION_DEAD_KEY)
    except Exception:  # noqa: BLE001
        return {}
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            try:
                out[str(k)] = {'n': int(v.get('n') or 0),
                               'at': float(v.get('at') or 0)}
            except (TypeError, ValueError):
                continue
        else:
            # 兼容旧形态（uid -> 时间戳）：当作已置位 1 次。
            try:
                out[str(k)] = {'n': _SESSION_DEAD_MIN_FAILS, 'at': float(v)}
            except (TypeError, ValueError):
                continue
    return out


def session_dead_uids() -> set[str]:
    """已置位且未过期的「登录态失效」uid 集合。"""
    now = time.time()
    out: set[str] = set()
    for uid, rec in _session_dead_raw().items():
        if rec['n'] < _SESSION_DEAD_MIN_FAILS:
            continue
        if now - rec['at'] <= _SESSION_DEAD_TTL:
            out.add(uid)
    return out


def _save_session_dead(marks: dict) -> None:
    try:
        db.set_setting(_SESSION_DEAD_KEY, json.dumps(marks, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        pass


def note_session_failure(uid: str) -> None:
    """记一次 401。只在计数变化时写库（列表轮询会频繁调用）。"""
    if not uid:
        return
    uid = str(uid)
    marks = _session_dead_raw()
    rec = marks.get(uid) or {'n': 0, 'at': 0.0}
    n = min(int(rec['n']) + 1, 99)
    at = rec['at']
    if n >= _SESSION_DEAD_MIN_FAILS and not at:
        at = time.time()   # 首次达到阈值 → TTL 从这里起算
    if n == rec['n'] and at == rec['at']:
        return
    marks[uid] = {'n': n, 'at': at}
    _save_session_dead(marks)


def clear_session_dead(uid: str) -> None:
    """清除标记（查询成功 / 重登成功 / 账号删除）。"""
    if not uid:
        return
    uid = str(uid)
    marks = _session_dead_raw()
    if uid not in marks:
        return
    marks.pop(uid, None)
    _save_session_dead(marks)


async def get_credits(
    auth: dict,
    *,
    force: bool = False,
    nickname: str = '',
    record: bool = True,
) -> tuple[bool, int | float | None, str, bool, int | None, list[dict]]:
    """查询积分（带 TTL 缓存）。

    返回 (是否成功, 积分, 消息, 是否来自缓存, 缓存已存在秒数, 到期列表)。
    到期列表见 tencent.fetch_credits 的说明。
    真实查询成功且 record=True 时，顺带比对余额并记录积分变动流水。
    """
    uid = str(auth.get('uid') or '')
    if uid and not force:
        hit = cached_credits(uid)
        if hit is not None:
            ok, credits, message, expiries = hit
            return ok, credits, f'{message}（缓存）', True, cache_age(uid), expiries

    ok, credits, message, expiries = await tencent.fetch_credits(auth)
    if uid:
        # 登录态失效标记：只在**明确的 401** 上计数，且连续达到阈值才算数。
        # 非 401 的临时失败（超时 / 5xx / 代理错误）什么都不做 —— 不能把已有的
        # 有效标记清掉，否则一次网络抖动就抹掉告警。
        if ok:
            clear_session_dead(uid)
        elif _is_upstream_401(message):
            note_session_failure(uid)
        # 仅缓存成功结果：失败往往是临时网络问题，不该被缓存住
        if ok:
            _cache[uid] = (time.time(), ok, credits, message, expiries)
        else:
            _cache.pop(uid, None)

    if ok and record:
        try:
            record_balance(uid, nickname, credits, expiries)
        except Exception:  # noqa: BLE001
            pass

    return ok, credits, message, False, None, expiries


def forget(uid: str) -> None:
    """账号删除后把它的余额登记一并清掉（issue #404）。

    快照不是审计日志：它回答的是「这些账号现在还有多少积分」。账号都删了，
    留着这条登记只会让按 uid/按域汇总的外部调用方算出虚高的余额。历史留痕
    由 task_logs 负责，不靠这张设置表。
    """
    if not uid:
        return
    uid = str(uid)
    snap = _load_snapshot()
    if uid in snap:
        snap.pop(uid, None)
        _save_snapshot(snap)
    _cache.pop(uid, None)
    # 账号已经删了，登录态失效标记也一并清掉，避免 uid 被复用时误报。
    clear_session_dead(uid)


def invalidate(uid: str | None = None) -> None:
    """清除缓存（uid 为空则全清）。签到等会改变余额的操作后调用。"""
    if uid:
        _cache.pop(uid, None)
    else:
        _cache.clear()

def _snapshot_value(raw: object) -> int | None:
    """快照条目的数值。新形态是 {'credits': n, 'at': ts}；升级前存的是纯数字。"""
    if isinstance(raw, dict):
        raw = raw.get('credits')
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _snapshot_time(raw: object) -> int | None:
    """快照条目的登记时刻（旧形态没有，返回 None）。"""
    if isinstance(raw, dict):
        try:
            return int(raw.get('at'))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    return None


def snapshot_entries() -> dict:
    """已登记的积分快照（**只读本地记录，不触发任何上游查询**）。

    给管理端的只读查询用：面板平时已经在记录余额（`record_balance`），这里只是把
    那份记录连同**登记时刻**一起交出来。数字可能是几分钟/几小时前的，调用方要如实
    标注为「已登记」而不是实时余额 —— 这也是它不查上游的原因：既省一次账单请求，
    也不会因为别人刷新页面就给上游加压。
    """
    snap = _load_snapshot()
    rows: list[dict] = []
    for uid, raw in snap.items():
        value = _snapshot_value(raw)
        if value is None:
            continue
        rows.append({'uid': str(uid), 'credits': value, 'registered_at': _snapshot_time(raw)})
    rows.sort(key=lambda r: r['uid'])
    newest = max((r['registered_at'] or 0 for r in rows), default=0)
    return {'snapshot_at': newest, 'count': len(rows), 'accounts': rows}
