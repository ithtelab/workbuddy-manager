"""上游网页「布局变化」监控 → LLM 判读 → 邮件上报。

## 为什么需要它

管理端对上游的依赖不止接口：活动入口、补偿公告、领取按钮都长在腾讯的网页里
（如 https://www.workbuddy.cn/profile/plans-usage ）。腾讯改版时**接口常常照旧**，
但页面结构、文案与入口位置会变——于是出现两类静默失效：

  · 原来的签名/领取逻辑仍然能跑，但**新活动**没人知道（页面上多了个「领取」）；
  · 我们依赖的某个 DOM 结构或接口路径被改掉，抓取逻辑看起来正常、实际抓空。

所以这里做的是「结构指纹」而不是「内容比对」：抓取目标页 → 归一化成
**结构行 + 可见文本行** → 存指纹；下次抓取指纹不同就认为布局变了，再把
差异摘要交给 LLM 判读「这是不是一次值得处理的变化、有没有新的可领积分」，
最后按配置发邮件。LLM 不是必须的：没配就退化成关键词规则，仍能告警。

## 与「签到/补偿」的关系

布局变化**不等于**有积分可领。本模块在检测到变化后会：

  1. 用 `tencent.compensation_status` 逐账号查一次「活动补偿」状态（**只读**，
     不发领取请求）——回答「现在到底有没有得领」；
  2. 可选（`auto_claim=true`）在确实有可领时调用现有的
     `dailyplan.run_task('compensation')` 领一次（幂等，有则领、无则空）。

把「页面变了」和「有积分」分开报告，避免每次改版都触发一次无谓的写操作。

## 邮件出口预检（这是「能不能启动」的硬条件）

很多部署环境（云主机、容器、办公网）**封了 25/465/587 出站**，此时「邮件通知
已开启」只是自欺欺人。因此：

  · 保存配置时若 `mail.enabled=true`，先做一次 SMTP 出口探测（DNS + TCP +
    等服务器 banner）；探不通就**不启用**并把原因回给调用方；
  · 每次真正发信前再探一次（结果缓存 10 分钟），探不通就跳过发信并记在
    `state.egress` 里，界面上能看到「不是没变化，是邮件出不去」。

出口探测只做 SMTP 所需的最小动作：TCP 连上 + 读到 220 banner 即认为出口可用；
不强求登录（很多服务器在 EHLO 前就允许 banner，而登录失败属于凭据问题，
与「出口是否被封」是两件事，要分开报告）。

## 安全边界

  · 目标 URL 只允许 http/https，默认拒绝解析到内网/回环/链路本地地址的域名
    （SSRF：监控目标由管理员填，但一台被拿下的面板不该能探内网）；
  · 邮件密码 / LLM API Key / AstrBot 密钥读取时打码，保存时回传
    `********` 表示「不改」；
  · 所有写操作（改配置、立即运行、发测试邮件 / QQ 消息）都是管理员接口并写
    审计日志。
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import ipaddress
import json
import logging
import re
import smtplib
import socket
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .. import config, db, upstreamsvc
from . import qq as qqsvc
from . import tencent, wb2api
from .realm import realm_of

logger = logging.getLogger(__name__)

CONFIG_KEY = 'watchdog_config'
STATE_KEY = 'watchdog_state'

# 巡检间隔的下限：比 5 分钟更密没有意义（上游改版不是分钟级的），
# 而且每次探测都是对腾讯网站的请求，过密容易被当成爬虫。
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 7 * 24 * 60

# 单次抓取的响应体上限。活动页通常 <300KB；设上限是为了防止目标返回
# 一个巨大的 bundle 把内存与 diff 都拖垮（也避免把整份 JS 存进快照）。
MAX_FETCH_BYTES = 4 * 1024 * 1024
# 快照里保留的归一化行数上限（超出部分截断，指纹仍按全量算）
MAX_SNAPSHOT_LINES = 8000
# 每个资源最多保留多少行（bundle 里的字符串可能上万条，截断后仍够 LLM 看）
MAX_ASSET_LINES = 2000

# 出口探测的端口：按「现代邮件客户端」的常见顺序，先测用户配置的端口。
_COMMON_SMTP_PORTS = (587, 465, 25)

# 关键词规则（LLM 不可用时的退化路径）：命中「领取/活动/积分」这类词时，
# 即使判读不了也要告警，而不是静默。规则只决定「值不值得看一眼」，
# 不决定「能不能领」——后者由 compensation_status 的真实回执回答。
_KEYWORDS = (
    '领取', '领取奖励', '活动', '奖励', '积分', '补偿', '签到', '礼包',
    '兑换', '任务', '成长', '限时', '福利', '红包',
    'claim', 'reward', 'credit', 'compensation', 'activity', 'coupon',
    'checkin', 'check-in', 'promotion', 'bonus',
)

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/124.0 Safari/537.36 WorkBuddy-Watchdog/1.0')


# ── 配置 ────────────────────────────────────────────────────────────────
def _default_config() -> dict:
    return {
        'enabled': False,
        'interval_minutes': 360,
        # 只监控**公开可达**的页面：活动页在未登录时通常落到登录页，但登录页
        # 的布局同样会随改版变化，且改版往往同时影响活动页——这正是要抓的信号。
        'targets': [
            {'name': 'plans-usage', 'url': 'https://www.workbuddy.cn/profile/plans-usage',
             'kind': 'assets', 'enabled': True},
            {'name': 'codebuddy-home', 'url': 'https://www.codebuddy.cn/',
             'kind': 'html', 'enabled': False},
        ],
        'timeout_seconds': 20,
        # kind=assets 时最多抓几个静态资源（0 = 只比页面骨架，不下载 bundle）
        'asset_limit': 4,
        'notify_on': 'change',   # change | claimable | anomaly
        'scan_compensation': True,
        'auto_claim': False,     # 只有 scan 明确「有可领」且这里开着才真领
        'allow_private_targets': False,
        'llm': {
            'enabled': False,
            'base_url': '',      # 例：https://wb.kuruomi.cloud/v1
            'api_key': '',
            'model': '',
            'timeout_seconds': 60,
        },
        'mail': {
            'enabled': False,
            'host': '',
            'port': 465,
            'security': 'ssl',   # ssl | starttls | none
            'username': '',
            'password': '',
            'from': '',
            'to': [],
            'subject_prefix': '[WorkBuddy 监控]',
        },
        # QQ / AstrBot：管理端只负责 HTTP 调用，QQ 官方机器人的 AppID、
        # IP 白名单与主动消息限制都在 AstrBot 侧（见 docs/qq-bot.md）。
        'qq': {
            'enabled': False,
            'base_url': '',      # 例：https://astrbot.example.com（自动补 /api/v1）
            'api_key': '',       # AstrBot「设置 → OpenAPI」创建的 abk_ 密钥
            'umo': '',           # 平台:消息类型:会话ID
            'timeout_seconds': 15,
        },
    }


def _merge_dict(base: dict, raw: object) -> dict:
    """把库里的值合并进默认值：缺项补默认、类型不符的项丢弃。

    为什么不直接 `{**default, **raw}`：库里的值可能被手改坏（字符串、null、
    多余键）。逐层合并能让坏值退回默认，而不是把整份配置变成不可用。
    """
    if not isinstance(raw, dict):
        return base
    for key, default in list(base.items()):
        if key not in raw:
            continue
        value = raw[key]
        if isinstance(default, dict):
            base[key] = _merge_dict(dict(default), value)
        elif isinstance(default, list):
            if isinstance(value, list):
                base[key] = value
        elif isinstance(default, bool):
            if isinstance(value, bool):
                base[key] = value
        elif isinstance(default, int):
            if isinstance(value, int) and not isinstance(value, bool):
                base[key] = value
        elif isinstance(value, str):
            base[key] = value
    return base


def load_config() -> dict:
    return _merge_dict(_default_config(), db.get_setting(CONFIG_KEY))


def _clean_targets(raw: list) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError('targets 的每一项必须是对象')
        name = str(item.get('name') or '').strip()
        url = str(item.get('url') or '').strip()
        kind = str(item.get('kind') or 'html').strip().lower()
        if not name or not url:
            raise ValueError('targets 的每一项都要有 name 与 url')
        if name in seen:
            raise ValueError(f'目标名重复：{name}')
        if kind not in ('html', 'js', 'text', 'assets'):
            raise ValueError(f'目标 {name} 的 kind 只能是 html / js / text / assets')
        seen.add(name)
        out.append({'name': name, 'url': url, 'kind': kind,
                    'enabled': bool(item.get('enabled', True))})
    if not out:
        raise ValueError('至少保留一个监控目标')
    if len(out) > 10:
        raise ValueError('监控目标最多 10 个（再多就不是"盯改版"而是爬站了）')
    return out


async def save_config(raw: object, *, actor: dict | None = None) -> dict:
    """校验并保存配置；返回 `{config, mail_egress?, warning?}`。

    **邮件启用是条件式**：`mail.enabled=true` 时必须先探通 SMTP 出口，否则
    落库时强制改成 false，并把探测结果带回去。这样“邮件通知已开启”这句话
    在任何时候都成立——不会出现配置显示开着、实际一个字节都发不出去。
    """
    if not isinstance(raw, dict):
        raise ValueError('配置必须是对象')
    current = load_config()
    # 以**当前配置**为底，而不是默认值：PUT 经常只带要改的几段（例如只改邮件），
    # 用默认值当底会把用户已保存的目标列表/LLM 配置悄悄重置回出厂值。
    merged = _merge_dict(dict(current), raw)

    # 打码值 = 保持原密码/密钥不变（前端不需要、也不应该拿到明文）
    for section, key in (('mail', 'password'), ('llm', 'api_key'), ('qq', 'api_key')):
        value = merged[section].get(key)
        if value == '********':
            merged[section][key] = current[section].get(key, '')

    merged['targets'] = _clean_targets(merged.get('targets') or [])

    interval = merged.get('interval_minutes')
    if not isinstance(interval, int) or not MIN_INTERVAL_MINUTES <= interval <= MAX_INTERVAL_MINUTES:
        raise ValueError(f'interval_minutes 必须在 {MIN_INTERVAL_MINUTES}-'
                         f'{MAX_INTERVAL_MINUTES} 之间')
    timeout = merged.get('timeout_seconds')
    if not isinstance(timeout, int) or not 5 <= timeout <= 120:
        raise ValueError('timeout_seconds 必须在 5-120 之间')
    asset_limit = merged.get('asset_limit')
    if not isinstance(asset_limit, int) or not 0 <= asset_limit <= 8:
        raise ValueError('asset_limit 必须是 0-8 的整数（0 = 不抓静态资源）')
    if merged.get('notify_on') not in ('change', 'claimable', 'anomaly'):
        raise ValueError('notify_on 只能是 change / claimable / anomaly')
    for target in merged['targets']:
        parsed = urlparse(target['url'])
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            raise ValueError(f"目标 {target['name']} 的 url 必须是 http(s) 地址")

    mail = merged['mail']
    if not isinstance(mail.get('port'), int) or not 1 <= mail['port'] <= 65535:
        raise ValueError('mail.port 必须是 1-65535 的端口')
    if mail.get('security') not in ('ssl', 'starttls', 'none'):
        raise ValueError('mail.security 只能是 ssl / starttls / none')
    to = mail.get('to')
    if isinstance(to, str):
        to = [x.strip() for x in re.split(r'[,;\s]+', to) if x.strip()]
    if not isinstance(to, list) or not all(isinstance(x, str) and '@' in x for x in to):
        raise ValueError('mail.to 必须是邮箱地址列表')
    mail['to'] = to

    llm = merged['llm']
    if llm.get('enabled'):
        if not str(llm.get('base_url') or '').strip() or not str(llm.get('model') or '').strip():
            raise ValueError('启用 LLM 时必须填 base_url 与 model')

    qq = merged['qq']
    for key in ('base_url', 'api_key', 'umo'):
        if isinstance(qq.get(key), str):
            qq[key] = qq[key].strip()
    if not isinstance(qq.get('timeout_seconds'), int) or not 3 <= qq['timeout_seconds'] <= 120:
        raise ValueError('qq.timeout_seconds 必须在 3-120 之间')
    if qq.get('enabled'):
        parsed_qq = urlparse(qq.get('base_url') or '')
        if parsed_qq.scheme not in ('http', 'https') or not parsed_qq.netloc:
            raise ValueError('启用 QQ 通知时 base_url 必须是 http(s) 绝对地址')
        if not qq.get('api_key'):
            raise ValueError('启用 QQ 通知时必须填开发者访问密钥（abk_...）')
        # UMO 是「平台:消息类型:会话ID」；不校验平台名，因为适配器 id 由用户在
        # AstrBot 里自定义（不一定叫 qq_official），校验太死反而挡住可用配置。
        if str(qq.get('umo') or '').count(':') < 2:
            raise ValueError('qq.umo 必须形如「平台:消息类型:会话ID」')

    warning = ''
    egress = None
    if mail.get('enabled'):
        if not mail.get('host') or not mail.get('from') or not mail.get('to'):
            raise ValueError('启用邮件时必须填 host / from / to')
        changed_endpoint = (mail['host'] != current['mail'].get('host')
                            or mail['port'] != current['mail'].get('port')
                            or not current['mail'].get('enabled'))
        if changed_endpoint:
            egress = await asyncio.to_thread(
                probe_smtp_egress, mail['host'], mail['port'], timeout=10)
        if egress is None:
            # 端点没变就不重复探测（每次保存都打一次腾讯/邮件商并不礼貌），
            # 用上次的结果；从没测过则视为「未验证」而不是「通过」。
            cached = (load_state().get('egress') or {})
            egress = cached if cached.get('host') == mail['host'] else {
                'ok': False, 'host': mail['host'], 'port': mail['port'],
                'ports': [], 'error': '尚未探测出口；请先执行「检测邮箱出口」',
                'checked_at': 0,
            }
        if not egress['ok']:
            merged['mail']['enabled'] = False
            warning = (f"部署环境到 {mail['host']}:{mail['port']} 的 SMTP 出口不通"
                       f"（{egress['error'] or '无响应'}），邮件通知**未启用**。"
                       '请换用放行出站的端口/服务商，或先修好网络后重试。')

    db.set_setting(CONFIG_KEY, merged)
    return {'config': public_config(merged), 'mail_egress': egress, 'warning': warning}


def public_config(cfg: dict | None = None) -> dict:
    """对外展示的配置：密码 / API Key 只回掩码（空值仍是空串，便于界面区分）。"""
    out = json.loads(json.dumps(cfg if cfg is not None else load_config()))
    out['mail']['password'] = '********' if out['mail'].get('password') else ''
    out['llm']['api_key'] = '********' if out['llm'].get('api_key') else ''
    # setdefault：兼容升级前落库、还没有 qq 段的配置，避免界面读取时 500
    qq_out = out.setdefault('qq', {})
    qq_out['api_key'] = '********' if qq_out.get('api_key') else ''
    return out


# ── 状态 ────────────────────────────────────────────────────────────────
def load_state() -> dict:
    raw = db.get_setting(STATE_KEY) or {}
    return raw if isinstance(raw, dict) else {}


def _save_state(**updates: object) -> dict:
    state = load_state()
    state.update(updates)
    db.set_setting(STATE_KEY, state)
    return state


def _snapshot_dir() -> Path:
    path = config.DATA_DIR / 'watchdog'
    path.mkdir(parents=True, exist_ok=True)
    return path


def _snapshot_path(url: str) -> Path:
    digest = hashlib.sha1(url.encode('utf-8')).hexdigest()[:16]
    return _snapshot_dir() / f'{digest}.json'


def load_snapshot(url: str) -> dict | None:
    path = _snapshot_path(url)
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _save_snapshot(url: str, digest: str, lines: list[str]) -> None:
    path = _snapshot_path(url)
    payload = {'url': url, 'digest': digest, 'fetched_at': int(time.time()),
               'lines': lines[:MAX_SNAPSHOT_LINES],
               'truncated': len(lines) > MAX_SNAPSHOT_LINES}
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)


def reset_baselines() -> int:
    """清掉全部快照（下次运行重新建立基线，不告警）。"""
    count = 0
    for path in _snapshot_dir().glob('*.json'):
        try:
            path.unlink()
            count += 1
        except OSError:
            logger.warning('删除快照失败: %s', path)
    return count


# ── 抓取与归一化 ────────────────────────────────────────────────────────
class TargetError(ValueError):
    """目标不可抓取（scheme 不对 / 解析到内网 / 响应过大）。"""


def _is_non_public(ip: ipaddress._BaseAddress) -> bool:
    """地址是否落在不该被监控的网段（含 IPv4-mapped IPv6）。"""
    mapped = getattr(ip, 'ipv4_mapped', None)
    if mapped is not None:
        return _is_non_public(mapped)
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_unspecified or ip.is_multicast)


async def _assert_public_url(url: str, *, allow_private: bool) -> None:
    """SSRF 防线：只放行 http(s)，且默认拒绝内网地址。

    解析用 `socket.getaddrinfo`（在事件循环里跑会卡住其他请求，所以丢线程池）。
    DNS 解析失败时**放行**：那是目标不可达，交给真正的请求去报错，而不是在这里
    把「临时解析失败」误判成「内网地址」。真正的防线是"解析成功且落在内网"。

    注意：这只校验**当前**这一跳。`fetch_target` 对手动跟随的每一跳都调用它，
    不能开 httpx 的自动跟随——否则公网 URL 302 到 `http://127.0.0.1/` 就能绕过。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise TargetError('只允许 http(s) 地址')
    if allow_private:
        return
    host = parsed.hostname or ''
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except OSError:
        return
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_non_public(ip):
            raise TargetError(f'目标解析到非公网地址 {addr}（如确需监控内网，'
                              '请显式打开 allow_private_targets）')


