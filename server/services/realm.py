"""版本（realm）：国内版 CN 与国际版 Global 的判定与端点分派。

为什么要单独一层：上游从 2026-09-14 起支持国际版，**单实例同时服务两套上游**
（共用账号池），按账号的 `realm` 字段或模型名前缀路由。本管理端此前把
腾讯域名与请求头写死在 config.py 里，只能对国内版；要支持切换就必须把
「这次请求该走哪套端点」变成可推导的量。

口径**严格镜像上游**（读其 Go 源码确认，非猜测）：

  - `auth.realm` 嵌在 auth 对象内（与 domain 同级），值 "cn" / "global"
  - 判定顺序：`global.enabled=false` → 恒 cn（逃生门）；
    否则 realm == "global" 或 domain 是 workbuddy.ai / *.workbuddy.ai → global
  - 存量账号没有 realm 字段，按 domain 回退；domain 也为空则判 cn，
    因此**升级后既有部署的行为不变**

端点差异（上游 client.go / headers.go）：

  | 维度        | CN                        | Global                    |
  |-------------|---------------------------|---------------------------|
  | chat base   | copilot.tencent.com       | www.workbuddy.ai          |
  | billing     | www.codebuddy.cn          | www.workbuddy.ai          |
  | Origin/Ref  | www.codebuddy.cn          | www.workbuddy.ai          |
  | UA 品牌段   | WorkBuddy                 | WorkBuddy AI              |
  | chat 路径   | /v2/chat/completions      | /console/... 404 回落 /v2 |
  | billing 路径| /v2/billing/meter/*       | /billing/meter/* 404 回落 |

注意 billing 的回落方向两边相反（CN 只有 /v2、Global 以无前缀优先），
这不是笔误，照上游实现来。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Literal

from .. import config

Realm = Literal['cn', 'global']

CN: Realm = 'cn'
GLOBAL: Realm = 'global'

# 国际版默认基址（上游 config 的 global.chat_base / billing_base 留空时用它）
DEFAULT_GLOBAL_BASE = 'https://www.workbuddy.ai'
# 国际版登录与普通接口的 Origin/Referer
GLOBAL_ORIGIN = 'https://www.workbuddy.ai'
CN_ORIGIN = 'https://www.codebuddy.cn'

# 读上游 config.json 的缓存：改动要在 10 秒内生效，又不必每请求读盘
_CFG_TTL = 10
_cfg_cache: dict = {'at': 0.0, 'data': None}
# upstream 段的独立缓存（device_token 等；与 global 段互不影响）
_up_cfg_cache: dict = {'at': 0.0, 'data': None}


def _read_global_config() -> dict:
    """读上游 config.json 的 `global` 段。读不到按默认（enabled=True、base 留空）。"""
    now = time.time()
    cached = _cfg_cache.get('data')
    if cached is not None and now - float(_cfg_cache['at']) < _CFG_TTL:
        return cached
    data = {'enabled': True, 'chat_base': '', 'billing_base': ''}
    try:
        raw = json.loads(config.UPSTREAM_CONFIG.read_text(encoding='utf-8'))
        g = raw.get('global') if isinstance(raw, dict) else None
        if isinstance(g, dict):
            if isinstance(g.get('enabled'), bool):
                data['enabled'] = g['enabled']
            for k in ('chat_base', 'billing_base'):
                v = g.get(k)
                if isinstance(v, str) and v.strip():
                    data[k] = v.strip().rstrip('/')
    except Exception:  # noqa: BLE001
        # 读不到就用默认：管理端不能因为读不到配置而无法工作
        pass
    _cfg_cache.update({'at': now, 'data': data})
    return data


def invalidate() -> None:
    """清空配置缓存（保存设置后调用，让改动立即生效）。"""
    _cfg_cache.update({'at': 0.0, 'data': None})
    _up_cfg_cache.update({'at': 0.0, 'data': None})
    _dt_cache.update({'path': '', 'at': 0.0, 'token': ''})


def global_enabled() -> bool:
    """国际版路由是否开启（镜像上游 global.enabled，缺省 true）。"""
    return bool(_read_global_config()['enabled'])


def has_global_domain(domain: str) -> bool:
    """域名是否属于国际版：workbuddy.ai 本身或其任意子域。

    镜像上游 isGlobalDomain：大小写不敏感、去空白，按后缀匹配。
    """
    d = (domain or '').strip().lower()
    return d == 'workbuddy.ai' or d.endswith('.workbuddy.ai')


def resolve_realm(explicit: str | None, domain: str | None) -> Realm:
    """纯推导，**不受逃生门影响**（对应上游 ResolveRealm）。

    显式值优先；其次按域名；都不满足则 cn。
    账号落盘时用这个，而不是 realm_of：否则一旦开了逃生门，
    会把国际版账号永久写成 cn（上游注释里专门警告过这点）。
    """
    e = (explicit or '').strip().lower()
    if e in ('cn', 'global'):
        return e  # type: ignore[return-value]
    return GLOBAL if has_global_domain(domain or '') else CN


def realm_of(auth: dict | None) -> Realm:
    """按账号判定其 realm，**含逃生门**（对应上游 Auth.Realm）。

    auth 可以是 `{'realm':..,'domain':..}` 或含这两键的更大字典
    （如 _auth_dict 的产物）。
    """
    if not global_enabled():
        return CN
    if not isinstance(auth, dict):
        return CN
    return resolve_realm(auth.get('realm'), auth.get('domain'))


def client_version() -> str:
    """出站 UA 的客户端版本段（上游 config upstream.client_version，空则内置默认）。"""
    try:
        raw = json.loads(config.UPSTREAM_CONFIG.read_text(encoding='utf-8'))
        up = raw.get('upstream') if isinstance(raw, dict) else None
        v = (up or {}).get('client_version') if isinstance(up, dict) else None
        if isinstance(v, str) and v.strip():
            return v.strip()
    except Exception:  # noqa: BLE001
        pass
    return '5.5.4'


def cli_version() -> str:
    """出站 UA 的 CLI 版本段（上游 config upstream.cli_version，空则内置默认）。"""
    try:
        raw = json.loads(config.UPSTREAM_CONFIG.read_text(encoding='utf-8'))
        up = raw.get('upstream') if isinstance(raw, dict) else None
        v = (up or {}).get('cli_version') if isinstance(up, dict) else None
        if isinstance(v, str) and v.strip():
            return v.strip()
    except Exception:  # noqa: BLE001
        pass
    return '2.137.1'


def _ua(realm: Realm) -> str:
    """chat 域出站 UA。镜像上游 defaultWorkBuddyUAFor：
    `WorkBuddy/<ver> <platform>/<ver> CLI/<cli>`，国际版的平台段是 `WorkBuddy AI`。
    """
    ver = client_version()
    platform = 'WorkBuddy AI' if realm == GLOBAL else 'WorkBuddy'
    return f'WorkBuddy/{ver} {platform}/{ver} CLI/{cli_version()}'


def billing_ua() -> str:
    """billing 域出站 UA：**单段** `WorkBuddy/<ver>`（不带 CLI 段）。

    镜像上游 billingUA：官方客户端在 banner/签到这类白名单接口显式覆写 UA
    为 `WorkBuddy/<pkgVer>`，RestOperations 层的 CLI 扩展段被业务层固化覆盖。
    上游 2026-09-14 起把这条从「仅当配置 client_name 才用」改为**默认生效**
    （原默认是不设 UA，用 Go 客户端自带的默认值——那才是最不像官方客户端的形态）。

    显式配置 client_name="SaaS" 时返回空串 = 不覆写 UA（还原旧行为）。
    """
    if attribution_client_name() == 'SaaS':
        return ''
    return f'WorkBuddy/{client_version()}'


def attribution_client_name() -> str:
    """用量归属名，镜像上游 attributionClientName。

    配置（upstream.client_name）非空取该值；**空则默认 "WorkBuddy"**——
    上游 2026-09-14 起把默认从「SaaS（不设 X-IDE-*）」翻转成「WorkBuddy 桌面端
    指纹」，理由是空的 client/agentPurpose 在官网用量归因里是显眼的「网关特征」。
    显式配 "SaaS" 可还原旧行为。
    """
    up = _read_upstream_sec()
    name = str(up.get('client_name') or '').strip()
    return name or 'WorkBuddy'


def attribution_headers() -> dict:
    """chat 路径的用量归属头（镜像上游 injectAttribution）。

    默认（含未配置）伪造官方 WorkBuddy 桌面端头组，与官方 banner 白名单
    （application-manifest.js）同形；显式 client_name="SaaS" 还原旧行为。
    """
    name = attribution_client_name()
    if name == 'SaaS':
        return {'X-Product': 'SaaS'}
    return {
        'X-Agent-Purpose': 'conversation',
        'X-IDE-Name': name,
        'X-IDE-Type': name,
        'X-IDE-Version': client_version(),
        'X-Product': name,
    }


def device_fingerprint_headers(uid: str | None) -> dict:
    """按账号稳定派生的设备指纹头（X-Machine-ID / X-Session-ID）。

    镜像上游 `deriveAccountStableID`（2026-09-15 新增）：

        sha256("wb2a:" + purpose + ":" + uid) 前 18 字节的 hex = 36 字符

    语义是「每个账号一台固定虚拟设备」——跨重启恒定、账号间互异、同 uid
    同用途恒同值。上游对齐的是官方桌面端行为，目的是「防多号被按设备指纹
    缺失/漂移关联风控」。

    **为什么我们也要发**：管理端有一批请求绕过上游直连腾讯（扫码登录、
    签到、查积分、地区注册、trial、探测）。上游这次给它的所有出站路径加了
    这两个头，我们这条路若不加，就成了唯一「没有设备标识」的流量。

    uid 为空时不返回任何头（匿名请求无设备可言，上游也是这个语义）。

    实现细节刻意与上游逐字一致（固定盐 "wb2a:"、截 18 字节）：两边派生出的
    值必须相同，否则同一个账号在「经上游」与「直连」两条路上会是两台设备，
    反而制造出可被关联的异常。
    """
    u = str(uid or '').strip()
    if not u:
        return {}

    def derive(purpose: str) -> str:
        return hashlib.sha256(f'wb2a:{purpose}:{u}'.encode()).hexdigest()[:36]

    return {
        'X-Machine-ID': derive('machine'),
        'X-Session-ID': derive('session'),
    }


def origin_of(realm: Realm) -> str:
    return GLOBAL_ORIGIN if realm == GLOBAL else CN_ORIGIN


def chat_base(realm: Realm) -> str:
    """该 realm 的 chat/登录/模型接口基址。"""
    if realm == GLOBAL:
        base = _read_global_config().get('chat_base') or ''
        return base or DEFAULT_GLOBAL_BASE
    return config.TENCENT_BASE


def billing_base(realm: Realm) -> str:
    """该 realm 的 billing（签到 / 积分 / trial）基址。"""
    if realm == GLOBAL:
        base = _read_global_config().get('billing_base') or ''
        return base or DEFAULT_GLOBAL_BASE
    return config.TENCENT_BILLING_BASE


def chat_paths(realm: Realm) -> list[str]:
    """聊天补全的候选路径，按尝试顺序：**/v2 优先，/console 兜底**。

    国际版原先是 `/console` 优先、404/405 回落 `/v2`；上游 2026-09-18（#119）改为
    **固定 `/v2`**：`/console` 挂腾讯云 WAF 的请求体内容规则——正文里出现反引号
    `printf` / `whoami` 这类命令执行特征会被确定性拦成 403（用户问一句 shell 命令
    就中招）。`/v2` 是同一 base 下不挂该规则的等价端点。

    2026-10-17 又把 `/console` 加回来做**兜底**：上游给 /v2 加了渠道强校验，被校验
    到的请求回「没通过渠道校验」；而官方客户端走的 /console 正常放行（用户实测：
    换过去连通性测试与对话都恢复）。**只在 /v2 被判渠道校验时才换 /console**
    （见 `probe_account` 与上游 `IsChannelCheck`），所以上面那条 WAF 规则只在兜底
    路径上才可能碰到，主路径行为零改动。
    """
    return ['/v2/chat/completions', '/console/chat/completions']


def billing_paths(realm: Realm, kind: str) -> list[str]:
    """billing 相关路径候选。kind: 'user-resource' | 'daily-checkin'。

    **注意两边顺序相反**（照上游 billingMeterPaths / checkingMeterPaths）：
    国际版无 `/v2` 前缀是首选，国内版只有 `/v2` 形式。
    """
    suffix = {
        'user-resource': 'get-user-resource',
        'daily-checkin': 'daily-checkin',
    }[kind]
    if realm == GLOBAL:
        return [f'/billing/meter/{suffix}', f'/v2/billing/meter/{suffix}']
    return [f'/v2/billing/meter/{suffix}']


def headers(realm: Realm, token: str | None = None,
            uid: str | None = None) -> dict:
    """该 realm 的通用请求头（Origin/Referer/UA 随 realm 变）。

    token 非空时附 Authorization；uid 非空时附账号级设备指纹头
    （X-Machine-ID / X-Session-ID，见 device_fingerprint_headers）。
    其余接口特有的头（X-User-Id 等）由调用方补，或走 billing_headers()。

    风控头对齐上游 2026-09-14 的改动（D1/D5/D6）——管理端有一批请求**绕过
    上游直连腾讯**（扫码登录、签到、积分、trial、注册），上游给它的出站加了
    这些头，我们这条路若不加就会成为唯一「形态不像官方客户端」的流量：
      * X-CodeBuddy-Request: 1  官方客户端风控闸门头，所有 API 请求必带（D1）
      * Accept-Language         按账号域切 zh-CN / en-US（D5）
      * Accept                  非流式收紧为 application/json（D6，原先是
                                `application/json, text/plain, */*`）
    聊天（流式）路径的 Accept 由调用方覆盖为流式形态，见 tencent.probe_account。

    关于「登录流程是否也该带 D1/D5」——上游自己的登录工具 cmd/login/main.go
    至今**没跟**（仍是旧的宽松 Accept、无这两个头）。我们选择跟，理由是：
      1. D1 的注释说这是「官方客户端风控闸门头，所有 API 请求必带」——
         判断依据来自官方客户端行为，与哪个上游进程实现无关；
      2. 登录是**最敏感**的一步（换 token、拿账号信息），形态不符的代价最高；
      3. 多带这两个头不会让请求变坏：上游网关自己也在发同样的组合。
    即：以「官方客户端行为」为参照，而不是以「上游某个工具的现状」为参照。
    """
    origin = origin_of(realm)
    h = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'Accept-Language': accept_language(realm),
        'X-Requested-With': 'XMLHttpRequest',
        'X-CodeBuddy-Request': '1',
        'User-Agent': _ua(realm),
        'Origin': origin,
        'Referer': f'{origin}/',
    }
    if token:
        h['Authorization'] = f'Bearer {token}'
    h.update(device_fingerprint_headers(uid))
    return h


def accept_language(realm: Realm) -> str:
    """Accept-Language 按账号域切：global → en-US，cn → zh-CN（对齐上游 D5）。"""
    return 'en-US' if realm == GLOBAL else 'zh-CN'


# ── device token（X-Device-Token 设备风控头）────────────────
# 上游把它注入 **chat 与 billing 两个域**（resolveDeviceToken），三级回退：
#   auth 每号 device_token > config upstream.device_token > upstream.device_token_file
# 文件读取有 5 分钟缓存与 1KB 上限（见 device_token.go），这里镜像其语义。
# 它是**凭据**：只进请求头，不得写日志、不得回显前端。
_DT_FILE_TTL = 300
_dt_cache: dict = {'path': '', 'at': 0.0, 'token': ''}


def _read_upstream_sec() -> dict:
    """读上游 config.json 的 `upstream` 段（10 秒缓存，同 global 段）。"""
    now = time.time()
    cached = _up_cfg_cache.get('data')
    if cached is not None and now - float(_up_cfg_cache['at']) < _CFG_TTL:
        return cached
    data: dict = {}
    try:
        raw = json.loads(config.UPSTREAM_CONFIG.read_text(encoding='utf-8'))
        up = raw.get('upstream') if isinstance(raw, dict) else None
        if isinstance(up, dict):
            data = up
    except Exception:  # noqa: BLE001
        pass
    _up_cfg_cache.update({'at': now, 'data': data})
    return data


def device_token_for(auth: dict | None) -> str:
    """出站设备风控 token，三级回退（对齐上游 resolveDeviceToken）。取不到返回空串。"""
    if isinstance(auth, dict):
        v = str(auth.get('device_token') or '').strip()
        if v:
            return v
    up = _read_upstream_sec()
    v = str(up.get('device_token') or '').strip()
    if v:
        return v
    path = str(up.get('device_token_file') or '').strip()
    if not path:
        return ''
    now = time.time()
    if _dt_cache.get('path') == path and now - float(_dt_cache.get('at') or 0) < _DT_FILE_TTL:
        return str(_dt_cache.get('token') or '')
    tok = ''
    try:
        p = Path(path)
        # 与上游一致：目录 / 超 1KB / 读失败一律当作没有（不报错，不 panic）
        if p.is_file() and p.stat().st_size <= 1024:
            tok = p.read_text(encoding='utf-8', errors='replace').strip()
    except Exception:  # noqa: BLE001
        tok = ''
    _dt_cache.update({'path': path, 'at': now, 'token': tok})
    return tok


def billing_headers(realm: Realm, auth: dict | None = None) -> dict:
    """billing 域请求头（对齐上游 BillingHeaders）。

    与 `headers()` 的差别是**身份头**——上游 billing 域（签到 / 积分 / trial /
    注册激活）一直带这些，而 chat 域的身份头由各调用方按接口补齐：

      X-User-Id       账号 uid（非空才发）
      X-Enterprise-Id / X-Tenant-Id   企业账号的两个同值头
      X-Domain        账号域（非空才发）
      X-Device-Token  设备风控 token（三级回退，空则不发）

    管理端直连这批接口时此前一个都不带，是「形态不像官方客户端」的主要来源；
    上游 Go 侧测试也明确断言 trial 必须携带 X-User-Id（trial_test.go）。

    UA 用**单段** `WorkBuddy/<ver>`（billing_ua）：官方客户端在这类白名单接口
    显式覆写 UA，上游 2026-09-14 起已改为默认如此（此前默认是不设 UA）。
    """
    auth = auth if isinstance(auth, dict) else {}
    token = str(auth.get('access_token') or '')
    h = headers(realm, token or None)
    # billing 域 UA：覆写为单段 WorkBuddy/<ver>；显式 client_name="SaaS" 时
    # **移除**该头（还原旧行为——上游此时不设 UA，交给 HTTP 客户端默认值）
    _bua = billing_ua()
    if _bua:
        h['User-Agent'] = _bua
    else:
        h.pop('User-Agent', None)
    uid = str(auth.get('uid') or '')
    if uid:
        h['X-User-Id'] = uid
    # 账号级设备指纹头（X-Machine-ID / X-Session-ID）。
    #
    # 注意这里与上游现状**有意的差异**：上游 BillingHeaders 的注释写着「billing
    # 域另行注入」，但代码里并没有真的调用 injectAccountStableHeaders —— 也就是
    # 上游自己的 billing 域（签到/积分/trial）实际上没带这两个头（注释与实现不符）。
    # 我们按**注释声称的意图**实现（全路径覆盖）：billing 正是我们直连的那批端点，
    # 少发这两个头等于把「不像官方客户端」的形态留在最敏感的路上。
    h.update(device_fingerprint_headers(uid))
    ent = str(auth.get('enterprise_id') or '')
    if ent:
        h['X-Enterprise-Id'] = ent
        h['X-Tenant-Id'] = ent
    domain = str(auth.get('domain') or '')
    if domain:
        h['X-Domain'] = domain
    dt = device_token_for(auth)
    if dt:
        h['X-Device-Token'] = dt
    return h


def supports_checkin(realm: Realm) -> bool:
    """该 realm 是否有签到体系。

    国际版**没有**签到 / 旅行（上游调度器对 global 账号直接过滤、不发起任何请求，
    理由是避免风控）。调用方据此跳过，而不是打过去吃 4xx。

    **活跃上报不在此列**（此前这句多写了它，让用户以为国际版刷不了活跃 —— 见
    issue #134）：上游 `RunActivityNow` 对 CN 与 global 都上报（其 `gating_test.go`
    的 `TestRunActivityNowReportsGlobalAccounts` 锁定了这条），面板文案也已同步。
    令牌保活同样两边都支持。
    """
    return realm != GLOBAL
