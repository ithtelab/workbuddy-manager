"""入站 IP 管控：全局开关/模式、白黑名单规则、访问审计。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import db, security
from ..iputil import client_ip
from ..iputil import ip_matches

router = APIRouter(prefix='/api/security', tags=['security'])

DEFAULT_CONFIG = {'enabled': False, 'mode': 'blacklist'}


class ConfigIn(BaseModel):
    enabled: bool
    mode: str = Field(pattern='^(whitelist|blacklist)$')


class RuleIn(BaseModel):
    kind: str = Field(pattern='^(allow|deny)$')
    cidr: str = Field(min_length=1, max_length=64)
    note: str = ''


def get_config() -> dict:
    return {**DEFAULT_CONFIG, **(db.get_setting('security', {}) or {})}


@router.get('/config')
def read_config(user: dict = Depends(security.current_user)) -> dict:
    return get_config()


@router.post('/config')
def write_config(body: ConfigIn, request: Request,
                 user: dict = Depends(security.require_session_admin)) -> dict:
    cfg = body.model_dump()
    db.set_setting('security', cfg)
    # 改 IP 管控开关/模式会直接影响对外放行策略，必须留痕
    security.audit(user, 'update_security', '',
                   f"enabled={cfg.get('enabled')} mode={cfg.get('mode')}；来源 {client_ip(request)}")
    return cfg


@router.get('/rules')
def list_rules(user: dict = Depends(security.current_user)) -> list[dict]:
    rows = db.query('SELECT * FROM ip_rules ORDER BY id DESC')
    return [
        {'id': r['id'], 'kind': r['kind'], 'cidr': r['cidr'], 'note': r['note'], 'created_at': r['created_at']}
        for r in rows
    ]


@router.post('/rules')
def add_rule(body: RuleIn, user: dict = Depends(security.require_session_admin)) -> dict:
    # 校验 CIDR 合法性（便于尽早发现错误输入）
    if not ip_matches('0.0.0.0', body.cidr) and not ip_matches('::', body.cidr):
        # ip_matches 对非法 CIDR 返回 False，这里额外做一次严格校验
        import ipaddress

        try:
            ipaddress.ip_network(body.cidr, strict=False)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f'非法的 IP / CIDR: {body.cidr}') from exc
    rule_id = db.execute(
        'INSERT INTO ip_rules(kind, cidr, note, created_at) VALUES(?, ?, ?, ?)',
        (body.kind, body.cidr, body.note, int(time.time())),
    )
    row = db.query_one('SELECT * FROM ip_rules WHERE id = ?', (rule_id,))
    return {'id': row['id'], 'kind': row['kind'], 'cidr': row['cidr'], 'note': row['note'], 'created_at': row['created_at']}


@router.delete('/rules/{rule_id}')
def delete_rule(rule_id: int, user: dict = Depends(security.require_session_admin)) -> dict:
    if not db.query_one('SELECT id FROM ip_rules WHERE id = ?', (rule_id,)):
        raise HTTPException(status_code=404, detail='规则不存在')
    db.execute('DELETE FROM ip_rules WHERE id = ?', (rule_id,))
    return {'ok': True}


@router.get('/logs')
def access_logs(limit: int = 200, user: dict = Depends(security.current_user)) -> list[dict]:
    rows = db.query(
        'SELECT * FROM ip_access_logs ORDER BY id DESC LIMIT ?',
        (min(1000, max(1, limit)),),
    )
    return [
        {
            'id': r['id'], 'ts': r['ts'], 'ip': r['ip'], 'path': r['path'],
            'blocked': bool(r['blocked']), 'ua': r['ua'],
            # 拦截原因短码（issue #33）。存量记录为 NULL——那时没记原因，
            # 界面按「未记录」展示，不编造。
            'reason': r['reason'] if 'reason' in r.keys() else None,
        }
        for r in rows
    ]


@router.post('/logs/clear')
def clear_logs(user: dict = Depends(security.require_session_admin)) -> dict:
    db.execute('DELETE FROM ip_access_logs')
    return {'ok': True}