_MAX_REDIRECTS = 5


async def fetch_target(target: dict, timeout: float, *, allow_private: bool) -> str:
    """抓取一个目标；**手动跟随重定向，每一跳都重新做 SSRF 校验**。"""
    url = target['url']
    async with config.http_client(timeout, lane='watchdog') as client:
        for _ in range(_MAX_REDIRECTS + 1):
            await _assert_public_url(url, allow_private=allow_private)
            chunks: list[bytes] = []
            size = 0
            async with client.stream('GET', url, headers={'User-Agent': _UA},
                                     follow_redirects=False) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get('location')
                    if not location:
                        raise TargetError(f'HTTP {resp.status_code} 重定向缺少 Location')
                    url = urljoin(url, location)
                    continue
                if resp.status_code >= 400:
                    raise TargetError(f'HTTP {resp.status_code}')
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_FETCH_BYTES:
                        raise TargetError(f'响应超过 {MAX_FETCH_BYTES // (1024 * 1024)} MB 上限')
                    chunks.append(chunk)
                encoding = resp.encoding or 'utf-8'
            try:
                return b''.join(chunks).decode(encoding, errors='replace')
            except LookupError:
                return b''.join(chunks).decode('utf-8', errors='replace')
    raise TargetError(f'重定向超过 {_MAX_REDIRECTS} 次')


