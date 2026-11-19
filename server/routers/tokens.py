"""管理面作用域化 API Token 的管理接口（见 docs/api-tokens.md）。

**这四个接口一律只接受会话鉴权**（`require_session_admin`），不接受 API Token
自己。否则一个泄露的 token 就能用来创建 / 吊销 / 提权另一个 token ——
「泄露即自助持久化 + 提权」，这是本设计里最重要的那条边界。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import security, tokensvc
from ..iputil import client_ip

router = APIRouter(prefix='/api/tokens', tags=['tokens'])


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    # '' 由 tokensvc.normalize_scope 归一化为最小权限 readonly（不报错，
    # 免得旧客户端不带该字段时被拒）。
    scope: str = Field(default='readonly', max_length=16)
    # epoch 秒；null / 0 = 永不过期
    expires_at: int | None = None


class TokenPatch(BaseModel):
    name: str | None = None
    scope: str | None = None
    enabled: bool | None = None
    expires_at: int | None = None


@router.get('')
def list_tokens(user: dict = Depends(security.require_session_admin)) -> list[dict]:
    """列出全部令牌（**不含明文**，含前缀、scope 与最近使用）。"""
    return tokensvc.list_tokens()


@router.post('')
def create_token(body: TokenIn, request: Request,
                 user: dict = Depends(security.require_session_admin)) -> dict:
    """创建令牌。返回体里的 `token` 是**明文，仅此一次**，请立即保存。"""
    name = str(body.name).strip()
    if not name:
        raise HTTPException(status_code=400, detail='令牌名称不能只有空格')
    created = tokensvc.create_token(
        name, body.scope, body.expires_at, created_by=user.get('username', ''),
    )
    security.audit(
        user, 'create_token', created['name'],
        f"scope={created['scope']} 过期={created['expires_at'] or '永不'}；来源 {client_ip(request)}",
    )
    return created


@router.patch('/{token_id}')
def update_token(token_id: int, body: TokenPatch, request: Request,
                 user: dict = Depends(security.require_session_admin)) -> dict:
    # 只提交本次真的改了的字段（PATCH 的 exclude_unset 语义）
    patch = body.model_dump(exclude_unset=True)
    if 'name' in patch and patch['name'] is not None and not str(patch['name']).strip():
        raise HTTPException(status_code=400, detail='令牌名称不能只有空格')
    updated = tokensvc.update_token(token_id, patch)
    if not updated:
        raise HTTPException(status_code=404, detail='令牌不存在')
    changed = '、'.join(sorted(patch)) or '无'
    security.audit(user, 'update_token', updated['name'],
                   f'字段={changed}；来源 {client_ip(request)}')
    return updated


@router.delete('/{token_id}')
def delete_token(token_id: int, request: Request,
                 user: dict = Depends(security.require_session_admin)) -> dict:
    row = next((t for t in tokensvc.list_tokens() if t['id'] == token_id), None)
    if not tokensvc.delete_token(token_id):
        raise HTTPException(status_code=404, detail='令牌不存在')
    security.audit(user, 'delete_token', (row or {}).get('name', str(token_id)),
                   f'来源 {client_ip(request)}')
    return {'ok': True}
