"""上游网页改版监控：配置、状态、立即运行、邮件出口检测、测试发信 / 发 QQ。

接口都挂在 `/api/watchdog` 下。**全部要求管理员**：状态里有账号昵称与收件
地址，配置里有 LLM base url 与打码后的凭据（打码仍能看出"配没配"）。写动作
与发信都写审计日志。

为什么「立即运行」不做成后台任务：一轮检测是「几次页面抓取 + 一两个账号的
只读查询」，正常几秒内结束。同步返回结果，界面能直接把差异与判读写出来，
不用再引入一套任务轮询。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from .. import security
from ..iputil import client_ip
from ..services import qq as qqsvc
from ..services import watchdog

router = APIRouter(prefix='/api/watchdog', tags=['watchdog'])


def _merge_mail(mail: dict, raw: object) -> dict:
    """用请求体里的值覆盖邮件配置（**只用于这一次测试，不落库**）。

    为什么需要它：用户填完 SMTP 主机就会去点「检测出口」，这时还没保存。
    如果测试读的是已保存的配置，实际体验就是「填了没用、必须先保存」——
    而那一步完全没有提示。测试用表单当前值，保存仍由 PUT /config 负责。
    """
    out = dict(mail)
    if not isinstance(raw, dict):
        return out
    for key in ('host', 'username', 'password', 'from', 'subject_prefix'):
        value = raw.get(key)
        if isinstance(value, str):
            out[key] = value if key == 'password' else value.strip()
    port = raw.get('port')
    if isinstance(port, int) and not isinstance(port, bool):
        out['port'] = port
    if raw.get('security') in ('ssl', 'starttls', 'none'):
        out['security'] = raw['security']
    to = raw.get('to')
    if isinstance(to, list):
        out['to'] = [str(x).strip() for x in to if str(x).strip()]
    # 打码值 = 保持已保存的密码：界面上不可能拿到明文
    if out.get('password') == '********':
        out['password'] = mail.get('password') or ''
    return out


def _merge_llm(llm: dict, raw: object) -> dict:
    """同 `_merge_mail`：测试 LLM 用表单当前值，不落库。"""
    out = dict(llm)
    if not isinstance(raw, dict):
        return out
    for key in ('base_url', 'model'):
        value = raw.get(key)
        if isinstance(value, str):
            out[key] = value.strip()
    if isinstance(raw.get('api_key'), str):
        out['api_key'] = raw['api_key']
    timeout = raw.get('timeout_seconds')
    if isinstance(timeout, int) and not isinstance(timeout, bool):
        out['timeout_seconds'] = timeout
    if isinstance(raw.get('enabled'), bool):
        out['enabled'] = raw['enabled']
    if out.get('api_key') == '********':
        out['api_key'] = llm.get('api_key') or ''
    return out


def _merge_qq(qq: dict, raw: object) -> dict:
    """同 `_merge_mail`：QQ 测试用表单当前值，不落库。

    两个测试端点都强制 `enabled=True`：用户是在「还没开启通知」时来验证，
    开关状态不该让测试返回「未启用」这种答非所问的结果。
    """
    out = dict(qq)
    if isinstance(raw, dict):
        for key in ('base_url', 'api_key', 'umo'):
            value = raw.get(key)
            if isinstance(value, str):
                out[key] = value if key == 'api_key' else value.strip()
        timeout = raw.get('timeout_seconds')
        if isinstance(timeout, int) and not isinstance(timeout, bool):
            out['timeout_seconds'] = timeout
    if out.get('api_key') == '********':
        out['api_key'] = qq.get('api_key') or ''
    out['enabled'] = True
    return out


@router.get('/config')
def get_config(user: dict = Depends(security.require_admin)) -> dict:
    return {'config': watchdog.public_config()}


@router.put('/config')
async def put_config(body: dict, request: Request,
                     user: dict = Depends(security.require_admin)) -> dict:
    try:
        result = await watchdog.save_config(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cfg = result['config']
    security.audit(
        user, 'update_watchdog', '',
        f"enabled={cfg['enabled']} interval={cfg['interval_minutes']}m "
        f"targets={len(cfg['targets'])} llm={cfg['llm']['enabled']} "
        f"mail={cfg['mail']['enabled']}；来源 {client_ip(request)}",
        ip=client_ip(request),
    )
    return result


@router.get('/status')
def get_status(user: dict = Depends(security.require_admin)) -> dict:
    return watchdog.status()


@router.post('/run')
async def run_now(request: Request,
                  user: dict = Depends(security.require_admin)) -> dict:
    result = await watchdog.run_once(trigger='manual')
    security.audit(user, 'run_watchdog', '',
                   f"changed={','.join(result.get('changed') or []) or '无'}；"
                   f"来源 {client_ip(request)}", ip=client_ip(request))
    return result


@router.post('/baseline')
def reset_baseline(request: Request,
                   user: dict = Depends(security.require_admin)) -> dict:
    count = watchdog.reset_baselines()
    security.audit(user, 'reset_watchdog_baseline', '',
                   f'清掉 {count} 份快照；来源 {client_ip(request)}',
                   ip=client_ip(request))
    return {'ok': True, 'cleared': count,
            'message': '已清除基线，下次检测会重新建立基线且不告警'}


@router.post('/mail/egress')
async def check_egress(request: Request,
                       payload: dict | None = Body(default=None),
                       user: dict = Depends(security.require_admin)) -> dict:
    cfg = watchdog.load_config()
    mail = _merge_mail(cfg['mail'], payload)
    if not mail.get('host'):
        raise HTTPException(status_code=400, detail='请先填写 SMTP 主机')
    result = await asyncio.to_thread(
        watchdog.probe_smtp_egress, mail['host'], mail['port'])
    watchdog._save_state(egress=result)  # noqa: SLF001 — 同模块状态，仅供界面展示
    security.audit(user, 'check_mail_egress', mail['host'],
                   f"port={mail['port']} ok={result['ok']}；来源 {client_ip(request)}",
                   ip=client_ip(request))
    return result


@router.post('/mail/test')
async def test_mail(request: Request,
                    payload: dict | None = Body(default=None),
                    user: dict = Depends(security.require_admin)) -> dict:
    cfg = watchdog.load_config()
    cfg = {**cfg, 'mail': _merge_mail(cfg['mail'], payload)}
    if not cfg['mail'].get('host') or not cfg['mail'].get('to'):
        raise HTTPException(status_code=400, detail='请先填好 SMTP 与收件人')
    result = await watchdog.test_mail(cfg)
    security.audit(user, 'test_watchdog_mail', cfg['mail'].get('host', ''),
                   f"ok={result.get('ok')}；来源 {client_ip(request)}",
                   ip=client_ip(request))
    return result


@router.post('/qq/bots')
async def qq_bots(request: Request,
                  payload: dict | None = Body(default=None),
                  user: dict = Depends(security.require_admin)) -> dict:
    """检测 AstrBot 地址 / 密钥 / scope，并列出可用平台适配器。

    单独一个端点（而不是并进测试发消息）是为了让「密钥没 im 权限」与
    「UMO 填错」两类失败在界面上分得开。
    """
    cfg = watchdog.load_config()
    qq = _merge_qq(cfg['qq'], payload)
    if not qq.get('base_url') or not qq.get('api_key'):
        raise HTTPException(status_code=400, detail='请先填写 AstrBot 地址与开发者访问密钥')
    result = await qqsvc.list_bots(qq)
    security.audit(user, 'check_watchdog_qq', qq.get('base_url', ''),
                   f"ok={result.get('ok')} bots={len(result.get('bots') or [])}；"
                   f"来源 {client_ip(request)}", ip=client_ip(request))
    return result


@router.post('/qq/test')
async def test_qq(request: Request,
                  payload: dict | None = Body(default=None),
                  user: dict = Depends(security.require_admin)) -> dict:
    cfg = watchdog.load_config()
    cfg = {**cfg, 'qq': _merge_qq(cfg['qq'], payload)}
    if not cfg['qq'].get('base_url') or not cfg['qq'].get('umo'):
        raise HTTPException(status_code=400, detail='请先填写 AstrBot 地址与 UMO')
    result = await watchdog.test_qq(cfg)
    security.audit(user, 'test_watchdog_qq', cfg['qq'].get('umo', ''),
                   f"ok={result.get('ok')} stage={result.get('stage')}；"
                   f"来源 {client_ip(request)}", ip=client_ip(request))
    return result


@router.post('/llm/test')
async def test_llm(request: Request,
                   payload: dict | None = Body(default=None),
                   user: dict = Depends(security.require_admin)) -> dict:
    cfg = watchdog.load_config()
    cfg = {**cfg, 'llm': _merge_llm(cfg['llm'], payload)}
    if not cfg['llm'].get('base_url') or not cfg['llm'].get('model'):
        raise HTTPException(status_code=400, detail='请先填写 LLM 接口地址与模型')
    sample_diff = {
        'added': 3, 'removed': 1,
        'excerpt': ('--- before\n+++ after\n+TAG button class=claim-btn\n'
                    '+TEXT 限时活动：领取 500 积分\n-TAG div class=old-entry'),
        'truncated': False,
    }
    sample_hits = [{'word': '领取', 'line': 'TEXT 限时活动：领取 500 积分'}]
    try:
        verdict = await watchdog.ask_llm(
            cfg['llm'], {'name': 'sample', 'url': 'https://example.invalid/'},
            sample_diff, sample_hits)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f'LLM 调用失败: {exc}') from exc
    security.audit(user, 'test_watchdog_llm', str(cfg['llm'].get('model') or ''),
                   f"source={verdict.get('source')}；来源 {client_ip(request)}",
                   ip=client_ip(request))
    return {'ok': True, 'verdict': verdict}