_TAG_RE = re.compile(r'<\s*([a-zA-Z][\w:-]*)((?:\s+[^<>]*?)?)\s*/?>')
_ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))')
_COMMENT_RE = re.compile(r'<!--.*?-->', re.S)
# 只清**内容**、保留标签本身：script/link 的 src 是前端构建的指纹，
# 整段删掉就连「这一版是哪个 bundle」都看不到了。
_SCRIPT_RE = re.compile(r'<(script|style)\b([^>]*)>.*?</\1\s*>', re.S | re.I)
_TEXT_RE = re.compile(r'>([^<>]+)<')
_WS_RE = re.compile(r'[\s\u00a0]+')
_CJK_RE = re.compile('[\u4e00-\u9fff]')
_ENDPOINT_RE = re.compile(r'/[\w./-]{3,120}')

# 结构行里保留哪些属性：class / id / data-* / role / aria-* 是版式的骨架；
# href / src 常带内容哈希（bundle 每次发布都变），保留反而让"改版"被误报成
# 每天一次——但它们的**存在性**仍有意义，所以只保留键名，不保留值。
_STRUCT_ATTRS = {'class', 'id', 'role', 'type', 'name', 'data-testid', 'aria-label'}
_ATTR_PRESENCE_ONLY = {'href', 'src', 'action', 'data-src'}
# 资源类属性：script/link 的 src/href 承载的就是「前端这一版是哪个构建」
_ASSET_ATTRS = {'src', 'href'}


