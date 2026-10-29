"""qoder2api 的服务端客户端：console 代理、会话管理、bridge 模型清单缓存。

职责边界（与仓库对 workbuddy2api 的处理方式同款）：qoder2api 是**外部上游**，
本项目只做对接——读它的 HTTP 契约、调它的端口，不 spawn、不侵入。两个入口：

  · **console**（Web 控制台，默认 :3588）：面板「设置 → Qoder」的所有读写操作
    都经这里代理。鉴权是会话 Cookie（`qoder2api_session_<port>`，见
    refs/qoder2api/auth.go L20），所以本模块做「面板代登录」——用管理员在这里
    存的密码换 Cookie，**Cookie 只进进程内存缓存，绝不落库**：它由密码派生、
    有寿命，存库只会制造一条绕过「改密码即失效」的旁路。
  · **bridge**（数据面桥，:8963/:8964）：数据面转发由 gateway 走既有链路完成，
    本模块只做一件事——拉 `/v1/models` 喂给 `routers/keys.py::check_models`
    的白名单校验缓存（C5：qoder 模型名无 realm 前缀，落不进腾讯目录的任何
    一个版本桶，没有这份缓存它们会被白名单校验标成 unknown 假警报）。

SSRF 口径（与既有信任模型对齐，刻意**不**套 wb2api._reject_internal_host）：
console 地址与上游 base_url 同属**管理员配置的服务端配置**——标准部署的
console 恰恰就在 127.0.0.1:3588，按「终端用户输入的探测目标」拦截会把主部署
形态直接打死。本模块的防线是：①保存凭据的端点要求会话管理员；②可代理路径
是硬编码白名单 `CONSOLE_PATHS`，**不存在用户可控的 URL/host**；③响应只回
JSON body，不回传上游响应头与 Set-Cookie；④凭据 write-only（见下）。
"""
from __future__ import annotations

import asyncio
import time

from .. import config, db

# settings 表里的唯一新键（KV、JSON 值，见 db.get_setting/set_setting）。
CONSOLE_SETTING_KEY = 'qoder_console'

# 登录响应的 Set-Cookie 名是 `qoder2api_session_<consolePort>`（auth.go L20），
# 端口号是上游自己的配置——所以这里只留前缀，实际名字从登录响应里**原样回读**，
# 不手工拼：拼错端口号 = 每次请求都 401，而日志里什么也看不出来。
SESSION_COOKIE_PREFIX = 'qoder2api_session'

# 超时分层：登录/读 5s 连接 + 15s 读取；写操作（签到/切激活/桥启停）上游侧
# 可能要做账号遍历，放宽到 30s——短了会把「正在做、马上好」误报成超时。
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 15
WRITE_TIMEOUT = 30

# bridge 模型清单缓存 TTL。清单本身在 bridge 侧有小缓存，这里 10 分钟够了：
# 缓存只服务白名单校验的「known 集合」，过期只是退化成「不判」，不会出错。
MODEL_CACHE_TTL = 600


class QoderError(Exception):
    """带 status_code 的用户可见错误；message 面向面板直接展示。"""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.message = str(message)


class QoderNotConfigured(QoderError):
    """console 尚未配置（没填地址或密码）。400——引导用户去「设置 → Qoder」。"""

    def __init__(self, message: str = 'Qoder 控制台未配置：请先在「设置 → Qoder」'
                                     '填写控制台地址与密码') -> None:
        super().__init__(400, message)


# ── console 凭据（settings 键 qoder_console，write-only）──────────────

