"""WorkBuddy Manager 入口：管理 API + 对外反代网关 + 静态前端托管。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import json
import logging

from . import config, db, redpacket, security
from .iputil import client_ip
from .stdio_utf8 import force_utf8_stdio
from .routers import (
    accounts, anthropic, auth, gateway, keys, logs, models, playground,
    pgsync as pgsync_router, redpackets, responses, security as security_router,
    proxies, settings, stats, system, tokens, upstreams,
)
from .services import accountlog, cooling, dailyplan, pgsync, renew, tasklog, taskrun

# 最早时刻把标准输出/错误钉成 UTF-8：Windows 中文区域默认按 cp936 编码，日志里
# 一旦出现 emoji（账号昵称就可能带）就会抛 UnicodeEncodeError —— 与「成长任务」
# 那次崩溃同源（issue #147），只是触发条件更少。
force_utf8_stdio()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    db.connect()
    security.load_users()  # 首次启动会自动生成管理员并打印一次密码
    weak_admins = security.weak_admin_passwords()
    if weak_admins:
        # 不拒绝启动：用户需要先进面板改密码。日志必须足够醒目。
        logger.error('管理员仍在使用常见弱口令：%s。请立即在「设置 → 管理用户」'
                     '中修改；本次生产事故的入口就是 admin123。',
                     '、'.join(weak_admins))
    _warn_if_exposed()
    _warn_weak_upstream_key()
    # 给「抽奖码」这一列上线之前建的红包补码（幂等）：没有码就拼不出抽奖链接，
    # 等于那些红包只能自己发 key、没法让大家抽。
    filled = redpacket.backfill_codes()
    if filled:
        logger.info('为 %d 个旧红包补上了抽奖码', filled)
    # 后台采集上游自动任务日志（旅行/活跃/签到/保活），容器日志会被重建清掉，
    # 这里解析后落库长期保留，界面才能看到「这趟旅行领了多少积分」
    tasklog.start_collector()
    # 定时领奖（成长任务里幂等的那一半）：只把已完成任务的奖励领回来，不伪造
    # 任何活跃上报，因此可以安全地到点自动跑。点亮那半只允许手动（见 taskrun）
    taskrun.start_scheduler()
    # token 自动续期（issue #40）：上游只在「保活时刻」与「有流量时」刷新，
    # 长期闲置的账号会一路走到过期。这里按剩余寿命巡检补齐那个空档。
    renew.start_scheduler()
    # 强制冷却到期清理：manual_disabled 不会随 until 自动消失，不清理会让
    # 定时冷却变成永久停用（见 services/cooling.py）。
    cooling.start_scheduler()
    # 每日任务计划（签到 / 领奖 / 做任务）：管理端自己的调度器，把「一键」动作
    # 做成每天到点自动跑。默认全部关闭，由用户在账号页显式开启（full 有风控风险）。
    dailyplan.start_scheduler()
    # 请求日志的「账号」回填（issue #69）：账号是上游选的、不在响应里回传，
    # 只能从它的容器日志里读出来再按时间对回去（见 accountlog 的说明）。
    accountlog.start_collector()
    # PostgreSQL 定时备份（用户反馈：怕服务器暴雷丢数据）。默认关闭 ——
    # 只有配好连接、打开开关并设了间隔才会真的推数据出去。
    pgsync.start_scheduler()
    try:
        yield
    finally:
        tasklog.stop_collector()
        taskrun.stop_scheduler()
        renew.stop_scheduler()
        cooling.stop_scheduler()
        dailyplan.stop_scheduler()
        accountlog.stop_collector()
        pgsync.stop_scheduler()
        # 出站客户端是共享的（issue #144：Windows 上每次新建要 ~1 秒），
        # 退出时统一关掉，别把连接池与 SSL 上下文丢在原地。
        await config.close_clients()


def _warn_weak_upstream_key() -> None:
    """上游 api_key 为空或仍是样例值时点名警告（2026-10-21 安全检查 H3）。

    随包上游 workbuddy2api 的鉴权是「api_key 非空才启用」：为空时 /v1 对话、
    /status、统计、管理端点全部无需凭据即可访问，且它的启动日志只打一个布尔
    值，用户很难察觉。本面板是它的前置入口，部署形态下端口已收敛回环，但
    只要 7863 被改绑到对外地址（或同机其他进程可达），弱 key 就等于公开。
    读不到配置（本地开发没有上游）时静默跳过。
    """
    try:
        cfg = json.loads(config.UPSTREAM_CONFIG.read_text(encoding='utf-8'))
    except Exception:  # noqa: BLE001
        return
    key = str(cfg.get('api_key') or '').strip()
    if not key:
        logger.error(
            '上游 config.json 的 api_key 为空：%s 的全部端点（对话、状态、统计、'
            '管理）此刻不需要任何凭据。请在「设置 → 上游」立即配置强 api_key。',
            config.UPSTREAM_CONFIG,
        )
    elif key.lower() in ('test_key', 'changeme', 'your_api_key'):
        logger.error(
            '上游 api_key 仍是样例值 %r：等同于公开，任何知道默认值的人都能'
            '消耗账号池积分。请立即改为强随机值。', key,
        )


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
    version='2.9.0',
    lifespan=lifespan,
    # 生产环境默认关闭交互式文档与 OpenAPI 描述：
    # 它们会把管理接口全貌（路径、参数、结构）暴露给任何未认证访问者，
    # 便于攻击者摸清面。需要时设 WB_ENABLE_DOCS=1 打开。
    docs_url='/docs' if config.ENABLE_DOCS else None,
    redoc_url='/redoc' if config.ENABLE_DOCS else None,
    openapi_url='/openapi.json' if config.ENABLE_DOCS else None,
)

def _validate_cors_origins(origins: list[str]) -> None:
    """`*` 与凭据模式互斥：命中直接抛错，让服务起不来（配置错误要在启动时暴露）。"""
    if any(o.strip() == '*' for o in origins):
        raise RuntimeError(
            'WB_CORS_ORIGINS 含 `*` 且管理端启用 Cookie 凭据：任意网站都能冒用'
            '管理员身份读写 /api/*。请改为列出具体来源（逗号分隔），或留空'
            '（同源部署，默认）。'
        )


if config.CORS_ORIGINS:
    # 只允许**明确列出的**来源。绝不要把 WB_CORS_ORIGINS 设成 `*`：
    # 本应用用 Cookie 认证管理端，而 Starlette 在 allow_credentials=True 时
    # 会把 `*` 回显成请求方 Origin（不是字面 `*`），于是**任意网站**都能带着
    # 管理员的 Cookie 调 /api/* 并读到响应 —— 等于把控制台交给任何网页。
    # 留空（默认）即同源部署，安全。
    #
    # 这是"误配即全面接管"的开关，因此检测到 `*` 直接拒绝启动，而不是只记
    # 一条日志继续跑：日志拦不住照抄配置的人，历史上确有人这么配过。
    _validate_cors_origins(config.CORS_ORIGINS)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=['*'],
        allow_headers=['*'],
    )

# 管理端请求体上限。网关那条路有自己的逐块校验（gateway._read_json_body），
# 但 `/api/*` 此前**没有任何上限** —— 未鉴权的 `/api/login` 就能用大 body
# 把内存占住（实测：12MB 请求体被完整缓冲并解析，没有 413）。这里统一兜住。
# 取得比网关默认（8MB）宽松些：管理端有「保存上游配置」这类正常的大请求。
MAX_API_BODY_BYTES = 16 * 1024 * 1024

# 唯一需要更大请求体的管理端路径：上传离线升级包（issue #153）。发布包现在约
# 11 MB，将来会变大，所以这里给到 64 MB —— 仍然是个**硬上限**，不是没有上限：
# 上传本身还要过会话管理员鉴权，且落盘后立刻做 tar.gz 结构与 .version 校验。
# 与 `updater.PACKAGE_MAX_BYTES` 必须一致（有测试盯着，别只改一边）。
MAX_PACKAGE_BODY_BYTES = 64 * 1024 * 1024
PACKAGE_UPLOAD_PATHS = ('/api/system/update-package',)


@app.middleware('http')
async def limit_api_body(request: Request, call_next):
    """给 `/api/*` 的请求体加上限（上传升级包那条路放宽，见上面的常量）。

    只看 `Content-Length`：本中间件拦的是「声明了超大长度」这条最容易发起、
    也最容易自动化的路径（无鉴权即可打 /api/login），不用读流就能立即拒绝。
    不带该头的分块请求在这里声明为 0、拦不住，由下面的
    `ApiBodyLimitMiddleware` 在**实际读取层**强制。
    """
    if request.url.path.startswith('/api/'):
        try:
            declared = int(request.headers.get('content-length') or 0)
        except ValueError:
            declared = 0
        limit = (MAX_PACKAGE_BODY_BYTES
                 if request.url.path in PACKAGE_UPLOAD_PATHS else MAX_API_BODY_BYTES)
        if declared > limit:
            return JSONResponse(
                {'detail': f'请求体过大（上限 {limit // 1024 // 1024} MB）'},
                status_code=413,
            )
    return await call_next(request)


class _ApiBodyTooLarge(Exception):
    """内部信号：/api/* 请求体在读取层超过上限。只被 ApiBodyLimitMiddleware 捕获。"""


class ApiBodyLimitMiddleware:
    """在 ASGI receive 层强制 `/api/*` 的请求体上限。

    为什么上面的 Content-Length 预检不够：分块传输（chunked）没有该头，
    `declared` 为 0 直接放行；而 handler 里的 `await request.json()` 会把整个
    body 无上限地聚合进内存（Starlette 的 `Request.body()` 逐块追加，uvicorn
    自身也不设请求体上限）。`/api/login` 是未鉴权可达的，少量并发分块大请求
    就能把这个进程（管理面、/v1 网关、静态托管同体）的内存吃光。
    网关那条路早有同款读取层限额（`gateway._read_json_body`），这里补管理面。

    实现：包住 receive 通道逐块计数，超限即抛内部异常中止 handler，由本层
    （异常向外传播的必经之路）直接发 413。此时 handler 还在读 body、尚未
    开始写响应，所以 413 是该连接上唯一的响应，不会出现"半个响应 + 413"。
    """

    def __init__(self, app, *, limit: int = MAX_API_BODY_BYTES,
                 package_limit: int = MAX_PACKAGE_BODY_BYTES,
                 package_paths: tuple = PACKAGE_UPLOAD_PATHS) -> None:
        self.app = app
        self.limit = limit
        self.package_limit = package_limit
        self.package_paths = package_paths

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http' or not scope.get('path', '').startswith('/api/'):
            await self.app(scope, receive, send)
            return
        limit = (self.package_limit
                 if scope['path'] in self.package_paths else self.limit)
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get('type') == 'http.request':
                received += len(message.get('body') or b'')
                if received > limit:
                    raise _ApiBodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _ApiBodyTooLarge:
            response = JSONResponse(
                {'detail': f'请求体过大（上限 {limit // 1024 // 1024} MB）'},
                status_code=413,
            )
            await response(scope, receive, send)


app.add_middleware(ApiBodyLimitMiddleware)

# ── 路由注册顺序很重要：先 API / 网关，最后挂静态文件 ──
app.include_router(auth.router)
app.include_router(accounts.router)
app.include_router(keys.router)
# 红包：批量发放带额度的密钥（与密钥同属「分发」这件事，所以挨着放）
app.include_router(redpackets.router)
# 抽奖：**公开端点**（收到链接的人不需要账号），单独挂便于区分边界
app.include_router(redpackets.claim_router)
app.include_router(logs.router)
app.include_router(stats.router)
app.include_router(security_router.router)
# 管理面作用域化 API Token（见 docs/api-tokens.md）；接口本身只接受会话鉴权
app.include_router(tokens.router)
app.include_router(settings.router)
# 代理线路与全局出口代理（设置 → 代理）
app.include_router(proxies.router)
# PostgreSQL 异地备份：把本地数据镜像出去、需要时拉回来（见 services/pgsync）
app.include_router(pgsync_router.router)
# 多上游（账号池分组）：密钥绑定上游 = 请求走那个池，见 upstreamsvc
app.include_router(upstreams.router)
app.include_router(system.router)
app.include_router(models.router)
app.include_router(playground.router)
app.include_router(gateway.router)
# Anthropic Messages API 兼容层（/v1/messages）——给只认该协议的客户端用
app.include_router(anthropic.router)
# OpenAI Responses API 兼容层（/v1/responses、/responses）——给 Codex /
# DeepSeek Harness 的 openai-responses 协议用
app.include_router(responses.router)


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

    # 会话滑动续期：current_user 判定「该续了」时在这里重签 cookie。
    #
    # 为什么放中间件而不是 current_user 里：那个函数是 FastAPI 依赖，只负责
    # **解析身份**，在它里面写响应会耦合出不必要的层，而且它被大量接口调用、
    # 有的还是只读的。中间件是唯一能看到「响应对象 + 请求状态」的地方。
    if getattr(request.state, 'session_renew', False):
        try:
            token = request.cookies.get(config.COOKIE_NAME) or ''
            cfg = security.load_users()
            obj = security._unsign(token, cfg['secret']) or {}
            username = str(obj.get('username') or '')
            if username:
                # orig 原样延续 —— 续期只推后**空闲截止**，不延长**总寿命**。
                # role 传空：鉴权一律以用户表为准（载荷里的 role 仅作展示参考）。
                fresh = security.issue_token(username, '', orig=int(obj.get('orig') or 0) or None)
                response.set_cookie(
                    config.COOKIE_NAME, fresh,
                    max_age=config.SESSION_DAYS * 86400, httponly=True,
                    samesite='lax', secure=security.cookie_secure(request), path='/',
                )
        except Exception as exc:  # noqa: BLE001
            # 续期失败不能让请求失败 —— 用户这次照常，只是下次要重登
            logger.warning('会话续期失败（不影响本次请求）: %s', exc)

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


def _is_document_request(request: Request) -> bool:
    """这次请求是不是「把结果当页面显示」的文档型导航。

    浏览器的地址栏导航会带 `Accept: text/html,...`；而客户端路由抓 RSC 数据
    用的是 `fetch`（`Accept: */*` 或 `text/x-component`）并带 `RSC: 1` 头。
    两者必须分开：前者拿到的若是 flight 文本，用户看到的就是满屏原始数据。
    """
    if request.headers.get('rsc'):
        return False
    return 'text/html' in (request.headers.get('accept') or '').lower()


def _rsc_page_for(full_path: str) -> str | None:
    """文档型请求命中 Next 静态导出的 RSC 数据文件时，返回它对应的页面路径。

    `output: 'export'` 会把每个页面的 RSC（flight）数据写成
    `<页>/index.txt`（实测：`dashboard/index.txt`、`settings/index.txt`…）。
    正常客户端拿它做客户端路由的数据源；但 router 的兜底分支
    （`Failed to fetch RSC payload … Falling back to browser navigation`）
    会把浏览器**整页导航**到这个地址，于是浏览器按 `text/plain` 渲染那份
    flight 数据、地址栏也变成 `.txt`，刷新只会继续显示它（issue #48）。

    **只有对应页面确实存在时才返回**：`.txt` 也可能是真实静态文件
    （如 `robots.txt`），那种没有同名页面，不能一并重定向。

    两种导出形态都要认：`<页>/index.html`（绝大多数页面，实测 dashboard 等）
    与 `<页>.html`（根页面就是这种：`index.html` + `index.txt`）。少认一种，
    对应形态的页面在真机上就会继续显示原始数据。
    """
    p = (full_path or '').strip('/')
    if not p.endswith('.txt'):
        return None
    stem = p[:-4]
    if stem.endswith('/index'):
        stem = stem[: -len('/index')]
    elif stem == 'index':
        stem = ''
    candidates = (f'{stem}/index.html' if stem else 'index.html',
                  f'{stem}.html' if stem else 'index.html')
    if not any(_safe_static_path(c) is not None for c in candidates):
        return None          # 没有同名页面 → 是真实文件，按普通静态资源处理
    return f'/{stem}' if stem else '/'


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
    def spa(full_path: str, request: Request):
        # 优先命中导出的静态页面 / 资源，否则回退到 404 页面。
        # 所有路径都必须先通过 _safe_static_path（越界即 None → 404）。
        #
        # 越界尝试会记一条 WARN 日志：这类请求 100% 是攻击或扫描行为，
        # 之前不记录任何痕迹，出事后无从追溯。日志只写路径，不含内容。
        if _looks_like_traversal(full_path):
            logger.warning('拦截疑似路径穿越请求: %r', full_path[:300])
        # ① 文档型请求命中 RSC 数据文件 → 送回对应页面（issue #48）。
        # 见 `_rsc_page_for` 的说明：客户端路由的兜底分支会把浏览器整页导航到
        # `<页>/index.txt`，那边返回的是 flight 文本，用户看到满屏原始数据、
        # 地址栏也变成了 .txt，只能手动改回地址。
        if _is_document_request(request):
            page = _rsc_page_for(full_path)
            if page is not None:
                # 补上子路径前缀：反代剥掉前缀后才到这里，但 Location 是发回浏览器
                # 的绝对地址，不带前缀就会跳到域名根（另一个站点）上。
                return RedirectResponse(f'{config.BASE_PATH}{page}', status_code=302)
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


# ── 子路径部署（反向代理前缀）────────────────────────────────────────
#
# 反向代理把本站挂在 /workbuddy-manager 这类前缀下、且**不剥离**前缀时，后端
# 收到的路径仍带着前缀 —— 路由、静态挂载与 SPA 兜底全都匹配不上，页面会 404。
# 这里在最外层统一剥掉，让内部逻辑只看到「前缀之后的路径」。
#
# 反向代理若已经剥离了前缀（proxy_pass 带 URI 的常见写法），本中间件找不到
# 前缀、原样放行 —— 两种反代配置都能工作，nginx 侧不必改。
#
# 响应侧同步处理：SPA 的 RSC 兜底等场景会 302 到站内绝对路径（`/xxx`），
# 不补回前缀就会把用户带出子路径、落到站点根目录。
#
# 与 `config.BASE_PATH` 的分工：那个值被用来**主动构造**带前缀的绝对地址
# （见上面 SPA 兜底的 RedirectResponse）；本中间件负责「进来的路径带前缀」与
# 「出去的 Location 漏前缀」这两件事。两者都指同一个 WB_BASE_PATH。
class StripBasePathMiddleware:
    """纯 ASGI 中间件：剥离请求前缀，并把响应里的站内绝对位置补回前缀。"""

    def __init__(self, app, prefix: str) -> None:
        self.app = app
        self.prefix = prefix
        self._prefix_slash = prefix + '/'

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return

        path = scope.get('path') or '/'
        if path == self.prefix or path.startswith(self._prefix_slash):
            stripped = path[len(self.prefix):] or '/'
            scope = dict(scope)
            scope['path'] = stripped
            scope['raw_path'] = stripped.encode('utf-8')

        async def send_with_prefix(message):
            if message.get('type') == 'http.response.start' and message.get('headers'):
                message = dict(message)
                message['headers'] = [
                    (k, self._rewrite_location(v) if k.lower() == b'location' else v)
                    for k, v in message['headers']
                ]
            await send(message)

        await self.app(scope, receive, send_with_prefix)

    def _rewrite_location(self, value: bytes) -> bytes:
        try:
            text = value.decode('latin-1')
        except Exception:  # noqa: BLE001
            return value
        # 只处理站内绝对路径（/ 开头，且不是 //host 这种协议相对写法）
        if not text.startswith('/') or text.startswith('//'):
            return value
        # 已经带前缀的不重复叠加（SPA 兜底那处已自己补过）
        if text == self.prefix or text.startswith(self._prefix_slash):
            return value
        return (self.prefix + text).encode('latin-1')


if config.BASE_PATH:
    # add_middleware 后注册的在外层，所以这行放在文件末尾：请求进来先过它，
    # 后面的限流 / 缓存头 / 路由看到的都是剥离后的路径。
    app.add_middleware(StripBasePathMiddleware, prefix=config.BASE_PATH)
    logger.info('子路径部署：已启用前缀 %s（反代可保留或自行剥离，两者都可用）', config.BASE_PATH)
