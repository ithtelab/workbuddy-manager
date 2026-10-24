"""上游配置、模型映射与管理端用户。"""
from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import db, security
from ..iputil import client_ip
from ..services import modelmap, reload, updater, wb2api

router = APIRouter(prefix='/api', tags=['settings'])


# ── 上游配置 ─────────────────────────────────────────────
@router.get('/settings/upstream')
def get_upstream(user: dict = Depends(security.current_user)) -> dict:
    return wb2api.load_upstream_config()


@router.post('/settings/upstream')
async def save_upstream(body: dict, request: Request,
                        user: dict = Depends(security.require_session_admin)) -> dict:
    # 必须是 async：同步路由会被 FastAPI 放进线程池执行，那里没有事件循环，
    # 无法调度后台重载任务（request_restart 将拿不到 running loop）。
    try:
        result = wb2api.save_upstream_config(body)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 改上游配置会影响全部账号行为，且可能写入 device_token/base 等敏感项，必须留痕。
    # 顶层标量（如 reasoning_as_content）不属于任何段，单独列出——否则日志会显示
    # 「段=」，看起来像什么都没改。
    sections = ','.join(sorted(k for k in body if isinstance(body.get(k), dict)))
    top_keys = ','.join(sorted(
        f'{k}={body[k]}' for k in body
        if not isinstance(body.get(k), dict) and k != 'listen'
    ))
    security.audit(user, 'update_upstream', '',
                   f'段={sections}' + (f'；顶层={top_keys}' if top_keys else '')
                   + f'；来源 {client_ip(request)}')
    # 上游只在启动时读 config.json，保存后自动重载使其生效
    result['reload_scheduled'] = reload.request_restart()
    # 无法自动重载上游时（没装 docker / 容器没挂 docker.sock），要如实告诉
    # 用户去宿主机重启，而不是让他以为改完就生效了。
    #
    # 判据是**实际能力**而非"是否容器"：容器挂了 docker.sock 就能自动重载
    # （与宿主部署等价），宿主没装 docker 反而不能。
    if not updater.can_control_docker():
        result['reload_scheduled'] = False
        result['reload_hint'] = (
            '配置已写入，但当前环境无法操作 docker，不会自动重启上游容器。'
            '请在宿主机执行 docker compose restart wb2api（上游目录下）使其生效。'
        )
    return result


@router.get('/upstream/reload-state')
def reload_state(user: dict = Depends(security.current_user)) -> dict:
    """上游重载状态，供前端展示「正在应用配置」。"""
    return reload.state()


class UpstashTestIn(BaseModel):
    url: str = ''
    # 留空则使用配置文件中已保存的 token
    token: str | None = None


@router.post('/settings/upstash/test')
async def test_upstash(body: UpstashTestIn,
                       user: dict = Depends(security.require_session_admin)) -> dict:
    """探测 Upstash 是否可用（走其 REST 接口 PING）。"""
    ok, message = await wb2api.test_upstash(body.url, body.token)
    return {'ok': ok, 'message': message}


@router.post('/settings/upstash/reload')
async def reload_upstream(user: dict = Depends(security.require_session_admin)) -> dict:
    """立即重启上游容器（等待结果）。一般无需手动调用——保存配置会自动重载。

    与会话绑定：它和 `/api/restart` 是**同一个动作**（都走 `reload.restart_now()`），
    后者在引入 API Token 时被划为「仅会话」。同一个动作不能因为走的是哪个路由就
    权限不同——否则 token 泄露者能从这里把上游重启掉，那条边界就形同虚设。
    """
    ok, message = await reload.restart_now()
    return {'ok': ok, 'message': message}


# ── 模型别名映射 ─────────────────────────────────────────
@router.get('/settings/model-map')
def get_model_map(user: dict = Depends(security.current_user)) -> dict:
    return db.get_setting('model_map', {}) or {}


