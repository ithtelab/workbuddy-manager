"""运行期配置：全部通过环境变量覆盖，默认值适配 1Panel 单机部署。"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent  # 仓库根目录


def _env(name: str, default: str) -> str:
    v = os.environ.get(name, '').strip()
    return v or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, '').strip() or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    """布尔开关。空值取默认；其余按 1/true/yes/on 判真（与既有 `== '1'` 写法兼容）。"""
    v = os.environ.get(name, '').strip().lower()
    if not v:
        return default
    return v in ('1', 'true', 'yes', 'on')


PORT = _env_int('WB_MANAGER_PORT', 7864)
HOST = _env('WB_MANAGER_HOST', '0.0.0.0')

# 上游 workbuddy2api（Go）
WB2API_BASE = _env('WB2API_BASE', 'http://127.0.0.1:7863').rstrip('/')
WB2API_KEY = _env('WB2API_KEY', '')
WB2API_CONTAINER = _env('WB2API_CONTAINER', 'workbuddy2api')
WB2API_MODE = _env('WB2API_MODE', 'docker').lower()  # docker | native | systemd
# systemd 模式（宿主机直跑上游二进制、由 systemd 托管）下上游的 unit 名
WB2API_SERVICE = _env('WB2API_SERVICE', 'workbuddy2api')

# 上游数据文件（与 workbuddy2api 共享）
AUTH_DIR = Path(_env('WB_AUTH_DIR', '/opt/workbuddy2api/auths'))
UPSTREAM_CONFIG = Path(_env('WB_UPSTREAM_CONFIG', '/opt/workbuddy2api/config.json'))
UPSTREAM_DIR = Path(_env('WB_UPSTREAM_DIR', str(UPSTREAM_CONFIG.parent)))
WB2API_START_SCRIPT = Path(_env(
    'WB2API_START_SCRIPT', str(UPSTREAM_DIR / 'start-workbuddy2api.cmd'),
))
WB2API_STOP_SCRIPT = Path(_env(
    'WB2API_STOP_SCRIPT', str(UPSTREAM_DIR / 'stop-workbuddy2api.cmd'),
))
WB2API_LOG_FILE = Path(_env(
    'WB2API_LOG_FILE', str(UPSTREAM_DIR / 'data' / 'server.err.log'),
))
# 上游把日志**分流**写两个流：每请求一行的对话记录（含实际使用的账号）是
# `fmt.Fprintf(os.Stdout, ...)`（上游 internal/server/logging.go），签到 / 活跃 /
# 旅行等业务编排走 Go log → stderr。原生启动脚本（start-workbuddy2api.cmd）把
# 两路分别重定向到 server.out.log 与 server.err.log。
#
# 所以账号回填必须读**对话日志**这个文件：读 WB2API_LOG_FILE 只会拿到业务日志，
# 一条 chat 行都解析不到，账号列恒为空（Docker 模式不区分，`docker logs` 合并两路）。
WB2API_CHAT_LOG_FILE = Path(_env(
    'WB2API_CHAT_LOG_FILE', str(UPSTREAM_DIR / 'data' / 'server.out.log'),
))

# 本管理端数据
DATA_DIR = Path(_env('WB_DATA_DIR', str(ROOT / 'data')))
DB_PATH = Path(_env('WB_DB', str(DATA_DIR / 'manager.db')))
USERS_FILE = Path(_env('WB_USERS_FILE', str(DATA_DIR / 'users.json')))
STATIC_DIR = Path(_env('WB_STATIC_DIR', str(ROOT / 'web' / 'out')))

# 子路径部署时的 URL 前缀，留空 = 根路径部署（默认）。
#
# 反向代理把 `/workbuddy-manager/...` 剥掉前缀再转发给本服务，所以路由本身不需要
# 感知前缀；但**服务端主动发出的绝对地址**会漏掉它——目前只有 RSC 兜底重定向
# （`main._rsc_page_for`）会发出 `Location: /dashboard` 这种根路径，浏览器跟过去就
# 落到域名根上（通常是另一个站点，404）。该值须与前端构建时的
# `NEXT_PUBLIC_BASE_PATH` 保持一致。
_BASE_PATH_RAW = _env('WB_BASE_PATH', '').strip('/')
BASE_PATH = f'/{_BASE_PATH_RAW}' if _BASE_PATH_RAW else ''

# 网络
UPSTREAM_TIMEOUT = _env_int('WB_UPSTREAM_TIMEOUT', 120)
TENCENT_TIMEOUT = _env_int('WB_TENCENT_TIMEOUT', 15)
# 「全部签到」的并发上限：太低会拖到前端超时（几十个账号时），
# 太高又容易触发腾讯风控。5 是保守且够快的取值。
CHECKIN_CONCURRENCY = max(1, _env_int('WB_CHECKIN_CONCURRENCY', 5))
# 「一键刷新令牌」的并发上限：与签到同理，太低会拖到前端超时，太高容易被
# 腾讯风控当成异常来源。刷新走的是刷新接口（比签到更敏感），默认取 3。
REFRESH_CONCURRENCY = max(1, _env_int('WB_REFRESH_CONCURRENCY', 3))
# 「刷新积分」的并发上限（issue #161）：
# 这个接口对**全部账号**扇出（页面加载与「刷新积分」按钮都走它），而它们共用
# 同一个 httpx 连接池。以前没有上限：账号一多就把池占满，排队超过 pool 超时
# （`WB_TENCENT_TIMEOUT`）就抛 PoolTimeout；而单号测活借的是同一个池，于是
# 「测活失败」先报到用户面前（真凶是这批批量请求）。8 与页面节奏匹配：缓存
# 命中时不发请求，冷启动也够快。
CREDITS_CONCURRENCY = max(1, _env_int('WB_CREDITS_CONCURRENCY', 8))
TRUST_PROXY = _env('WB_TRUST_PROXY', '1') == '1'
# 可信反向代理跳数：用于从 X-Forwarded-For 右侧取真实客户端 IP。
# 前面直接是 1Panel/OpenResty 时保持 1；若还挂了 CDN 则设为 CDN+反代的层数。
TRUSTED_PROXY_HOPS = _env_int('WB_TRUSTED_PROXY_HOPS', 1)
# 可信代理的**来源网段**（逗号分隔，支持 CIDR）。
#
# 为什么需要它：仅凭「配置里开了 TRUST_PROXY」就无条件采信 X-Real-IP 是错的——
# 服务直接暴露（systemd 默认 0.0.0.0）时，X-Real-IP 只是普通客户端头，
# 任何人加一个 `X-Real-IP: 9.9.9.9` 就能冒充任意来源 IP，从而绕过
# 全局 IP 白/黑名单、密钥 IP 白名单与 max_ips、以及登录失败按 IP 锁定。
# 现在只在**TCP 对端确实来自这些网段**时才采信转发头，否则一律用对端地址。
#
# 默认**只信回环**（反代与面板同机，最常见也最安全）。
#
# 以前默认包含全部 RFC1918：同一局域网 / 同一 Docker 网络里的任意主机都能
# 伪造 `X-Real-IP`，绕过登录失败锁定、密钥 IP 白名单与红包限领。反代在另一台
# 机器（或 CDN 回源）时，把那台的网段**显式**加进 WB_TRUSTED_PROXY_CIDRS。
TRUSTED_PROXY_CIDRS = [
    c.strip() for c in _env(
        'WB_TRUSTED_PROXY_CIDRS',
        '127.0.0.0/8,::1/128',
    ).split(',') if c.strip()
]
# 是否暴露 /docs、/openapi.json、/redoc。生产环境建议关闭（默认关闭）。
ENABLE_DOCS = _env('WB_ENABLE_DOCS', '0') == '1'

# 静态前端产物（JS/CSS）未压缩时首屏可达 2 MB 量级，公网带宽有限（轻量服务器
# 常见 3~6 Mbps）时表现为「页面很卡」。开启后文本类资源通常压到 25%~35%，
# 实测首屏 1.98 MB -> 0.65 MB。
#
# 安全前提：Starlette 的 GZipMiddleware **内置跳过** `text/event-stream`，
# 因此 /v1 的 SSE 流式响应不会被缓冲或改写（已实测验证：24 个 data 帧完整、
# 含 [DONE]、无 content-encoding）。CPU 开销可忽略（压缩发生在静态资源首次
# 命中，且 Etag 命中时浏览器直接续用缓存）。
#
# 需要关掉时设 WB_GZIP_ENABLED=0。
GZIP_ENABLED = _env_bool('WB_GZIP_ENABLED', True)
# 小于这个字节数的响应不压（避免给极小的 JSON / 错误体反而增大体积）。
GZIP_MIN_SIZE = max(512, _env_int('WB_GZIP_MIN_SIZE', 1024))
# 入站访问日志（安全页的「IP 访问日志」）是否记录**放行**的请求。
#
# 默认关闭：那张表的用途是安全审计（谁在扫我、谁被挡了），记全量会把信号淹没。
# 实测线上 7877 行里只有 17 行是拦截记录（0.2%），而表有 2 万行滚动上限，
# 被正常流量占满后保留窗口从数月压到约 17 天——真出事时记录可能已被挤掉。
# 放行的明细在「请求日志」页有完整记录，这里不重复记不丢信息。
#
# 需要核对「某个 IP 到底来过什么」时临时设 1 恢复全量记录。
AUDIT_ALL_ACCESS = _env('WB_AUDIT_ALL_ACCESS', '0') == '1'
# 显式出口代理（可选，如 http://127.0.0.1:7890）。
# 留空时所有请求都不使用任何代理：httpx 默认 trust_env=True 会读取系统/环境代理，
# 会把内网请求（如 127.0.0.1:7863）也交给系统代理，导致连接被劫持或长时间超时。
HTTP_PROXY = _env('WB_HTTP_PROXY', '')
# 出口代理探测用的地址（界面上「测试」按钮；默认回出口 IP 与归属地）
PROXY_PROBE_URL = _env('WB_PROXY_PROBE_URL', 'https://ipinfo.io/json')
DEFAULT_ACCOUNT_PROXY = _env('WB_DEFAULT_ACCOUNT_PROXY', '')

# 本机一键导入：允许把新密钥直接写进**本机的** cc-switch / ZCode 配置。
#
# 默认关闭。理由：这是本面板唯一会写用户桌面应用数据的动作，写坏的代价是
# 客户端里原本可用的供应商一起受影响。而且它只在「面板与客户端同一台机器」
# 时才有意义，所以端点还会再校验**来源必须是回环地址**
# （见 routers/keys.py 的 `_require_local_caller`），仅靠开关不足以开启。
#
# 路径可用 WB_CCSWITCH_DIR / WB_ZCODE_DIR 覆盖（非默认安装位置），
# 见 services/keyimport.py。
LOCAL_IMPORT_ENABLED = _env_bool('WB_LOCAL_IMPORT', False)

# ── 调用抓包（Trace Tap，见 services/tap.py）──────────────────────────────
#
# **默认关闭**：打开后会把每一次调用的请求体（system prompt、全部历史消息、
# 工具定义）与模型回复写进本地库。这是排障时最想要的东西，也是最该由用户
# 自己决定开不开的东西 —— 面板不替用户做这个决定。面板里可随时开关（状态
# 存在 settings 表），这里的值只作为**默认值**。
TAP_ENABLED = _env_bool('WB_TAP_ENABLED', False)
# 单条请求/响应的存储上限（字节）。超出部分截断并标记，界面按原文展示。
# 下限 4 KiB：再小连一个 system prompt 都存不下，抓了也没有意义。
TAP_MAX_BODY = max(4096, _env_int('WB_TAP_MAX_BODY', 512 * 1024))
# 保留天数：抓包是排障证据，过期就该消失，否则库会无声地涨到几十 GB。
TAP_RETENTION_DAYS = max(1, _env_int('WB_TAP_RETENTION_DAYS', 7))
# 会话数上限：按天清理之外再加一道「条数」闸门 —— 一个高频客户端能在一天
# 里刷出上千个会话，只按时间清理挡不住它。
TAP_MAX_SESSIONS = max(10, _env_int('WB_TAP_MAX_SESSIONS', 300))
# 会话**总时长**上限（天）：登录后最多维持这么久，到点必须重新登录。
#
# 下限钳到 1 天：0（或负数）会让 cookie 的 exp 等于签发时刻，即「登录成功但
# 立刻过期」——用户被锁在门外，而报错只会说「未登录」，看不出是配置写错了。
# 想「几乎不过期」就把值调大（如 30），而不是写 0。
SESSION_DAYS = max(1, _env_int('WB_SESSION_DAYS', 1))
# 会话**空闲**上限（小时）：距上次活动超过它就失效（滑动续期的窗口）。
# **0 = 关闭空闲判定**，只按总时长失效（`security.idle_expired` 显式处理）。
#
# 为什么要有这个、而不只是把总时长调短：只减总时长会惩罚**天天用**的人
# （每天都要重登一次），却对「登录一次就再也不碰」的会话没有额外约束。
# 滑动续期把两件事分开——常用的人不断续、不被打扰；放着不用的会话自己过期。
SESSION_IDLE_HOURS = _env_int('WB_SESSION_IDLE_HOURS', 12)
COOKIE_NAME = 'wb_session'
SECURE_COOKIE = _env('WB_SECURE_COOKIE', 'auto')  # auto | true | false

# 允许的 CORS 来源（同源部署时留空即可）
CORS_ORIGINS = [o for o in _env('WB_CORS_ORIGINS', '').split(',') if o]

TENCENT_BASE = 'https://copilot.tencent.com'
TENCENT_BILLING_BASE = 'https://www.codebuddy.cn'  # 国内版 billing 基址
TENCENT_CHECKIN = 'https://www.codebuddy.cn/v2/billing/meter/daily-checkin'
# 积分余额查询（与上游 BillingBaseCN 一致）
TENCENT_BILLING = 'https://www.codebuddy.cn/v2/billing/meter/get-user-resource'
# 国内版通用请求头。国际版用 realm.headers('global') 取（Origin/UA 都不同）——
# 这里的常量保留为 CN 默认值，供既有调用点与不区分版本处使用。
#
# 当前所有腾讯出站都走 realm.headers()（含风控头 X-CodeBuddy-Request /
# Accept-Language，见 realm.py 注释），本常量暂无调用点。保留但它也必须与
# 那边口径一致：否则将来有人照着这里取值，就会发出「形态不像官方客户端」
# 的请求（Accept 的宽松值与 D6 收紧后的口径相矛盾）。
TENCENT_HEADERS = {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
    'Accept-Language': 'zh-CN',
    'X-Requested-With': 'XMLHttpRequest',
    'X-CodeBuddy-Request': '1',
    'User-Agent': 'CLI/2.63.2 CodeBuddy/2.63.2',
    'Origin': 'https://www.codebuddy.cn',
    'Referer': 'https://www.codebuddy.cn/',
}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)


def upstream_api_key() -> str:
    """优先环境变量，其次读取 workbuddy2api 的 config.json。"""
    if WB2API_KEY:
        return WB2API_KEY
    try:
        cfg = json.loads(UPSTREAM_CONFIG.read_text(encoding='utf-8'))
        return str(cfg.get('api_key') or '')
    except Exception:
        return ''


DEFAULT_POOL = {
    'enabled': False,
    'strategy': 'priority',
    'interval_seconds': 300,
}
STRATEGIES = ('priority', 'round_robin', 'random', 'least_connections')

# least_connections 用「最近调用次数」近似连接数：面板发出的都是短请求（登录/签到/
# 积分/模型），排队长度基本等于最近这段时间的调用量 —— 与其报一个假的并发数，
# 不如用这个能自圆其说的近似，并在界面上写明口径。
_RECENT_CALLS: dict[str, list[float]] = {}
_RECENT_WINDOW = 60.0
_ROUND_ROBIN = {'cursor': 0}


def _raw_routes() -> dict[str, dict]:
    """线路表的**原始**形态：名字 → {url, weight, enabled}。

    值允许三种写法（老配置不用动）：
      `"名字": "地址"`                      → 权重 100、启用
      `"名字": {"url": "地址", "weight": 30}` → 权重 30、启用
      `"名字": {"url": "地址", "enabled": false}` → 停用（池里跳过，绑定它的账号报错）
    """
    try:
        table = json.loads(UPSTREAM_CONFIG.read_text(encoding='utf-8')).get('proxies') or {}
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(table, dict):
        return {}
    out: dict[str, dict] = {}
    for name, value in table.items():
        if not isinstance(name, str) or not name.strip():
            continue
        if isinstance(value, str):
            if value.strip():
                out[name] = {'url': value, 'weight': 100, 'enabled': True}
        elif isinstance(value, dict):
            url = value.get('url')
            if not isinstance(url, str) or not url.strip():
                continue
            weight = value.get('weight', 100)
            if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight <= 0:
                weight = 100
            out[name] = {'url': url, 'weight': int(weight),
                         'enabled': value.get('enabled', True) is not False}
    return out


def proxy_routes() -> dict[str, str]:
    """读取线路表（名字 → 地址）。客户端只能取得线路名，不能取得凭据。"""
    return {name: entry['url'] for name, entry in _raw_routes().items()}


def proxy_route_weights() -> dict[str, int]:
    """线路名 → 权重（按权重分摊/挑选时用；老写法默认 100）。"""
    return {name: entry['weight'] for name, entry in _raw_routes().items()}


def proxy_route_enabled() -> dict[str, bool]:
    """线路名 → 是否启用（池里只挑启用的；停用的线路仍可被账号显式绑定）。"""
    return {name: entry['enabled'] for name, entry in _raw_routes().items()}


def proxy_pool_config() -> dict:
    """代理池配置：开关 / 策略 / 间隔（存面板设置，缺项补默认值）。"""
    try:
        from . import db
        stored = db.get_setting('proxy_pool', {}) or {}
    except Exception:            # noqa: BLE001 —— 读不到就用默认（池默认关闭）
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    strategy = str(stored.get('strategy') or DEFAULT_POOL['strategy'])
    if strategy not in STRATEGIES:
        strategy = DEFAULT_POOL['strategy']
    try:
        interval = int(stored.get('interval_seconds', DEFAULT_POOL['interval_seconds']))
    except (TypeError, ValueError):
        interval = DEFAULT_POOL['interval_seconds']
    return {
        'enabled': bool(stored.get('enabled', DEFAULT_POOL['enabled'])),
        'strategy': strategy,
        'interval_seconds': max(0, interval),
    }


def pool_routes() -> dict[str, str]:
    """池里**可供自动挑选**的线路（启用中）。"""
    return {name: entry['url'] for name, entry in _raw_routes().items() if entry['enabled']}


def _validate_route(name: str, url: str) -> None:
    try:
        parsed = urlsplit(url)
        # socks5 / socks5h 由 httpx + socksio 支持（socks5h 把域名解析交给代理）。
        # socks4/socks4a 实测 httpx 直接报 Unknown scheme，不放进来。
        valid = (parsed.scheme in ('http', 'https', 'socks5', 'socks5h')
                 and bool(parsed.hostname) and parsed.port != 0)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f'代理线路 {name!r} 配置无效（需要 HTTP/HTTPS/SOCKS5 代理）')


def note_route_use(name: str) -> None:
    """记一次调用（least_connections 策略用它做近似）。"""
    if not name:
        return
    now = time.time()
    bucket = _RECENT_CALLS.setdefault(name, [])
    bucket.append(now)
    cutoff = now - _RECENT_WINDOW
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(_RECENT_CALLS) > 500:
        for key in [k for k, v in _RECENT_CALLS.items() if not v][:100]:
            _RECENT_CALLS.pop(key, None)


def choose_pool_route(names: list[str]) -> str:
    """按池策略从候选里挑一条（names 都已确认可用且启用）。"""
    pool = proxy_pool_config()
    strategy = pool['strategy']
    weights = proxy_route_weights()
    ordered = sorted(names, key=lambda n: (-weights.get(n, 100), n))
    if strategy == 'priority':
        return ordered[0]
    if strategy == 'round_robin':
        cursor = _ROUND_ROBIN['cursor'] = (_ROUND_ROBIN['cursor'] + 1) % len(ordered)
        return ordered[cursor]
    if strategy == 'least_connections':
        return min(ordered, key=lambda n: (len(_RECENT_CALLS.get(n, [])), ordered.index(n)))
    # random：按权重加权随机（权重越大越常被选中）
    total = sum(max(1, int(weights.get(n, 100))) for n in ordered)
    point = secrets.randbelow(total)
    for name in ordered:
        point -= max(1, int(weights.get(name, 100)))
        if point < 0:
            return name
    return ordered[-1]


def _sticky_pick(auth: dict, candidates: list[str]) -> str:
    """池内选择：间隔窗口内沿用上次那条，窗口过了再按策略重挑并记下。

    为什么要有窗口：一会儿换一个出口在风控眼里就是特征（mihomo 的 sticky-sessions
    是同一个道理）。窗口长度就是池配置里的「间隔」。
    """
    interval = proxy_pool_config()['interval_seconds']
    if len(candidates) == 1 or interval <= 0:
        return choose_pool_route(candidates)
    uid = str(auth.get('uid') or '').strip() or '?'
    try:
        from . import db
        table = db.get_setting('proxy_sticky', {}) or {}
    except Exception:            # noqa: BLE001 —— 读不到就当作没有粘性
        table = {}
    entry = table.get(uid) if isinstance(table, dict) else None
    if isinstance(entry, dict):
        last = str(entry.get('route') or '')
        at = int(entry.get('at') or 0)
        if last in candidates and time.time() - at < interval:
            return last
    chosen = choose_pool_route(candidates)
    try:
        from . import db
        table = db.get_setting('proxy_sticky', {}) or {}
        if not isinstance(table, dict):
            table = {}
        table[uid] = {'route': chosen, 'at': int(time.time())}
        if len(table) > 2000:      # 上限：粘性过期后自然被覆盖，不会无限膨胀
            keep = sorted(table.items(), key=lambda kv: int(kv[1].get('at') or 0),
                          reverse=True)[:2000]
            table = dict(keep)
        db.set_setting('proxy_sticky', table)
    except Exception:            # noqa: BLE001 —— 写不进去只是少了粘性
        pass
    return chosen


# ── 兼容 1.0.84 的 `{account}` 会话模板 ─────────────────────────────────
# 那一版让用户把 `{account}` 写在线路地址里，每个账号渲染出自己的会话名（住宅代理
# 按会话名给粘性 IP）。现在的方案是「池策略 + 账号绑定」，界面上不再有会话这个概念；
# 但**已经那么写过的地址不能突然发不出去**，所以命中占位符时仍按账号渲染。会话名
# 沿用账号文件里存下来的 `proxy_session`（那正是它当初被存下来的原因：改昵称不该
# 悄悄换掉出口 IP），没有就按昵称 / uid 现算。
SESSION_PLACEHOLDER = '{account}'


def route_session_name(auth: dict) -> str:
    """这个账号在会话模板里用的会话名。"""
    stored = str(auth.get('proxy_session') or '').strip()
    if stored:
        return stored
    nickname = str(auth.get('nickname') or '').strip()
    # 只留 ASCII 字母数字：住宅代理的用户名里带中文会被百分号编码，多数服务商不认
    slug = ''.join(ch.lower() if (ch.isascii() and ch.isalnum()) else '-'
                   for ch in nickname)
    while '--' in slug:
        slug = slug.replace('--', '-')
    slug = slug.strip('-')[:24]
    if len(slug) >= 3:
        return slug
    return str(auth.get('uid') or '').strip()[:8] or 'acct'


def render_route_url(template: str, session: str) -> str:
    """把 `{account}` 换成会话名；没有占位符就原样返回（= 这条线路人人都一样）。"""
    return template.replace(SESSION_PLACEHOLDER, session)


def _render_for(auth: dict, url: str) -> str:
    return render_route_url(url, route_session_name(auth)) \
        if SESSION_PLACEHOLDER in url else url


# 账号级「强制直连」的保留线路名。与上游 `upstream.DirectRouteName` 是**同一个值**
# （它写在账号文件顶层 proxy 字段里，两边都读；改一处必须同时改另一处）。
# 上游对它的语义是「绕过进程级环境代理」，面板这边对应「绕过代理池与全局上游代理」。
DIRECT_ROUTE = '__direct__'


def resolve_account_proxy(auth: dict) -> str | None:
    """这个账号这次该走哪条线路。顺序（与参考项目的语义一致）：

      1. **账号绑定的线路**（「指定代理」）—— 存在就必须可用：线路被删或地址非法时
         抛错，而不是悄悄换出口（出口被换掉在风控里是特征变化）；
         绑定的线路被**停用**同样报错，让用户明确知道「这条线暂停了」。
      2. **代理池**（开启且有启用中的线路）→ 按策略挑一条（间隔内粘住）。
      3. **全局上游代理**（开启且配了地址）。
      4. 直连。
    """
    bound = str(auth.get('proxy') or '').strip()
    if bound:
        if bound == DIRECT_ROUTE:
            # 强制直连：返回空串（而不是 None）—— None 会让调用方回落到
            # `effective_http_proxy()`，也就是仍走 WB_HTTP_PROXY；空串在
            # `_build_client` 里是「不建 proxy transport + trust_env=False」，
            # 那才是真正的直连。
            return ''
        # 走公开访问器（而不是自己重读文件）：既有调用点/测试按这两个函数打桩，
        # 绕过它们会出现「线路表长什么样」的第二个真相来源。
        on = proxy_route_enabled()
        table = {name: {'url': url, 'enabled': on.get(name, True)}
                 for name, url in proxy_routes().items()}
        entry = table.get(bound)
        if entry is None:
            raise ValueError(f'账号绑定的代理线路 {bound!r} 不存在')
        if not entry['enabled']:
            raise ValueError(f'账号绑定的代理线路 {bound!r} 已停用')
        _validate_route(bound, entry['url'])
        note_route_use(bound)
        return _render_for(auth, entry['url'])

    pool = proxy_pool_config()
    if pool['enabled']:
        available = pool_routes()
        if available:
            chosen = _sticky_pick(auth, list(available))
            _validate_route(chosen, available[chosen])
            note_route_use(chosen)
            return _render_for(auth, available[chosen])

    global_proxy = global_http_proxy()
    if global_proxy:
        _validate_route('全局上游代理', global_proxy)
        return global_proxy
    return None


def global_http_proxy() -> str:
    """全局上游代理：面板设置里配了且开启时生效，否则回落环境变量 WB_HTTP_PROXY。

    位置与参考项目的「Global Proxy」一致：池不可用（关了 / 没有启用中的线路）时的
    兜底出口。
    """
    try:
        from . import db
        if db.get_setting('global_proxy_enabled', True) is False:
            return ''
        stored = str(db.get_setting('http_proxy', '') or '').strip()
    except Exception:            # noqa: BLE001
        stored = ''
    return stored or HTTP_PROXY


def account_proxy(auth: dict) -> str | None:
    """无绑定时沿用既有出口；绑定无效时拒绝发出请求（对外保持原签名）。"""
    return resolve_account_proxy(auth)


def new_secret() -> str:
    return secrets.token_urlsafe(48)


# ── 出站客户端池（issue #144）────────────────────────────────
#
# 为什么不再每次新建：Windows 上构造 httpx.AsyncClient 会去加载系统证书库建 SSL
# 上下文，实测 **~1 秒**（Linux 上没这么夸张，所以这个问题只在原生 Windows 部署
# 上暴露）。面板的每个页面切换都要问上游几次（账号/状态/模型），于是「每次出站
# 新建客户端」直接表现成「切页面慢 1~3 秒」。共享之后同一套配置只建一次。
#
# 键里带**事件循环**：httpx 的客户端（连接池）绑在创建它的那个循环上，跨循环
# 复用会炸（同步路由里 `asyncio.run` 另起循环那条路就是例子，见
# `modelcatalog.fetch_ids_blocking`）。不同循环各建一个，互不干扰。
_CLIENTS: dict[tuple, tuple] = {}
_CLIENTS_MAX = 24


class _BorrowedClient:
    """借出去用的客户端：退出 `async with` 时**不关闭**（归还给池）。

    调用点大多是 `async with config.http_client(...) as client:` 的写法，所以这里
    只把「上下文管理」这层语义接过来，避免改十几个调用点。关闭由
    `close_clients()`（应用退出）或池满淘汰时统一做。

    另外**代理属性访问**（`__getattr__`）并让 `aclose()` 变成「归还」而不是真关：
    有几处调用点是不用 `async with`、自己 `client = http_client(...)` 然后
    `finally: await client.aclose()` 的写法（网关、Anthropic 兼容、测试台、Responses
    各一处）。共享之后，那些 `aclose()` 一旦真关，就会把别人正在用的连接池一起关掉
    ——并发下表现为「偶发请求失败」。所以借来的客户端只支持「还」，真关只发生在池
    淘汰与退出清理里。
    """

    __slots__ = ('_client',)

    def __init__(self, client) -> None:  # noqa: ANN001
        self._client = client

    def __getattr__(self, name: str):  # noqa: ANN201
        # 借用包装要能当客户端用（post/stream/headers/... 一律转发）
        return getattr(self._client, name)

    async def __aenter__(self):  # noqa: ANN201
        return self._client

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def aclose(self) -> None:
        """借用语义：只归还，不真关（共享客户端由池统一管理）。"""
        return None


def _timeout_key(tmo) -> tuple:  # noqa: ANN001
    return (tmo.connect, tmo.read, tmo.write, tmo.pool)


_INTERNAL_SUFFIXES = ('.local', '.internal', '.lan', '.docker', '.localhost')


def _is_internal_host(host: str | None) -> bool:
    """这个目标算不算「内网 / 本机」——算的话不该交给外部代理。

    覆盖三类：回环与常见内网别名、私有 / 链路本地 / 保留网段的 IP、**单标签主机名**
    （容器服务名 `workbuddy2api` 这类；公网域名一定带点）。
    """
    h = (host or '').strip().lower().rstrip('.')
    if not h:
        return False
    if h in ('localhost', 'host.docker.internal', 'host.containers.internal'):
        return True
    if h.endswith(_INTERNAL_SUFFIXES) or '.' not in h:
        # 单标签 = 内网 DNS（容器服务名、`myhost` 之类）。公网目标必然带点。
        return True
    try:
        addr = ipaddress.ip_address(h)
    except ValueError:
        return False
    return bool(addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_unspecified)


def _proxy_transport(proxy: str):  # noqa: ANN202
    """「内网/本机直连、其余走代理」的 httpx transport。

    用自定义 transport 而不是 `mounts=`：mounts 只能按主机名模式匹配，表达不了
    「私有网段」「单标签主机名」这两类，而它们恰恰是容器部署的常态（`workbuddy2api`、
    `host.docker.internal`、`10.x`…）。

    类建在这里而不是模块顶层，是因为 httpx 在本模块是**按需导入**的（保持冷启动
    不加载它）；每个 (循环, 超时, 代理) 组合只建一次客户端，这点构造开销可忽略。
    """
    import httpx

    class _DirectForInternalTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self._proxied = httpx.AsyncHTTPTransport(proxy=proxy)
            self._direct = httpx.AsyncHTTPTransport()

        async def handle_async_request(self, request):  # noqa: ANN001, ANN201
            transport = (self._direct if _is_internal_host(request.url.host)
                         else self._proxied)
            return await transport.handle_async_request(request)

        async def aclose(self) -> None:
            await self._proxied.aclose()
            await self._direct.aclose()

    return _DirectForInternalTransport()


def _build_client(tmo, proxy: str | None):  # noqa: ANN001, ANN202
    import httpx

    kwargs: dict = {'timeout': tmo, 'trust_env': False}
    if proxy:
        kwargs['transport'] = _proxy_transport(proxy)
    return httpx.AsyncClient(**kwargs)


async def close_clients() -> None:
    """关掉池里所有客户端（应用退出时调用；幂等）。"""
    clients = [entry[1] for entry in _CLIENTS.values()]
    _CLIENTS.clear()
    for client in clients:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 —— 退出路径上的清理失败不该再抛
            continue


def _evict(loop) -> None:  # noqa: ANN001
    """池满了淘汰最早的一个（按插入顺序）。"""
    while len(_CLIENTS) > _CLIENTS_MAX:
        key = next(iter(_CLIENTS))
        entry = _CLIENTS.pop(key, None)
        if not entry or entry[1].is_closed:
            continue
        old_loop, client = entry
        try:
            if old_loop is loop:
                loop.create_task(client.aclose())      # 同一循环才能安全关闭
        except Exception:  # noqa: BLE001
            continue


def stored_http_proxy() -> str:
    """面板设置里存的出口代理；空串 = 没设过（回落到环境变量）。"""
    try:
        from . import db          # 延迟导入：config 被 db 反过来 import，顶层会成环
        value = db.get_setting('http_proxy', '')
    except Exception:             # noqa: BLE001 —— 读不到就当没设过，别让出站挂掉
        return ''
    return str(value or '').strip()


def effective_http_proxy() -> str:
    """当前生效的**全局**出口代理（面板设置优先、环境变量兜底，开关可关）。

    注意它只影响「没有走账号线路/代理池」的那部分请求；账号出口的优先级见
    `resolve_account_proxy`（绑定线路 → 代理池 → 这里 → 直连）。
    """
    return global_http_proxy()


def http_client(timeout, *, connect: float | None = None, proxy: str | None = None,
                lane: str = ''):
    """统一的 httpx 客户端：默认忽略系统/环境代理，避免内网请求被代理劫持。

    需要走代理时显式设置 WB_HTTP_PROXY。

    timeout 可以传数字，也可以传已构造好的 httpx.Timeout。
    注意：httpx 不允许「Timeout 实例 + connect 关键字」同时传，
    因此传入实例时忽略 connect，避免 AssertionError。

    **返回的是借用语义的上下文管理器**（退出时不关客户端，见 `_BorrowedClient`）：
    同一事件循环 + 同一组超时/代理只建一次，连接池复用（issue #144：Windows 上
    每次新建要 ~1 秒，正好是页面切换的耗时）。

    `lane` 把客户端**按用途分池**（issue #161）：池是 httpx 的私有属性，缓存键
    一样就共用一个池，于是「批量刷积分的几十个请求」和「用户手点的单号测活」
    会互相排队——批量把池占满时，交互操作得等到 pool 超时才报 PoolTimeout，
    看起来像是「测活坏了」。给交互操作单独的 lane，两边各有各的连接预算。
    留空 = 默认车道（既有调用点全部不受影响）。
    """
    import httpx

    if isinstance(timeout, httpx.Timeout):
        tmo = timeout
    elif connect is not None:
        tmo = httpx.Timeout(timeout, connect=connect)
    else:
        tmo = httpx.Timeout(timeout)
    selected_proxy = effective_http_proxy() if proxy is None else proxy

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 事件循环外（同步夹具/脚本场景）：不缓存，按老样子建一个即用即弃的，
        # 免得把一个绑在未知循环上的客户端留在池里。
        return _build_client(tmo, selected_proxy)

    key = (*_timeout_key(tmo), selected_proxy or '', lane)
    entry = _CLIENTS.get(key)
    if entry is not None and entry[0] is loop and not entry[1].is_closed:
        return _BorrowedClient(entry[1])
    client = _build_client(tmo, selected_proxy)
    _CLIENTS[key] = (loop, client)
    _evict(loop)
    return _BorrowedClient(client)
