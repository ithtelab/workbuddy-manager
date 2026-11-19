"""调用抓包（Trace Tap）的管理接口。

鉴权口径：**读会话内容要管理员**。请求日志（`/api/logs`）只暴露元数据
（模型、token、IP、UA），被普通用户看到也就是「谁用了多少」；抓包存的是
**完整对话内容**——用户的 prompt、模型回答、工具参数都在里面。同一台机器上
的只读账号不该因为「能看日志」就顺带能看所有人聊了什么。

配置读写（开关、上限）沿用既有口径：读 `current_user`，写 `require_session_admin`。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import security
from ..iputil import client_ip
from ..services import tap

router = APIRouter(prefix='/api/tap', tags=['tap'])


@router.get('/config')
def get_config(user: dict = Depends(security.current_user)) -> dict:
    """抓包开关与容量。**任何登录用户都能读**：界面要靠它决定页面上显示
    「开启抓包」还是会话列表；里面没有对话内容。"""
    return {'config': tap.settings(), 'stats': tap.stats()}


@router.put('/config')
def put_config(body: dict, request: Request,
               user: dict = Depends(security.require_session_admin)) -> dict:
    """改开关与上限。

    写入即生效（网关每次请求读一次设置），不需要重启：抓包开关的意义就是
    「现在要看，立刻打开」——重启服务会丢掉正在跑的任务的上下文。
    """
    before = tap.settings()
    after = tap.save_settings(body if isinstance(body, dict) else {})
    # 抓包会落盘完整对话内容，开关状态属于需要留痕的动作（与日志清空同级）。
    changed = ','.join(k for k in after if before.get(k) != after.get(k)) or '无变化'
    security.audit(user, 'update_tap_config', '',
                   f'变更={changed}；来源 {client_ip(request)}')
    return {'config': after, 'stats': tap.stats()}


@router.post('/backfill')
def post_backfill(user: dict = Depends(security.require_session_admin)) -> dict:
    """按入库的 UA 重新归类历史会话。

    单独一个端点而不是启动时自动跑：这是一次**会改历史数据**的动作（把旧的
    `other` 纠正成真实客户端名），应当有人在界面上明确触发，也能在日志里查到
    是谁做的。UA 原文一直在 `tap_turns` 里，重算不需要重新抓包。
    """
    changed = tap.backfill_clients()
    aux = tap.backfill_aux()
    return {'changed': changed, 'aux': aux, 'stats': tap.stats()}


@router.post('/prune')
def post_prune(user: dict = Depends(security.require_session_admin)) -> dict:
    """立刻按保留策略清理一次。写入侧本来是周期的，这里给一个「马上腾地方」的入口。"""
    result = tap.prune()
    return {**result, 'stats': tap.stats()}


@router.get('/sessions')
def list_sessions(page: int = 1, size: int = 20, days: int | None = None,
                  client: str | None = None, q: str | None = None,
                  include_aux: bool = False,
                  user: dict = Depends(security.require_admin)) -> dict:
    return tap.list_sessions(page=page, size=size, days=days, client=client, q=q,
                             include_aux=include_aux)


@router.get('/sessions/{session_id}')
def get_session(session_id: int,
                user: dict = Depends(security.require_admin)) -> dict:
    data = tap.session(session_id)
    if not data:
        raise HTTPException(status_code=404, detail='会话不存在（可能已按保留策略清理）')
    return data


@router.delete('/sessions/{session_id}')
def delete_session(session_id: int, request: Request,
                   user: dict = Depends(security.require_session_admin)) -> dict:
    if not tap.delete_session(session_id):
        raise HTTPException(status_code=404, detail='会话不存在（可能已按保留策略清理）')
    security.audit(user, 'delete_tap_session', str(session_id),
                   f'来源 {client_ip(request)}')
    return {'ok': True}


@router.get('/turns/{turn_id}')
def get_turn(turn_id: int, user: dict = Depends(security.require_admin)) -> dict:
    data = tap.turn_view(turn_id)
    if not data:
        raise HTTPException(status_code=404, detail='该轮记录不存在（可能已按保留策略清理）')
    return data


@router.get('/diff')
def get_diff(before: int, after: int,
             user: dict = Depends(security.require_admin)) -> dict:
    """两轮请求的结构化差异。`before` / `after` 是 turn id（不是 seq）。

    用 id 而不是 (session_id, seq)：界面上「和上一轮比」拿到的是列表里的两项，
    它们本就带 id；再让前端换算一遍 seq，等于给一个必然会错的环节。
    """
    data = tap.diff(before, after)
    if not data:
        raise HTTPException(status_code=404, detail='其中一轮记录不存在（可能已按保留策略清理）')
    return data


@router.get('/sessions/{session_id}/export')
def export_session(session_id: int,
                   user: dict = Depends(security.require_admin)) -> HTMLResponse:
    """导出自包含 HTML。

    `Content-Disposition: attachment` 必须带：在浏览器里直接打开这份 HTML 时，
    它渲染的是**别人机器上的对话原文**——当成页面打开就少了「这是一份文件」的
    心理提示，而它会跟着浏览器缓存走。
    """
    doc = tap.export_html(session_id)
    if doc is None:
        raise HTTPException(status_code=404, detail='会话不存在（可能已按保留策略清理）')
    headers = {
        'Content-Disposition': f'attachment; filename="workbuddy-trace-{session_id}.html"',
        # 导出的文件里含对话原文，禁止任何中间层缓存。
        'Cache-Control': 'no-store',
    }
    return HTMLResponse(doc, headers=headers)


@router.post('/clear')
def clear_all(request: Request,
              user: dict = Depends(security.require_session_admin)) -> dict:
    result = tap.clear()
    security.audit(user, 'clear_tap', '', f'来源 {client_ip(request)}')
    return result

