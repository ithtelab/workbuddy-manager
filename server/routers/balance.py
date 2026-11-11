"""余额查询端点：让客户端面板按统一单位显示密钥剩余额度。

两个端点分工固定 —— `/api/usage/token/` 要密钥鉴权、负责算数值；`/api/status`
是公开元数据、只声明单位。因为后者拿不到密钥，单位无法随密钥变化，所以恒为
`UNIT`：三档口径算出的量纲不同（百分比折算后的积分 / 剩余积分 / 百万 token），
没有任何真实货币或量词能同时表达它们。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import db, keysvc, upstreamsvc
from ..services import balance_realm, credits, wb2api

router = APIRouter(tags=['balance'])

UNIT = 'UNIT'

# token 额度按百万折算：token 数以百万计，与几十量级的积分额度差三个数量级，
# 不折算的话同一个位置上的数字会忽长忽短。
TOKEN_SCALE = 1_000_000

# 账号池总积分的缓存时长。客户端会周期性轮询余额，每轮都穿透到上游没有必要；
# 60 秒也远小于上游自己的积分刷新周期，不会让数字显得比实际更新。
POOL_TTL_SECONDS = 60

_pool_cache: dict[str, tuple[float, float]] = {}


def _number(value: object) -> float:
    """转成非负浮点。脏值（None / 字符串 / NaN / 负数）一律当 0。"""
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if result != result or result in (float('inf'), float('-inf')):
        return 0.0
    return max(0.0, result)


def _amounts(key: dict, pool_credits: float) -> tuple[float, float, float]:
    """算出 (可用, 已用, 总量)，三个数同量纲。

    · 两份额度都设了上限 —— 取更紧的那份额度的剩余比例，再折算回积分额度。
      只报一个数字时，这是唯一能同时反映两条限制的写法：谁先见底就跟着谁走。
    · 只有一份设了上限 —— 直接报它的剩余量。
    · 都不设上限 —— 报账号池的剩余积分总额，这才是用户实际能花掉的东西。
    """
    limit_credit = _number(key.get('quota_credit'))
    used_credit = _number(key.get('used_credit'))
    limit_token = _number(key.get('quota'))
    used_token = _number(key.get('used_tokens'))
    remain_credit = max(0.0, limit_credit - used_credit)
    remain_token = max(0.0, limit_token - used_token)

    if limit_credit and limit_token:
        ratio = min(remain_credit / limit_credit, remain_token / limit_token)
        available = round(ratio * limit_credit, 2)
        return available, round(limit_credit - available, 2), round(limit_credit, 2)
    if limit_credit:
        return round(remain_credit, 2), round(used_credit, 2), round(limit_credit, 2)
    if limit_token:
        return (round(remain_token / TOKEN_SCALE, 2),
                round(used_token / TOKEN_SCALE, 2),
                round(limit_token / TOKEN_SCALE, 2))
    total = round(pool_credits, 2)
    return total, 0.0, total


def _snapshot_total(uids: set[str] | None = None) -> float:
    """本端已登记的余额快照合计，不发任何网络请求。

    uids 非空时只算这些账号：快照不会随账号删除而清理，不过滤就会把已经
    不在池里的号也加进来。
    """
    try:
        rows = credits.snapshot_entries().get('accounts') or []
    except Exception:  # noqa: BLE001
        return 0.0
    return sum(_number(row.get('credits')) for row in rows
               if isinstance(row, dict)
               and (uids is None or str(row.get('uid')) in uids))


def _pool_total(status: dict) -> float | None:
    """账号池的剩余积分。拿不到返回 None —— 不能拿 0 冒充。

    上游整池报 0 时改用本地快照：它的 `credits` 只在自身定时任务里刷新，
    重启后或两次刷新之间会整池报 0，而这个 0 分不清「真花光」与「没上报」。
    快照来自腾讯账单，更可信。
    """
    if not status.get('connected'):
        return None
    accounts = status.get('accounts')
    if not isinstance(accounts, list):
        return None
    rows = [item for item in accounts if isinstance(item, dict)]
    total = sum(_number(item.get('credits')) for item in rows)
    if total:
        return total
    uids = {str(item.get('uid')) for item in rows if item.get('uid')}
    return _snapshot_total(uids)


async def _pool_credits(key: dict) -> float:
    """密钥所属账号池的剩余积分总额：内存缓存 → 上游 /status → 本地快照。"""
    try:
        upstream = upstreamsvc.resolve_for_key(key)
    except upstreamsvc.UpstreamUnavailable:
        return _snapshot_total()

    marker = str(upstream.get('id') or 'default')
    hit = _pool_cache.get(marker)
    now = time.time()
    if hit and now - hit[0] <= POOL_TTL_SECONDS:
        return hit[1]

    status = await wb2api.get_status(base_url=upstream.get('base_url'),
                                     api_key=upstreamsvc.forward_api_key(upstream))
    total = _pool_total(status)
    if total is None:
        total = _snapshot_total()
    _pool_cache[marker] = (now, total)
    return total


async def _realm_credits(key: dict, realm: str) -> float | None:
    """按版本汇总池内积分；拿不到返回 None（调用方回退合并口径）。"""
    try:
        upstream = upstreamsvc.resolve_for_key(key)
    except upstreamsvc.UpstreamUnavailable:
        return None
    marker = f"{upstream.get('id') or 'default'}:{realm}"
    now = time.time()
    hit = _pool_cache.get(marker)
    if hit and now - hit[0] <= POOL_TTL_SECONDS:
        return hit[1]
    status = await wb2api.get_status(base_url=upstream.get('base_url'),
                                     api_key=upstreamsvc.forward_api_key(upstream))
    value = balance_realm.realm_credits(status, realm)
    if value is None:
        return None
    if not value:
        # 与 `_pool_total` 同口径：上游该版本整档报 0 时改用本地快照
        # （上游 credits 只在自身定时任务里刷新，0 分不清「真花光」与「没上报」）
        value = _snapshot_total(balance_realm.realm_uids(status, realm))
    _pool_cache[marker] = (now, value)
    return value


def _last_call_realm(key: dict) -> str:
    """这把密钥最近一次**真实**调用的版本。

    必须过滤 `model=''`：查余额自身也会写一条 0 token 的请求日志，
    不过滤的话每查一次余额就把自己污染成「国内」。
    """
    try:
        row = db.query_one(
            "select realm from request_logs"
            " where key_id=? and model!='' order by id desc limit 1",
            (key.get('id'),))
    except Exception:  # noqa: BLE001
        return ''
    return str((row['realm'] if row else '') or '')


@router.get('/api/usage/token/')
async def token_usage(request: Request):
    """按密钥返回剩余额度。停用 / 过期 / 用尽都照常给数，不拦。

    这里回答的是「我还剩多少」——恰恰在调不动的时候最需要，所以不套用调用侧
    的额度与状态校验，只认密钥本身（与 `/v1/usage` 同口径）。IP 白名单同理
    不套：请求方是客户端面板，不是最终用户的来源 IP。
    """
    token = request.headers.get('authorization') or ''
    token = token[7:].strip() if token[:7].lower() == 'bearer ' else ''
    key = keysvc.resolve(token) if token else None
    if not key:
        return JSONResponse(
            {'error': {'message': 'API Key 无效或缺失',
                       'type': 'authentication_error', 'code': 'invalid_api_key'}},
            status_code=401,
        )

    has_limit = _number(key.get('quota_credit')) or _number(key.get('quota'))
    scope = balance_realm.resolve_scope(
        key_realm=key.get('realm') or '',
        has_limit=bool(has_limit),
        last_realm=_last_call_realm(key),
        requested=request.query_params.get('scope') or '',
    )
    pool = 0.0 if has_limit else await _pool_credits(key)
    available, used, total = _amounts(key, pool)
    # 未设上限时才按池拆分：拆出来的是「那个版本此刻能花多少」；
    # 设了上限的密钥回答的是它自己的额度，拆了就自相矛盾。
    if not has_limit and scope in balance_realm.POOL_SCOPES:
        split = await _realm_credits(key, scope)
        if split is not None:
            available, used, total = split, 0.0, split
    return {
        'code': True,
        'data': {
            'object': 'token_usage',
            # 恒为 false：即便两份额度都不设上限，也要把池里的剩余积分显示出来；
            # 标成 true 会让面板只画「无限」而丢掉这个数字。
            'unlimited_quota': False,
            'total_granted': total,
            'total_used': used,
            'total_available': available,
            # 口径：cn / global / merged。客户端不必理解，供面板与排障
            'scope': scope,
        },
    }


@router.get('/api/status')
def status() -> dict:
    """公开元数据：只声明数值单位，不含任何额度，因此无鉴权也不泄露信息。"""
    return {
        'success': True,
        'data': {
            'quota_per_unit': 1,
            'quota_display_type': 'CUSTOM',
            'custom_currency_symbol': UNIT,
            'custom_currency_exchange_rate': 1,
        },
    }

@router.get('/v1/credits')
async def client_credits(request: Request):
    """客户端默认余额路径。

    RikkaHub 的 OpenAI 兼容服务商默认配置是 `{{baseUrl}}/credits`（即这里），
    并把 `data.total_usage` 当作「余额」显示；new-api 的写法是
    `data.total_available`。同一份数值给两个键，客户端不必改配置。
    """
    result = await token_usage(request)
    if isinstance(result, JSONResponse):
        return result
    data = result.get('data') or {}
    return {
        'code': True,
        'data': {
            'object': 'credits',
            'total_credits': data.get('total_granted'),
            'total_used': data.get('total_used'),
            'total_available': data.get('total_available'),
            # RikkaHub 读这个当余额；与 total_available 同值
            'total_usage': data.get('total_available'),
            'scope': data.get('scope'),
        },
    }