@router.post('/settings/model-map')
def save_model_map(body: dict[str, str], user: dict = Depends(security.require_admin)) -> dict:
    """保存映射表。键既可以是真实别名，也可以是兜底的 `*`（issue #173）。

    校验集中在 `services/modelmap.validate`：这里只拒「落库后一定会坏」的那一类
    —— 目前只有「目标写成 `*`」（查表会把 `*` 当模型名发给上游，而界面上显示着
    "已设置兜底"，用户从报错里反推不出原因）。

    ValueError 要**转成 400**：服务端校验按 4xx 送达，浏览器里才能把 detail 那句
    原因显示出来；漏了这一步它就是 500，用户只看到「服务器错误」而拿不到该改什么
    （浏览器验收里真踩到过：直接调接口写 `{"*": "*"}` 得到的是 500 空 body）。
    """
    try:
        clean = modelmap.validate(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.set_setting('model_map', clean)
    return clean

# ── GitHub Token（版本检测 / 一键更新）────────────────────
@router.get('/settings/github-token')
def get_github_token(user: dict = Depends(security.require_admin)) -> dict:
    """读当前生效的 GitHub Token（**只回掩码**）。

    为什么只给管理员：它是凭据，能操作对应的 GitHub 账号配额；普通用户不需要它。
    环境变量里已有值时界面要说明「由部署注入、面板填写不生效」，否则用户会以为
    自己填的那份没保存（issue #205 的现场就是「没地方填、也不知道有没有生效」）。
    """
    env_token = next((str(os.environ.get(k) or '').strip()
                      for k in ('WB_GITHUB_TOKEN', 'GITHUB_TOKEN')
                      if (os.environ.get(k) or '').strip()), '')
    stored = str(db.get_setting('github_token', '') or '').strip()
    effective = env_token or stored
    return {
        'configured': bool(effective),
        'masked': updater.mask_token(effective),
        'source': 'env' if env_token else ('panel' if stored else ''),
    }


@router.put('/settings/github-token')
def save_github_token(body: dict, user: dict = Depends(security.require_admin)) -> dict:
    """保存面板这份 GitHub Token（空串 = 清除）。

    不做格式校验（GitHub 的 token 形态换过几次），但长度上限兜住误贴整段文本；
    保存后不返回明文 —— 与其它凭据字段同一口径。
    """
    raw = str((body or {}).get('token') or '').strip()
    if len(raw) > 512:
        raise HTTPException(status_code=400, detail='GitHub Token 过长（最多 512 字符）')
    db.set_setting('github_token', raw)
    security.audit(user, 'update_github_token', '', '已配置' if raw else '已清除')
    return {'configured': bool(raw), 'masked': updater.mask_token(raw)}


# ── GitHub 镜像（issue #216）─────────────────────────────────
@router.get('/settings/github-mirror')
def get_github_mirror(user: dict = Depends(security.require_admin)) -> dict:
    """当前生效的 GitHub 镜像基址 + 来源（面板 / 环境变量 / 未配置）。"""
    env_value = updater.normalize_mirror(os.environ.get('WB_GITHUB_MIRROR') or '')
    stored = updater.stored_github_mirror()
    effective = updater.normalize_mirror(stored or env_value)
    return {
        'mirror': effective,
        'stored': stored,
        'env_mirror': env_value,
        'source': 'panel' if stored else ('env' if env_value else ''),
        'presets': ['https://gh-proxy.com/', 'https://gh-proxy.org/'],
    }


@router.put('/settings/github-mirror')
def save_github_mirror(body: dict, user: dict = Depends(security.require_admin)) -> dict:
    """保存镜像基址（空串 = 清除，回落到环境变量）。

    只接受「协议 + 主机 + 可选路径」：拼地址的规则是「基址 + 完整原始地址」，
    带查询串或片段的写法拼出来必然是一个错误页，所以在入口就拒掉。
    """
    raw = str((body or {}).get('mirror') or '').strip()
    if not raw:
        db.set_setting('github_mirror', '')
        security.audit(user, 'update_github_mirror', '', '已清除')
        return get_github_mirror(user)
    value = updater.normalize_mirror(raw)
    if not value:
        raise HTTPException(
            status_code=400,
            detail='镜像地址要以 http:// 或 https:// 开头，且不能带查询串或 # 片段',
        )
    db.set_setting('github_mirror', value)
    security.audit(user, 'update_github_mirror', '', value)
    return get_github_mirror(user)


# ── 管理端用户 ───────────────────────────────────────────
class UserIn(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1)
    role: str = Field(pattern='^(admin|viewer)$')


class UserPatch(BaseModel):
    password: str | None = None
    role: str | None = Field(default=None, pattern='^(admin|viewer)$')


def _public(cfg: dict) -> list[dict]:
    return [{'username': u['username'], 'role': u.get('role', 'viewer')} for u in cfg.get('users', [])]


@router.get('/users')
def list_users(user: dict = Depends(security.current_user)) -> list[dict]:
    return _public(security.load_users())


@router.post('/users')
def add_user(body: UserIn, request: Request,
             user: dict = Depends(security.require_session_admin)) -> dict:
    """新建管理用户。

    审计是必须的：上次入侵里攻击者正是「先建自己的账号、再正常登录」，
    而这一步当时没有任何记录。新建用户写随机 sv，避免同名重建后旧 cookie 复活。
    """
    pw = str(body.password)
    problem = security.password_problem(pw, body.username)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    cfg = security.load_users()
    if any(u.get('username') == body.username for u in cfg.get('users', [])):
        raise HTTPException(status_code=409, detail='用户名已存在')
    cfg.setdefault('users', []).append({
        'username': body.username,
        'role': body.role,
        'pwd_hash': security.make_hash(pw),
        # 随机初始 sv：删除同名用户再重建时，不会让旧会话「复活」
        'sv': secrets.randbelow(1 << 30),
    })
    security.save_users(cfg)
    ip = client_ip(request)
    security.audit(user, 'add_user', body.username,
                   f'角色={body.role}；来源 {ip}', ip=ip)
    return {'username': body.username, 'role': body.role}


@router.patch('/users/{username}')
def update_user(username: str, body: UserPatch, request: Request,
                user: dict = Depends(security.require_session_admin)) -> dict:
    """改密码 / 改角色。

    两种改动都必须**吊销该用户既有会话**：
      - 改密码：否则被盗会话在改密码后仍能用到过期（7 天）——本次事故里攻击者
        改掉 admin 密码后，真正管理员手里的旧会话不该还能管理后台
      - 改角色：权限必须立即生效，不能等 cookie 过期（载荷里的 role 只是快照）

    吊销按用户进行（递增其 sv），不影响其他已登录用户。
    """
    cfg = security.load_users()
    target = next((u for u in cfg.get('users', []) if u.get('username') == username), None)
    if not target:
        raise HTTPException(status_code=404, detail='用户不存在')

    changed_pwd = changed_role = False
    if body.password:
        pw = str(body.password)
        # 管理端是公网入口：弱口令等于没有防护，统一走与启动时相同的策略。
        problem = security.password_problem(pw, username)
        if problem:
            raise HTTPException(status_code=400, detail=problem)
        target['pwd_hash'] = security.make_hash(pw)
        changed_pwd = True
    if body.role and body.role != target.get('role'):
        admins = [u for u in cfg.get('users', []) if u.get('role') == 'admin']
        if target.get('role') == 'admin' and len(admins) <= 1:
            raise HTTPException(status_code=400, detail='至少保留一个管理员')
        target['role'] = body.role
        changed_role = True

    if changed_pwd or changed_role:
        try:
            target['sv'] = int(target.get('sv') or 0) + 1
        except (TypeError, ValueError):
            target['sv'] = 1

    security.save_users(cfg)
    if changed_pwd and username == 'admin':
        # 改掉初始密码后，把 issue #179 留的那份明文副本抹掉 ——
        # 这里只存了初始 admin 密码，改其他用户的密码不应让它失效。
        security.clear_initial_password_file()
    if changed_pwd or changed_role:
        parts = []
        if changed_pwd:
            parts.append('改密码')
        if changed_role:
            parts.append('改角色')
        ip = client_ip(request)
        security.audit(user, 'update_user', username,
                       '、'.join(parts) + f'；来源 {ip}', ip=ip)
    # 把自己的密码改了，当前会话也会失效——前端据此提示重新登录
    self_revoked = bool(changed_pwd and username == user.get('username'))
    return {
        'username': target['username'],
        'role': target.get('role', 'viewer'),
        'sessions_revoked': changed_pwd or changed_role,
        'relogin_required': self_revoked,
    }


@router.delete('/users/{username}')
def delete_user(username: str, request: Request,
                user: dict = Depends(security.require_session_admin)) -> dict:
    cfg = security.load_users()
    users = cfg.get('users', [])
    target = next((u for u in users if u.get('username') == username), None)
    if not target:
        raise HTTPException(status_code=404, detail='用户不存在')
    admins = [u for u in users if u.get('role') == 'admin']
    if target.get('role') == 'admin' and len(admins) <= 1:
        raise HTTPException(status_code=400, detail='至少保留一个管理员')
    if target.get('username') == user.get('username'):
        raise HTTPException(status_code=400, detail='不能删除当前登录用户')
    cfg['users'] = [u for u in users if u.get('username') != username]
    security.save_users(cfg)
    if target.get('username') == 'admin':
        # 被删掉的就是初始账号，那份明文初始密码也随之失效。
        security.clear_initial_password_file()
    security.audit(user, 'delete_user', username, f'来源 {client_ip(request)}')
    return {'ok': True}


@router.get('/audit-logs')
def audit_logs(
    limit: int = 200,
    offset: int = 0,
    user: dict = Depends(security.require_admin),
) -> dict:
    """管理端审计日志（登录、改密码、增删用户、改安全配置等）。

    仅管理员可读：里面含用户名与来源 IP，属于敏感信息。
    """
    return {
        'items': db.list_audit_logs(limit=limit, offset=offset),
        'total': db.count_audit_logs(),
    }
