"""账号列表的短 TTL 缓存（issue #444）。

## 为什么需要

196 个账号时，打开仪表板一次要读近 200 个 auth 文件、问一次上游 `/status`，
再把运行时字段逐条合并回列表；仪表板还会另外问一次 `/api/upstream/status`，
账号页与仪表板之间来回切换时整套扫描又会重跑一遍。本地读文件实测约 20 ms，
真正贵的是上游 `/status` 这段网络往返，以及同一轮首屏里被重复触发的那一份。

## 缓存什么

**只缓存 `/api/accounts` 的最终响应**（已合并上游运行时字段的那一份）。
不缓存账号文件本身，也不缓存 `/status` 的原始响应——列表里的备注、签到状态、
禁用标记来自本端库与磁盘，任何一处变化都应当立刻可见。

三重失效路径：

1. 键里带**账号目录的 mtime**：增删账号、`.disabled` 改名都会改目录 mtime，
   自动换键——即使某条写路径忘了显式失效，也不会出现「加了账号页面不更新」；
2. 写操作（增删改、禁用、重登、任务、清冷却、重启…）显式 `invalidate()`；
3. 兜底 TTL（4 秒）：漏掉的路径最多滞后一个 TTL。

## 为什么不缓存更久

冷却 / 积分 / 在途数这些运行时字段变化很快，长 TTL 会让界面说假话。4 秒只够
挡住同一轮首屏的重复请求与页面切换，**不改变任何数字口径**：命中的就是上一次
被丢弃的那份响应，字段与重新计算出来的完全一致。
"""
from __future__ import annotations

import time
from typing import Any

# 4 秒：落在 issue #444 建议的 3–5 秒区间内。再短挡不住页面切换，再长会让
# 「刚改完立刻刷新」看起来没生效。
TTL_SECONDS = 4.0

# key = (group_id, auth_dir, dir_mtime_ns) -> (写入时刻, 响应体)
# 每个分组只保留最新一份：目录 mtime 一变 key 就变了，旧 key 由 put 时清理，
# 避免频繁增删账号时缓存无限增长。
_cache: dict[tuple[Any, ...], tuple[float, dict]] = {}


def get(key: tuple[Any, ...]) -> dict | None:
    """命中未过期的缓存则返回响应体，否则 None。

    返回值是**只读**的：路由把它直接交给 FastAPI 序列化，任何调用方都不得
    修改它（否则后续命中会拿到被改过的数据）。
    """
    item = _cache.get(key)
    if item is None:
        return None
    at, payload = item
    if time.time() - at > TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return payload


def put(key: tuple[Any, ...], payload: dict) -> None:
    now = time.time()
    group = key[0] if key else None
    # 清掉过期的与同分组的旧键（同分组换了目录 mtime 时旧键已不可能再命中）。
    for old in list(_cache):
        at, _ = _cache[old]
        if now - at > TTL_SECONDS or (old and old[0] == group):
            _cache.pop(old, None)
    _cache[key] = (now, payload)


def invalidate(group_id: object) -> None:
    """清掉某个分组的缓存。`group_id` 用分组 id（默认分组是 None），不是请求参数。"""
    for key in list(_cache):
        if key and key[0] == group_id:
            _cache.pop(key, None)


def clear() -> None:
    """清空全部缓存（分组增删改等跨分组动作后使用）。"""
    _cache.clear()


def stats() -> dict:
    """缓存现状（测试与排查用）：条数与最早一条的年龄。"""
    now = time.time()
    ages = [now - at for at, _ in _cache.values()]
    return {'entries': len(_cache), 'oldest_age': max(ages, default=0.0)}
