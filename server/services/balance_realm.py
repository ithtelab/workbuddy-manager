"""余额口径：按账号版本（国内 cn / 国际 global）分档。

为什么单独一个模块：这里的判定全是纯函数——不读库、不发请求，
所以能在没有数据库、没有上游的单元测试里直接跑；`balance.py` 只负责取数，
口径判断集中在这里，改口径不会碰到路由代码。

口径优先级（见 `resolve_scope`）：
  1. 显式指定 `?scope=` → 用指定值
  2. 密钥设了额度上限 → merged（余额按密钥自己的上限算，拆池没有意义）
  3. 密钥本身限定版本（realm=cn/global）→ 那一档
  4. 密钥不限版本 → 跟最近一次真实调用的版本走
  5. 都没有 → merged
"""
from __future__ import annotations

# auto = 自动判定；其余为显式口径
SCOPES = ('auto', 'cn', 'global', 'merged')
# 池口径只有这两个
POOL_SCOPES = ('cn', 'global')


def normalize_scope(value: object) -> str:
    """归一化口径参数：非法值一律当 auto，不报错（调用方是客户端面板）。"""
    text = str(value or '').strip().lower()
    return text if text in SCOPES else 'auto'


def normalize_realm(value: object) -> str:
    """归一化版本值：只认 cn / global，其余当空（= 未限定）。"""
    text = str(value or '').strip().lower()
    return text if text in POOL_SCOPES else ''


def resolve_scope(*, key_realm: object = '', has_limit: bool = False,
                  last_realm: object = '', requested: object = '') -> str:
    """决定这次余额报哪个池。返回 'cn' / 'global' / 'merged'。

    注意 `has_limit` 优先于 realm：有额度上限的密钥回答的是「我这把还能花多少」，
    那是按它自己的上限和已用量算的，与账号池无关；硬拆成池会出现
    「上限 100、池里剩 5000」这种自相矛盾的读数。
    """
    want = normalize_scope(requested)
    if want != 'auto':
        return want
    if has_limit:
        return 'merged'
    realm = normalize_realm(key_realm)
    if realm:
        return realm
    last = normalize_realm(last_realm)
    return last or 'merged'


def realm_credits(status: object, realm: object) -> float | None:
    """从上游 `/status` 里按 realm 汇总账号 credits。

    口径与 `_pool_total` 保持一致：不区分正常 / 冷却 / 停用，全池相加——
    否则客户端上的数字会与管理端首页对不上，用户会以为是余额错了。
    拿不到（未连接 / 无账号）返回 None，由调用方回退到合并口径。
    """
    target = normalize_realm(realm)
    if not target or not isinstance(status, dict):
        return None
    accounts = status.get('accounts')
    if not isinstance(accounts, list) or not accounts:
        return None
    total = 0.0
    seen = False
    for item in accounts:
        if not isinstance(item, dict):
            continue
        if normalize_realm(item.get('realm') or 'cn') != target:
            continue
        seen = True
        try:
            total += float(item.get('credits') or 0)
        except (TypeError, ValueError):
            continue
    return round(total, 2) if seen else None


def realm_uids(status: object, realm: object) -> set[str]:
    """该版本下所有账号的 uid。

    用途：上游整池（或某个版本）报 0 时，按 uid 过滤本地快照——
    与 `_pool_total` 的回退口径保持一致，避免「合并有数、分档是 0」。
    """
    target = normalize_realm(realm)
    if not target or not isinstance(status, dict):
        return set()
    accounts = status.get('accounts')
    if not isinstance(accounts, list):
        return set()
    out: set[str] = set()
    for item in accounts:
        if not isinstance(item, dict):
            continue
        if normalize_realm(item.get('realm') or 'cn') != target:
            continue
        uid = str(item.get('uid') or '').strip()
        if uid:
            out.add(uid)
    return out


def realm_breakdown(status: object) -> dict[str, float] | None:
    """一次算出两档，供「分开展示」用；拿不到返回 None。"""
    cn = realm_credits(status, 'cn')
    gl = realm_credits(status, 'global')
    if cn is None and gl is None:
        return None
    return {'cn': cn or 0.0, 'global': gl or 0.0}
