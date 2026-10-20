"""对外反代网关：密钥鉴权 → IP 管控 → 模型映射 → 转发 → 计量落库。"""
from __future__ import annotations

import json
import os
from urllib.parse import urlsplit
import logging
import threading
import time

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import config, db, iputil, keysvc, upstreamsvc
from ..services import modelmap, proxyroutes
from ..config import _env_int
from ..routers.security import get_config as get_security_config

logger = logging.getLogger('workbuddy.gateway')

router = APIRouter(tags=['gateway'])


def _oai_error(message: str, status: int = 400, err_type: str = 'invalid_request_error',
               code: str | None = None, hint: str | None = None) -> JSONResponse:
    """OpenAI 形状的错误体。

    `hint` 对应上游 2026-09-16 新增的 `error.gateway_hint`：与 message **并列**的
    网关视角补充说明（措辞为英文，面向客户端工具链）。上游只给**可执行的**建议
    （如「no healthy account available in pool; check /status or retry later」），
    未覆盖的错误形态不带该字段。

    为空时**不写这个字段**：与上游「不编造 hint」的口径一致，也避免客户端拿到
    一个空值还要自己判断。
    """
    err: dict = {'message': message, 'type': err_type, 'code': code}
    if hint:
        err['gateway_hint'] = hint
    return JSONResponse({'error': err}, status_code=status)


def _bearer(request: Request) -> str:
    auth = request.headers.get('authorization', '')
    if auth.lower().startswith('bearer '):
        return auth[7:].strip()
    return request.headers.get('x-api-key', '').strip()


# 请求体上限。
#
# 上游 9d1a21b **移除了** `server.max_body_mb`，其 chat handler 也不再预拦截
# （`io.ReadAll(r.Body)` 不设限，超限问题交给上游自然响应）。所以本端不能再拿
# 那个键当上限来源 —— 读不到就回落默认值的话，本端会变成**隐性瓶颈**：
# 用户发一个 20MB 的正常大上下文请求，上游能处理，我们却先 413 拦掉，而且
# 报错里指的那个配置项在上游已经不存在了，按文档怎么调都没用。
#
# 但**不能因此去掉上限**：上游是 Go，读的是流且能背压；本端要在内存里拼出完整
# body 再 json.loads，几个并发的大请求就能把内存吃光（这正是当初加上限的原因）。
# 所以做法是：上限改成**本端自己的**配置（`WB_GATEWAY_MAX_BODY_MB`），默认调到
# 32 MB —— 足够容纳常见的长上下文与附件，又不会让单进程内存失控。
#
# 兼容：上游若仍是**旧版**（配置里还有 server.max_body_mb），继续尊重它的取值
# （见 max_body_bytes），这样升级顺序不受限：先升哪边都不会出现「一边说 256MB
# 另一边 413」的错配。
_BODY_LIMIT_TTL = 10
_body_limit_cache: dict[str, float | int] = {'at': 0.0, 'bytes': 0, 'source': ''}
DEFAULT_MAX_BODY_MB = _env_int('WB_GATEWAY_MAX_BODY_MB', 32)
# 上限是**谁**定的：'upstream'（旧版上游 config.json 的 server.max_body_mb）/
# 'env'（本端 WB_GATEWAY_MAX_BODY_MB）/ ''（默认值）。
# 为什么要记它：413 的提示必须告诉用户「该去哪改」。issue #180 里用户按旧文案
# 改了环境变量却毫无效果 —— 因为上限其实来自上游配置，文案指错了地方。
_BODY_LIMIT_SOURCE: dict[str, str] = {'value': ''}


def max_body_bytes() -> int:
    """当前生效的请求体上限（字节）。

    取值顺序：
      1. 上游 config.json 的 `server.max_body_mb`（**仅旧版上游还有这个键**）；
      2. 否则用本端默认值（`WB_GATEWAY_MAX_BODY_MB`，默认 32 MB）。

    每次读配置有 IO 成本，故做 10 秒缓存：改动很快生效，又不必每请求读文件。
    """
    now = time.time()
    cached = int(_body_limit_cache['bytes'])
    if cached and now - float(_body_limit_cache['at']) < _BODY_LIMIT_TTL:
        return cached
    limit = DEFAULT_MAX_BODY_MB * 1024 * 1024
    source = 'env' if _env_set('WB_GATEWAY_MAX_BODY_MB') else ''
    try:
        cfg = json.loads(config.UPSTREAM_CONFIG.read_text(encoding='utf-8'))
        mb = int((cfg.get('server') or {}).get('max_body_mb') or 0)
        if mb > 0:
            limit = mb * 1024 * 1024
            source = 'upstream'
    except Exception:  # noqa: BLE001
        # 配置读不到就沿用默认值：网关不能因为读不到配置而拒绝服务
        pass
    _body_limit_cache['at'] = now
    _body_limit_cache['bytes'] = limit
    _body_limit_cache['source'] = source
    _BODY_LIMIT_SOURCE['value'] = source
    return limit


def _env_set(name: str) -> bool:
    value = (os.environ.get(name) or '').strip()
    return value != ''

# ── max_tokens 上限钳制 ────────────────────────────────────────────
#
# 上游的 `Request.max_tokens` 是 **int32**（上限 2147483647）。超出后上游拒绝时
# 不是回 400，而是把它包成 `no_healthy_account` + 503——客户端据此以为「服务暂时
# 不可用」而重试，但参数越界重试多少次都一样失败。
#
# 实测触发来源：有客户端按「剩余额度 → max_tokens」做减法（形如 `1e10 - 已消耗`，
# 约 99.99 亿，是 int32 上限的 4.66 倍）。这类值是**算出来的**、不是用户填的，
# 客户端那侧的换算逻辑我们改不到，只能由网关兜住。
#
# 阈值取 1e6：上游 /v1/models 里最大的 max_output_tokens 实测为 131072，
# 1e6 有约 7 倍余量，不会误伤正常请求，又能挡住这类减法式产物。
MAX_TOKENS_CEILING = 1_000_000

# 一并钳制的字段：三种协议各自的输出上限字段名。
# `max_completion_tokens` 是 OpenAI 较新的名字（o-series / gpt-5 时代取代
# `max_tokens`），部分客户端已默认用它；上游同样按 int32 读，实测越界时一样
# 回 503。网关是透传的，客户端发什么就转发什么，故必须一并兜住。
_MAX_TOKENS_FIELDS = ('max_tokens', 'max_completion_tokens', 'max_output_tokens')


