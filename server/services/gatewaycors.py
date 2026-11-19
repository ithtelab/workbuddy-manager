"""网关（`/v1`、`/v2`）的 CORS 来源白名单 —— 面板可改、即时生效（issue #445）。

## 为什么它与 `WB_CORS_ORIGINS` 是两套东西

`WB_CORS_ORIGINS`（`server/config.py`）是**管理面**的来源白名单：那边用 Cookie
认证会话，所以 `allow_credentials=True`，一旦允许 `*` 等于把控制台交给任何网页
（见 `main.py` 里 `_validate_cors_origins` 的说明）。它必须留在环境变量里：
改错就是全面接管，不能在网页上点一下就改。

数据面（`/v1`、`/v2`）的鉴权是 `Authorization: Bearer sk-...`，**不依赖 Cookie**，
浏览器不会自动携带它。因此这里可以安全地允许 `*`：任意站点能发跨域请求，但拿不到
密钥就调不动；密钥本来就由持有者主动放到前端代码里，与来源无关。

于是两件事分开：
  · 管理面 → `WB_CORS_ORIGINS`（环境变量，禁止 `*`，`allow_credentials=True`）
  · 数据面 → 本模块（面板可配，允许 `*`，`allow_credentials=False`）

## 为什么后端要自己实现，而不是再挂一个 `CORSMiddleware`

`config.CORS_ORIGINS` 是**启动时**读取的：挂 `CORSMiddleware` 之后改设置必须重启。
而面板上的配置项如果改了不生效，用户会反复点保存、怀疑是自己填错——所以这里
每个预检请求现读设置（读 SQLite 一行，代价可忽略），改完立刻生效。

## 覆盖范围

只作用于 `DATA_API_PREFIXES` 里的路径；管理面 `/api/*` 一律不动（那条路由走
`WB_CORS_ORIGINS`）。预检（OPTIONS + `Access-Control-Request-Method`）由本模块
直接应答，普通请求只在来源命中时补 `Access-Control-Allow-Origin` 等头。
"""
from __future__ import annotations

from typing import Any

from .. import db
from .autoupdate import _as_bool

# 设置项在 settings 表里的键名。单键存整个配置对象（与 tap / proxy_pool 同思路）。
SETTING_KEY = 'gateway_cors'

# 受本模块管辖的路径前缀。`/v1` 与 `/v2` 是数据面（OpenAI / Anthropic 兼容层），
# `/api/status`、`/api/usage/token/` 是给客户端读余额的、同样用 Bearer 鉴权，
# 但它们挂在 `/api/*` 下 —— 不在这里放开，避免与 Cookie 管理面的边界混淆。
DATA_API_PREFIXES = ('/v1', '/v2')


def is_data_api_path(path: str) -> bool:
    """判断路径是否属于数据面。

    **按路径段匹配**，不是 `startswith`：后者会把 `/v1foo`、`/v10/x`、`/v2abc`
    也算进来（实测确认过），给这些本不属于数据面的路径加上 CORS 头。
    将来若真挂了 `/v1beta/x` 这类路由，那也是**同一个数据面**，按段匹配仍能覆盖。
    """
    p = path or ''
    for prefix in DATA_API_PREFIXES:
        if p == prefix or p.startswith(prefix + '/'):
            return True
    return False

# 允许的来源条数上限。来源列表是人手维护的（谁会在前端嵌入这个网关），几十条
# 已远超正常用量；设上限是为了让「误粘贴一大段」不会把设置项撑大。
MAX_ORIGINS = 64

# 单个来源的长度上限（`Origin` 头本身极短，超长必然是垃圾/攻击载荷）。
MAX_ORIGIN_LEN = 255

# 通配：`*` 表示任意来源。`null` 是浏览器给 `file://` 页面 / 沙箱 iframe 的
# 来源串，本地 HTML 直连网关时常见，所以允许显式列出（它是**字面量**，
# 不是「任意」，必须用户自己填）。
WILDCARD = '*'
NULL_ORIGIN = 'null'

DEFAULT: dict[str, Any] = {
    'enabled': False,
    'origins': [],
    # 预检结果缓存秒数。0 = 不缓存（每次预检都回源）。浏览器侧的上限是
    # 7200；默认 600 与 `CORSMiddleware` 的默认值一致。
    'max_age': 600,
}