def load_console_config() -> dict:
    """读取 console 配置的**对外形状**：密码永不出此函数。

    与 settings.github_token 同款既有口径（routers/settings.py 只回显 set 与否）：
    明文落库（库文件权限已收 0600），接口层 write-only。
    """
    raw = db.get_setting(CONSOLE_SETTING_KEY, {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    url = str(raw.get('url') or '').strip()
    password = str(raw.get('password') or '')
    return {
        'configured': bool(url and password),
        'url': url,
        'password_set': bool(password),
    }


def save_console_config(url: str, password: str | None) -> dict:
    """保存 console 凭据。`password is None` = 不修改（PATCH 语义）。

    与 upstreamsvc.update_upstream 的 `'key' in patch` 口径一致——但这里的入参
    是展开后的两个值，所以用 None 表示「没传」：显式传 **空串** 是「清空密码」，
    也是能生效的动作（保存后 configured 变 False，代理端点开始报未配置）。
    保存成功后使会话缓存失效：密码改了，旧 Cookie 不该再被复用。
    """
    base = str(url or '').strip().rstrip('/')
    # 复用 upstreamsvc.normalize_base_url 的校验口径（http(s) 前缀 + 去尾斜杠）。
    # 刻意 import 它而不是抄一份：转发侧是字符串拼接，两处校验口径漂移时，
    # 「保存成功、调用 404」会是最难排查的一类现场。
    from .. import upstreamsvc
    base = upstreamsvc.normalize_base_url(base) if base else ''
    current = db.get_setting(CONSOLE_SETTING_KEY, {}) or {}
    if not isinstance(current, dict):
        current = {}
    next_cfg: dict = {'url': base, 'saved_at': int(time.time())}
    if password is None:
        next_cfg['password'] = str(current.get('password') or '')
    else:
        next_cfg['password'] = str(password or '').strip()
    if base:
        db.set_setting(CONSOLE_SETTING_KEY, next_cfg)
    else:
        # 地址被清空 = 整个关闭 Qoder 接入：连同密码一起删，不留半开状态。
        db.set_setting(CONSOLE_SETTING_KEY, {})
    _invalidate_session(base)
    return load_console_config()


# ── console 会话（进程内存缓存）───────────────────────────────────────
# {base: (cookie_value, cookie_name, expires_at)}。单进程假设与仓库既有的
# 限流计数 / 登录失败计数一致（main.py lifespan 是单进程形态）。
_sessions: dict[str, tuple[str, str, float]] = {}
_session_lock = asyncio.Lock()


async def _login(base: str, password: str) -> tuple[str, str, int]:
    """POST {base}/api/auth/login 换会话 Cookie（refs/qoder2api/auth.go L223-245）。

    返回 (cookie_value, cookie_name, expires_epoch)。非 200 / ok≠true 一律
    QoderError(401)：控制台的失败语义只有「密码不对」一种，细分没有价值。
    """
    body: dict = {}
    try:
        import httpx
        timeout = httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT)
        async with config.http_client(timeout) as client:
            resp = await client.post(f'{base}/api/auth/login',
                                     json={'password': password})
        if resp.status_code == 200:
            payload = resp.json()
            if isinstance(payload, dict) and payload.get('ok'):
                expires_at = int(payload.get('expires_at') or 0)
                cookie_name, cookie_value = '', ''
                for name, value in resp.cookies.items():
                    if name.startswith(SESSION_COOKIE_PREFIX):
                        cookie_name, cookie_value = name, str(value)
                        break
                if cookie_value:
                    return cookie_value, cookie_name, expires_at
            body = payload if isinstance(payload, dict) else {}
    except Exception as exc:  # noqa: BLE001 —— 网络/解析失败统一转 502 语义
        raise QoderError(502, f'无法连接 Qoder 控制台：{exc}') from exc
    raise QoderError(401, 'Qoder 控制台密码错误或已变更，请在「设置 → Qoder」更新')


async def _session(base: str) -> str:
    """取当前会话 Cookie（形如 `name=value`）；缓存无效则重新登录。"""
    async with _session_lock:
        cached = _sessions.get(base)
        if cached:
            value, name, expires_at = cached
            # 上游的 expires_at 是秒级 epoch；本地时钟略有偏差时宁可提前重登，
            # 也不要带着一个临期 Cookie 去吃 401 再重登（多一次往返）。
            if expires_at and expires_at - 30 > time.time():
                return f'{name}={value}'
        cfg = db.get_setting(CONSOLE_SETTING_KEY, {}) or {}
        if not isinstance(cfg, dict):
            cfg = {}
        password = str(cfg.get('password') or '')
        if not password:
            raise QoderNotConfigured()
        value, name, expires_at = await _login(base, password)
        _sessions[base] = (value, name, float(expires_at or (time.time() + 3600)))
        return f'{name}={value}'


def _invalidate_session(base: str) -> None:
    """使某基地址的会话缓存失效（保存新密码 / 吃到 401 时调用）。

    同步函数、直接改 dict：asyncio.Lock 只保护「检查+重登」这条读路径，
    清除动作本身是单条赋值，不需要锁。
    """
    _sessions.pop(base, None)


# ── console 代理 ──────────────────────────────────────────────────────
# 可代理路径白名单。oauth 三兄弟（start/wait/cancel）、accounts/add|delete、
# cleanup 不在名单：P1 只做只读 + 轻写（R07-R09）——账号增删与 OAuth 涉及
# 凭据录入与设备指纹，走 qoder2api 自己的控制台更合适，面板代理反而多一份
# 泄露面。路由层只传常量，这里是纵深防御：将来有人拼接用户输入也出不去。
CONSOLE_PATHS = frozenset({
    '/api/status', '/api/accounts', '/api/accounts/quota', '/api/models',
    '/api/settings', '/api/checkin', '/api/active',
    '/api/bridge/start', '/api/bridge/stop',
})

_WRITE_PATHS = frozenset({'/api/checkin', '/api/active',
                          '/api/bridge/start', '/api/bridge/stop'})


async def console_request(method: str, path: str, *,
                          json_body: dict | None = None,
                          timeout: float = READ_TIMEOUT) -> dict:
    """代理一次 console 请求：带会话 Cookie，401 自动重登一次再重放一次。

    错误约定（routers/qoder.py 按此映射）：
      · 未配置           → QoderNotConfigured（400）
      · 白名单外路径      → QoderError(400)（纵深防御，正常流量到不了这里）
      · 重登后仍 401     → QoderError(401, 密码错误或已变更)
      · 连接失败 / 超时   → QoderError(502, 无法连接)
      · 响应非 JSON      → {'raw': text[:2000]}（console 的失败页是 HTML，
                           吞掉它换 500 会让面板看不到上游说了什么）
    """
    cfg = load_console_config()
    if not cfg['configured']:
        raise QoderNotConfigured()
    if path not in CONSOLE_PATHS:
        raise QoderError(400, f'不允许代理的路径：{path}')
    base = str(cfg['url'] or '').rstrip('/')
    for attempt in (1, 2):
        cookie = await _session(base)
        headers = {'Cookie': cookie}
        try:
            import httpx
            t = httpx.Timeout(timeout, connect=CONNECT_TIMEOUT)
            async with config.http_client(t) as client:
                if method.upper() == 'POST':
                    resp = await client.post(f'{base}{path}', json=json_body or {},
                                             headers=headers)
                elif method.upper() == 'GET':
                    resp = await client.get(f'{base}{path}', headers=headers)
                else:
                    raise QoderError(400, f'不支持的代理方法：{method}')
        except QoderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise QoderError(502, f'无法连接 Qoder 控制台：{exc}') from exc
        if resp.status_code == 401:
            # 会话失效（密码被改 / 控制台重启清了会话）：清缓存重登一次。
            # 只重试一次——第二次 401 就是密码本身的问题，再重试只会把
            # 「密码错了」放大成一场 401 风暴。
            _invalidate_session(base)
            if attempt == 1:
                continue
            raise QoderError(401, 'Qoder 控制台密码错误或已变更，'
                                  '请在「设置 → Qoder」更新')
        try:
            return resp.json()
        except ValueError:
            return {'raw': str(resp.text or '')[:2000]}
    raise QoderError(401, 'Qoder 控制台密码错误或已变更，请在「设置 → Qoder」更新')


async def probe_console() -> tuple[bool, str]:
    """「测试连接」：取会话 + 拉一次 /api/status，返回 (ok, 摘要)。"""
    try:
        status = await console_request('GET', '/api/status', timeout=READ_TIMEOUT)
    except QoderError as exc:
        return False, exc.message
    running = bool(status.get('running')) if isinstance(status, dict) else False
    active = str((status or {}).get('active_account') or '') if isinstance(status, dict) else ''
    summary = '控制台可达' + ('，桥接运行中' if running else '（桥接未运行）')
    if active:
        summary += f'，激活账号：{active}'
    return True, summary


# ── bridge 模型 id 缓存 ───────────────────────────────────────────────
# {base: (ids, fetched_at)}；check_models 只读缓存不发网络（routers/keys.py
# L117 的既有原则），清单由 /api/qoder/models 顺手暖缓存。
_model_cache: dict[str, tuple[frozenset, float]] = {}
_model_lock = asyncio.Lock()


async def fetch_model_ids(upstream: dict) -> frozenset[str]:
    """拉一个 qoder 上游的 bridge 模型清单并写入缓存。

    响应形状见 refs/qoder2api/internal/bridge/claude.go L315-370：
    `{"object":"list","data":[{"id":…,"owned_by":"qoder",…}]}`。取不到
    `data[].id` 时返回空集（而不是抛错）：清单拉不到只是「校验退化为不判」，
    不该让模型页跟着报错。
    """
    base = str((upstream or {}).get('base_url') or '').rstrip('/')
    if not base:
        return frozenset()
    from .. import upstreamsvc
    headers = {}
    token = upstreamsvc.forward_api_key(upstream)
    if token:
        headers['Authorization'] = f'Bearer {token}'
    try:
        import httpx
        t = httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT)
        async with config.http_client(t) as client:
            resp = await client.get(f'{base}/v1/models', headers=headers)
        payload = resp.json()
    except Exception:  # noqa: BLE001 —— 拉不到清单只影响白名单提示，不报错
        return frozenset()
    items = payload.get('data') if isinstance(payload, dict) else None
    ids = frozenset(
        str(m.get('id') or '').strip()
        for m in (items or []) if isinstance(m, dict) and str(m.get('id') or '').strip()
    )
    async with _model_lock:
        _model_cache[base] = (ids, time.time())
    return ids


def cached_model_ids() -> frozenset[str] | None:
    """全部 qoder 上游缓存条目的**并集**；一个条目都没有（从未拉取/全过期）→ None。

    返回 None 的语义是「拿不到」，调用方（check_models）据此**不判**——
    把 None 当成空集会把「还没拉过清单」显示成「你白名单里的名字都写错了」，
    假警报比不提示更糟（与 modelcatalog.cached_ids 同一条约定）。
    """
    now = time.time()
    out: set[str] = set()
    for ids, fetched_at in list(_model_cache.values()):
        if now - fetched_at <= MODEL_CACHE_TTL:
            out.update(ids)
    return frozenset(out) if out else None