def clamp_max_tokens(body: dict) -> None:
    """原地钳制请求体里的输出上限字段（见 _MAX_TOKENS_FIELDS）。

    为什么是**原地改**而不是返回新 dict：`_chat` 与两个协议层都是拿到 body 后
    直接转发，原地把越界值压回上限，调用方无需改动其余逻辑。

    非数字一律**原样保留**（不猜测、不置默认）：把它交回上游去判，网关不替
    上游做协议决定——历史上替上游兜底过的字段都留下了难以回溯的偏差。
    """
    if not isinstance(body, dict):
        return
    for field in _MAX_TOKENS_FIELDS:
        value = body.get(field)
        # bool 是 int 的子类，必须排除；None 交给上游按「未提供」处理
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value <= MAX_TOKENS_CEILING:
            continue
        body[field] = MAX_TOKENS_CEILING


def _payload_too_large(limit: int) -> JSONResponse:
    """413 的正文必须说清「上限是多少、谁定的、该去哪改」。

    issue #180 的教训：以前不管上限从哪来都写「由环境变量 WB_GATEWAY_MAX_BODY_MB
    决定」—— 而用户的上游是旧版、config.json 里还留着 `server.max_body_mb`，
    面板**优先**听它，于是用户改了环境变量毫无效果，还以为面板不认这个配置。
    """
    mb = limit // 1024 // 1024
    source = _BODY_LIMIT_SOURCE.get('value') or ''
    if source == 'upstream':
        where = ('该上限来自**上游** config.json 的 server.max_body_mb（你的上游是旧版、'
                 '还认这个键；面板优先听它，所以改环境变量不生效）：把那个值调大并重启上游，'
                 '或者删掉该键，让面板改用环境变量 WB_GATEWAY_MAX_BODY_MB')
    elif source == 'env':
        where = ('该上限来自环境变量 WB_GATEWAY_MAX_BODY_MB（改完要重启面板）')
    else:
        where = ('当前用的是默认上限；可在 .env 里设 WB_GATEWAY_MAX_BODY_MB 提高'
                 '（改完要重启面板）')
    return _oai_error(
        f'请求体超过 {mb} MB 上限：请压缩内容（精简上下文或附件），或提高上限。'
        f'{where}。上游自 9d1a21b 起已不再限制请求体大小',
        413, 'invalid_request_error', 'payload_too_large',
    )


async def _read_json_body(request: Request) -> tuple[dict | None, JSONResponse | None]:
    """读取并解析网关请求体，带大小与格式校验。

    大小校验必须落在**实际读取**上，不能只看 Content-Length：
    分块传输（chunked）时该头缺失，伪造该头也可以偏小，
    只看头会让超大请求被整段读进内存（少量并发即可耗尽内存）。
    """
    limit = max_body_bytes()
    try:
        declared = int(request.headers.get('content-length') or 0)
    except ValueError:
        declared = 0
    if declared > limit:
        return None, _payload_too_large(limit)

    chunks: list[bytes] = []
    size = 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                return None, _payload_too_large(limit)
            chunks.append(chunk)
    except Exception:  # noqa: BLE001
        return None, _oai_error('读取请求体失败', 400)

    try:
        body = json.loads(b''.join(chunks))
    except Exception:  # noqa: BLE001
        return None, _oai_error('请求体不是合法 JSON', 400)
    if not isinstance(body, dict):
        return None, _oai_error('请求体必须是 JSON 对象', 400)
    return body, None


# ── 简单的每密钥速率限制（滑动窗口）─────────────────────
# 目的：单个密钥被打爆时保护上游账号池，避免拖垮其他调用方。
# 计数放在进程内存，单实例足够；多实例部署时可换成 Redis。
_rate: dict[int, list[float]] = {}
RATE_WINDOW = 60
RATE_MAX_PER_MIN = _env_int('WB_GATEWAY_RATE_PER_MIN', 120)


def _rate_limited(key: dict) -> tuple[bool, int]:
    """返回 (是否限流, 窗口内计数)。

    **只有放行的请求才占用窗口额度**（issue #52）。此前不分放行与拒绝一律
    `append()`，被拒的请求同样计入，于是与客户端重试构成正反馈：客户端以高于
    阈值的速率持续重试时，窗口计数永远降不回阈值以下，表现为「凑满一窗之后
    持续全拒、永不恢复」——限流器本该限速，却变成了熔断。

    现在的语义是「滑动窗口内**放行**了多少次」：超出的请求被拒且不占额度，
    因此客户端把速率降到阈值以下能立刻恢复，保持高于阈值则稳定放行到上限
    （多余的被拒，不会雪崩）。放行数上限仍是 `RATE_MAX_PER_MIN`。
    """
    kid = int(key['id'])
    if RATE_MAX_PER_MIN <= 0:
        return False, 0
    now = time.time()
    hits = [t for t in _rate.get(kid, []) if now - t < RATE_WINDOW]
    limited = len(hits) >= RATE_MAX_PER_MIN
    if not limited:
        hits.append(now)
    # 拒绝的分支也要回写：窗口内已过期的记录要顺手清掉，否则长期被限的密钥
    # 会一直拖着一串陈旧时间戳（金额上限看不出问题，但内存是白占的）。
    _rate[kid] = hits
    # 顺带清理过期键，避免长期运行后字典无限增长
    if len(_rate) > 2000:
        for k in [k for k, v in _rate.items() if not v or now - v[-1] > RATE_WINDOW]:
            _rate.pop(k, None)
    return limited, len(hits)


# ── 未鉴权入口的入站日志采样 ─────────────────────────────
# 「没有密钥」「密钥不认识」这两条路径**在校验密钥之前**就写库，任何匿名者都能
# 触发（`/v1/chat/completions` 与 `/v1/usage` 都是这样）。攻击者只要写个循环
# 用随机 token 打过来，就能以每请求一行的速度把 `ip_access_logs` 的两万行窗口
# 顶掉——真正该看的扫描记录反而被自己人（同一个人）挤了出去，外加持续的写盘与
# 行数清理开销。这是**匿名写放大**，与 `_log_ip` 里清洗/截断防的是同一类问题。
#
# 做法：按 (来源 IP, 原因) 做**窗口内计数**，前 N 次照常逐条记录（保留细节：
# 路径、UA 都不一样，排查时有用），超出的折叠掉，并在本窗口第一次折叠时写一条
# `key_auth_flood` 汇总行——运维能看到「这个 IP 在这一分钟里刷了更多」，但库里
# 每分钟最多 N+1 行/IP。折叠不是丢弃信号，而是把「很多条一样的东西」压成一条
# 仍能看出发生了什么的记录。
#
# 只对这两个**未鉴权可达**的原因采样：`ip_blocked` / `rate_limited` / 密钥层
# 拒绝都要求先拿出一把已知密钥，写放大的成本已经由密钥本身承担，采样反而会
# 藏掉「某把密钥被反复拒绝」的线索。
#
# 计数在进程内存里（与上面的每密钥限流同一口径）：单实例足够；`workers > 1`
# 时每个进程各自计数，上限按进程数放大——仍然有界，可接受。
_AUTH_FAIL_REASONS = frozenset({'missing_key', 'invalid_key'})
_AUTH_LOG_WINDOW = 60
_AUTH_LOG_PER_WINDOW = max(1, _env_int('WB_AUTH_LOG_PER_MIN', 5))
# (ip, reason) -> [窗口起点, 本窗次数, 是否已写过汇总行]
_auth_log_state: dict[tuple[str, str], list] = {}
_auth_log_lock = threading.Lock()