def _norm_attr(name: str, value: str, tag: str = '') -> str:
    name = name.lower()
    if name in _ASSET_ATTRS and tag in ('script', 'link'):
        # 静态资源的**路径**要留：SPA 的页面骨架是常量，前端一发布变的就是
        # `index-B9cHY3Wf.js` 这种带构建哈希的文件名（构建号也在路径里）。
        # 只留 path、去掉 origin/query，避免 CDN 域名或跟踪参数制造噪音。
        path = urlparse(value).path
        return f'{name}={path}' if path else name
    if name in _ATTR_PRESENCE_ONLY:
        return f'{name}'
    if name.startswith('data-') or name in _STRUCT_ATTRS:
        # class 里的工具类顺序在打包时可能变，排序后再比
        if name == 'class':
            return 'class=' + ' '.join(sorted(value.split()))
        return f'{name}={value}'
    return ''


def normalize_html(text: str) -> list[str]:
    """HTML → 结构行 + 可见文本行。

    刻意**丢弃** script/style 的内容与注释：活动页的 bundle 内联脚本几乎每天
    都变（版本号、时间戳），保留它们会让指纹永远在抖，监控就退化成噪音。
    """
    body = _COMMENT_RE.sub('', text)
    body = _SCRIPT_RE.sub(
        lambda m: f'<{m.group(1)}{m.group(2)}></{m.group(1)}>', body)
    lines: list[str] = []
    for match in _TAG_RE.finditer(body):
        tag = match.group(1).lower()
        attrs = []
        for attr in _ATTR_RE.finditer(match.group(2) or ''):
            value = next((g for g in attr.groups()[2:] if g is not None), '')
            piece = _norm_attr(attr.group(1), value, tag)
            if piece:
                attrs.append(piece)
        lines.append('TAG ' + tag + (' ' + ' '.join(sorted(attrs)) if attrs else ''))
    seen_text: set[str] = set()
    for raw in _TEXT_RE.findall(body):
        line = _WS_RE.sub(' ', raw).strip()
        if len(line) < 2 or line in seen_text:
            continue
        seen_text.add(line)
        lines.append('TEXT ' + line)
    return lines


def _js_strings(text: str) -> list[str]:
    """线性扫描 JS 的字符串字面量（单引号 / 双引号 / 反引号）。

    为什么不用正则：带反向引用与惰性量词的字符串正则，在 800KB 的压缩 bundle
    上会**灾难性回溯**（实测把一次检测挂住几分钟）。手写扫描是 O(n)，且能正确
    跳过转义。
    """
    out: list[str] = []
    i, n = 0, len(text)
    quotes = ('"', "'", '`')
    backslash = chr(92)
    newline = chr(10)
    while i < n:
        ch = text[i]
        if ch not in quotes:
            i += 1
            continue
        quote = ch
        j = i + 1
        buf: list[str] = []
        closed = False
        while j < n:
            ch = text[j]
            if ch == quote:
                closed = True
                break
            if ch == backslash and j + 1 < n:
                buf.append(text[j:j + 2])  # 转义：原样保留下一个字符
                j += 2
                continue
            if ch == newline:
                break
            buf.append(ch)
            j += 1
        if closed and 4 <= len(buf) <= 200:
            out.append(''.join(buf))
        i = j + 1 if closed else j
    return out


def normalize_js(text: str) -> list[str]:
    """JS → 字符串字面量 + 端点路径（只为「改版/新活动」服务，不还原代码）。"""
    lines: set[str] = set()
    for raw in _js_strings(text):
        if _CJK_RE.search(raw) or any(k in raw.lower() for k in _KEYWORDS):
            lines.add('STR ' + _WS_RE.sub(' ', raw)[:200])
    for match in _ENDPOINT_RE.finditer(text):
        path = match.group(0)
        if path.count('/') >= 2:
            lines.add('URL ' + path)
    return sorted(lines)


def normalize_text(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.splitlines():
        line = _WS_RE.sub(' ', raw).strip()
        if line:
            out.append(line)
    return out


_ASSET_RE = re.compile(
    r'<(?:script|link)\b[^>]*(?:src|href)\s*=\s*["\']([^"\']+)["\']', re.I)
# 明显不是前端代码的资源，抓来分析只会浪费字节
_SKIP_ASSET_EXT = ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.woff',
                   '.woff2', '.ttf', '.eot', '.map')


def discover_assets(html: str, base_url: str, limit: int) -> list[str]:
    """从页面里找出前端 bundle / 样式表（保持出现顺序、去重、限个数）。

    SPA 的活动文案与「领取」按钮长在这些 bundle 里，页面 HTML 本身只有骨架。
    只取**同站或 CDN 的 script/link**，不递归（bundle 里再引 bundle 的情况极少，
    递归会让一次检测变成爬站）。
    """
    if limit <= 0:
        return []
    candidates: list[str] = []
    seen: set[str] = set()
    for raw in _ASSET_RE.findall(html):
        url = urljoin(base_url, raw.strip())
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or url in seen:
            continue
        if parsed.path.lower().endswith(_SKIP_ASSET_EXT):
            continue
        seen.add(url)
        candidates.append(url)

    def rank(url: str) -> tuple[int, int]:
        host = (urlparse(url).hostname or '').lower()
        # 自有 CDN 的构建产物排在前面：第三方脚本（jweixin 之类）与活动无关，
        # 让它占掉 asset_limit 的名额就等于没监控到前端。
        own = 0 if ('workbuddy' in host or 'codebuddy' in host) else 1
        return own, candidates.index(url)

    return sorted(candidates, key=rank)[:limit]


def asset_lines(url: str, body: str) -> list[str]:
    """把一个资源的可读信号归一化成行（JS 抽字符串/端点，CSS 只留选择器）。"""
    path = urlparse(url).path
    if path.lower().endswith('.css'):
        lines = normalize_text(body)
        # CSS 的声明值（颜色、尺寸）改版时爱变，只留选择器块更稳
        lines = [line for line in lines if line.endswith('{') or line.startswith('@')]
    else:
        lines = normalize_js(body)
    return [f'ASSET {path}'] + lines[:MAX_ASSET_LINES]


def normalize(kind: str, text: str) -> list[str]:
    if kind == 'js':
        return normalize_js(text)
    if kind == 'text':
        return normalize_text(text)
    return normalize_html(text)


def digest_of(lines: list[str]) -> str:
    return hashlib.sha256('\n'.join(lines).encode('utf-8')).hexdigest()


def keyword_hits(lines: list[str], limit: int = 40) -> list[dict]:
    """按关键词找证据行（去重、限量）。用于 LLM 不可用时的退化判读与邮件正文。"""
    hits: list[dict] = []
    seen: set[str] = set()
    for line in lines:
        low = line.lower()
        for word in _KEYWORDS:
            if word not in low:
                continue
            key = line[:160]
            if key in seen:
                break
            seen.add(key)
            hits.append({'word': word, 'line': key})
            break
        if len(hits) >= limit:
            break
    return hits