def _clean_origin(value: object) -> str:
    """归一化单个来源：去掉尾部斜杠、拒绝非法字符。

    `Origin` 头的形态是 `scheme://host[:port]`（或字面量 `null`），**不带路径**。
    用户常把 `https://app.example.com/` 连斜杠一起粘进来，浏览器比对时会不相等
    （预检通过、真实请求被拒，症状很费解），所以在这里统一削掉。

    **必须是纯 ASCII**：值最终要写进 HTTP 响应头，而 ASGI 头按 latin-1 编码。
    中文域名（`https://例子.com`）看似合法，一旦回显就会抛 UnicodeEncodeError，
    把每次预检变成 500 —— 而且配置已经落库，用户从面板上再也进不去。
    浏览器发出的 `Origin` 一律是 punycode（`xn--...`），所以拒绝非 ASCII 不会
    挡住任何真实场景，只挡住手写中文域名的配置错误。
    """
    text = str(value or '').strip()
    if not text:
        return ''
    if text == NULL_ORIGIN or text == WILDCARD:
        return text
    if len(text) > MAX_ORIGIN_LEN:
        return ''
    # 必须带 scheme，且不含空白 / 引号 / 控制字符 —— 这些都会让响应头非法
    # （Starlette 编码时直接抛错，等于把一次配置错误变成 500）。
    if '://' not in text:
        return ''
    if any(ch in text for ch in ' \t\r\n"\'<>,;\\'):
        return ''
    # 非 ASCII 一律拒绝（理由见 docstring）
    try:
        text.encode('latin-1')
    except UnicodeEncodeError:
        return ''
    return text.rstrip('/')


def settings() -> dict[str, Any]:
    """当前配置。DB 里的值优先，缺项/脏值回落到默认值。

    **绝不抛异常**：这个函数跑在 `/v1` 的每个请求上。数据库忙、连接断了、
    settings 行被手工改成怪东西——任何一种都不该让网关整体 500。读失败时
    按「未配置」处理（等于关闭），请求照常往后走，由鉴权与上游决定成败。
    """
    try:
        raw = db.get_setting(SETTING_KEY, {}) or {}
    except Exception:  # noqa: BLE001 —— 旁路配置不能拖死数据面
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    origins: list[str] = []
    stored = raw.get('origins')
    if isinstance(stored, list):
        for item in stored[:MAX_ORIGINS]:
            clean = _clean_origin(item)
            # 去重但保留顺序：面板上显示的顺序即用户填写的顺序
            if clean and clean not in origins:
                origins.append(clean)
    try:
        max_age = int(raw.get('max_age', DEFAULT['max_age']))
    except (TypeError, ValueError):
        max_age = int(DEFAULT['max_age'])
    return {
        # 严格布尔：`bool('false')` 是 True —— 那会让「已经关掉了」的配置
        # 实际仍在放行，方向上是**错误放行**，必须按字面值判断（同 autoupdate）。
        'enabled': _as_bool(raw.get('enabled'), bool(DEFAULT['enabled'])) and bool(origins),
        'origins': origins,
        # 浏览器侧上限 7200；负值没有意义（等同于每次预检）
        'max_age': max(0, min(max_age, 7200)),
    }


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """保存配置。只接受已知字段，其余静默忽略（避免把任意键写进 settings）。

    **不允许出现重复项**，也不接受非字符串；超过 `MAX_ORIGINS` 直接拒绝而不是
    静默截断——截断会让用户以为全都存进去了。
    """
    cur = settings()
    merged = dict(cur)
    if 'enabled' in patch:
        # 与读路径同一套严格布尔：外部调用方把开关写成 "false" 时不能变成开启
        merged['enabled'] = _as_bool(patch['enabled'], bool(DEFAULT['enabled']))
    if 'origins' in patch:
        raw = patch['origins']
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise ValueError('origins 必须是字符串数组')
        if len(raw) > MAX_ORIGINS:
            raise ValueError(f'最多 {MAX_ORIGINS} 条来源')
        cleaned: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                raise ValueError('origins 里的每一项都必须是字符串')
            if not item.strip():
                continue
            clean = _clean_origin(item)
            if not clean:
                raise ValueError(f'来源格式不对：{item!r}（要形如 https://app.example.com）')
            if clean not in cleaned:
                cleaned.append(clean)
        merged['origins'] = cleaned
    if 'max_age' in patch:
        try:
            merged['max_age'] = int(patch['max_age'])
        except (TypeError, ValueError) as exc:
            raise ValueError('max_age 必须是整数秒') from exc
    db.set_setting(SETTING_KEY, merged)
    return settings()


def allows(origin: str, cfg: dict[str, Any] | None = None) -> bool:
    """给定请求的 `Origin`，判断是否放行。

    纯函数（`cfg` 可注入）—— 便于单测，也便于将来在别处复用同一判据。
    精确匹配**大小写敏感**：`Origin` 的 scheme/host 在规范里是大小写不敏感的，
    但浏览器发出的值与注册值来自同一处（用户粘贴），强行小写化反而可能让
    「有意区分」的来源失效；这里保持逐字比对，与 Starlette 的行为一致。
    """
    if not origin:
        return False
    conf = cfg if cfg is not None else settings()
    if not conf.get('enabled'):
        return False
    allowed = conf.get('origins') or []
    if WILDCARD in allowed:
        return True
    return origin in allowed


def allow_origin_value(origin: str, cfg: dict[str, Any] | None = None) -> str:
    """响应头里该回显的 `Access-Control-Allow-Origin` 值。

    命中 `*` 时回显字面 `*`（因为 `allow_credentials=False`，浏览器接受字面
    `*`）；否则回显请求的 Origin 本身。未放行时返回空串。
    """
    if not allows(origin, cfg):
        return ''
    conf = cfg if cfg is not None else settings()
    return WILDCARD if WILDCARD in (conf.get('origins') or []) else origin