def _auth_log_verdict(ip: str, reason: str) -> str:
    """返回 'log'（逐条记录）/ 'flood'（写一条汇总行）/ 'drop'（折叠掉）。"""
    now = time.time()
    key = (ip, reason)
    with _auth_log_lock:
        state = _auth_log_state.get(key)
        if not state or now - state[0] >= _AUTH_LOG_WINDOW:
            state = [now, 0, False]
            _auth_log_state[key] = state
        state[1] += 1
        # 顺手清理过期项，避免被大量来源 IP 撑爆字典（键里含外部输入的 IP）。
        # 只在真的很大、且不是每次请求都做时才扫（扫描本身是 O(n)）。
        if len(_auth_log_state) > 4096 and state[1] % 64 == 0:
            for k in [k for k, v in _auth_log_state.items()
                      if now - v[0] >= _AUTH_LOG_WINDOW]:
                _auth_log_state.pop(k, None)
        if state[1] <= _AUTH_LOG_PER_WINDOW:
            return 'log'
        if not state[2]:
            state[2] = True
            return 'flood'
        return 'drop'


def _log_ip(ip: str, path: str, blocked: bool, ua: str | None,
            reason: str | None = None) -> None:
    """记录一次入站访问。**默认只记拦截**，放行不记（见下）。

    reason 是**被拦的原因**（issue #33）。此前只有一个 blocked 布尔，界面上
    显示「已拦截」却看不出是哪一关拦的：没带密钥 / 密钥不认识 / IP 规则拦的，
    三者的处置方式完全不同（改客户端配置 / 重新发密钥 / 改 IP 规则）。用户
    只能靠猜——实测有用户因此提了 issue 也说不清属于哪一种。

    为什么放行不记（服务器审计的结论）：这张表在安全页叫「IP 访问日志」，
    用途是**安全审计**——回答「谁在扫我、谁被挡了」。此前每个成功请求也写一行，
    实测线上 **7877 行里只有 17 行是拦截记录（0.2%）**，真正该看的信号被
    7860 行正常流量淹没；而表有 2 万行滚动上限（其注释写明「按每次拒绝一行
    估算足够回溯近期攻击」），被成功请求占满后保留窗口从数月压到约 17 天
    —— 真出事时记录可能已经被挤掉了。正常流量在「请求日志」页有完整记录，
    这里重复记一遍只制造噪声。

    例外：`WB_AUDIT_ALL_ACCESS=1` 时恢复全量记录（需要核对"某 IP 到底来过
    什么"时临时打开）。放行的明细始终能在请求日志里查到，所以默认不记不丢信息。

    **这条路径在校验密钥之前执行**，所以它是**未鉴权可达**的写库入口：
    「没有 token」「token 无效」都会先写一行。因此这里必须自我约束，
    否则任何匿名者都能用它膨胀数据库（实测：带 2KB UA 的请求每条约 1.3KB，
    而 ip_access_logs 此前既无清洗也无上限、更没有清理机制）：

      * `ua` / `path` / `reason` 都过 `db._clean`（去换行等控制字符 + 截断）
        ——文档一直声称「日志写入前统一 `_clean()`」，但实际上只有 audit_logs
        这么做了，网关这两张表被漏掉：带换行的 UA 能伪造出额外日志行，
        污染事后排查（实测确认）。
      * 写入走 `db.add_ip_access_log`，由它负责行数上限（超出就丢最旧的），
        避免表无限增长直到磁盘写满。
      * 「没有密钥」「密钥不认识」这两种**匿名可达**的原因再做一层窗口采样
        （见 `_auth_log_verdict`）：每 IP 每分钟前几条照常记，其余折叠成一条
        `key_auth_flood`。所以「一次都没记」不会发生，被折叠的那部分仍留了
        一条能看出「这个 IP 在刷」的记录。
    """
    if not blocked and not config.AUDIT_ALL_ACCESS:
        return
    if reason and reason in _AUTH_FAIL_REASONS:
        verdict = _auth_log_verdict(ip, reason)
        if verdict == 'drop':
            return
        if verdict == 'flood':
            reason = 'key_auth_flood'
    db.add_ip_access_log(ip, path, blocked, ua, reason)


def _error_hint(data: object) -> str | None:
    """从上游错误体里取 `error.gateway_hint`（取不到返回 None）。

    上游 2026-09-16 起在错误响应里附这个字段：与 message **并列**的网关视角补充
    说明，只给**可执行的**建议（例如「no healthy account available in pool;
    check /status or retry later」），未覆盖的错误形态不带。

    两个**协议翻译层**（Anthropic / Responses）此前只取 `message` 重建错误体，
    这个字段会被丢掉——于是同一份上游错误，走 `/v1/chat/completions` 的客户端
    能看到建议、走 `/v1/messages` 的看不到。与 issue #18 同一条原则：**真实原因
    与可执行建议都要能到达客户端**。故抽成共用实现，三处口径一致。
    """
    if not isinstance(data, dict):
        return None
    err = data.get('error')
    if not isinstance(err, dict):
        return None
    hint = err.get('gateway_hint')
    if isinstance(hint, str) and hint.strip():
        return hint.strip()[:300]
    return None


def _usage_credit(usage: dict | None) -> float | None:
    """从 usage 里取上游的真实扣费（credit）。

    上游从 2026-09-13 起在末帧 usage 里带 credit（本次真实扣费）。
    取不到就返回 None（存 NULL），不要退化成 0——「没数据」和「免费」
    在成本判断上是两回事，混为一谈会误判成免费号。
    """
    if not isinstance(usage, dict):
        return None
    raw = usage.get('credit')
    if raw is None or isinstance(raw, bool):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    return val if val >= 0 else None


