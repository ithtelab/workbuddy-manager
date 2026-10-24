"""管理端鉴权与会话。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import config, security
from ..iputil import client_ip

logger = logging.getLogger('workbuddy.auth')

router = APIRouter(prefix='/api', tags=['auth'])


@router.get('/healthz')
def healthz() -> dict:
    return {'ok': True, 'service': 'workbuddy-manager'}


@router.post('/login')
async def login(request: Request) -> JSONResponse:
    ip = client_ip(request)

    # 先解析用户名，以便同时做「按 IP」与「按用户名」的锁定判断
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail='请求格式错误') from None
    # body 必须是 JSON 对象：`null` / `[]` / `"str"` / `123` 都是**合法 JSON**，
    # 上面的 try 不会拦住它们，随后 body.get(...) 会抛 AttributeError → 500。
    # 500 不泄露内容，但属于未处理异常：每次触发都在服务端留下错误日志
    # （可被用来刷日志），且暴露了输入校验不完整。这里显式拒绝。
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail='请求格式错误') from None
    username = str(body.get('username', '')).strip()[:64]
    password = str(body.get('password', ''))

    if security.login_blocked(ip, username):
        raise HTTPException(status_code=429, detail='失败次数过多，请 10 分钟后再试')

    cfg = security.load_users()
    user = next((u for u in cfg.get('users', []) if u.get('username') == username), None)
    # 用户不存在时也跑一遍 PBKDF2（对哑哈希）：短路跳过会让响应快慢暴露
    # 用户名是否存在（260000 轮是毫秒级的时间差，公网可测）。
    if user:
        creds_ok = security.verify_pwd(password, user.get('pwd_hash', ''))
    else:
        security.dummy_verify(password)
        creds_ok = False
    if not creds_ok:
        # 同时记 IP 与用户名：前者防单机爆破，后者防换 IP 打同一账号
        security.record_fail(ip, username)
        # 登录失败也要留痕：本次事故中攻击者拿到了会话，但服务端没有任何记录
        security.audit({'username': username or '(空)'}, 'login_failed', username,
                       f'来源 {ip}', ip=ip)
        raise HTTPException(status_code=401, detail='用户名或密码错误')

    security.clear_fail(ip, username)
    # URL 与 Host 都写进日志，便于在反馈里直接看出真实监听形态
    logger.info('登录成功: %s（来源 %s）', username, ip)
    # 反代存在但 cookie 拿不到 Secure 时明确告警 —— 这类「以为安全其实没有」
    # 靠用户自己去翻 DevTools 才能发现，不如在日志里点出来（每个登录一条，不刷屏）。
    warn = security.insecure_cookie_warning(request)
    if warn:
        logger.warning(warn)
    # orig 缺省 = 现在（首次登录）：这次是会话的**起点**，后续续期不会推后它。
    token = security.issue_token(username, user.get('role', 'viewer'))
    security.audit(user, 'login', username, f'来源 {ip}', ip=ip)
    resp = JSONResponse({'ok': True, 'username': username, 'role': user.get('role', 'viewer')})
    resp.set_cookie(
        config.COOKIE_NAME,
        token,
        max_age=config.SESSION_DAYS * 86400,
        httponly=True,
        samesite='lax',
        secure=security.cookie_secure(request),
        path='/',
    )
    return resp


@router.post('/logout')
def logout() -> JSONResponse:
    resp = JSONResponse({'ok': True})
    resp.delete_cookie(config.COOKIE_NAME, path='/')
    return resp


@router.get('/me')
def me(user: dict = Depends(security.current_user)) -> dict:
    return {'username': user.get('username'), 'role': user.get('role', 'viewer')}


@router.post('/sessions/revoke')
def revoke_own_sessions(request: Request,
                        user: dict = Depends(security.require_session_admin)) -> dict:
    """吊销**当前用户**的全部会话（含本机这次），并清除本机 cookie。

    用途：怀疑会话被盗用（在别人电脑上登录过、旧设备没退出、备份里有 cookie）时，
    一键让所有已签发的 cookie 立即失效，而不必去改密码（改密码会连带影响
    下游配置与其它用途）。

    为什么限定「当前用户」而不是提供「踢掉所有人」：本管理端通常只有一两个账号，
    真正的诉求是「我自己的会话失控了」。把别人的会话一起踢掉属于过度杀伤，
    而且那种操作在多人协作时容易误伤（对方正在操作）。
    若确实要踢掉某个用户，用「用户管理」里的改角色/改密码（同样走 sv 递增）。

    注意：吊销后**本会话也失效**（这正是它要的效果），前端需提示重新登录。
    """
    username = str(user.get('username') or '')
    if not username:
        raise HTTPException(status_code=401, detail='未登录')
    security.revoke_sessions(username)
    ip = client_ip(request)
    security.audit(user, 'revoke_sessions', username, f'来源 {ip}', ip=ip)
    resp = JSONResponse({'ok': True, 'relogin_required': True})
    resp.delete_cookie(config.COOKIE_NAME, path='/')
    return resp
