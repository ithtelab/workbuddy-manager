"""WorkBuddy Manager 入口：管理 API + 对外反代网关 + 静态前端托管。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import logging

from . import config, db, security
from .iputil import client_ip
from .routers import (
    accounts, anthropic, auth, gateway, keys, logs, models, playground,
    security as security_router, settings, stats, system,
)
from .services import tasklog

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    db.connect()
    security.load_users()  # 首次启动会自动生成管理员并打印一次密码
    _warn_if_exposed()
    # 后台采集上游自动任务日志（旅行/活跃/签到/保活），容器日志会被重建清掉，
    # 这里解析后落库长期保留，界面才能看到「这趟旅行领了多少积分」
    tasklog.start_collector()
    try:
        yield
    finally:
        tasklog.stop_collector()


def _warn_if_exposed() -> None:
    """监听所有网卡时提醒：确保前面有反代，不要把 7864 直接暴露到公网。

    为什么只警告不自动改：标准部署（1Panel 反代到本机端口）与"直连公网"用的是
    同一个 0.0.0.0，自动改成 127.0.0.1 会把前者一起改坏。IP 伪造的问题已在
    `iputil.client_ip` 从源头修掉（只在 TCP 对端来自可信网段时才采信转发头），
    这里的提示是纵深防御——少一层暴露就少一类风险。
    """
    if config.HOST not in ('0.0.0.0', '::'):
        return
    logger.warning(
        '服务监听在 %s（所有网卡）：请确认 7864 端口没有直接暴露到公网，'
        '仅在反向代理后使用；直连会让来源 IP 类管控与登录锁定失去意义。',
        config.HOST,
    )


app = FastAPI(
    title='WorkBuddy Manager',
    version='1.0.35',
    lifespan=lifespan,
    # 生产环境默认关闭交互式文档与 OpenAPI 描述：
    # 它们会把管理接口全貌（路径、参数、结构）暴露给任何未认证访问者，
    # 便于攻击者摸清面。需要时设 WB_ENABLE_DOCS=1 打开。
    docs_url='/docs' if config.ENABLE_DOCS else None,
    redoc_url='/redoc' if config.ENABLE_DOCS else None,
    openapi_url='/openapi.json' if config.ENABLE_DOCS else None,
)

if config.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=['*'],
        allow_headers=['*'],
    )

# ── 路由注册顺序很重要：先 API / 网关，最后挂静态文件 ──
app.include_router(auth.router)
app.include_router(accounts.router)
app.include_router(keys.router)
app.include_router(logs.router)
app.include_router(stats.router)
app.include_router(security_router.router)
app.include_router(settings.router)
app.include_router(system.router)
app.include_router(models.router)
app.include_router(playground.router)
app.include_router(gateway.router)
# Anthropic Messages API 兼容层（/v1/messages）——给只认该协议的客户端用
app.include_router(anthropic.router)


@app.middleware('http')
async def cache_headers(request: Request, call_next):
    """按内容性质设置缓存策略与安全响应头。

    - /_next/static/**：文件名含内容哈希，可长期强缓存（immutable）
    - /api/**、/v1/**：动态数据，禁止任何缓存（含浏览器与中间代理）
    - 其余（HTML 文档）：no-cache，即每次回源校验 ETag，避免拿到旧页面

    安全头说明：
    - X-Content-Type-Options: 阻止浏览器嗅探类型（防内容被当作脚本执行）
    - X-Frame-Options / frame-ancestors: 禁止被其他站点内嵌（防点击劫持）
    - Referrer-Policy: 跨站请求不带完整 URL（避免泄露路径）
    - CSP: 只允许同源资源与内联样式（前端使用内联样式属性）；
      限制外联目标，降低 XSS 得手后的影响面
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith('/_next/static/'):
        # 文件名含内容哈希，内容变了文件名就变，可长期强缓存
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    elif path.startswith(('/api/', '/v1/', '/v2/', '/healthz')):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
    elif path.startswith('/favicon/') or path.endswith(('.png', '.ico', '.svg', '.woff2', '.webmanifest')):
        # 图标 / 字体等静态资源，内容基本不变，缓存一天
        response.headers['Cache-Control'] = 'public, max-age=86400'
    else:
        response.headers['Cache-Control'] = 'no-cache'

    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'no-referrer')
    response.headers.setdefault('Permissions-Policy', 'geolocation=(), microphone=(), camera=()')
    response.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'",
    )
    return response


@app.get('/api/sysinfo')
def sysinfo(user: dict = Depends(security.current_user)) -> dict:
    """服务信息。需要登录 —— 路径类信息不应对未认证访问者暴露。"""
    return {
        'service': 'workbuddy-manager',
        'version': app.version,
    }


# ── 静态前端（Next.js 静态导出）────────────────────────
def _looks_like_traversal(full_path: str) -> bool:
    """判断请求路径是否像目录穿越尝试（用于记日志，不参与拦截决策）。

    拦截一律由 `_safe_static_path` 的包含性校验负责，这里只是让安全事件
    留下痕迹：静态路径原本不记录任何访问日志，万一被利用了也无从发现。
    """
    p = (full_path or '').replace(chr(92), '/')
    return '..' in p.split('/') or p.startswith('/') or ':' in p


def _safe_static_path(full_path: str) -> Path | None:
    """把请求路径解析为静态目录下的真实文件；越界或非法一律返回 None。

    安全（重要）：这里曾直接把请求路径拼到 STATIC_DIR 上，未做任何越界校验。
    由于 ASGI 会先对 %2f 解码，`/..%2f..%2fdata%2fusers.json` 这类请求
    在 `Path / str` 拼接后指向了部署目录**之外**，导致任意文件读取——
    实测可读到 `users.json`（内含签发会话的 secret，可据此伪造 admin 会话）、
    `.env`、上游 `config.json`（api_key）与 `auths/*.json`（账号 accessToken）。

    修法：用 `resolve()` 归一化后，强制要求结果仍位于 STATIC_DIR 之内。
    这是防目录穿越的标准做法，能同时覆盖 `..`、编码斜杠、绝对路径与
    符号链接等各变体；不合规的直接当作 404，不泄露任何信息。
    """
    if not full_path:
        return None
    try:
        root = config.STATIC_DIR.resolve()
        # 绝对路径（如 full_path 以 / 开头或形如 C:\...）会被 Path 当作新根，
        # 这里先剥掉前导分隔符，再统一在后面做包含性校验。
        candidate = (root / full_path.lstrip('/\\')).resolve()
    except (OSError, ValueError, RuntimeError):
        # resolve() 在符号链接成环等情况下可能抛错：一律视为不可访问
        return None
    if candidate != root and root not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


if config.STATIC_DIR.is_dir():
    app.mount('/_next', StaticFiles(directory=str(config.STATIC_DIR / '_next')), name='next-assets')
    if (config.STATIC_DIR / 'favicon.ico').exists():
        @app.get('/favicon.ico', include_in_schema=False)
        def favicon() -> FileResponse:
            return FileResponse(config.STATIC_DIR / 'favicon.ico')

    @app.get('/', include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(config.STATIC_DIR / 'index.html')

    @app.get('/{full_path:path}', include_in_schema=False)
    def spa(full_path: str):
        # 优先命中导出的静态页面 / 资源，否则回退到 404 页面。
        # 所有路径都必须先通过 _safe_static_path（越界即 None → 404）。
        #
        # 越界尝试会记一条 WARN 日志：这类请求 100% 是攻击或扫描行为，
        # 之前不记录任何痕迹，出事后无从追溯。日志只写路径，不含内容。
        if _looks_like_traversal(full_path):
            logger.warning('拦截疑似路径穿越请求: %r', full_path[:300])
        target = _safe_static_path(full_path)
        if target is not None:
            return FileResponse(target)
        index_candidate = _safe_static_path(f'{full_path}/index.html')
        if index_candidate is not None:
            return FileResponse(index_candidate)
        not_found = config.STATIC_DIR / '404.html'
        if not_found.is_file():
            return FileResponse(not_found, status_code=404)
        return JSONResponse({'error': 'not found'}, status_code=404)