def diff_lines(old: list[str], new: list[str], limit: int = 160) -> dict:
    """归一化行的 unified diff（截断），带新增/删除计数。"""
    diff = list(difflib.unified_diff(old, new, fromfile='before', tofile='after', n=2))
    added = sum(1 for line in diff if line.startswith('+') and not line.startswith('+++'))
    removed = sum(1 for line in diff if line.startswith('-') and not line.startswith('---'))
    return {'added': added, 'removed': removed,
            'excerpt': '\n'.join(diff[:limit]),
            'truncated': len(diff) > limit}


# ── LLM 判读 ────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """你是网站改版与活动运营的监控助手。你会收到一个网页的「结构行」
（TAG 开头，class/id/data-* 等骨架属性）与「可见文本行」（TEXT 开头）的差异片段，
以及命中的关键词。请只输出一个 JSON 对象，字段如下：

{
  "layout_changed": true/false,   // 差异是否真的意味着页面结构/入口变化
  "claimable": true/false,        // 是否出现了新的「可领取积分/活动奖励」入口或公告
  "anomaly": true/false,          // 是否值得人工处理（结构变化且影响抓取或运营）
  "confidence": 0.0-1.0,
  "summary": "一句话结论（中文，<=80 字）",
  "evidence": ["支撑结论的差异行/关键词，最多 5 条"],
  "actions": ["建议动作，最多 3 条"]
}

