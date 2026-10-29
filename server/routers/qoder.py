"""面板侧 Qoder 代理路由（/api/qoder/*）。

形态：**固定路径白名单代理** —— 只读端点挂 `current_user`（与设置页其它只读
接口同口径），写操作挂 `require_session_admin`（高危操作拒绝 `wbt_` Token，
与「保存上游 / 一键更新」同级），四个写操作全部落审计且**失败也记**——审计是
留痕不是成功日志。

为什么这些端点不直接复用 console 的形状：console 的鉴权是它自己的会话
Cookie，面板用户根本没有那个 Cookie；由面板代登录并转发（见 services/qoder.py）。
错误映射统一走 `_proxy` 包装成 `HTTPException`——响应形状是 `{'detail': …}`，
与中间件 413 一致，前端 `errText` 不用为这一组接口加特例。
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import security
from ..iputil import client_ip
from ..services import qoder
from ..services.qoder import QoderError, QoderNotConfigured

router = APIRouter(prefix='/api/qoder', tags=['qoder'])

logger = logging.getLogger(__name__)


async def _proxy(call: Callable[[], Awaitable[dict]]) -> dict:
    """统一错误映射：QoderError → 原状态码 + {'detail': message}。

    502 的兜底不做特殊化：它已经是 HTTPException 之外的普通异常路径，让
    FastAPI 默认 500 接住即可——但 QoderError 自带「用户可照做」的文案，
    丢失它等于让管理员去翻服务端日志，所以必须在这里转。
    """
    try:
        return await call()
    except QoderNotConfigured as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None
    except QoderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from None


def _audit(user: dict, request: Request, action: str, target: str,
           detail: str) -> None:
    """写操作审计。失败也记：调用方先拼好「成功/失败摘要」再传进来。"""
    security.audit(user, action, target, f'{detail}；来源 {client_ip(request)}',
                   ip=client_ip(request))


# ── 设置（write-only 凭据）────────────────────────────────────────────

class QoderSettingsIn(BaseModel):
    url: str = Field(default='', max_length=500)
    # None = 不修改（PATCH 语义）；空串 = 清空。见 qoder.save_console_config。
    password: str | None = Field(default=None, max_length=500)


class QoderActiveIn(BaseModel):
    id: str = Field(min_length=1, max_length=200)


@router.get('/settings')
def get_settings(user: dict = Depends(security.current_user)) -> dict:
    """读取配置的对外形状：只回 configured / url / password_set，密码绝不出接口。"""
    return qoder.load_console_config()


@router.put('/settings')
def put_settings(body: QoderSettingsIn, request: Request,
                 user: dict = Depends(security.require_session_admin)) -> dict:
    """保存 console 凭据。要求**会话管理员**：它带着 console 的明文密码，
    与「保存上游 api_key / 一键更新」同级（见 docs/api-tokens.md 的作用域约定）。"""
    try:
        out = qoder.save_console_config(body.url, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 审计里绝不写密码，只写「密码是否变更」
    _audit(user, request, 'qoder_console_updated', body.url,
           f"configured={out['configured']}；password_set={out['password_set']}")
    return out


@router.post('/settings/test')
async def test_settings(user: dict = Depends(security.require_session_admin)) -> dict:
    """「测试连接」：无状态变更，不审计（与上游 probe 端点同口径）。"""
    ok, message = await _proxy(lambda: qoder.probe_console())
    return {'ok': ok, 'message': message}


# ── 只读代理（登录即可看）─────────────────────────────────────────────

@router.get('/status')
async def status(user: dict = Depends(security.current_user)) -> dict:
    return await _proxy(lambda: qoder.console_request('GET', '/api/status'))


@router.get('/accounts')
async def accounts(user: dict = Depends(security.current_user)) -> dict:
    return await _proxy(lambda: qoder.console_request('GET', '/api/accounts'))


@router.get('/accounts/quota')
async def accounts_quota(user: dict = Depends(security.current_user)) -> dict:
    return await _proxy(lambda: qoder.console_request('GET', '/api/accounts/quota'))


@router.get('/models')
async def models(user: dict = Depends(security.current_user)) -> dict:
    """console 的模型管理视图原样代理；**旁路**为每个 qoder 上游暖 bridge
    模型缓存——`check_models` 只读缓存不发网络（routers/keys.py 的既有原则），
    得有人顺手把缓存填上，而打开这一页正是「管理员正在关心 qoder 模型」的
    信号。异常吞掉记 debug：暖缓存失败只是白名单提示退化，不该拖垮页面。"""
    out = await _proxy(lambda: qoder.console_request('GET', '/api/models'))
    from .. import upstreamsvc
    for up in upstreamsvc.list_upstreams(include_default=False):
        if str(up.get('provider') or '') == 'qoder':
            try:
                await qoder.fetch_model_ids(up)
            except Exception as exc:  # noqa: BLE001
                logger.debug('预热 qoder bridge 模型缓存失败（%s）：%s', up.get('name'), exc)
    return out


@router.get('/console-settings')
async def console_settings(user: dict = Depends(security.require_session_admin)) -> dict:
    """console 自身的设置原样代理。可能含 bridge_token / console_password，
    收紧到会话管理员——`current_user` 会让只读账号读到另一套系统的凭据。"""
    return await _proxy(lambda: qoder.console_request('GET', '/api/settings'))


# ── 写操作代理（会话管理员 + 审计）────────────────────────────────────

@router.post('/checkin')
async def checkin(request: Request,
                  user: dict = Depends(security.require_session_admin)) -> dict:
    """触发 console 侧签到。console 的 checkin 有指纹幂等，重复提交无害；
    审计留痕用于回答「谁在什么时候点过」。"""
    try:
        out = await _proxy(lambda: qoder.console_request(
            'POST', '/api/checkin', timeout=qoder.WRITE_TIMEOUT))
        detail = '成功'
    except HTTPException as exc:
        _audit(user, request, 'qoder_checkin', '', f'失败：{exc.detail}')
        raise
    _audit(user, request, 'qoder_checkin', '', detail)
    return out


@router.post('/active')
async def activate(body: QoderActiveIn, request: Request,
                   user: dict = Depends(security.require_session_admin)) -> dict:
    try:
        out = await _proxy(lambda: qoder.console_request(
            'POST', '/api/active', json_body={'id': body.id},
            timeout=qoder.WRITE_TIMEOUT))
        detail = '成功'
    except HTTPException as exc:
        _audit(user, request, 'qoder_account_activated', body.id, f'失败：{exc.detail}')
        raise
    _audit(user, request, 'qoder_account_activated', body.id, detail)
    return out


@router.post('/bridge/start')
async def bridge_start(request: Request,
                       user: dict = Depends(security.require_session_admin)) -> dict:
    try:
        out = await _proxy(lambda: qoder.console_request(
            'POST', '/api/bridge/start', timeout=qoder.WRITE_TIMEOUT))
        detail = '成功'
    except HTTPException as exc:
        _audit(user, request, 'qoder_bridge_started', '', f'失败：{exc.detail}')
        raise
    _audit(user, request, 'qoder_bridge_started', '', detail)
    return out


@router.post('/bridge/stop')
async def bridge_stop(request: Request,
                      user: dict = Depends(security.require_session_admin)) -> dict:
    try:
        out = await _proxy(lambda: qoder.console_request(
            'POST', '/api/bridge/stop', timeout=qoder.WRITE_TIMEOUT))
        detail = '成功'
    except HTTPException as exc:
        _audit(user, request, 'qoder_bridge_stopped', '', f'失败：{exc.detail}')
        raise
    _audit(user, request, 'qoder_bridge_stopped', '', detail)
    return out
