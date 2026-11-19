"""红包：批量发放带额度的密钥（见 server/redpacket.py 的说明）。

路由形状沿用密钥那套（`/api/keys/{id}` 的风格）：`/api/red-packets/{id}`。

**权限**：只有管理员能创建与收回 —— 红包是**发放额度**的动作，消耗的是账号池
的积分/token（真金白银）。查看（列表与详情）对登录用户开放，与密钥列表一致。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import redpacket, security
from ..iputil import client_ip

router = APIRouter(prefix='/api/red-packets', tags=['red-packets'])

# 抽奖端点**单独一个 router**，因为它与上面那组的前提完全不同：上面全要登录
# 甚至要管理员，这里**必须公开**（收到链接的是同事、朋友，让他们注册账号不合理）。
# 分开挂的目的就是让这条边界一眼可见 —— 混在同一个 router 里，将来加端点时
# 很容易顺手写上一个 Depends，或者反过来漏掉。
#
# 公开端的防滥用靠三件事：每个 IP 只能抽一次、抽奖码 128 位熵（猜不到）、
# 以及红包自带的失效时间。
claim_router = APIRouter(prefix='/api/claim', tags=['red-packet-claim'])


class PacketIn(BaseModel):
    # 标题是**备注**（「给老王的福利」），会拼进每个密钥的名字里，
    # 所以要限长；留空时用默认名。
    title: str = ''
    quota_kind: str = Field(default=redpacket.KIND_CREDIT)
    total_amount: float = Field(gt=0)
    shares: int = Field(ge=1, le=redpacket.MAX_SHARES)
    mode: str = Field(default=redpacket.MODE_LUCKY)
    # 默认 7 天（见 redpacket.DEFAULT_TTL_DAYS）。传 0/负数会被 validate 拒掉。
    ttl_days: int | None = None
    # 模型白名单。**token 红包必填、积分红包必须为空**（见 redpacket.validate 的
    # 说明：token 是「量」与模型强相关，积分是「钱」任何模型都能用）。
    models: list[str] = Field(default_factory=list)


@router.post('')
def create_packet(body: PacketIn, request: Request,
                  user: dict = Depends(security.require_admin)) -> dict:
    """创建红包。**返回里含明文 key，且仅此一次**（库里只存哈希）。"""
    try:
        out = redpacket.create_packet(
            name=body.title,
            kind=body.quota_kind,
            total=body.total_amount,
            shares=body.shares,
            mode=body.mode,
            ttl_days=body.ttl_days,
            actor=str(user.get('username') or ''),
            models=body.models,
        )
    except redpacket.RedPacketError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 留痕，但**不记明文 key** —— 审计日志会展示给所有管理员看。
    # 模型范围要记：它决定这个红包能用来干什么（token 红包必填）。
    scope = f" 模型={'/'.join(out['models'])}" if out['models'] else ' 模型=不限'
    security.audit(
        user, 'create_red_packet', str(out['id']),
        f"{out['quota_kind']} 总额={out['total_amount']} 份数={out['shares']} "
        f"方式={out['mode']}{scope}；来源 {client_ip(request)}",
    )
    return out


@router.get('')
def list_packets(user: dict = Depends(security.current_user)) -> list[dict]:
    """红包列表（不含明文 key —— 库里也没有）。"""
    return redpacket.list_packets()


@router.get('/{packet_id}')
def packet_detail(packet_id: int,
                  user: dict = Depends(security.current_user)) -> dict:
    """红包详情：每一份的密钥前缀与用量。"""
    out = redpacket.packet_detail(packet_id)
    if out is None:
        raise HTTPException(status_code=404, detail='红包不存在')
    return out


@router.post('/{packet_id}/revoke')
def revoke_packet(packet_id: int, request: Request,
                  user: dict = Depends(security.require_admin)) -> dict:
    """收回整批：停用这批密钥（可逆，不删数据）。

    为什么是停用而不是删除：停用后还能看到「这批红包被用掉多少」，
    删了就只剩一个数字都没有的空壳。
    """
    if redpacket.packet_detail(packet_id) is None:
        raise HTTPException(status_code=404, detail='红包不存在')
    n = redpacket.revoke_packet(packet_id)
    security.audit(user, 'revoke_red_packet', str(packet_id),
                   f'停用 {n} 个密钥；来源 {client_ip(request)}')
    return {'revoked': n}


# ── 抽奖（公开）──────────────────────────────────────────
# 收到链接的人不需要登录 —— 他们多半没有账号，也不该为了领个红包去注册。
# 防滥用靠「每 IP 一次」+ 128 位抽奖码 + 有效期，见 claim_router 的说明。

@claim_router.get('/{code}')
def claim_info(code: str, request: Request) -> dict:
    """抽奖页要显示的信息（份数、还剩几份、本机抽过没有）。**不含密钥**。

    没点「开启」之前不该能拿到密钥，所以这里只回元信息。
    """
    try:
        return redpacket.claim_info(code, client_ip(request))
    except redpacket.ClaimError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@claim_router.post('/{code}')
def claim_draw(code: str, request: Request) -> dict:
    """抽一份，返回**明文密钥**与这一份的额度。

    409 = 「这个网络已经领过了」（可区分，前端提示不同）；
    404 = 链接失效 / 已过期 / 已领完 —— 都归为「来晚了」，不必让外部区分
    （区分了反而给探测者线索：能试出「这个码存在但领完了」）。
    """
    try:
        return redpacket.draw(code, client_ip(request))
    except redpacket.ClaimError as exc:
        raise HTTPException(status_code=409 if exc.already else 404,
                            detail=str(exc)) from exc