def _usage_cache(usage: dict | None) -> tuple[int | None, int | None, int | None]:
    """从 usage 里取提示词缓存的三段 token（issue #69）。

    腾讯在流式末帧 usage 里给 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`
    / `prompt_cache_write_tokens`；**上游自己的缓存统计就是从这三个字段来的**
    （其 `/v1/stats` 的 cache_hit_rate 同源），所以我们不必自己估算。

    三个都要能区分「没给」与「给了 0」：老上游不给 = None（界面上显示「—」），
    给了 0 = 这次真的没命中缓存。混为一谈会让用户以为缓存生效了。
    """

    def _one(name: str) -> int | None:
        if not isinstance(usage, dict):
            return None
        raw = usage.get(name)
        # bool 是 int 的子类，`True` 不能当成 1 个 token
        if raw is None or isinstance(raw, bool):
            return None
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return None
        return val if val >= 0 else None

    return (_one('prompt_cache_hit_tokens'),
            _one('prompt_cache_miss_tokens'),
            _one('prompt_cache_write_tokens'))


def _record(key: dict | None, ip: str, model: str, mapped: str, status: int, pt: int, ct: int, latency: int, ua: str | None, error: str | None, stream: bool, *, credit: float | None = None, first_token: int | None = None, usage: dict | None = None) -> None:
    """记录调用日志与用量。

    credit 为上游返回的真实扣费（usage.credit）。None 表示上游没给，
    与「扣了 0」是两回事，因此用 NULL 存而不是 0。

    `usage` 可以直接把上游那份 usage 传进来：扣费与**提示词缓存三段**都从它里面取
    （issue #69）。之所以整份传进来而不是在各调用点各取一遍——四条协议路径
    （chat 流式/非流式、Responses、Anthropic）都各自有一份 usage，分开取迟早漏一处，
    而漏的表现是「某个协议的缓存统计永远是空的」。传 `credit` 仍然有效（显式值优先）。

    first_token 为首字延迟（毫秒）。None 表示未采集到：非流式请求本来就没有
    中间过程，历史记录也没这个值，因此同样用 NULL 存，而不是 0。

    注意：日志/统计属于旁路，任何异常都不能影响用户请求本身
    （曾因统计函数缺失导致流式响应在收尾阶段中断，客户端看到
    内容正常但报 terminated）。因此这里整体兜底。
    """
    if credit is None:
        credit = _usage_credit(usage)
    cache_hit, cache_miss, cache_write = _usage_cache(usage)

    try:
        # realm 由**实际发往上游的模型名**判定（上游按 `cn:` / `global:` 前缀路由）：
        # 它决定这次调用实际走了哪个账号池，也是界面按版本切换日志/统计的依据。
        #
        # 有「模型映射」时判**映射后**的名字（issue #47）：请求名可能是个别名，
        # 别名本身不带前缀，按它判会把一次真实的国际版调用记成国内版，界面按
        # 版本筛选时这条就跑到另一栏去了。没有映射时 mapped 为空，判请求名。
        #
        # **所有外部来源的文本都要清洗 + 截断**（`db._clean`）：
        #   * `model` 来自请求体，长度无上限。此前一个 1 MiB 的 model 会被写进
        #     `request_logs.model`、`mapped_model` 以及 `usage_daily.model`
        #     **三处**（后者还在主键里，等于再加一份索引），单次请求就能放大
        #     数 MB —— 持密钥者可用少量请求把库撑大（实测 11 次请求 25MB）。
        #   * `ua` / `error` 同样来自外部（error 还含上游响应原文），带换行就能
        #     在日志页伪造出额外行，污染排查。
        # 清洗只影响入库文本，**不影响转发给上游的内容**（body 早已发走）。
        model_clean = db._clean(model, 128)
        realm = db.realm_of_model(db._clean(mapped, 128) or model_clean)
        db.add_request_log(
            ts=int(time.time()),
            key_id=key['id'] if key else None,
            ip=db._clean(ip, 64),
            model=model_clean,
            mapped_model=db._clean(mapped, 128),
            status=status,
            prompt_tokens=pt,
            completion_tokens=ct,
            latency_ms=latency,
            first_token_ms=first_token,
            ua=db._clean(ua, 512),
            error=db._clean(error, 500),
            stream=1 if stream else 0,
            credit=credit,
            realm=realm,
            cache_hit_tokens=cache_hit,
            cache_miss_tokens=cache_miss,
            cache_write_tokens=cache_write,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning('写入请求日志失败（不影响请求）: %s', exc)
        return

    if key:
        try:
            total = pt + ct
            # credit 一并传入：密钥的**积分额度**（issue #27）靠它累计。
            # 原先只记 token，积分用量就永远是 0，超限判定无从谈起。
            keysvc.touch(key, ip, total, credit)
            if total or credit:
                db.bump_usage(key['id'], model_clean, pt, ct, credit, realm=realm)
        except Exception as exc:  # noqa: BLE001
            logger.warning('累计用量失败（不影响请求）: %s', exc)


def _authorize(request: Request, model: str | None,
               *, is_model_list: bool = False,
               mapped: str | None = None) -> tuple[dict | None, str, JSONResponse | None]:
    """返回 (key, ip, error_response)。

    mapped：`model` 经「模型映射」后的名字。**默认在这里算**，因为版本归属判的
    是映射后的名字（issue #47，见 `keysvc.validate`）——调用方算好了可以传进来
    省一次设置读取，但漏传不会退回旧行为。调用方仍要自己再算一次用于改写 body
    （或把这里的算法抽出去共用）。
    """
    ip = iputil.client_ip(request)
    ua = request.headers.get('user-agent')
    path = request.url.path

    # 拦截原因写成**稳定的短码**而不是散文：界面按码翻译（5 种语言都能准确
    # 对应），改文案不必迁移历史数据，也不会因为中英混排让日志列宽失控。
    token = _bearer(request)
    if not token:
        _log_ip(ip, path, True, ua, 'missing_key')
        return None, ip, _oai_error('缺少 API Key，请在 Authorization 头中提供 Bearer 令牌', 401, 'authentication_error', 'missing_api_key')

    key = keysvc.resolve(token)
    if not key:
        _log_ip(ip, path, True, ua, 'invalid_key')
        return None, ip, _oai_error('API Key 无效', 401, 'authentication_error', 'invalid_api_key')

    # 映射只算一次，后面三处拒绝路径都要用它记账（见下）。
    # 为什么拒绝路径也要传：日志的 `realm` 按**实际要用的那个名字**归档（issue #47
    # 的统一口径）。别名不带 `global:` 前缀，若拒绝路径按请求名归档，一次「别名指向
    # 国际版、却被国内版密钥拒绝」的记录会落在国内版栏目里——按版本筛日志的人会看到
    # 一条本不该在那儿的记录，且看不出它原本指向国际版。
    mapped = _map_model(model) if mapped is None else mapped

    # 全局入站 IP 规则
    sec = get_security_config()
    if sec.get('enabled'):
        rules = [
            {'kind': r['kind'], 'cidr': r['cidr']}
            for r in db.query('SELECT kind, cidr FROM ip_rules')
        ]
        if not iputil.evaluate(ip, rules, sec.get('mode', 'blacklist')):
            _log_ip(ip, path, True, ua, 'ip_blocked')
            _record(key, ip, model or '', mapped or '', 403, 0, 0, 0, ua, 'IP 被拦截', False)
            return None, ip, _oai_error(f'来源 IP {ip} 被安全策略拦截', 403, 'permission_error', 'ip_blocked')

    # 注意这一行**在密钥校验之前**：密钥层面的拒绝（停用 / 过期 / 配额用尽 /
    # 模型与版本不符 / IP 白名单）发生在这之后，若就这样返回，日志里会显示
    # 「已放行」而请求其实失败了 —— 用户对着「已放行」找问题，方向直接跑偏。
    # 因此校验失败时改写这一行为拦截（见下），放行时才落「已放行」。
    reason = keysvc.validate(key, ip, model, is_model_list=is_model_list,
                             mapped_model=mapped)
    if reason:
        # 状态码由 keysvc 决定，不再一律 403：一批客户端（DeepSeek Harness 等）
        # 把 401/403 统一显示成「API 密钥无效」，一律 403 会把「密钥版本不匹配」
        # 这种配置问题说成密钥坏了，用户便反复重建密钥（issue #18）。
        _record(key, ip, model or '', mapped or '',
                getattr(reason, 'status', 403), 0, 0, 0, ua, reason, False)
        _log_ip(ip, path, True, ua, _key_reject_code(reason, is_model_list))
        return None, ip, _oai_error(
            reason,
            getattr(reason, 'status', 403),
            getattr(reason, 'err_type', 'permission_error'),
            getattr(reason, 'code', 'forbidden'),
        )

    limited, count = _rate_limited(key)
    if limited:
        msg = f'请求过于频繁（{RATE_WINDOW}s 内超过 {RATE_MAX_PER_MIN} 次）'
        _record(key, ip, model or '', mapped or '', 429, 0, 0, 0, ua, msg, False)
        _log_ip(ip, path, True, ua, 'rate_limited')
        return None, ip, _oai_error(msg, 429, 'rate_limit_error', 'rate_limit_exceeded')

    _log_ip(ip, path, False, ua)
    return key, ip, None


def _key_reject_code(reason: object, is_model_list: bool) -> str:
    """把密钥层的拒绝原因归成一个稳定的短码，供入站日志显示。

    为什么不用 keysvc 的原话：那是**给调用方客户端看的完整句子**（含具体数字，
    如「已用 1200 / 上限 1000」），直接塞进日志列表会撑爆列宽；而且它是中文的，
    界面切成英文时那一列会中英混排。这里只归**类别**，展示侧按语言翻译。

    取不到 code 时回落到 'key_rejected'（宁可说得笼统，也不要漏记成「已放行」）。
    """
    code = getattr(reason, 'code', '') or ''
    return code if isinstance(code, str) and code else 'key_rejected'


def _map_model(model: str | None) -> str | None:
    """「模型映射」查表：别名 → 真名；没命中就落到**兜底模型**（issue #173），
    两者都没有时原样返回。

    设置坏掉（存成了非对象）时按「没有映射」处理，**不能让整个网关 5xx**：
    映射是可选的便利功能，坏数据只该影响它自己那一项，不连坐（与
    `db._json_list` 对非法 JSON 列的处理同一原则）。

    语义集中在 `services/modelmap`，这里只取结果：白名单补条目与别名校验走同一份
    实现 —— 三处各写一遍必然漂移，而漂移出来的错位（能调用、但客户端的模型清单里
    没有它）只有用户能发现。
    """
    return modelmap.target_for(db.get_setting('model_map', {}) or {}, model)


def _upstream_headers(upstream: dict | None = None) -> dict:
    """转发时用的请求头；`upstream` 为空 = 默认上游。

    多上游（密钥绑定上游）之后，请求头**必须按密钥解析出来的上游来构造** ——
    拿错上游的 api_key 会打到别的账号池上，而这正是本功能要隔离的东西。
    默认参数只为兼容「没有密钥」的调用方（探活 / 管理端调试台）。
    """
    up = upstream if upstream is not None else upstreamsvc.default_upstream()
    headers = {'Content-Type': 'application/json'}
    # 分组把 api_key 留空、且地址与默认上游相同时沿用默认那把（见
    # upstreamsvc.forward_api_key）——「添加分组」只填名称就是这个形态，
    # 不沿用的话绑定这类分组的密钥一调用就吃上游 401。
    api_key = upstreamsvc.forward_api_key(up)
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    return headers


def _upstream_hint(upstream: dict | None) -> str:
    """502/503 时补一句「本次打的是哪个上游、这一跳会不会走出口代理」。

    为什么要有（issue #167：两位用户都报「直接 curl 上游是通的、经面板全 502」）：
    原来的报错只有异常文本，看不出面板往哪打的、中间有没有代理。而最常见的两种成因
    恰恰要靠这两条信息才能自查：

      · 上游地址 / 端口填错（面板打的是配置里那个地址，不是你以为的那个）；
      · **上游地址写成公网域名、同时又配了出口代理**：面板→上游这一跳会走代理，
        代理绕不回你自己那台机器，于是全 502 —— 而内网与本机地址是自动直连的。

    `_is_internal_host` 与转发用的判断是同一个函数，所以这里说的「会走代理」就是
    真实行为，不是提示语里另算一套。
    """
    up = upstream or upstreamsvc.default_upstream()
    base = str(up.get('base_url') or '').strip()
    if not base:
        return ''
    try:
        host = urlsplit(base).hostname
    except ValueError:
        host = None
    proxy = config.effective_http_proxy()
    if proxy and host:
        if config._is_internal_host(host):       # noqa: SLF001 —— 与转发判断同一份实现
            return f'（本次请求的上游：{base}；该地址属内网/本机，这一跳直连）'
        masked = proxyroutes.mask_url(proxy)
        return (f'（本次请求的上游：{base}；这一跳会经出口代理 {masked} —— '
                f'代理到不了这个地址就会 502）')
    return f'（本次请求的上游：{base}）'


def _upstream_for(key: dict | None, *, ip: str, model: str, mapped: str,
                  ua: str | None) -> tuple[dict | None, JSONResponse | None]:
    """按密钥解析本次请求该走哪个上游；不可用时返回**明确报错**。

    不静默回落默认上游的原因见 upstreamsvc 模块注释第 2 条：那把密钥本应只碰
    某个分组的账号，回落到默认上游会让它打到全池——隔离失效而且无人察觉。
    失败同时记一条请求日志（503 + 原因），排障的人第一时间就能看到。
    """
    try:
        return upstreamsvc.resolve_for_key(key), None
    except upstreamsvc.UpstreamUnavailable as exc:
        _record(key, ip, model, mapped, 503, 0, 0, 0, ua, str(exc), False)
        # 这里不加 _upstream_hint：本函数讲的是「密钥绑定的上游不可用」，作用域里
        # 也没有那个上游对象 —— 报出默认上游的地址只会把人带偏（异常文本已经说清
        # 是哪个分组、该启用还是改绑）。
        return None, _oai_error(str(exc), 503, 'api_error', 'upstream_unavailable')


def _scan_sse(pending: str, usage: dict) -> tuple[str, bool]:
    """扫描 SSE 文本片段：提取 usage，并判断是否已出现首个正文。

    返回 (未处理完的残留缓冲, 本次是否见到正文 delta)。
    为什么要单独判断「正文」：OpenAI 流的第一块通常只有 role、content 为空，
    若用「收到首块」当首字，会把连接建立时间也算进去，数字偏小且失真。
    """
    saw_content = False
    while '\n' in pending:
        line, pending = pending.split('\n', 1)
        line = line.strip()
        if not line.startswith('data:'):
            continue
        payload = line[5:].strip()
        if not payload or payload == '[DONE]':
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if isinstance(obj.get('usage'), dict):
            usage.update(obj['usage'])
        choices = obj.get('choices')
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get('delta') or choice.get('message') or {}
                if isinstance(delta, dict) and (
                    delta.get('content') or delta.get('reasoning_content')
                ):
                    saw_content = True
    return pending, saw_content


# ── 密钥自查用量（issue #137）─────────────────────────────
# 拿密钥自身鉴权、只回这把密钥自己的数字，**只读面板已记录的值、不触发任何上游请求**
# （否则「查一下还剩多少」会变成一次真实调用，反过来加剧额度消耗与限流）。
#
# 有意**不套用**调用时的那套校验（额度用尽 / 已过期 / 被停用 / IP 白名单）：恰恰是
# 在调不动的时候，用户最需要看到「还剩多少、为什么调不动」。密钥本身已证明归属，
# 这里只暴露它自己的元数据，把状态如实回给调用方，而不是再甩一个 401。
@router.get('/v1/usage')
def key_usage(request: Request):
    ip = iputil.client_ip(request)
    ua = request.headers.get('user-agent')
    path = request.url.path
    token = _bearer(request)
    # 认不出调用方的访问仍要进**入站日志**：这条路径与其它网关入口一样是
    # 未鉴权可达的，漏记会让「谁在扫我」少一块（安全审计口径，见 `_log_ip`）。
    if not token:
        _log_ip(ip, path, True, ua, 'missing_key')
        return _oai_error('缺少 API Key，请在 Authorization 头中提供 Bearer 令牌',
                          401, 'authentication_error', 'missing_api_key')
    key = keysvc.resolve(token)
    if not key:
        _log_ip(ip, path, True, ua, 'invalid_key')
        return _oai_error('API Key 无效', 401, 'authentication_error', 'invalid_api_key')

    def allowance(limit: int | None, used: int | None) -> dict:
        used_n = int(used or 0)
        limit_n = int(limit or 0)
        return {
            'used': used_n,
            'limit': limit_n or None,          # None = 不限制
            'remaining': max(0, limit_n - used_n) if limit_n else None,
        }

    now = int(time.time())
    expires_at = int(key.get('expires_at') or 0)
    # 记一行**请求日志**（与 `/v1/models` 同口径，0 token）：否则一把密钥被
    # 监控脚本每分钟轮询查用量时，运维在「请求日志」里完全看不出流量从哪来。
    # 这一行经 `_record` 顺带把「最近使用」与「来源 IP」留痕——与模型列表页、
    # 面板 API 令牌一致；但 token / 积分消耗**一分不记**（记账口径只认真实调用）。
    _record(key, ip, '', '', 200, 0, 0, 0, ua, None, False)
    return {
        'object': 'key.usage',
        'key': {
            'name': key.get('name') or '',
            'prefix': key.get('prefix') or '',
            'realm': key.get('realm') or '',
            'enabled': bool(key.get('enabled')),
            'expires_at': expires_at,
            'expired': bool(expires_at and now >= expires_at),
            'models': list(key.get('models') or []),
        },
        'tokens': allowance(key.get('quota'), key.get('used_tokens')),
        'credits': allowance(key.get('quota_credit'), key.get('used_credit')),
        'last_used_at': int(key.get('last_used_at') or 0),
        # 这几个数字是面板**已记录**的值，读取时刻即 snapshot_at；不是向上游现问的。
        'snapshot_at': now,
        'data_source': 'panel-recorded',
    }


# ── 模型列表 ─────────────────────────────────────────────
# 列表按密钥的版本归属过滤：国际版密钥只看到 `global:` 条目、国内版密钥只看到
# 其余条目（限定了版本的密钥看不到另一版本，免得挑出一个注定 403 的模型）。
# 未限定版本的密钥（存量）照旧看到全部——它们本来就两版都能调。
@router.get('/v1/models')
async def list_models(request: Request):
    key, ip, err = _authorize(request, None, is_model_list=True)
    if err:
        return err
    # 模型列表也按密钥的上游取：不同上游的模型清单未必一致（版本 / 部署差异），
    # 拿默认上游的清单给一把绑定了别的上游的密钥，会给出它其实调不了的模型。
    upstream, upstream_err = _upstream_for(
        key, ip=ip, model='', mapped='', ua=request.headers.get('user-agent'))
    if upstream_err:
        return upstream_err
    started = time.time()
    try:
        async with config.http_client(30, connect=3) as client:
            resp = await client.get(f'{upstream["base_url"]}/v1/models',
                                    headers=_upstream_headers(upstream))
        latency = int((time.time() - started) * 1000)
        _record(key, ip, '', '', resp.status_code, 0, 0, latency, request.headers.get('user-agent'), None, False)
        payload = _scope_models(resp.json(), key)
        # 能力字段兜底（issue #211）：随包上游的版本可能比面板旧 —— 补丁只在上游
        # 侧时，下游经面板拿到的清单仍缺字段，客户端会整条跳过模型。
        payload = _listing_compat_models(payload)
        # Anthropic 客户端（Claude Code 等）也会调这个路径，但期望的结构不同
        if request.headers.get('anthropic-version'):
            payload = _as_anthropic_models(payload)
        return JSONResponse(payload, status_code=resp.status_code)
    except Exception as exc:  # noqa: BLE001
        latency = int((time.time() - started) * 1000)
        _record(key, ip, '', '', 502, 0, 0, latency, request.headers.get('user-agent'), str(exc), False)
        return _oai_error(f'上游不可用: {exc}' + _upstream_hint(upstream), 502, 'api_error', 'upstream_unavailable')


def _as_anthropic_models(payload: object) -> object:
    """把 OpenAI 形状的模型列表翻成 Anthropic 的形状。

    两边都叫 `/v1/models`，结构却完全不同：Anthropic 是
    `{data:[{type:"model", id, display_name, created_at}], has_more, first_id, last_id}`。
    Claude Code 按这个结构解析，形状不对会直接报错——所以只能在**同一个路径上
    按请求头分流**（用 `anthropic-version` 区分），而不能各注册一个路由
    （FastAPI 里先注册的会赢，另一个永远收不到请求）。

    认不出的结构**原样返回**：不在我们看不懂的响应上动手脚。
    """
    items = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return payload
    data = [
        {
            'type': 'model',
            'id': str(m.get('id') or ''),
            'display_name': str(m.get('name') or m.get('id') or ''),
            # 协议要求 ISO8601；上游给的是 created(epoch)，缺省时用纪元起点占位
            'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(m.get('created') or 0)),
        }
        for m in items
        if isinstance(m, dict) and m.get('id')
    ]
    return {
        'data': data,
        'has_more': False,
        'first_id': data[0]['id'] if data else None,
        'last_id': data[-1]['id'] if data else None,
    }


def _model_realm_of(m: object) -> str:
    """清单条目的版本：`global:` 前缀 = 国际版，其余 = 国内版。"""
    return 'global' if str((m or {}).get('id') or '').lower().startswith('global:') else 'cn'


def _alias_entries(items: list, allow: list[str]) -> list[dict]:
    """白名单里写的是**别名**时，补一条以别名为 id 的模型条目（issue #46）。

    别名（设置页「模型映射」）是给下游用的名字，它**不在上游模型清单里**。
    若只按清单字面裁剪，白名单填成别名的密钥会拿到**空列表**——而那个别名明明
    能调用（鉴权判的是请求里的名字，模型映射发生在鉴权**之后**）。空列表比不裁
    更糟：客户端会显示「没有可用模型」，用户看不出是自己把名字写成了别名。

    条目取别名所指模型的信息，只把 `id` 换成别名——**那才是客户端要发的名字**
    （发清单里的真名会被白名单拒掉）。

    解析走 `modelmap.target_for`（含兜底，issue #173）：配了兜底模型之后，白名单里
    任意名字**都调得通**，那就不该只在没有兜底时补条目 —— 否则客户端拿到的清单里
    缺了那个名字，用户会以为"白名单没生效"，而其实是清单没列。
    """
    mapping = db.get_setting('model_map', {}) or {}
    if not isinstance(mapping, dict) or not mapping:
        return []
    by_bare: dict[str, dict] = {}
    for m in items:
        if isinstance(m, dict) and m.get('id'):
            by_bare.setdefault(keysvc._bare_model(m.get('id')), m)
    out: list[dict] = []
    seen: set[str] = set()
    for raw in allow:
        name = keysvc._bare_model(raw)
        # 本身就是清单里的模型 → 上面的白名单裁剪已保留它，不重复添加
        if not name or name in seen or name in by_bare:
            continue
        target = modelmap.target_for(mapping, name)
        src = by_bare.get(keysvc._bare_model(target)) if target else None
        if not src:
            continue          # 别名指向的模型不在本版本清单里 → 调不通，不给
        out.append({**src, 'id': name})
        seen.add(name)
    return out


def _scope_models(payload: object, key: dict | None) -> object:
    """按密钥的**版本**与**模型白名单**裁剪模型列表。

    这个接口要回答的是「**你能用**什么模型」——客户端普遍拿它当模型选择器
    （issue #46）。所以两维都得裁：只裁版本会出现「列表里有、一选就失败」
    （白名单外的模型照旧列出来，选中就 400）。

    白名单判据与调用侧**共用 `keysvc.model_allowed`**，不另写一份：两处各写一遍
    迟早漂移，而漂移的表现正是这个函数要修的问题。白名单为空 = 不限制，这一维
    不裁（与调用侧语义一致）。

    结构不是预期的 `{data: [...]}` 时**原样透传**——配额耗尽之类的判断
    不该因为我们认不出结构就去改动上游的响应。
    """
    key = key or {}
    want = keysvc._norm_realm(key.get('realm'))
    allow = [str(x) for x in (key.get('models') or []) if str(x).strip()]
    if not want and not allow:
        return payload
    if not isinstance(payload, dict):
        return payload
    items = payload.get('data')
    if not isinstance(items, list):
        return payload

    in_realm = [m for m in items
                if isinstance(m, dict) and (not want or _model_realm_of(m) == want)]
    if not allow:
        return {**payload, 'data': in_realm}

    scoped = [m for m in in_realm if keysvc.model_allowed(key, m.get('id'))]
    # 别名：白名单可能写的是下游熟悉的别名，它不在清单里，要补进来
    scoped += _alias_entries(in_realm, allow)
    return {**payload, 'data': scoped}


def _listing_compat_models(payload: object) -> object:
    """补齐第三方模型目录客户端要读的能力字段（issue #211）。

    上游从 #156 起在它自己的 `/v1/models` 里补了 `modalities` /
    `supported_reasoning_levels`。但面板是**独立进程**，随包分发的上游可能比面板
    旧（补丁只打在上游），于是下游经面板拿到的清单仍缺字段 —— 报告人的 Magpie
    就是这样整条跳过模型、看不到多模态与推理档位的。

    这里在面板侧再补一遍，口径与上游的 `addListingCompat` 一致，且**只增不改**：

      · `modalities`：缺字段时补 `{"input": ["text"] + (["image"] if
        supports_images)}`。文本是所有模型都有的能力；缺整个字段会让客户端
        直接跳过（这正是 #211 的现场）；
      · `supported_reasoning_levels`：缺字段且有 `reasoning_supported_efforts`
        时派生成 `[{"effort": e}]`；没有档位就不编造；
      · `supports_reasoning`：有档位而字段缺失时补 `true`。

    认不出的结构（不是 `{data:[...]}`）原样透传 —— 不在看不懂的响应上动手脚。
    """
    if not isinstance(payload, dict):
        return payload
    items = payload.get('data')
    if not isinstance(items, list):
        return payload
    out: list = []
    for m in items:
        if not isinstance(m, dict) or not m.get('id'):
            out.append(m)
            continue
        entry = dict(m)
        if 'modalities' not in entry:
            inputs = ['text']
            if entry.get('supports_images') is True:
                inputs.append('image')
            entry['modalities'] = {'input': inputs}
        efforts = entry.get('reasoning_supported_efforts')
        if 'supported_reasoning_levels' not in entry and isinstance(efforts, list):
            levels = [{'effort': e} for e in efforts if isinstance(e, str) and e]
            if levels:
                entry['supported_reasoning_levels'] = levels
                entry.setdefault('supports_reasoning', True)
        out.append(entry)
    return {**payload, 'data': out}


# ── 对话补全（v1 / v2）──────────────────────────────────
async def _chat(request: Request, upstream_path: str):
    body, err = await _read_json_body(request)
    if err:
        return err

    requested_model = body.get('model') if isinstance(body, dict) else None
    # model 必须是字符串：非字符串（对象/数组/数字）会让后面的 _map_model 与
    # 上游处理出现意外行为（历史上有过 dict 触发 dict.get 未哈希 → 500）。
    # 这里直接拒掉，也顺带让模型白名单的判定有确定的输入。
    if requested_model is not None and not isinstance(requested_model, str):
        return _oai_error('model 必须是字符串', 400, 'invalid_request_error', 'invalid_model')
    # 映射**先于鉴权**：版本归属判的是实际要用的名字（issue #47），配了
    # 「别名 → 带前缀的真名」的密钥此前会被 realm 检查当成错版本打回。
    mapped = _map_model(requested_model)
    key, ip, err = _authorize(request, requested_model, mapped=mapped)
    if err:
        return err

    if mapped:
        body['model'] = mapped

    # 越界的 max_tokens 会被上游包装成 503（见 clamp_max_tokens 的说明），
    # 鉴权后、转发前压回上限。
    clamp_max_tokens(body)

    stream = bool(isinstance(body, dict) and body.get('stream'))
    if stream:
        # 让上游在最后一个 chunk 返回 usage，便于精确计量
        body.setdefault('stream_options', {})
        if isinstance(body['stream_options'], dict):
            body['stream_options'].setdefault('include_usage', True)

    ua = request.headers.get('user-agent')
    upstream, upstream_err = _upstream_for(key, ip=ip, model=requested_model or '',
                                           mapped=mapped or '', ua=ua)
    if upstream_err:
        return upstream_err
    url = f'{upstream["base_url"]}{upstream_path}'
    started = time.time()

    if not stream:
        try:
            async with config.http_client(config.UPSTREAM_TIMEOUT, connect=5) as client:
                resp = await client.post(url, json=body, headers=_upstream_headers(upstream))
            latency = int((time.time() - started) * 1000)
            usage = {}
            try:
                data = resp.json()
                usage = data.get('usage') or {}
            except Exception:
                data = None
            pt = int(usage.get('prompt_tokens') or 0)
            ct = int(usage.get('completion_tokens') or 0)
            error = None if resp.status_code < 400 else (str(data)[:500] if data is not None else resp.text[:500])
            _record(
                key, ip, requested_model or '', mapped or '', resp.status_code, pt, ct,
                latency, ua, error, False, usage=usage,
            )
            if data is not None:
                return JSONResponse(data, status_code=resp.status_code)
            return JSONResponse({'error': {'message': resp.text[:1000], 'type': 'api_error'}}, status_code=resp.status_code)
        except Exception as exc:  # noqa: BLE001
            latency = int((time.time() - started) * 1000)
            _record(key, ip, requested_model or '', mapped or '', 502, 0, 0, latency, ua, str(exc), False)
            return _oai_error(f'上游不可用: {exc}' + _upstream_hint(upstream), 502, 'api_error', 'upstream_unavailable')

    # 流式转发
    client = config.http_client(config.UPSTREAM_TIMEOUT, connect=5)
    try:
        req = client.build_request('POST', url, json=body, headers=_upstream_headers(upstream))
        resp = await client.send(req, stream=True)
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        latency = int((time.time() - started) * 1000)
        _record(key, ip, requested_model or '', mapped or '', 502, 0, 0, latency, ua, str(exc), True)
        return _oai_error(f'上游不可用: {exc}' + _upstream_hint(upstream), 502, 'api_error', 'upstream_unavailable')

    status_code = resp.status_code
    content_type = resp.headers.get('content-type', 'text/event-stream')

    async def generator():
        usage: dict = {}
        pending = ''
        error_text: str | None = None
        # 首字延迟：只记一次，取「首个含正文的 delta」到达时刻。
        # 注意起点含建连 + 上游排队 + 模型开始思考，这正是「上游多久开始回话」。
        first_token_ms: int | None = None
        try:
            async for chunk in resp.aiter_bytes():
                if status_code >= 400:
                    pending += chunk.decode('utf-8', errors='ignore')
                    # 有数据就留一份：早先要等到 4000 字节才取，而上游的错误体
                    # 通常只有几百字节 → error_text 恒为空，**错误被静默丢弃**：
                    # 客户端看得到（原样透传），管理端日志却什么都不记，用户来问
                    # 「为什么失败」时查不到任何线索。与 anthropic 层同口径。
                    if error_text is None and pending.strip():
                        error_text = pending[:500]
                    yield chunk
                    continue
                pending += chunk.decode('utf-8', errors='ignore')
                pending, saw_content = _scan_sse(pending, usage)
                if saw_content and first_token_ms is None:
                    first_token_ms = int((time.time() - started) * 1000)
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()
            latency = int((time.time() - started) * 1000)
            pt = int(usage.get('prompt_tokens') or 0)
            ct = int(usage.get('completion_tokens') or 0)
            _record(
                key, ip, requested_model or '', mapped or '', status_code, pt, ct,
                latency, ua, error_text, True, usage=usage,
                first_token=first_token_ms,
            )

    return StreamingResponse(generator(), status_code=status_code, media_type=content_type)


@router.post('/v1/chat/completions')
async def chat_v1(request: Request):
    return await _chat(request, '/v1/chat/completions')


@router.post('/v2/chat/completions')
async def chat_v2(request: Request):
    return await _chat(request, '/v2/chat/completions')


# ── 存活探测 ─────────────────────────────────────────────
@router.get('/healthz')
async def gateway_health() -> dict:
    """未认证的存活探测。

    **只回布尔**：上游 `/healthz` 会带 total / healthy 等账号池统计，
    直接透传等于把池子规模告诉任何未认证访问者（实测线上确实返回了 total）。
    探测方只需要「上游是否可用」，不需要知道池子里有几个号。
    """
    try:
        async with config.http_client(5, connect=2) as client:
            resp = await client.get(f'{config.WB2API_BASE}/healthz')
        return {'service': 'workbuddy-manager', 'upstream_ok': resp.status_code == 200}
    except Exception:  # noqa: BLE001
        # 不回异常详情：那会暴露上游地址与网络拓扑
        return {'service': 'workbuddy-manager', 'upstream_ok': False}
