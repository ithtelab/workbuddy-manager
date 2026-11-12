"""账号列表的「自定义顺序」（拖拽排序）。

为什么存在服务端而不是浏览器本地：顺序是这份部署的**共享视图** —— 同一个面板
可能有多个人在用（管理员 + 只读用户），也可能换设备打开。放在 localStorage 里
的话，每个人看到的是自己那套顺序，清一次缓存就丢。

顺序**只是展示顺序**：它不进上游、不影响选号。选号发生在上游 workbuddy2api
（按积分、权重与冷却挑号），改这里不会改变任何一次调用的结果，也不改变
`/api/accounts` 返回的数据本身。

存储：`settings` 表，键 `accounts_order:<group_id>`，值为 uid 数组。
按分组建键，因为一个分组 = 一套账号池，两个分组的账号互不重叠 —— 共用一份
顺序既没有意义，又会在切换分组时把对方的顺序打乱。
"""
from __future__ import annotations

from typing import Iterable

from .. import db

_KEY_PREFIX = 'accounts_order'
# 单条顺序的上限：正常分组是几十个号，5000 已经远远够用；再大只可能是坏客户端
# 或恶意请求，没必要让它把 settings 表撑大。
MAX_ENTRIES = 5000


def _key(group_id: object) -> str:
    """默认分组的 id 是 None（upstreamsvc.DEFAULT_ID），统一落成 0。"""
    try:
        gid = int(group_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        gid = 0
    return f'{_KEY_PREFIX}:{gid}'


def normalize(raw: object) -> list[str]:
    """把任意输入归一成 uid 数组；不是数组就抛 ValueError。

    **去重是必要的而不是顺手**：重复的 uid 会让名次表里先出现的那个生效，
    后面的重复项变成死数据 —— 用户拖了半天发现某一项永远不动，却看不出原因。
    顺序里出现非字符串同理：直接拒绝比静默丢弃好，静默丢弃会让"保存成功了但
    顺序少了一个号"这种问题没人查得出来。
    """
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError('order 必须是 uid 数组')

    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise ValueError('order 的每一项必须是字符串（账号 uid）')
        uid = item.strip()
        if not uid or uid in seen:
            continue
        seen.add(uid)
        out.append(uid)
        if len(out) >= MAX_ENTRIES:
            break
    return out


def load(group_id: object) -> list[str]:
    """读该分组的自定义顺序；没有存过（或存坏了）返回空数组。

    **坏数据不抛错**：`settings` 里那一行可能来自手工改库或旧版本写进去的形态。
    顺序只是展示偏好，为它让整个账号页 500 是最差的取舍 —— 退化成「后端顺序」
    用户仍能正常操作，而报错时他连改回来的入口都看不到。
    """
    try:
        return normalize(db.get_setting(_key(group_id), []) or [])
    except ValueError:
        return []


def save(group_id: object, raw: object) -> list[str]:
    """保存并返回归一后的顺序。"""
    order = normalize(raw)
    db.set_setting(_key(group_id), order)
    return order


def rank(order: Iterable[str]) -> dict[str, int]:
    """uid → 名次。重复项取**第一次**出现的位置（与 normalize 同口径）。"""
    out: dict[str, int] = {}
    for i, uid in enumerate(order):
        if uid not in out:
            out[uid] = i
    return out


def apply(accounts: Iterable, order: Iterable[str]) -> list:
    """按 order 重排账号；顺序里没有的账号保持原有相对次序排在后面。

    为什么未知项排最后而不是排最前：新加的账号一定不在旧顺序里，排最前会让它们
    每次都顶到列表顶部、把用户排好的顺序整个往下挤一屏。排最后则只是 appended，
    用户看到的就是"新号加在末尾"。
    """
    rows = list(accounts)
    table = rank(order)
    if not table:
        return rows
    keyed = []
    for i, row in enumerate(rows):
        uid = str(getattr(row, 'uid', '') or '')
        r = table.get(uid)
        keyed.append((r if r is not None else None, i, row))
    keyed.sort(key=lambda item: (item[0] is None, item[0] or 0, item[1]))
    return [item[2] for item in keyed]