判断规则：
- 只有文案微调（日期、数字、错别字）→ layout_changed=false；
- 出现「领取/兑换/限时/补偿/奖励」且伴随按钮或新入口 → claimable=true；
- 差异里出现新的 /billing、/activity、/reward、/compensat 等路径 → 视为活动线索；
- 拿不准时 anomaly=true、confidence 给低值，不要编造不存在的活动。"""


async def ask_llm(cfg: dict, target: dict, diff: dict, hits: list[dict]) -> dict:
    """调用 OpenAI 兼容的 chat/completions；失败时抛异常由调用方退化。"""
    base = str(cfg.get('base_url') or '').rstrip('/')
    if not base:
        raise ValueError('未配置 LLM base_url')
    if base.endswith('/v1'):
        url = base + '/chat/completions'
    else:
        url = base + '/v1/chat/completions'
    payload = {
        'model': cfg.get('model'),
        'temperature': 0.1,
        'response_format': {'type': 'json_object'},
        'messages': [
            {'role': 'system', 'content': _SYSTEM_PROMPT},
            {'role': 'user', 'content': json.dumps({
                'target': target['name'],
                'url': target['url'],
                'diff': diff,
                'keyword_hits': hits,
            }, ensure_ascii=False)},
        ],
    }
    headers = {'Authorization': f"Bearer {cfg.get('api_key')}"} if cfg.get('api_key') else {}
    timeout = cfg.get('timeout_seconds') or 60
    async with config.http_client(timeout, lane='watchdog-llm') as client:
        resp = await client.post(url, json=payload, headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(f'LLM HTTP {resp.status_code}: {resp.text[:200]}')
    data = resp.json()
    try:
        content = data['choices'][0]['message']['content']
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError('LLM 返回结构无法识别') from exc
    return _parse_verdict(content)


def _parse_verdict(content: object) -> dict:
    text = str(content or '').strip()
    # 兼容 ```json ... ``` 包裹（有的网关忽略 response_format）
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.I)
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'LLM 输出不是 JSON：{text[:120]}') from exc
    if not isinstance(raw, dict):
        raise RuntimeError('LLM 输出不是对象')
    verdict = {
        'layout_changed': bool(raw.get('layout_changed', True)),
        'claimable': bool(raw.get('claimable', False)),
        'anomaly': bool(raw.get('anomaly', True)),
        'confidence': float(raw.get('confidence') or 0),
        'summary': str(raw.get('summary') or '')[:200],
        'evidence': [str(x)[:200] for x in (raw.get('evidence') or [])][:5],
        'actions': [str(x)[:200] for x in (raw.get('actions') or [])][:3],
        'source': 'llm',
    }
    return verdict


def rule_verdict(diff: dict, hits: list[dict]) -> dict:
    """LLM 不可用时的退化判读：结构变了 + 关键词命中就告警。"""
    claim_words = {'领取', '兑换', '奖励', '补偿', '红包', 'claim', 'reward',
                   'compensation', 'coupon', 'bonus'}
    claimable = any(h['word'] in claim_words for h in hits)
    summary = (f"结构差异 {diff['added']} 增 / {diff['removed']} 删"
               + ('，命中领取类关键词' if claimable else ''))
    return {
        'layout_changed': True, 'claimable': claimable, 'anomaly': True,
        'confidence': 0.0, 'summary': summary,
        'evidence': [h['line'] for h in hits[:5]],
        'actions': ['人工打开目标页确认是否有新活动'] if claimable else [],
        'source': 'rule',
    }


# ── 活动积分扫描（只读）────────────────────────────────────────────────
def _account_groups() -> list[dict]:
    out, seen = [], set()
    for group in upstreamsvc.list_upstreams():
        raw = str(group.get('auth_dir') or '').strip()
        if not raw:
            continue
        try:
            key = str(Path(raw).resolve())
        except OSError:
            key = raw
        if key in seen:
            continue
        seen.add(key)
        out.append(group)
    return out


async def scan_compensation() -> dict:
    """逐账号查「活动补偿」状态（**只读**，不发领取请求）。

    返回 `{ok, total, claimable, already, unknown, failed, results[]}`。
    `claimable` 的判据是真实回执（未领过 + 有金额/按钮文案），不是关键词——
    关键词只能说明页面上有「领取」两个字，不能说明这个账号有得领。
    """
    sem = asyncio.Semaphore(4)
    results: list[dict] = []

    async def one(group: dict, account: dict) -> None:
        filename = str(account.get('file') or '')
        nickname = str(account.get('nickname') or '')
        base_dir = Path(group['auth_dir'])
        try:
            raw = wb2api.read_account_file(filename, base_dir)
        except Exception as exc:  # noqa: BLE001
            results.append({'file': filename, 'nickname': nickname,
                            'status': 'failed', 'message': f'读取失败: {exc}'})
            return
        auth = raw.get('auth') or {}
        token = str(auth.get('accessToken') or '')
        if not token:
            results.append({'file': filename, 'nickname': nickname,
                            'status': 'failed', 'message': '无有效 accessToken'})
            return
        self_auth = {
            'access_token': token,
            'uid': str((raw.get('account') or {}).get('uid') or account.get('uid') or ''),
            'enterprise_id': str((raw.get('account') or {}).get('enterpriseId') or ''),
            'domain': str(auth.get('domain') or ''),
            'realm': auth.get('realm'),
            'device_token': str(raw.get('device_token') or ''),
            'proxy': str(raw.get('proxy') or ''),
        }
        realm = realm_of(auth)
        async with sem:
            ok, data, message = await tencent.compensation_status(self_auth, realm)
        if not ok:
            results.append({'file': filename, 'nickname': nickname,
                            'status': 'failed', 'message': message})
            return
        if tencent.compensation_already_claimed(data):
            results.append({'file': filename, 'nickname': nickname,
                            'status': 'already', 'message': '已领过'})
            return
        credit = data.get('credit_num') if isinstance(data, dict) else None
        button = str((data or {}).get('button_text') or '')
        if (isinstance(credit, (int, float)) and not isinstance(credit, bool) and credit > 0) \
                or '领' in button:
            results.append({'file': filename, 'nickname': nickname,
                            'status': 'claimable',
                            'message': f'可领 {credit if credit is not None else ""}'
                                       + (f'（按钮：{button}）' if button else '')})
        else:
            results.append({'file': filename, 'nickname': nickname,
                            'status': 'unknown', 'message': '回执认不出是否可领'})

    tasks = []
    for group in _account_groups():
        try:
            accounts = wb2api.list_auth_accounts(Path(group['auth_dir']))
        except Exception:  # noqa: BLE001
            continue
        for account in accounts:
            tasks.append(one(group, account))
    if tasks:
        await asyncio.gather(*tasks)

    counts = {k: sum(1 for r in results if r['status'] == k)
              for k in ('claimable', 'already', 'unknown', 'failed')}
    return {'ok': counts['failed'] == 0, 'total': len(results), **counts,
            'results': results}


# ── SMTP 出口预检与发信 ─────────────────────────────────────────────────
def _probe_one(host: str, port: int, timeout: float) -> dict:
    """TCP 连上并读到 SMTP banner 才算通。

    为什么不等 EHLO 之后：不少服务器在 TLS 握手前就发 banner，读到 220 已经
    证明「这个网络能出去、对面是 SMTP」；再往下走会牵扯证书/凭据，把「出口被
    封」和「密码错了」混成一个错误，界面就分不清该改网络还是改密码。
    """
    out = {'port': port, 'ok': False, 'banner': '', 'error': ''}
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            if port == 465:
                ctx = ssl.create_default_context()
                with ctx.wrap_socket(sock, server_hostname=host) as tls:
                    banner = tls.recv(256)
            else:
                banner = sock.recv(256)
        text = banner.decode('utf-8', errors='replace').strip()
        out['banner'] = text[:120]
        out['ok'] = text.startswith('220')
        if not out['ok']:
            out['error'] = '未收到 SMTP 220 banner'
    except OSError as exc:
        out['error'] = str(exc) or exc.__class__.__name__
    return out


def probe_smtp_egress(host: str, port: int, timeout: float = 10) -> dict:
    """探测部署环境到 SMTP 的出口。

    除了用户配置的端口，还会顺带测 587/465/25（去重），这样「22 端口能出、465
    被云厂商封了」这类情况能一眼看出来该换哪个端口。`ok` 只代表**配置的端口**
    通过；其它端口只作诊断信息。
    """
    host = str(host or '').strip()
    if not host:
        return {'ok': False, 'host': '', 'checked_at': int(time.time()),
                'ports': [], 'error': '未配置 SMTP 主机'}
    ports = [port] + [p for p in _COMMON_SMTP_PORTS if p != port]
    results = [_probe_one(host, p, timeout) for p in ports]
    primary = results[0]
    return {
        'ok': bool(primary['ok']),
        'host': host,
        'port': port,
        'banner': primary['banner'],
        'error': primary['error'],
        'ports': results,
        'checked_at': int(time.time()),
    }


def _send_mail_sync(mail: dict, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg['Subject'] = f"{mail.get('subject_prefix') or ''} {subject}".strip()
    msg['From'] = formataddr(('WorkBuddy 监控', mail['from']))
    msg['To'] = ', '.join(mail['to'])
    msg['Date'] = formatdate(localtime=True)
    msg['Message-ID'] = make_msgid(domain='workbuddy.local')
    msg.set_content(body, subtype='plain', charset='utf-8')

    host, port = mail['host'], int(mail['port'])
    security = mail.get('security') or 'ssl'
    timeout = 20
    if security == 'ssl':
        client = smtplib.SMTP_SSL(host, port, timeout=timeout,
                                  context=ssl.create_default_context())
    else:
        client = smtplib.SMTP(host, port, timeout=timeout)
    try:
        client.ehlo()
        if security == 'starttls':
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
        if mail.get('username'):
            client.login(mail['username'], mail.get('password') or '')
        client.send_message(msg)
    finally:
        try:
            client.quit()
        except Exception:  # noqa: BLE001
            client.close()


async def send_mail(subject: str, body: str, *, cfg: dict | None = None) -> dict:
    """发一封信。**先探出口**：探不通就不发，并返回原因。"""
    cfg = cfg or load_config()
    mail = cfg['mail']
    if not mail.get('enabled'):
        return {'ok': False, 'skipped': '邮件通知未启用'}
    if not mail.get('host') or not mail.get('to'):
        return {'ok': False, 'skipped': '邮件配置不完整'}
    cached = load_state().get('egress') or {}
    now = int(time.time())
    if (cached.get('ok') and cached.get('host') == mail['host']
            and cached.get('port') == mail['port']
            and now - int(cached.get('checked_at') or 0) < 600):
        # 10 分钟内复用上次的探测结果：一轮里多个目标/多次发信时，不必每次都
        # 连四个端口。过期或换端口则重测——出口状态是可能被中途改掉的。
        egress = cached
    else:
        egress = await asyncio.to_thread(probe_smtp_egress, mail['host'], mail['port'])
        _save_state(egress=egress)
    if not egress['ok']:
        return {'ok': False, 'skipped': 'SMTP 出口不通', 'egress': egress}
    try:
        await asyncio.to_thread(_send_mail_sync, mail, subject, body)
    except Exception as exc:  # noqa: BLE001
        logger.warning('发送监控邮件失败: %s', exc)
        return {'ok': False, 'error': f'{exc.__class__.__name__}: {exc}'}
    return {'ok': True, 'sent_at': int(time.time()), 'to': mail['to']}


async def test_mail(cfg: dict) -> dict:
    """「发送测试邮件」：先探出口，再真发一封。

    测试发信**不看 `enabled`**：用户正是在「还没开启通知」的时候需要验证主机、
    账号与收件人是否可用。开关只约束定时任务里的自动发信。
    """
    mail = {**cfg['mail'], 'enabled': True}
    cfg = {**cfg, 'mail': mail}
    egress = await asyncio.to_thread(probe_smtp_egress, mail['host'], mail['port'])
    if not egress['ok']:
        return {'ok': False, 'egress': egress,
                'message': f"SMTP 出口不通：{egress['error'] or '无响应'}"}
    subject = '测试邮件：监控通道正常'
    body = (
        '这是一封来自 WorkBuddy 管理端的测试邮件。\n\n'
        f'SMTP: {mail["host"]}:{mail["port"]}（{mail.get("security")}）\n'
        f'发送时间: {time.strftime("%Y-%m-%d %H:%M:%S")}\n'
        f'出口探测: 配置端口 {mail["port"]} 收到 banner：{egress.get("banner") or "（空）"}\n'
        '收到这封信说明部署环境允许邮件出站，后续布局变化告警会发到这个地址。\n'
    )
    result = await send_mail(subject, body, cfg=cfg)
    return {**result, 'egress': egress}


async def send_qq(text: str, *, cfg: dict | None = None) -> dict:
    """QQ 通知：把 AstrBot 的调用隔离在一个薄包装后面。

    这样 `run_once` 与测试代码都只依赖本模块，替换实现（例如将来直连别的
    推送服务）不需要改调用点；单测也只需要 mock 这一个名字。
    """
    cfg = cfg or load_config()
    return await qqsvc.send_message(cfg['qq'], text)


async def test_qq(cfg: dict) -> dict:
    """「发送测试消息」：先验密钥/scope，再真发一条。

    分两步而不是直接发：`/im/bots` 的失败能确定是「地址/密钥/scope」的问题，
    而发送失败还可能是 UMO 写错。把 stage 带回去，界面才能给出可操作的提示。
    测试发消息**不看 `enabled`**：用户正是在还没开启通知时来验证配置。
    """
    qq = {**cfg['qq'], 'enabled': True}
    bots = await qqsvc.list_bots(qq)
    if not bots.get('ok'):
        return {**bots, 'stage': 'auth'}
    text = (
        '【WorkBuddy 监控】QQ 通知通道测试\n'
        f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        '收到这条消息说明管理端 → AstrBot 的 OpenAPI 调用正常，'
        '后续页面变化告警会发到这个会话。\n'
    )
    result = await qqsvc.send_message(qq, text)
    return {**result, 'stage': 'send', 'bots': bots.get('bots') or []}


# ── 主流程 ──────────────────────────────────────────────────────────────
def _should_notify(cfg: dict, verdict: dict, scan: dict | None) -> bool:
    mode = cfg.get('notify_on') or 'change'
    if mode == 'change':
        return True
    if mode == 'claimable':
        return bool(verdict.get('claimable') or (scan and scan.get('claimable')))
    return bool(verdict.get('anomaly') or verdict.get('claimable')
                or (scan and scan.get('claimable')))


def _format_report(target: dict, diff: dict, verdict: dict, hits: list[dict],
                   scan: dict | None, claim_result: dict | None) -> tuple[str, str]:
    subject = f"检测到上游页面变化：{target['name']}"
    lines = [
        f"目标: {target['name']}",
        f"地址: {target['url']}",
        f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        '',
        f"结构差异: +{diff['added']} / -{diff['removed']}"
        + ('（片段已截断）' if diff.get('truncated') else ''),
        f"判读来源: {verdict.get('source')}；置信度: {verdict.get('confidence')}",
        f"结论: {verdict.get('summary')}",
        f"可能有可领活动: {'是' if verdict.get('claimable') else '否'}",
        '',
        '证据：',
        *[f'  - {e}' for e in (verdict.get('evidence') or [])[:5]],
        '',
        '建议：',
        *[f'  - {a}' for a in (verdict.get('actions') or [])[:3]],
    ]
    if hits:
        lines += ['', '关键词命中：',
                  *[f"  - [{h['word']}] {h['line']}" for h in hits[:10]]]
    if scan is not None:
        lines += ['', '活动补偿状态（只读扫描）：',
                  f"  可领 {scan.get('claimable', 0)} / 已领 {scan.get('already', 0)} / "
                  f"未知 {scan.get('unknown', 0)} / 失败 {scan.get('failed', 0)}"
                  f"（共 {scan.get('total', 0)} 个账号）"]
        claimable = [r for r in scan.get('results', []) if r['status'] == 'claimable']
        for row in claimable[:10]:
            lines.append(f"  - {row.get('nickname') or row.get('file')}: {row.get('message')}")
    if claim_result is not None:
        lines += ['', '自动领取结果：',
                  f"  {claim_result.get('message') or ('成功' if claim_result.get('ok') else '失败')}"]
    lines += ['', '差异片段：', diff.get('excerpt') or '（无）']
    return subject, '\n'.join(lines)


async def _check_target(cfg: dict, target: dict) -> dict:
    allow_private = bool(cfg.get('allow_private_targets'))
    timeout = cfg['timeout_seconds']
    # wait_for 是**总时长**上限：httpx 的 timeout 是单次读写的上限，连接建立后
    # 对端一点点往回挤字节时，那些超时永远不会触发。监控是定时任务，不能因为
    # 一个慢 CDN 就把整轮检测挂住。
    text = await asyncio.wait_for(
        fetch_target(target, timeout, allow_private=allow_private), timeout=timeout + 5)
    if target['kind'] == 'assets':
        # 页面骨架 + 前端 bundle 里的字符串/端点：前者看「发布没」，
        # 后者看「有没有新的活动文案/领取入口」。
        lines = normalize_html(text)
        # 整组资源共用一个预算：单个慢 CDN 不能把整轮检测拖成几分钟。
        deadline = asyncio.get_running_loop().time() + timeout * 2
        for url in discover_assets(text, target['url'], int(cfg.get('asset_limit') or 0)):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 1:
                logger.info('资源抓取预算用尽，跳过剩余资源（%s）', target['name'])
                lines.append('ASSET budget-exhausted')
                break
            try:
                body = await asyncio.wait_for(
                    fetch_target({'url': url}, timeout, allow_private=allow_private),
                    timeout=min(timeout + 5, remaining))
            except Exception as exc:  # noqa: BLE001
                logger.info('抓取资源失败（跳过） %s: %s', url, exc)
                lines.append(f'ASSET {urlparse(url).path} ERROR')
                continue
            lines.extend(asset_lines(url, body))
    else:
        lines = normalize(target['kind'], text)
    digest = digest_of(lines)
    snapshot = load_snapshot(target['url'])
    if snapshot is None:
        _save_snapshot(target['url'], digest, lines)
        return {'name': target['name'], 'url': target['url'], 'status': 'baseline_created',
                'digest': digest[:16], 'lines': len(lines)}
    if snapshot.get('digest') == digest:
        return {'name': target['name'], 'url': target['url'], 'status': 'unchanged',
                'digest': digest[:16], 'lines': len(lines)}

    old = [str(x) for x in (snapshot.get('lines') or [])]
    diff = diff_lines(old, lines)
    hits = keyword_hits(lines)
    verdict: dict
    llm_error = ''
    if cfg['llm'].get('enabled'):
        try:
            verdict = await ask_llm(cfg['llm'], target, diff, hits)
        except Exception as exc:  # noqa: BLE001
            llm_error = str(exc)
            logger.warning('LLM 判读失败，退化为规则判读: %s', exc)
            verdict = rule_verdict(diff, hits)
            verdict['llm_error'] = llm_error
    else:
        verdict = rule_verdict(diff, hits)

    _save_snapshot(target['url'], digest, lines)
    return {'name': target['name'], 'url': target['url'], 'status': 'changed',
            'digest': digest[:16], 'previous_digest': str(snapshot.get('digest'))[:16],
            'lines': len(lines), 'diff': diff, 'keywords': hits,
            'verdict': verdict}


def _state_target(result: dict) -> dict:
    """把一次抓取结果裁剪成能进 `watchdog_state` 的形状。

    为什么不原样存：`diff.excerpt` 是整段 unified diff（最多 160 行），
    `keywords` 可能上百条——两者都不该进数据库设置项（每轮都整段重写）。
    但界面要说清「变了什么、判读写的是什么」，所以保留差异的**计数**与判读的
    **结论 / 证据**，丢掉原文；单条证据与结论都截断到一句话的长度。
    """
    out = {k: v for k, v in result.items()
           if k not in ('diff', 'keywords', 'verdict')}
    diff = result.get('diff')
    if isinstance(diff, dict):
        out['diff'] = {
            'added': _as_int(diff.get('added')),
            'removed': _as_int(diff.get('removed')),
            'truncated': bool(diff.get('truncated')),
        }
    verdict = result.get('verdict')
    if isinstance(verdict, dict):
        out['verdict'] = {
            'layout_changed': bool(verdict.get('layout_changed')),
            'claimable': bool(verdict.get('claimable')),
            'anomaly': bool(verdict.get('anomaly')),
            'confidence': _as_float(verdict.get('confidence')),
            'summary': str(verdict.get('summary') or '')[:300],
            'evidence': _str_list(verdict.get('evidence'), 5),
            'actions': _str_list(verdict.get('actions'), 3),
            'source': str(verdict.get('source') or ''),
            'llm_error': str(verdict.get('llm_error') or '')[:300],
        }
    return out


def _str_list(value: object, limit: int, width: int = 200) -> list[str]:
    """只接受真正的列表；字符串 `'oops'` 被当成列表会切成 `['o','o','p']`。"""
    if not isinstance(value, (list, tuple)):
        return []
    return [str(x)[:width] for x in list(value)[:limit]]


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _as_float(value: object) -> float:
    try:
        return round(float(value), 2)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


_run_lock = asyncio.Lock()


async def run_once(trigger: str = 'manual') -> dict:
    """跑一轮：抓取 → 指纹比对 → 变化则判读 / 扫描补偿 / 发邮件。

    返回的结果会原样落进 `state.last_result`（截断到合理大小），接口直接回它。
    """
    if _run_lock.locked():
        return {'ok': False, 'skipped': '已有一次检测在运行', 'trigger': trigger}
    async with _run_lock:
        cfg = load_config()
        started = int(time.time())
        target_results: list[dict] = []
        changed: list[dict] = []
        errors: list[dict] = []
        for target in cfg['targets']:
            if not target.get('enabled'):
                continue
            try:
                result = await _check_target(cfg, target)
            except Exception as exc:  # noqa: BLE001
                logger.warning('抓取监控目标失败 %s: %s', target.get('name'), exc)
                errors.append({'name': target.get('name'), 'error': str(exc)})
                continue
            target_results.append(result)
            if result['status'] == 'changed':
                changed.append(result)

        scan = None
        claim_result = None
        mail_result = None
        if changed and cfg.get('scan_compensation'):
            try:
                scan = await scan_compensation()
            except Exception as exc:  # noqa: BLE001
                logger.warning('活动补偿扫描失败: %s', exc)
                scan = {'ok': False, 'error': str(exc)}

        if (changed and cfg.get('auto_claim') and scan
                and scan.get('claimable')):
            try:
                # 复用每日计划的实现：逐分组、幂等（有则领、无则空），
                # 与手动「领补偿」是同一条路，不另写一份协议。
                from . import dailyplan  # noqa: PLC0415
                ok, message = await dailyplan.run_task('compensation')
                claim_result = {'ok': ok, 'message': message}
            except Exception as exc:  # noqa: BLE001
                claim_result = {'ok': False, 'message': f'自动领取异常: {exc}'}

        qq_result = None
        if changed:
            for item in changed:
                if not _should_notify(cfg, item['verdict'], scan):
                    continue
                subject, body = _format_report(
                    item, item['diff'], item['verdict'], item['keywords'],
                    scan, claim_result)
                # 每个变化的目标各发一封：只发第一封会让第二个目标的变化
                # 完全没有出口（状态里有、邮件里没有），而邮件正是告警通道。
                mail_result = await send_mail(subject, body, cfg=cfg)
                # 未启用时不调用：否则每轮都会在状态里留一条「QQ 未启用」，
                # 界面会一直显示一个「QQ 未发送」的徽标，像是出了问题。
                if cfg['qq'].get('enabled'):
                    qq_result = await send_qq(f'{subject}\n\n{body}', cfg=cfg)

        state_result = {
            'trigger': trigger,
            'started_at': started,
            'finished_at': int(time.time()),
            'targets': [_state_target(r) for r in target_results],
            'changed': [r['name'] for r in changed],
            'errors': errors,
            'scan': ({k: v for k, v in scan.items() if k != 'results'}
                     if isinstance(scan, dict) else None),
            'claim': claim_result,
            'mail': mail_result,
            'qq': qq_result,
        }
        _save_state(last_run_at=state_result['finished_at'], last_result=state_result,
                    last_error='; '.join(f"{e['name']}: {e['error']}" for e in errors))
        logger.info('监控检测完成 trigger=%s changed=%d errors=%d mail=%s qq=%s',
                    trigger, len(changed), len(errors),
                    'sent' if (mail_result or {}).get('ok') else 'no',
                    'sent' if (qq_result or {}).get('ok') else 'no')
        return {'ok': not errors, **state_result}


# ── 调度器 ──────────────────────────────────────────────────────────────
_TICK_SECONDS = 60
_loop_task: asyncio.Task | None = None


def _is_due(cfg: dict, state: dict, now: int | None = None) -> bool:
    if not cfg.get('enabled'):
        return False
    now = int(time.time()) if now is None else now
    last = state.get('last_run_at') or 0
    try:
        last = int(last)
    except (TypeError, ValueError):
        last = 0
    return now - last >= int(cfg.get('interval_minutes') or 360) * 60


async def _loop() -> None:
    # 启动后等 30 秒再跑第一轮：让应用完成初始化，也避免每个进程重启都
    # 立刻打一次腾讯的页面（重启频繁时那就是在刷别人的站）。
    await asyncio.sleep(30)
    while True:
        try:
            cfg = load_config()
            if _is_due(cfg, load_state()):
                await run_once(trigger='schedule')
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception('监控调度器异常')
        await asyncio.sleep(_TICK_SECONDS)


def start_scheduler() -> None:
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    try:
        _loop_task = asyncio.get_running_loop().create_task(_loop())
    except RuntimeError:
        # 事件循环外（测试/脚本）不启动：调用方需要时直接 await run_once()
        _loop_task = None


def stop_scheduler() -> None:
    global _loop_task
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None


def status() -> dict:
    cfg = load_config()
    state = load_state()
    return {
        'config': public_config(cfg),
        'state': state,
        'due': _is_due(cfg, state),
        'next_run_at': (int(state.get('last_run_at') or 0)
                        + int(cfg.get('interval_minutes') or 360) * 60
                        if state.get('last_run_at') else None),
    }
