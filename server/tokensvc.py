"""管理面作用域化 API Token：生成、解析与校验（见 docs/api-tokens.md）。

与 `keysvc`（数据面网关密钥）**刻意分开**：那套只授权模型调用，与后台权限无关；
本模块的 token 授权的是管理面 `/api/*`，因此安全要求更高——

  * 明文只在创建时返回一次，库中只存 SHA-256 哈希；
  * 按 prefix 定位 + 常量时间比较（`secrets.compare_digest`），不全表扫描；
  * scope 决定角色（readonly → viewer，admin → admin），非法值降级为最小权限；
  * 失败时**不区分**「不存在 / 已停用 / 已过期」，不给探测者任何信号。

历史（2026-09-14）：管理端曾接受 users.json 里 `api_keys` 数组中的 `X-API-Key`
并直接授予 admin，那条提权后门已彻底移除。本模块的形态与它刻意不同，理由见
设计文档的「必须避免重蹈的覆辙」一节。
"""
from __future__ import annotations

import hashlib
import secrets
import time

from . import db

# 与网关密钥 `wbk_` 刻意区分，避免两类凭据在同一处被混用。
TOKEN_PREFIX = 'wbt_'
SCOPES = ('readonly', 'admin')

# last_used 写库节流（秒）。管理面在总览页会被 30 秒轮询，逐请求写会把库打热。
LAST_USED_THROTTLE = 60


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def normalize_scope(value: object) -> str:
    """归一化 scope；**非法值一律降级为最小权限** readonly。

    宁可给少了权限（用户发现功能不可用会来问），也不要因为一个拼写错误
    意外给出管理员权限。
    """
    v = str(value or '').strip().lower()
    return v if v in SCOPES else 'readonly'


def scope_to_role(scope: object) -> str:
    """把 scope 映射为管理端角色（供 `security.current_user` 使用）。"""
    return 'admin' if normalize_scope(scope) == 'admin' else 'viewer'


def _parse(row) -> dict:
    return {
        'id': row['id'],
        'name': row['name'],
        'prefix': row['prefix'],
        'scope': row['scope'],
        'enabled': bool(row['enabled']),
        'expires_at': row['expires_at'],
        'created_at': row['created_at'],
        'created_by': row['created_by'],
        'last_used_at': row['last_used_at'],
        'last_used_ip': row['last_used_ip'],
    }


def list_tokens() -> list[dict]:
    """列出全部令牌。**永不回传明文或哈希。**"""
    rows = db.query('SELECT * FROM api_tokens ORDER BY id DESC')
    return [_parse(r) for r in rows]


def create_token(name: str, scope: object = 'readonly',
                 expires_at: int | None = None, created_by: str = '') -> dict:
    """创建一个令牌。返回体里的 `token` 是**明文，仅此一次**。"""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    exp = int(expires_at) if expires_at else None
    token_id = db.execute(
        'INSERT INTO api_tokens(name, token_hash, prefix, scope, enabled, '
        'expires_at, created_at, created_by) VALUES(?, ?, ?, ?, 1, ?, ?, ?)',
        (str(name)[:64], _hash(token), token[:12], normalize_scope(scope),
         exp, int(time.time()), str(created_by or '')[:64]),
    )
    out = _parse(db.query_one('SELECT * FROM api_tokens WHERE id = ?', (token_id,)))
    out['token'] = token  # 仅此一次
    return out


def update_token(token_id: int, patch: dict) -> dict | None:
    """部分更新（PATCH 语义：只改真的提交了的字段）。不存在返回 None。"""
    if not db.query_one('SELECT id FROM api_tokens WHERE id = ?', (token_id,)):
        return None
    fields: dict[str, object] = {}
    if 'name' in patch and patch['name']:
        fields['name'] = str(patch['name'])[:64]
    if 'enabled' in patch:
        fields['enabled'] = 1 if patch['enabled'] else 0
    if 'scope' in patch:
        fields['scope'] = normalize_scope(patch['scope'])
    if 'expires_at' in patch:
        # 显式传 null/0 表示「改为永不过期」——不能用真值判断。
        v = patch['expires_at']
        fields['expires_at'] = int(v) if v else None
    if fields:
        assignments = ', '.join(f'{k} = ?' for k in fields)
        db.execute(f'UPDATE api_tokens SET {assignments} WHERE id = ?',
                   (*fields.values(), token_id))
    return _parse(db.query_one('SELECT * FROM api_tokens WHERE id = ?', (token_id,)))


def delete_token(token_id: int) -> bool:
    if not db.query_one('SELECT id FROM api_tokens WHERE id = ?', (token_id,)):
        return False
    db.execute('DELETE FROM api_tokens WHERE id = ?', (token_id,))
    return True


def resolve(token: object) -> dict | None:
    """校验明文令牌：先按 prefix 定位候选行，再对哈希做常量时间比较。

    返回行字典（含 token_hash，仅内部使用）；不匹配返回 None。
    """
    if not token or not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
        return None
    digest = _hash(token)
    for row in db.query('SELECT * FROM api_tokens WHERE prefix = ?', (token[:12],)):
        if secrets.compare_digest(row['token_hash'], digest):
            return dict(row)
    return None


def usable(row: dict) -> bool:
    """令牌是否可用：已启用，且未过期（无 expires_at = 永不过期）。

    逐次校验、不做缓存——保证吊销与过期**即时生效**。
    """
    if not row.get('enabled'):
        return False
    exp = row.get('expires_at')
    return not exp or int(exp) > int(time.time())


def touch(token_id: int, ip: str) -> None:
    """记录最近使用（**节流**：距上次记录不足 LAST_USED_THROTTLE 秒则跳过）。

    记账属于旁路，任何异常都不能影响请求本身（与请求日志同一原则）。
    """
    try:
        now = int(time.time())
        row = db.query_one('SELECT last_used_at FROM api_tokens WHERE id = ?', (token_id,))
        if row and row['last_used_at'] and now - int(row['last_used_at']) < LAST_USED_THROTTLE:
            return
        db.execute('UPDATE api_tokens SET last_used_at = ?, last_used_ip = ? WHERE id = ?',
                   (now, db._clean(ip, 64), token_id))
    except Exception:  # noqa: BLE001
        pass
