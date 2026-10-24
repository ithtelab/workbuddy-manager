"""腾讯 CodeBuddy 登录 / 签到协议客户端（对齐 workbuddy2api cmd/login）。

国内版与国际版共用**同一套路径**，只是 base 与 Origin/Referer/UA 随版本变；
少数接口的候选路径顺序两边相反（见 realm.billing_paths 的注释）。
本模块内所有请求都经 realm 层取端点，不再直接引用写死的域名。
"""
from __future__ import annotations

import asyncio
import calendar
import json
import os
import re
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .. import config
from .realm import (
    CN,
    GLOBAL,
    Realm,
    attribution_headers,
    billing_base,
    billing_headers,
    billing_paths,
    chat_base,
    chat_paths,
    device_token_for,
    headers as realm_headers,
    realm_of,
    resolve_realm,
    supports_checkin,
)

from .errtext import err_text
from .filelock import file_lock

class AccountExistsError(ValueError):
    """Raised when an import attempts to replace an existing account without consent."""

    def __init__(self, uid: str) -> None:
        self.uid = uid
        super().__init__('账号已存在')

# 扫码 state 缓存：state -> (登记时间, 发起时的版本)。
# 记 realm 是为了在回调时校验一致——若用户先开国内版的码、又切到国际版再轮询，
# 不校验就会把国际版的 token 写进国内版的会话流程（上游 validateRealmMatch 同此意图）。
_state_cache: dict[str, tuple[float, Realm] | tuple[float, Realm, str]] = {}

# state 的有效期（秒）。
#
# **给得宽松是刻意的**（社区反馈：手机号登录时「返回登录」出问题）：
# 腾讯那个授权页除了扫码，还支持**手机号 + 短信验证码**登录，而短信有运营商延迟、
# 用户也可能中途去翻手机 —— 全程超过 5 分钟很常见。此前这里写 300 秒，超时后前端
# 报「二维码已失效」，而用户觉得自己刚授权成功，完全对不上。
#
# 上游 workbuddy2api 对此**没有任何超时**（它的 login.sh 是手动按 y 才 poll）。
# 我们保留 TTL 只是为了不给内存留垃圾——每个条目只有几十字节，且是**人工**低频操作，
# 放宽到 15 分钟的开销可以忽略，换来的是不必让用户赶时间。
STATE_TTL = 900

# 模型目录的 v3 端点（国内版与国际版同路径，按账号切 base）。
# 它是官方客户端模型目录的第二级取数：企业端点拿不到的那几个模型只在
# /v3/config 下发——实测国际版独有的 deepseek-v4.1-flash / gpt-6-astra /
# hy4-preview-f / kimi-k2.8-preview 都从这里来（见 fetch_models 的说明）。
# 该端点有 UA 门禁：只有三段式 CLI UA（我们 _ua 的形态）能过，web UA 被 400 拒。
V3_CONFIG_PATH = '/v3/config'

# 「活动补偿」两个端点（2026-10-16 的 DeepSeek 故障补偿公告，网页入口
# https://www.workbuddy.cn/profile/plans-usage 的「领取」按钮）。
#
# **路径不带 `/v2`**，这与 daily-checkin / get-user-resource 不同 —— 那两个两种
# 前缀都通，而这两个只有非前缀形式：2026-10-16 实测
#
#     POST https://www.codebuddy.cn/billing/meter/compensation-status      → 401（存在，待鉴权）
#     POST https://www.codebuddy.cn/v2/billing/meter/compensation-status   → 404
#     POST https://www.codebuddy.cn/billing/meter/claim-compensation       → 401
#     POST https://www.codebuddy.cn/v2/billing/meter/claim-compensation    → 404
#
# 所以**不能**走 `billing_paths()`（它给国内版拼的正是 `/v2`）：那样每次都会 404，
# 而 404 的表现是「无可领补偿」，看起来跟「今天没得领」一模一样 —— 静默失效。
# 端点名与网页 bundle（assets.workbuddy.cn/web/usercenter）里的调用逐字一致。
#
# **基址用 codebuddy.cn（= desktop 客户端的 billing 基址），不是公告链接里的
# workbuddy.cn**：两条都实测存在（都是 401），但我们的账号是客户端账号，出生入死的
# 是客户端那套 Origin/UA/身份头；而该域上的领取历史有效（升级日志里出现过
# 「compensation ok (+100 credit)」）。公告页与客户端应是同一后端的不同品牌前端。
# 将来若要改基址，先确认身份头（Origin/Referer）一起改 —— 不匹配的 Origin 正是
# 风控最容易挑出来的形态。
COMPENSATION_STATUS_PATH = '/billing/meter/compensation-status'
COMPENSATION_CLAIM_PATH = '/billing/meter/claim-compensation'


def _envelope(resp: httpx.Response) -> tuple[int, Any]:
    """腾讯接口统一信封 {code, msg, data}；HTTP 4xx 也可能是业务信封。"""
    try:
        env = resp.json()
    except Exception:
        return resp.status_code, None
    if isinstance(env, dict) and 'code' in env:
        return int(env.get('code', -1)), env.get('data')
    return resp.status_code, env


def _hdr(realm: Realm, token: str | None = None,
         uid: str | None = None) -> dict:
    """该版本的通用请求头（Origin/Referer/UA 随版本变）。

    uid 非空时附账号级设备指纹头（X-Machine-ID / X-Session-ID）。
    能拿到账号 uid 的调用点都应传——那是官方客户端「每账号一台固定虚拟设备」
    的形态，缺失会被按设备指纹异常关联风控。
    """
    return realm_headers(realm, token, uid)


def _billing_hdr(realm: Realm, auth: dict | str | None = None) -> dict:
    """billing 域请求头（带身份头，对齐上游 BillingHeaders）。

    billing 域（签到 / 积分 / trial / 注册）在上游一直携带 X-User-Id 等身份头，
    我们此前只发通用头——Go 侧测试明确断言 trial 必须带 X-User-Id，签到与查
    积分同理。这里统一走 realm.billing_headers。

    auth 允许传 token 字符串（兼容既有调用），此时只有 Authorization 与
    通用头，**身份头与设备指纹头都缺失**——新调用点应尽量传完整 dict。
    """
    if isinstance(auth, str):
        return realm_headers(realm, auth)
    return billing_headers(realm, auth)


def _account_http_client(auth: dict, *, lane: str = ''):
    """账号出站客户端（按账号线路选代理）。

    `lane` 透传给 `config.http_client`，把客户端**按用途分池**（issue #161）：
    批量任务（刷积分/签到/刷新令牌）与交互操作（单号测活）各有各的连接预算，
    否则批量把池占满时，用户手点的那次测活会一直排队到 PoolTimeout。
    """
    proxy = config.account_proxy(auth)
    # lane 只在**非空**时才传：既有调用点（签到/积分/刷新…）的调用形态保持
    # `http_client(15, connect=5)` 一字不差 —— 那些路径的 mock 断言就是这么写的，
    # 多传一个空 lane 会把它们全打红，而语义上「默认车道」本来就不该有痕迹。
    extra = {'lane': lane} if lane else {}
    if proxy is None:
        return config.http_client(config.TENCENT_TIMEOUT, connect=5, **extra)
    return config.http_client(config.TENCENT_TIMEOUT, connect=5, proxy=proxy, **extra)


def _login_net_hint(proxy_name: str, exc: Exception) -> str:
    """把「扫码登录」出站的网络错误翻成可执行的中文提示（issue #194）。

    为什么这条要专门翻译：登录是唯一「用户正站在弹窗前、失败就卡死」的出站调用。
    此前 httpx 的异常直接抛到路由层，没被 `except RuntimeError` 接住 → FastAPI 兜成
    500 + 英文 `Internal Server Error`，界面上就是一句看不懂的英文，用户既不知道是
    线路问题也不知道该改哪里。报告人正是这么被卡住的：换了一条解析不了 .cn 的国家
    线路，二维码一直出不来，关掉代理才好 —— 修复前他从错误里看不出这层关系。
    """
    detail = err_text(exc)
    if proxy_name:
        return (
            f'线路「{proxy_name}」连不上腾讯登录接口：{detail}。'
            '常见原因：这条线路访问不了国内域名、需要额外认证、或已经失效 —— '
            '换一条线路，或先在弹窗里关掉它再试'
        )
    return f'连不上腾讯登录接口：{detail}。请检查这台机器的网络出口（DNS 与外网连通性）'


async def start_login(realm: Realm = CN, proxy_name: str = '') -> dict:
    """发起扫码登录。realm 决定用哪套端点与 Origin（默认国内版）。

    网络层失败统一转成 `RuntimeError`（带可执行的中文原因，见 `_login_net_hint`）：
    路由层把它转 502，界面就能显示「哪条线路连不上、该怎么办」，而不是英文 500。
    """
    try:
        async with _account_http_client({'proxy': proxy_name}) as client:
            resp = await client.post(
                f'{chat_base(realm)}/v2/plugin/auth/state',
                params={'platform': 'CLI'},
                json={},
                headers=_hdr(realm),
            )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(_login_net_hint(proxy_name, exc)) from exc
    code, data = _envelope(resp)
    if code != 0 or not data:
        raise RuntimeError(f'获取授权链接失败 code={code}')
    state = data.get('state') or ''
    if state:
        _state_cache[state] = (time.time(), realm, proxy_name)
    return {'state': state, 'authUrl': data.get('authUrl') or '', 'realm': realm, 'proxy': proxy_name}


def is_pending(state: str) -> bool:
    return state in _state_cache


def state_realm(state: str) -> Realm | None:
    """该 state 登记时用的版本；未知返回 None。"""
    entry = _state_cache.get(state)
    return entry[1] if entry else None


def drop_state(state: str) -> None:
    _state_cache.pop(state, None)


async def poll_login(state: str, realm: Realm | None = None) -> dict:
    """轮询扫码结果。waiting / expired / ready(含 token 与账号信息)。

    realm 用于校验一致性：轮询方声明的版本必须与发起时一致，
    否则返回 realm_mismatch 而不是把另一个版本的凭证混进来。
    不传 realm 时沿用登记时的版本（兼容既有调用）。
    """
    entry = _state_cache.get(state)
    if entry is None:
        return {'status': 'invalid'}
    created, reg_realm = entry[:2]
    proxy_name = entry[2] if len(entry) > 2 else ''
    if time.time() - created > STATE_TTL:
        drop_state(state)
        return {'status': 'expired'}
    if realm is not None and realm != reg_realm:
        # 不 drop：用户可能切错了版本，切回去还能继续用这张码
        return {'status': 'realm_mismatch', 'expected': reg_realm, 'got': realm}
    realm = reg_realm

    # 出站失败同样翻成可执行的中文（与 start_login 同一口径）：中途线路挂掉时，
    # 前端轮询此前会拿到英文 500，用户看不出是线路的问题。
    try:
        async with _account_http_client({'proxy': proxy_name}) as client:
            resp = await client.get(
                f'{chat_base(realm)}/v2/plugin/auth/token',
                params={'state': state},
                headers=_hdr(realm),
            )
            code, data = _envelope(resp)
            if code != 0 or not data or not data.get('accessToken'):
                return {'status': 'waiting'}

            access_token = data['accessToken']
            refresh_token = data.get('refreshToken', '')
            expires_in = int(data.get('expiresIn', 3600) or 3600)
            domain = data.get('domain', '')

            acct_resp = await client.get(
                f'{chat_base(realm)}/v2/plugin/login/account',
                params={'state': state},
                headers=_hdr(realm, access_token),
            )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(_login_net_hint(proxy_name, exc)) from exc
    _, acct = _envelope(acct_resp)
    acct = acct or {}
    uid = acct.get('uid')
    if not uid:
        return {'status': 'waiting'}

    # 这里**不** drop_state —— 刻意留给调用方在账号真正落盘之后再丢。
    #
    # 早先是先丢再返回，看起来更整洁，实际制造了一个很难查的故障（issue #26）：
    # 轮询拿到 ready 后，路由还要落盘（写 auths/）并记签到日志；若其中任何一步抛错
    # （宝塔/1Panel 部署下 auths 目录属主不对 → PermissionError 很常见），
    # 前端那次请求拿到 500、它的 catch 静默吞掉，下一轮再轮询时 state 已不在缓存里
    # → 返回 invalid → 界面显示「二维码已失效」。而**腾讯侧其实已经授权成功**，
    # 用户被引导去重新扫码，重扫还是一样 —— 因为真正的毛病是目录权限，
    # 报错信息却指向二维码。
    #
    # 语义上也不对：state 的有效期是「发码起 5 分钟」，不是「拿到 token 就走完一生」。
    # 留给路由 drop，超时兜底仍由上面的 TTL 分支负责（那才是它该管的范围）。
    return {
        'status': 'ready',
        'uid': str(uid),
        'nickname': acct.get('nickname', '') or '',
        'enterprise_id': acct.get('enterpriseId', '') or '',
        'access_token': access_token,
        'refresh_token': refresh_token,
        'expires_at': int(time.time()) + expires_in,
        'domain': domain,
        'realm': realm,
        'proxy': proxy_name,
    }


def _atomic_write_json(target: Path, payload: dict) -> None:
    """把 payload 以**原子替换**方式写到 target（临时文件 + os.replace）。

    为什么必须原子（不能直接 write_text）：上游 2026-09-18 起新增了 auths 目录
    热加载——每 5 秒轮询目录指纹（文件名 + mtime + 大小），一有变化就重新全量
    加载。而 `write_text` 是「先截断再写」，中间存在**长度为 0 的窗口**；轮询若
    正好落在那里，读到空文件 → `Parse` 失败 → 该账号被判定为「已删除」而从池里
    剔除，随后才被写回。表现是账号偶发地短时间掉线，且日志里看不出原因（大概率
    碰不上，但轮询是永久的，迟早会碰上）。

    上游自己也依赖这个前提：其 watch.go 注释写明「半写入的临时文件
    （login.sh 用 tempfile + os.replace 原子替换）不会造成误判」—— 我们的写入
    路径必须符合同一个约定。

    临时文件名以 `.` 开头且不以 `.json` 结尾：既不会被上游的 `workbuddy*.json`
    glob 收到，也不会被它的目录指纹计入（指纹只统计 `.json`）。

    **名字必须唯一**（mkstemp 的随机后缀），不能用固定的 `.workbuddy-x.json.tmp`：
    同一账号被并发写入时（批量扫码、或用户连点重试），两次写会共用同一个临时
    文件——先完成者 `os.replace` 成功后该临时文件已不存在，后完成者的 replace
    失败并触发清理，把对方刚写好的内容一并删掉。实测 4 个并发线程全部报错、
    且原账号文件消失。唯一名让每次写各自独立。

    权限**必须显式放宽**：mkstemp 在 Linux 上固定创建 0600，而这里写入的文件是
    给**上游**读的 —— 宿主部署下本面板以 root 写、上游容器以 uid 10001 读，
    0600 会让上游读不到该账号（表现为账号加进去了但池里没有）。原来的
    `write_text` 走 umask（典型 0644），这里要保持同样的可读性。

    为什么不照抄上游 SaveAtomic 的 0o600：上游是「同一个进程既写又读」，
    0600 自洽；我们是跨 uid 写读，前提不同。
    """
    original = target.stat() if target.exists() else None
    fd, tmp_name = tempfile.mkstemp(prefix=f'.{target.name}.', suffix='.tmp',
                                    dir=str(target.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=1))
            fh.flush()
            os.fsync(fh.fileno())
        if original is not None and hasattr(os, 'chown'):
            os.chown(tmp_name, original.st_uid, original.st_gid)
        os.chmod(tmp_name, (original.st_mode & 0o777) if original is not None else 0o644)
        os.replace(tmp_name, target)
    except Exception:
        # 失败时清掉临时文件，避免在 auths 目录里留垃圾（它不会被加载，但会让人困惑）
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def update_auth_tokens(filename: str, fields: dict,
                       auth_dir: Path | None = None) -> None:
    """就地更新账号文件里的 token 字段，**其余原样保留**（issue #40）。

    与 `write_auth_file` 的分工：那个是「新建/重登」，按登录响应重建整份文件；
    这个是「续期」，只动 `auth` 下几个键。**必须保留未知键**——账号文件里还有
    `device_token` 等上游写入的字段，重建式写入会把它们冲掉（device_token 是
    设备风控凭据，丢了会静默降级风控形态）。

    只接受 `auth.*` 与顶层 `device_token` 之外的键由调用方负责，这里只做：
      · 合并 `fields` 里的 accessToken / refreshToken / expiresAt / domain 到 auth；
      · 原子替换写回。

    文件不存在时抛 FileNotFoundError；解析失败时抛 ValueError（宁可不写，
    也不要把一份坏内容覆盖到用户仅存的凭证上）。
    """
    target = _safe_auth_path(filename, auth_dir)
    # 读—改—写必须串行（见 filelock.py）：否则续期与其它写者会互相覆盖。
    with file_lock(target):
        try:
            raw = json.loads(target.read_text(encoding='utf-8'))
        except FileNotFoundError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f'账号文件无法解析，已放弃写入：{err_text(exc)}') from exc
        if not isinstance(raw, dict):
            raise ValueError('账号文件格式异常，已放弃写入')

        auth = raw.get('auth')
        if not isinstance(auth, dict):
            auth = {}
            raw['auth'] = auth
        if 'access_token' in fields:
            auth['accessToken'] = fields['access_token']
        if 'refresh_token' in fields:
            auth['refreshToken'] = fields['refresh_token']
        if 'expires_at' in fields:
            auth['expiresAt'] = fields['expires_at']
        if 'domain' in fields:
            auth['domain'] = fields['domain']
        # realm 若缺失则补上（与 write_auth_file 同口径，用 resolve_realm 不含逃生门）
        if not auth.get('realm'):
            auth['realm'] = resolve_realm(raw.get('realm'),
                                          str(auth.get('domain') or ''))

        _atomic_write_json(target, raw)


def _safe_auth_path(filename: str, auth_dir: Path | None = None) -> Path:
    """把文件名解析为 auths 目录下的真实路径（与 wb2api._safe_file 同口径）。

    auth_dir 非空时按该**分组**的账号目录解析（多分组 / 账号池）；缺省仍读
    config.AUTH_DIR。
    """
    if ('/' in filename or '\\' in filename or '..' in filename
            or '\x00' in filename):
        raise ValueError('非法的文件名')
    base = filename[:-len('.disabled')] if filename.endswith('.disabled') else filename
    if not re.fullmatch(r'workbuddy[A-Za-z0-9_.-]*\.json', base):
        raise ValueError('非法的文件名')
    return (auth_dir or config.AUTH_DIR) / filename


def write_auth_file(account: dict, auth_dir: Path | None = None,
                    *, allow_overwrite: bool = True) -> tuple[str, bool]:
    """严格按 workbuddy2api 的嵌套结构落盘，返回 (文件名, 是否覆盖)。

    realm 写在 `auth` 对象内（与 domain 同级）——上游就是从这里读的。
    落盘用的是 resolve_realm（**不含逃生门**）：否则一旦 global.enabled=false，
    新登的国际版账号会被永久写成 cn（上游 BackfillRealm 注释专门警告过这点）。

    device_token 是顶层键（与上游 auth 解析位置一致）。**重新登录不能把它冲掉**：
    它是设备风控凭据，用户手动写入后若因换 token 重登而丢失，会静默降级风控
    形态——所以这里读旧文件保留，而不是当作字段缺失。
    """
    uid = str(account['uid'])
    # uid 会被拼进文件名，写入 auths 目录，所以必须先校验字符集。
    #
    # 它来自腾讯 `/v2/plugin/login/account` 的响应（`acct.get('uid')`），
    # 是**外部输入**：真实 uid 是 uuid（`9b212d8c-f5f7-...`），但没有校验时
    # `../x` 这类值会让路径拐出 auths 目录（`workbuddy-` 前缀只挡住了大部分形态，
    # 分隔符仍能生效）。同目录下 `wb2api._safe_file` 早已对**读**路径做了同样的
    # 白名单，这里把**写**路径补齐，两边口径一致。
    if not re.fullmatch(r'[0-9A-Za-z_-]{1,80}', uid):
        raise ValueError(f'账号 uid 形态异常，已拒绝写入（{uid[:40]!r}）')
    base = auth_dir or config.AUTH_DIR
    base.mkdir(parents=True, exist_ok=True)
    target = base / f'workbuddy-{uid}.json'
    disabled_target = base / f'{target.name}.disabled'
    if not target.exists() and disabled_target.exists():
        target = disabled_target
    existed = target.exists()
    if existed and not allow_overwrite:
        raise AccountExistsError(uid)
    domain = account.get('domain', '')
    resolved = resolve_realm(account.get('realm'), domain)

    # 读旧值 → 合并 → 原子写，整体串行：重登/导入不能把别人刚写的字段盖掉。
    with file_lock(target):
        # 保留旧的 device_token（若有）。读失败不影响主流程。
        old_device_token = ''
        old_proxy = ''
        old_weight = None
        if existed:
            try:
                old = json.loads(target.read_text(encoding='utf-8'))
                if isinstance(old, dict):
                    old_device_token = str(old.get('device_token') or '')
                    old_proxy = str(old.get('proxy') or '')
                    old_weight = old.get('weight')
            except Exception:  # noqa: BLE001
                old_device_token = ''

        payload = {
            'account': {
                'uid': uid,
                'enterpriseId': account.get('enterprise_id', ''),
                'nickname': account.get('nickname', ''),
            },
            'auth': {
                'accessToken': account['access_token'],
                'refreshToken': account.get('refresh_token', ''),
                'expiresAt': account['expires_at'],
                'domain': domain,
                'realm': resolved,
            },
        }
        if old_device_token:
            payload['device_token'] = old_device_token

        # 空串同样回退到旧值（`or old_proxy`，而不是只对「键缺失」生效的默认值）：
        # 登录流（account 里通常没有这个键）与**导入流**（`_uploaded_account` 恒带上
        # `'proxy': ''`）两条路都要「文件没提就不动」。否则覆盖导入一份没写 proxy 的
        # 文件会把已绑定的线路**静默清掉**——出口无声改变正是这个功能最忌讳的事
        # （与上面 device_token「读旧文件保留」的语义对齐）。
        proxy_name = str(account.get('proxy') or old_proxy or '').strip()
        if proxy_name:
            payload['proxy'] = proxy_name

        # 手写权重同 device_token / proxy：文件没提就沿用旧值。
        # 重新登录/覆盖导入是「按响应重建整份文件」，不读旧值就会把
        # 用户设的权重静默清零（#187 的同一条坑）。
        weight = account.get('weight', old_weight)
        if (isinstance(weight, (int, float)) and not isinstance(weight, bool)
                and weight > 0):
            payload['weight'] = weight

        # 原子替换的完整理由见 `_atomic_write_json`（热加载读到空文件会误判账号被删）
        _atomic_write_json(target, payload)
    return target.name, existed


async def refresh_token(auth: dict) -> tuple[bool, str, dict]:
    """用 refreshToken 换新的 accessToken（issue #40）。

    返回 `(是否成功, 说明, 新字段)`；成功时 `新字段` 含
    `access_token` / `refresh_token` / `expires_at`（后两者可能缺省 = 上游没返回、
    保持旧值）。

    ## 为什么管理端要自己实现这一步

    上游 workbuddy2api **有**刷新能力（`internal/upstream/client.go` 的
    `RefreshToken`），但它**没有任何对外接口**——其路由表里只有
    `/v1/chat/completions`、`/v1/models`、`/status`、`/v1/stats` 与三个
    `/admin/accounts/...`（后者还默认关闭）。刷新只发生在三个**内部**时机：

      · 保活排程（`schedule.keepalive_hours`，默认每天 22 点一次）；
      · 每次 chat 选号后、token 距到期不足 `RefreshSkew`（默认 10 分钟）时；
      · 签到前 token 临近过期时。

    所以「点了刷新按钮却没续期」不是偶然而是**必然**：面板此前那个按钮只是
    触发一次上游重载（`reload.restart_now()`），而重载既不续期、也不改变任何
    token。用户报的 issue #40 正是这个——他期待「刷新」能做它字面承诺的事。

    ## 协议

    `POST {chat_base}/v2/plugin/auth/token/refresh`，refreshToken 走
    **`X-Refresh-Token` 头**（不是 body），响应 `{accessToken, refreshToken,
    expiresIn, domain}`。与上游 `RefreshHeaders` 对齐：
    `X-Auth-Refresh-Source: plugin` 是官方客户端的刷新渠道标识，缺了可能被风控
    当异常来源（上游 D3 实测）。
    """
    refresh = str(auth.get('refresh_token') or '').strip()
    if not refresh:
        return False, '该账号没有 refreshToken（只能重新扫码或登录）', {}

    realm = realm_of(auth)
    uid = str(auth.get('uid') or '')
    enterprise_id = str(auth.get('enterprise_id') or '')
    # 刷新带的是旧 accessToken（上游同样用刷新前的值构造头）
    headers = _hdr(realm, str(auth.get('access_token') or ''), uid)
    headers['X-Refresh-Token'] = refresh
    headers['X-Auth-Refresh-Source'] = 'plugin'
    if enterprise_id:
        headers['X-Enterprise-Id'] = enterprise_id
    if uid:
        headers['X-User-Id'] = uid
    _dt = device_token_for(auth)
    if _dt:
        headers['X-Device-Token'] = _dt

    url = f'{chat_base(realm)}/v2/plugin/auth/token/refresh'
    try:
        async with _account_http_client(auth) as client:
            resp = await client.post(url, headers=headers)
    except Exception as exc:  # noqa: BLE001
        return False, f'刷新异常: {err_text(exc)}', {}

    if resp.status_code >= 400:
        # 12153 / session dead 的典型表现：refreshToken 也失效了，只能重新登录。
        # 说清楚「该重新扫码」比让用户反复点刷新有用。
        return False, (f'刷新失败（HTTP {resp.status_code}）——'
                       '若反复失败说明登录态已失效，需重新扫码或登录'), {}

    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return False, '刷新响应不是 JSON', {}

    # 上游可能把结果包在 data 里（与登录接口同风格），两种形态都认
    body = (data.get('data') if isinstance(data, dict)
            and isinstance(data.get('data'), dict) else data)
    if not isinstance(body, dict):
        return False, '刷新响应格式异常', {}
    new_access = str(body.get('accessToken') or '')
    if not new_access:
        return False, '刷新响应里没有 accessToken —— 需重新扫码或登录', {}

    out: dict = {'access_token': new_access}
    new_refresh = str(body.get('refreshToken') or '')
    if new_refresh:
        out['refresh_token'] = new_refresh
    # expiresIn 缺省时**保留旧到期时间**（与上游 preserveExpiry 同口径）：
    # 拿不到就不要乱写，否则会把有效期改成错的值、让面板显示误导信息。
    expires_in = body.get('expiresIn')
    if isinstance(expires_in, bool):
        expires_in = None
    if isinstance(expires_in, (int, float)) and 0 < expires_in < 10 * 365 * 86400:
        out['expires_at'] = int(time.time() + expires_in)
    if isinstance(body.get('domain'), str) and body['domain']:
        out['domain'] = body['domain']

    when = out.get('expires_at')
    if when:
        left = max(0, when - int(time.time()))
        return True, f'刷新成功，新令牌有效期 {left // 86400} 天', out
    return True, '刷新成功（上游未返回有效期，沿用原值）', out


async def checkin(access_token: str | dict, realm: Realm = CN) -> tuple[int, str]:
    """每日签到。10001 = 今日已签到，属正常幂等。

    **国际版没有签到体系**：上游调度器对 global 账号直接过滤、不发起任何请求
    （理由是避免风控）。这里同样直接返回、不打接口——否则会白吃一个 4xx，
    还可能被当成异常行为。

    access_token 可传字符串（只有 Authorization）或完整 auth dict
    （额外带上 X-User-Id 等身份头——上游 billing 域一直这么做，见 BillingHeaders）。
    """
    if not supports_checkin(realm):
        return -2, '国际版无签到体系，已跳过'
    try:
        async with _account_http_client(access_token if isinstance(access_token, dict) else {}) as client:
            resp = await client.post(
                f'{billing_base(realm)}{billing_paths(realm, "daily-checkin")[0]}',
                json={},
                headers=_billing_hdr(realm, access_token),
            )
        code, _ = _envelope(resp)
        if code == 0:
            return 0, '签到成功'
        if code == 10001:
            return 10001, '今日已签到'
        return code, f'签到返回 code={code}'
    except Exception as exc:  # noqa: BLE001
        return -1, f'签到异常: {err_text(exc)}'


async def compensation_status(access_token: str | dict,
                              realm: Realm = CN) -> tuple[bool, dict, str]:
    """查询「活动补偿」当前状态（有没有得领、领过没）。

    页面口径（`/profile/plans-usage` 的「领取」模块，2026-10 补偿公告）读的字段：
    `claimed` / `claimed_at` / `claimed_text` / `button_text` / `credit_num` / `title`。
    返回 `(ok, data, message)`；`ok=False` 时 message 是原因（网络/业务错误）。

    **只在官方网页上才有的可领状态**：这个端点比「直接领」多一次请求，但它能回答
    「有得领吗」，而领取接口在没得领时只回业务错误——用户看到「无可领」无法区分
    「已经领过了」和「活动还没给我」。
    """
    try:
        async with _account_http_client(access_token if isinstance(access_token, dict) else {}) as client:
            resp = await client.post(
                f'{billing_base(realm)}{COMPENSATION_STATUS_PATH}',
                json={},
                headers=_billing_hdr(realm, access_token),
            )
        code, data = _envelope(resp)
        if code != 0:
            return False, {}, f'查询返回 code={code}'
        if not isinstance(data, dict):
            return False, {}, '查询返回结构无法识别'
        return True, data, '已查询'
    except Exception as exc:  # noqa: BLE001
        return False, {}, f'查询异常: {err_text(exc)}'


async def claim_compensation(access_token: str | dict,
                             realm: Realm = CN) -> tuple[str, int, str]:
    """领取「活动补偿」（有则领，领过/没有则跳过）。

    返回 `(status, credit, message)`，status 只有四种取值 —— **分开「无可领」与
    「失败」是刻意的**：上游对「没有可领的补偿」和「已经领过了」都回业务错误，
    而这两种都不是故障。混成一个 ok=False，界面会把「今天本来就没得领」报成
    「N 个账号失败」，用户于是反复重试一个永远不会成功的动作。

      · `claimed` —— 本次到账（credit 是积分，回执缺字段记 0）
      · `nothing` —— 业务错误：没有可领的补偿 / 已经领过
      · `failed`  —— 网络或协议层失败（可重试）
      · `skipped` —— 国际版没有这个活动，未发请求
    """
    if not supports_checkin(realm):
        # 公告只面向国内版（原话「面向 WorkBuddy 国内版全量用户」）；国际版连这个
        # 模块都没有，打过去只会白吃一次 4xx —— 与签到的门控同理。
        return 'skipped', 0, '国际版无此活动，已跳过'
    try:
        async with _account_http_client(access_token if isinstance(access_token, dict) else {}) as client:
            resp = await client.post(
                f'{billing_base(realm)}{COMPENSATION_CLAIM_PATH}',
                json={},
                headers=_billing_hdr(realm, access_token),
            )
        code, data = _envelope(resp)
        if code == 0:
            credit = 0
            if isinstance(data, dict):
                raw = data.get('credit')
                if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                    credit = int(raw)
            return 'claimed', credit, ('已领取 +%d 积分' % credit) if credit else '已领取（回执未给积分数）'
        # 业务错误：**不是**故障（HTTP 多半还是 200，只是信封里的 code != 0）
        return 'nothing', 0, f'无可领补偿（code={code}）'
    except Exception as exc:  # noqa: BLE001
        return 'failed', 0, f'领取异常: {err_text(exc)}'


def compensation_already_claimed(data: object) -> bool:
    """补偿状态回执是否表示「已经领过」。

    字段按网页 bundle 的口径解析，且**宽容**：三个字段任一能证明领过就算领过
    （`claimed` 布尔 / `claimed_at` 时间 / `claimed_text` 文案）。字段全缺时返回
    False —— 那表示我们认不出这个结构，此时应继续尝试领取，而不是当成「已领」
    把机会放过（认不出 ≠ 领过了）。
    """
    if not isinstance(data, dict):
        return False
    if data.get('claimed') is True:
        return True
    for key in ('claimed_at', 'claimed_text'):
        value = data.get(key)
        if value not in (None, '', 0):
            return True
    return False


async def fetch_credits(auth: dict) -> tuple[bool, int | float | None, str, list[dict]]:
    """查询账号**实时**积分余额与各套餐到期时间。

    为什么必须由管理端自己查：workbuddy2api 只在它的定时任务
    （签到 / 保活时刻）刷新 credits，之后 /status 里一直是旧值；
    手动签到也不会触发它刷新。因此要拿到当前余额只能直接调腾讯接口。

    口径与上游保持一致：优先取套餐的 CycleCapacityRemain，
    无 Cycle 字段时退回 CapacityRemain（见 upstream.packageRemainUsed）。
    billing 路径按版本分派（国际版无 /v2 前缀优先，与国内版相反）。
    返回 (ok, credits, message, expiries)。

    expiries 是 [{'at': epoch 秒, 'amount': 额度}]，按到期时间升序，**只含仍有
    余额的套餐**（余额为 0 的套餐到期与否不影响任何决策）。上游只用它算一个
    「快过期总额」的数字、不下发到期时刻，所以倒计时只能我们自己在这里取。
    """
    access_token = str(auth.get('access_token') or '')
    if not access_token:
        return False, None, '该账号无有效 accessToken', []
    realm = realm_of(auth)

    now = time.time()
    # 时间窗用本机时区格式化，**照抄上游**（其 getUserResourceBody 用 now.Format）。
    # 这与下面解析 CycleEndTime 时固定 UTC+8 不一致，看着像 bug，其实是上游原样：
    # 这个窗口只是「往后 101 年、从今天起」的粗过滤，8 小时偏移不会漏掉任何套餐
    # （窗口边界离真实到期时间有 101 年的余量）。而解析出来的到期时刻要展示给
    # 用户、还要跟腾讯官网对账，那里的 8 小时偏移是看得见的错误 —— 两处要求不同，
    # 因此口径也就不同。不要「顺手统一」，那会偏离上游行为。
    body = {
        'PageNumber': 1,
        'PageSize': 100,
        'ProductCode': 'p_tcaca',
        'Status': [0, 3],
        'PackageEndTimeRangeBegin': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)),
        'PackageEndTimeRangeEnd': time.strftime(
            '%Y-%m-%d %H:%M:%S', time.localtime(now + 365 * 101 * 24 * 3600)
        ),
    }
    enterprise_id = str(auth.get('enterprise_id') or '').strip()
    try:
        # 头走 billing 域（带 X-User-Id / X-Enterprise-Id / X-Tenant-Id 身份头，
        # 对齐上游 BillingHeaders）。企业与个人两条路共用同一份头。
        hdr = _billing_hdr(realm, auth)

        # ── 企业账号：额度不在个人「资源包」体系内 ──────────────
        # 个人端点 get-user-resource 对企业账号恒返回空 Accounts（我校的
        # _extract_resource_accounts 解析不出来 → "响应结构无法识别" → 显示 0）。
        # 企业额度必须走专用端点 get-enterprise-user-usage。
        #
        # 口径差异（与个人接口**相反**，极易搞错）：
        #   个人：CapacityRemain 就是「剩余」
        #   企业：credit 是**本周期已用**，limitNum 是**分配给该账号的额度**
        #        → remain = limitNum - credit
        #   limitNum == -1 表示不限量
        if enterprise_id:
            return await _fetch_enterprise_credits(realm, auth, hdr)

        resp = None
        async with _account_http_client(auth) as client:
            for path in billing_paths(realm, 'user-resource'):
                resp = await client.post(
                    f'{billing_base(realm)}{path}', json=body, headers=hdr
                )
                if resp.status_code != 404:
                    break
        assert resp is not None
        code, data = _envelope(resp)
        if code != 0 or not data:
            return False, None, f'查询失败 code={code}', []

        accounts = _extract_resource_accounts(data)
        if accounts is None:
            return False, None, '响应结构无法识别', []

        total = 0.0
        expiries: list[dict] = []
        for item in accounts:
            if not isinstance(item, dict):
                continue
            # 逐个套餐钳负值**再**累加，与上游同序（其 `if r < 0 { r = 0 }` 在
            # `remain += r` 之前）。顺序不能换：先求和再钳的话，一个 -100 的坏
            # 套餐会从合计里扣掉 100，而上游只把它当 0 —— 同一个账号我们显示的
            # 余额会比上游少，用户对不上账。
            remain = max(0.0, _package_remain(item))
            total += remain
            at = _package_expiry(item) if remain > 0 else None
            if at is not None:
                expiries.append({'at': at, 'amount': _round_credits(remain)})
        expiries.sort(key=lambda e: e['at'])
        return True, _round_credits(total), '查询成功', expiries
    except Exception as exc:  # noqa: BLE001
        return False, None, f'查询异常: {err_text(exc)}', []



def _enterprise_cycle_expiry(data: dict) -> int | None:
    """解析企业额度的周期结束时间（cycleEndTime，UTC+8 墙钟字符串）。

    上游格式 "2026-11-13 23:59:59"，与个人套餐 CycleEndTime 同口径。
    解析失败返回 None —— 调用方据此跳过 expiries，不让倒计时显示错时间。
    """
    raw = str(data.get('cycleEndTime') or '').strip()
    if not raw:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            st = time.strptime(raw, fmt)
        except (ValueError, OverflowError):
            continue
        # 上游给的是 UTC+8 墙钟。calendar.timegm 按 UTC 解释 st，
        # 减 8 小时即得到该墙钟对应的真实 epoch。
        return int(calendar.timegm(st) - 8 * 3600)
    return None


async def _fetch_enterprise_credits(
    realm: Realm, auth: dict, hdr: dict
) -> tuple[bool, int | float | None, str, list[dict]]:
    """企业账号额度查询（get-enterprise-user-usage）。

    返回与 fetch_credits 同结构：(ok, credits, message, expiries)。
    口径：上游 credit 是**已用**、limitNum 是**配额**，
    故 remain = limitNum - credit（与个人接口语义相反）。
    """
    path = '/v2/billing/meter/get-enterprise-user-usage'
    try:
        async with _account_http_client(auth) as client:
            resp = await client.post(
                f'{billing_base(realm)}{path}', json={}, headers=hdr
            )
        code, data = _envelope(resp)
        if code != 0 or not isinstance(data, dict):
            return False, None, f'企业额度查询失败 code={code}', []

        raw_limit = data.get('limitNum')
        limit = float(raw_limit) if raw_limit is not None else 0.0
        if limit < 0:
            return True, None, '企业额度不限量', []

        used = float(data.get('credit') or 0.0)
        remain = max(0.0, limit - used)
        credits = _round_credits(remain)

        expiries: list[dict] = []
        at = _enterprise_cycle_expiry(data)
        if at is not None and remain > 0:
            expiries.append({'at': at, 'amount': credits})
        return True, credits, '查询成功（企业额度）', expiries
    except Exception as exc:  # noqa: BLE001
        return False, None, f'企业额度查询异常: {err_text(exc)}', []


async def fetch_models(auth: dict) -> tuple[bool, list | str]:
    """拉取该账号可用的 CLI 模型（含显示名、上下文、最大输出、推理档位）。

    为什么要管理端自己拉、而不是用上游的 /v1/models：上游把腾讯返回的
    `name`（显示名）和 `reasoning.supportedEfforts`（推理档位）**丢掉了**，
    只暴露 id / context_length / max_output_tokens。要做「模型中心」这类
    带显示名与能力的展示，只能照上游约定直连腾讯接口（同一路径、同一信封）。

    **模型目录是两级取数**（照官方客户端，上游 2026-09-15 commit 0adc345 修的同
    一件事）：企业端点（国内 `/console/...`、国际 `/v2/...` → `/console/...`）**加上**
    `/v3/config`。只探测企业端点会丢掉 `/v3/config` 独有的模型——实测国际版少了
    `deepseek-v4.1-flash`、`gpt-6-astra`、`hy4-preview-f`、`kimi-k2.8-preview`
    四个（用户报的「国际版没有 DeepSeek」即此）。两路**并发**，任一路失败降级用
    另一路（都失败才算失败）。

    两域口径各自保留（上游两域各走各的解析）：
      - 国内版（上游 FetchModels）：企业端点按 agents 的 `cli` 列表过滤；
      - 国际版（上游 parseGlobalModelNames）：企业端点取 `data.models` **全量**，
        不看 agents——国际版模型目录不由国内版的 `cli` 白名单定义；
      - `/v3/config` 两域都是**全量**（它没有 agents），但要过非对话过滤。
      - `disabled` 的条目不收录，跳过无 id 的条目。

    合并口径与上游一致：`/v3/config` 条目为主（同 id 时字段以它为准），企业端点
    只补它没有的模型；输出顺序稳定（v3 在前、企业端点补充项在后）。

    返回 (ok, models 或错误信息)。不含任何凭据。
    """
    access_token = str(auth.get('access_token') or '')
    if not access_token:
        return False, '该账号无有效 accessToken'
    realm = realm_of(auth)
    uid = str(auth.get('uid') or '')
    enterprise_paths = (
        ['/v2/enterprises/personal/models', '/console/enterprises/personal/models']
        if realm == GLOBAL
        else ['/console/enterprises/personal/models']
    )

    async def _probe(paths: list[str]) -> tuple[object | None, str]:
        """按候选路径顺序取第一个成功响应，返回 (data, 错误说明)。"""
        last_code = -1
        try:
            async with _account_http_client(auth) as client:
                for path in paths:
                    resp = await client.get(
                        f'{chat_base(realm)}{path}',
                        headers=_hdr(realm, access_token, uid),
                    )
                    code, body = _envelope(resp)
                    last_code = code
                    if code == 0 and isinstance(body, (dict, list)):
                        return body, ''
            return None, f'code={last_code}'
        except Exception as exc:  # noqa: BLE001
            return None, f'异常: {err_text(exc)}'

    # 两路并发：串行会把模型中心的等待时间翻倍，而两路互不依赖。
    ent_res, v3_res = await asyncio.gather(
        _probe(enterprise_paths),
        _probe([V3_CONFIG_PATH]),
    )
    ent_data, ent_err = ent_res
    v3_data, v3_err = v3_res
    if ent_data is None and v3_data is None:
        return False, f'模型接口返回 {ent_err}（/v3/config 亦失败：{v3_err}）'

    # 各自解析成「id → 条目」再合并。解析函数与 order 分离，是为了让合并
    # 能按「主路原序在前、补缺项在后」输出，而不是依赖字典的插入序。
    ent_items, ent_order = _parse_model_payload(ent_data, realm)
    v3_items, v3_order = _parse_model_payload(v3_data, realm)
    if not ent_items and not v3_items:
        return False, '模型接口未返回任何可用模型'

    merged: dict[str, dict] = {}
    order: list[str] = []
    for items, ids in ((v3_items, v3_order), (ent_items, ent_order)):
        for mid in ids:
            if mid in merged:
                continue
            merged[mid] = items[mid]
            order.append(mid)
    for mid, item in merged.items():
        item.update(_image_capability(ent_items.get(mid), v3_items.get(mid)))
    out = [merged[i] for i in order]
    if not out:
        return False, '模型接口未返回任何可用模型'
    return True, out


def _image_capability(enterprise: dict | None, config_v3: dict | None) -> dict:
    """Platform image-input declarations, not native model multimodality.

    Contradictory official declarations are unknown, never silently overridden.
    Missing/malformed values remain unknown rather than becoming False or True.
    """
    sources = {}
    for label, item in (('enterprise_models', enterprise), ('v3_config', config_v3)):
        if item is not None:
            value = item.get('supports_images')
            sources[label] = value if type(value) is bool else None
    known = {v for v in sources.values() if type(v) is bool}
    conflict = len(known) > 1
    return {
        'supports_images': next(iter(known)) if len(known) == 1 else None,
        'image_input_conflict': conflict,
        'image_input_sources': sources,
    }


def _parse_model_payload(data: object, realm: Realm) -> tuple[dict[str, dict], list[str]]:
    """把一路响应解析成「id → 条目」与输出顺序。

    容忍两种形态：对象列表（常规）与字符串数组（窄表，只有模型名）。
    国内版的企业端点按 agents 的 `cli` 列表过滤；国际版与 /v3 全量。
    """
    # 窄表形态：data 直接是模型名字符串数组（上游 parseGlobalModelNames 兼容）。
    if isinstance(data, list):
        items: dict[str, dict] = {}
        order: list[str] = []
        for raw in data:
            mid = str(raw).strip()
            if mid and mid not in items:
                items[mid] = {'id': mid}
                order.append(mid)
        return items, order

    if not isinstance(data, dict):
        return {}, []

    raw_models = data.get('models') if isinstance(data.get('models'), list) else []
    agents = data.get('agents') if isinstance(data.get('agents'), list) else []

    # 国内版才用 agents 的 `cli` 白名单；国际版取全量。窄表的 v3 响应没有 agents，
    # 自然走全量。
    cli_ids: list[str] = []
    if realm != GLOBAL:
        for ag in agents:
            if isinstance(ag, dict) and ag.get('name') == 'cli':
                ids = ag.get('models')
                if isinstance(ids, list):
                    cli_ids = [str(x) for x in ids if x]
                break

    info: dict[str, dict] = {}
    for m in raw_models:
        if not isinstance(m, dict) or not m.get('id'):
            continue
        reasoning = m.get('reasoning') if isinstance(m.get('reasoning'), dict) else {}
        efforts = reasoning.get('supportedEfforts')
        mid = str(m['id'])
        tags = m.get('tags') if isinstance(m.get('tags'), list) else []
        info[mid] = {
            'id': mid,
            'name': str(m.get('name') or '').strip(),
            'context_length': _as_int(m.get('maxInputTokens')),
            'max_output_tokens': _as_int(m.get('maxOutputTokens')),
            'disabled': bool(m.get('disabled')),
            'efforts': [str(x) for x in efforts if x] if isinstance(efforts, list) else [],
            # 推理默认档位（上游 2026-09-14 起解析并用于 thinking 决策）。
            # 空 = 上游未声明，此时上游会回退到自己的硬编码默认。
            'default_effort': str(reasoning.get('defaultEffort') or '').strip(),
            # 官方平台图片输入声明；不代表模型原生多模态能力。
            'supports_images': m.get('supportsImages') if type(m.get('supportsImages')) is bool else None,
            # ── 以下为上游 2026-09-15 补齐的模型目录字段（我们直连腾讯，本就能取到）──
            # 说明：字段名照上游 dynModelEntry 的 JSON 标签（descriptionZh / credits /
            # tags / vendor …），那是它从同一接口解析出来的实测结果，不是猜的。
            'description': str(m.get('descriptionZh') or '').strip(),
            # 积分倍率原文（如 "x0.05"）：同一个 prompt 在不同模型上的扣费倍率，
            # 用户据此挑更省的模型。仅展示，不参与选号（与上游口径一致）。
            'credits': str(m.get('credits') or '').strip(),
            'vendor': str(m.get('vendor') or '').strip(),
            'tags': [str(t) for t in tags if t],
            'is_default': bool(m.get('isDefault')),
            'supports_reasoning': bool(m.get('supportsReasoning')),
            'supports_tool_call': bool(m.get('supportsToolCall')),
            'only_reasoning': bool(m.get('onlyReasoning')),
            # 推理摘要模式（如 "auto"）；与 supportedEfforts 不同源
            'reasoning_summary': str(reasoning.get('summary') or '').strip(),
            # 非对话过滤（nes- / 输出≤256 / text-to-image）：**国内版全域**适用
            # （上游在 CN 的 console 与 v3 两路都做）。国际版不做——那套规则源自
            # 国内版 harness，套到国际版会重犯「用国内版口径裁剪国际版」的错误。
            '_non_chat': realm != GLOBAL and _non_chat_model(
                mid, _as_int(m.get('maxOutputTokens')), tags),
        }

    # 国内版：cli 列表为空时退回全部未禁用模型（上游此时直接报错，但管理端只是
    # 展示，给个可用列表比整页空白更有用，来源会在 UI 上如实标注）。
    # 国际版与 /v3：未取 cli_ids（空）→ 天然走全量。
    ids = cli_ids or list(info.keys())
    items = {}
    order = []
    for i in ids:
        item = info.get(i)
        if not item or item['disabled']:
            continue
        # 内部标记一律去掉（无论是否命中过滤）——否则它会随 API 响应漏到前端
        if item.pop('_non_chat', False):
            continue
        items[i] = item
        order.append(i)
    return items, order


def _non_chat_model(mid: str, max_output_tokens: int, tags: list) -> bool:
    """是否非对话模型（应从可选列表里剔除）。

    镜像上游 nonChatModel（2026-09-14 新增，来源 harness buddy.ts:547-555）。
    三类：
      * id 前缀 `nes-` / `completion-` / `codewise-`：嵌入 / 补全 / 代码专用，
        选了会报 `code=11102`；
      * `maxOutputTokens <= 256`：输出上限过小，属 tiny 非对话模型；
      * tags 含 `text-to-image`：图片生成，不是本网关用途。

    为什么要跟：模型中心是给用户**挑模型**的地方，列出选不了的东西
    等于制造一次必然失败的尝试。
    """
    low = (mid or '').strip().lower()
    if low.startswith(('nes-', 'completion-', 'codewise-')):
        return True
    if 0 < max_output_tokens <= 256:
        return True
    return any(str(t) == 'text-to-image' for t in tags)


def _as_int(v: object) -> int:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _extract_resource_accounts(data: object) -> list | None:
    """不同层级的信封包装，尽量把套餐数组取出来。"""
    cur = data
    for _ in range(4):
        if isinstance(cur, dict):
            if 'Accounts' in cur and isinstance(cur['Accounts'], list):
                return cur['Accounts']
            # 逐层下钻：Response / Data / data 等
            for key in ('Response', 'Data', 'data', 'response'):
                if isinstance(cur.get(key), (dict, list)):
                    cur = cur[key]
                    break
            else:
                return None
        elif isinstance(cur, list):
            return cur
        else:
            return None
    return None


def _num(item: dict, key: str) -> float:
    """取数值字段，非数值（含 null / 布尔 / 字符串）一律按 0。"""
    v = item.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0.0
    return float(v)


def _round_credits(v: float) -> int | float:
    """积分的对外形态：整数去掉小数尾（1000.0 → 1000），否则保留两位。"""
    return int(v) if float(v).is_integer() else round(float(v), 2)


def _package_remain(item: dict) -> float:
    """单个套餐的剩余额度，口径与上游 packageRemainUsed 逐条对齐。

    两段：有周期额度（CycleCapacitySize>0）时只看 Cycle 三字段，且把 remain 钳进
    [0, size]、再用 used 反修正一次；没有周期额度时回退 Capacity 三字段。

    钳位不是洁癖：腾讯偶发 `CycleCapacityRemain > CycleCapacitySize` 的脏数据，
    只钳负值会**高估**余额（上游为此把双份逻辑合并到了这一个函数）。
    """
    size = _num(item, 'CycleCapacitySize')
    if size > 0:
        remain = min(max(_num(item, 'CycleCapacityRemain'), 0.0), size)
        used = size - remain
        cycle_used = _num(item, 'CycleCapacityUsed')
        if cycle_used > used:
            used = cycle_used
            if size >= used:
                remain = size - used
        return remain
    return _num(item, 'CapacityRemain')


# 套餐到期时刻：布局与时区都取上游同款（packageEndLayout / softRateResetLoc）。
# 腾讯给的是 **UTC+8 墙钟**，与容器时区无关——必须显式带 +08:00 解析；若按本机
# 时区解析（mktime / fromtimestamp），西半球或 UTC 容器上算出的到期时刻会整体
# 偏移数小时，倒计时跟着错。上游用 time.ParseInLocation(layout, s, UTC+8)，
# 这里等价。
_PACKAGE_END_LAYOUT = '%Y-%m-%d %H:%M:%S'
_PACKAGE_END_TZ = timezone(timedelta(hours=8))


def _package_expiry(item: dict) -> int | None:
    """套餐到期时刻（epoch 秒，绝对时刻）。字段缺失/空/解析失败 → None。

    解析失败返回 None 而不是抛错：到期时间是锦上添花的展示字段，不能因为它
    让整次积分查询失败（上游对解析失败也是保守忽略）。
    """
    raw = item.get('CycleEndTime')
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        naive = datetime.strptime(raw.strip(), _PACKAGE_END_LAYOUT)
    except ValueError:
        return None
    return int(naive.replace(tzinfo=_PACKAGE_END_TZ).timestamp())


async def probe_account(auth: dict, model: str = 'glm-5.2') -> tuple[bool, str]:
    """以最小**流式**对话请求探测账号可用性。

    必须用流式：上游强制要求 stream=true，非流式会返回
    code=11101「Non-stream chat request is currently not supported」
    （见 workbuddy2api payload.go 中强制 obj["stream"]=true 的处理）。
    因此这里发起流式请求，读到首个数据块即判定可用，随即断开。

    请求头复刻上游 ChatHeaders：缺失字段用 X-No-* 约定。
    国内版带 X-Enterprise-Id / X-Domain（有值则带、否则 X-No-*）；
    国际版按上游 injectGlobalChatHeaders 固定 `X-No-Enterprise-Id: 1` +
    `X-Domain: www.workbuddy.ai`，且 chat 路径以 /console 优先、404 回落 /v2。
    """
    import time as _time

    access_token = str(auth.get('access_token') or '')
    if not access_token:
        return False, '该账号无有效 accessToken'

    realm = realm_of(auth)
    uid = str(auth.get('uid') or '')
    enterprise_id = str(auth.get('enterprise_id') or '')
    domain = str(auth.get('domain') or '')
    # 账号自带的 domain 若已是完整 URL，说明部署方指定了 base，优先采用
    base = domain if domain.startswith('http') else chat_base(realm)

    headers = _hdr(realm, access_token, uid)
    # chat 是流式路径：Accept 覆盖为流式形态（对齐上游 D6 —— 非流式默认收紧为
    # application/json，只有 chat 才声明 text/event-stream）
    headers['Accept'] = 'application/json, text/event-stream'
    headers['X-User-Id' if uid else 'X-No-User-Id'] = uid or '1'
    if realm == GLOBAL:
        # 镜像上游：国际版声明「个人账号无企业 ID」并断言国际域
        headers['X-No-Enterprise-Id'] = '1'
        headers['X-Domain'] = 'www.workbuddy.ai'
    else:
        headers['X-Enterprise-Id' if enterprise_id else 'X-No-Enterprise-Id'] = enterprise_id or '1'
        headers['X-Domain' if domain else 'X-No-Department-Info'] = domain or '1'
    headers['X-Product'] = 'SaaS'
    # 用量归属头：默认伪造官方桌面端指纹（X-Agent-Purpose + X-IDE-* 四头），
    # 与上游 2026-09-14 起的 injectAttribution 默认值一致——此前只有
    # X-Product=SaaS，在官网用量归因里是显眼的「网关特征」。
    headers.update(attribution_headers())
    # 设备风控头：上游 chat 域同样注入（ChatHeaders → injectDeviceToken）。
    # 三级回退 auth 每号 > config 全局 > 文件；取不到就不发（与上游一致）。
    _dt = device_token_for(auth)
    if _dt:
        headers['X-Device-Token'] = _dt
    # 会话头族（issue #193）：上游 2026-10-16 起对**部分账号**灰度启用渠道校验，
    # 缺 X-Conversation-ID 即判 11-128「conversation id missing」—— 被灰度到的账号在
    # 探测里必红（真实对话走网关，同一族头由上游补）。这里补齐官方客户端那几支；
    # 四个 ID 各有语义（会话 / 轮 / 消息 / 根），不要图省事复用成同一个值。
    headers.update({
        'X-Conversation-ID': uuid.uuid4().hex,
        'X-Conversation-Request-ID': uuid.uuid4().hex,
        'X-Conversation-Message-ID': uuid.uuid4().hex,
        'X-Root-Request-ID': uuid.uuid4().hex,
        'X-Trace-ID': uuid.uuid4().hex,
    })

    payload = {
        'model': model,
        # **首条必须是 system**：上游要求 messages[0].role == 'system'，否则返回
        # 11-128「first message is not system prompt」。
        #
        # 为什么以前不报错：过去 prompt.mode 缺省是 custom，上游会用自有提示词
        # 在头部插一条 system；改用缺省 passthrough（2026-09-14 起）后不再插入，
        # 客户端原样透传 —— 我们这条只带 user 的探测就被拒了。
        # 探测请求是我们自己造的（不是真实客户端），所以这里显式补上。
        'messages': [
            {'role': 'system', 'content': 'You are a helpful assistant.'},
            {'role': 'user', 'content': 'ping'},
        ],
        'max_tokens': 1,
        'stream': True,
    }

    started = _time.time()
    # 路径候选：/v2 优先、/console 兜底（见 realm.chat_paths 的注释）。这里逐个尝试，
    # 触发回落的条件是**渠道校验**或 404/405：2026-10-17 上游给 /v2 加了渠道强校验，
    # 被校验到的请求回「没通过渠道校验」，而官方客户端走的 /console 正常放行 ——
    # 用户报告里的「连通性测试被拦、换 /console 就正常」正是这条。
    try:
        paths = chat_paths(realm)
        # lane='probe'：测活是**用户正在等**的那一次请求，给它独立连接池，
        # 不被并发的批量任务（刷积分/签到）挤到 pool 超时（issue #161）。
        async with _account_http_client(auth, lane='probe') as client:
            for idx, path in enumerate(paths):
                async with client.stream(
                    'POST', f'{base}{path}', json=payload, headers=headers,
                ) as resp:
                    if resp.status_code >= 400:
                        raw = (await resp.aread()).decode('utf-8', errors='replace')
                        code, msg = _parse_error_body(raw, resp.status_code)
                        channel_check = _is_channel_check(code, raw)
                        if channel_check and idx < len(paths) - 1:
                            continue  # 换下一条路径再试，别当成账号问题
                        if channel_check:
                            # 两条都被拦：说清「不是账号问题」，否则用户会以为号坏了去重扫。
                            return False, ('这次调用被上游的渠道校验拦下了（已试过 ' +
                                           '/'.join(paths) + '）—— 账号本身没问题；'
                                           '请先升级到最新版本，仍不行就把这句反馈给维护者')
                        return False, _explain_code(code, msg)
                    # 读到首个非空数据块即可确认账号可用，无需等流结束
                    async for chunk in resp.aiter_bytes():
                        if chunk:
                            elapsed = int((_time.time() - started) * 1000)
                            return True, f'连通正常（{model}，{elapsed}ms）'
                    return False, '上游未返回任何数据'
        return False, '所有候选路径均不可用'
    except Exception as exc:  # noqa: BLE001
        return False, f'请求异常: {err_text(exc)}'



def _is_channel_check(code: object, raw: str) -> bool:
    """判上游的「渠道校验」拒绝（与上游 `IsChannelCheck` 同一口径）。

    指纹**两条同时成立**：业务码（实测体里是带横线的写法，报告方另见无横线写法，
    两种都认）**且**文案里出现渠道校验的措辞。只认码不行 —— 同一个码还承载
    「内容审核拦截」，那种要按内容问题处理（不换路径）。
    码的字面量用拼接构造：它极易手抄成 1138/1128，而判据写错不会有人发现。
    """
    text = str(code or '')
    if '11-128' not in text and '11128' not in text:
        return False
    low = raw.lower()
    return ('渠道校验' in raw or 'channel verification' in low or 'official client' in low)

def _parse_error_body(raw: str, status: int) -> tuple[int | str, str]:
    """错误响应可能是 {code,msg} 信封，也可能是纯文本。"""
    try:
        env = json.loads(raw)
        if isinstance(env, dict):
            return env.get('code', status), str(env.get('msg') or env.get('message') or '')
    except Exception:  # noqa: BLE001
        pass
    return status, raw.strip()[:200]


# 已知业务码 -> 可读说明（来源：workbuddy2api 源码与实测）
#
# 注意键**必须带引号**：`11-128` 不加引号会被 Python 当成算术表达式
# （11 - 128 = -117），于是这个提示永远匹配不上，而且真正收到 "11-128" 时
# int() 还会抛异常被静默吞掉。上游这个错误码是不带引号的形态，必须按字符串存。
_CODE_HINTS: dict[int | str, str] = {
    0: '成功',
    10001: '今日已签到',
    11101: '上游不接受非流式请求（协议问题，非账号问题）',
    # 14018：上游 2026-09-20 起把它明确归为「积分耗尽」（只认这个结构化业务码，
    # 不靠文案猜——429 上「额度不足」的措辞跨计费与限流两界）。本端跟着翻译：
    # 连通性测试打到欠费号时，用户要知道是没积分了，而不是「账号坏了」。
    14018: '账号积分耗尽（等签到恢复或换账号）',
    '11-128': '首条消息必须是 system（网关提示词未注入时客户端需自带）',
    12153: '会话已失效，需重新登录',
}


def _explain_code(code: int | str, msg: str = '') -> str:
    """把上游错误码翻译成人能看懂的一句话。

    码可能是数字（int/json number）也可能是带横线的字符串（"11-128"），
    所以查找要**先按原值、再按 int**，并对 int() 失败做好兜底。
    """
    key: int | str = code
    if key not in _CODE_HINTS:
        try:
            key = int(code)
        except (TypeError, ValueError):
            key = code  # 保持原样（如 "11-128"），按字符串查
    hint = _CODE_HINTS.get(key)
    parts = [f'上游返回 code={code}']
    if msg:
        parts.append(msg)
    if hint:
        parts.append(f'（{hint}）')
    return ' '.join(parts)


# ── 国际版：地区注册与 trial ──────────────────────────────
# 国际版新号必须先完成地区注册，否则聊天会报 14017「trial not activated」。
# 端口与流程对齐上游 scripts/global_region.py（从其反编译结果整理）：
#   GET  /auth/realms/copilot/overseas/user/register?userId=<uid>  查是否已注册
#   POST /console/login/account                                     提交地区
#   POST /billing/ide/trial                                         领一次性 trial
# 注意：**地区由使用者决定**，这里只提供提交能力，不替用户选国家。
INTERNATIONAL_REGIONS: tuple[tuple[str, str], ...] = (
    # (IOS2 代码, 地区中文名) —— 国际版官网给出的可选短名单
    ('HK', '中国香港'),
    ('MO', '中国澳门'),
    ('SG', '新加坡'),
    ('TH', '泰国'),
    ('PH', '菲律宾'),
    ('MY', '马来西亚'),
    ('ID', '印度尼西亚'),
)


async def registration_status(auth: dict) -> tuple[bool, str]:
    """查该国际版账号是否已完成地区注册。

    返回 (ok, 说明)。ok=True 表示**已完成**（无需再注册）。
    国内版无此步骤，直接返回已完成。
    """
    realm = realm_of(auth)
    if realm != GLOBAL:
        return True, '国内版无需地区注册'
    uid = str(auth.get('uid') or '')
    token = str(auth.get('access_token') or '')
    if not uid or not token:
        return False, '缺少 uid 或 accessToken，无法查询注册状态'
    url = f'{billing_base(realm)}/auth/realms/copilot/overseas/user/register'
    try:
        # 上游参照实现（scripts/global_region.py activate_region）明确带 X-User-Id，
        # 这里走 billing 域头（含身份头），保持一致
        async with _account_http_client(auth) as client:
            resp = await client.get(url, params={'userId': uid}, headers=_billing_hdr(realm, auth))
        code, data = _envelope(resp)
        if code == 200 or code == 0:
            return True, '地区注册已完成'
        if code == 500:
            return False, '尚未完成地区注册'
        # 「region required」也是未注册的一种表述
        text = str(data or '')
        if 'region required' in text.lower():
            return False, '尚未完成地区注册'
        return False, f'注册状态未知 code={code}'
    except Exception as exc:  # noqa: BLE001
        return False, f'查询注册状态异常: {err_text(exc)}'


async def submit_region(auth: dict, region_code: str) -> tuple[bool, str]:
    """提交国际版账号的地区。

    region_code 取 INTERNATIONAL_REGIONS 里的代码（如 'HK'）。
    提交成功后建议再调 registration_status 复核（上游脚本也是这么做的）。

    请求体形状**照上游参照实现 scripts/global_region.py**（其注释标注「实测」）：

        {"attributes": {"countryCode": [Code],        ← 数字地区码（如 810000）
                        "countryFullName": [EnName],  ← 英文全名（如 China Hong Kong）
                        "countryName": [IOS2]}}       ← 短码（如 HK）

    三个字段**取值各不相同**，此前我们错把三者都填成 IOS2，且少了 attributes
    外层——上游是按这三个字段落库的，填错会把地区归属写歪。
    这里按 IOS2 先查回真实条目，取不到就退回只用 IOS2（不至于提交非法值）。
    """
    realm = realm_of(auth)
    if realm != GLOBAL:
        return False, '国内版无需地区注册'
    code_upper = (region_code or '').strip().upper()
    valid = {c for c, _ in INTERNATIONAL_REGIONS}
    if code_upper not in valid:
        return False, f'不支持的地区代码 {region_code!r}（可选：{"、".join(sorted(valid))}）'
    token = str(auth.get('access_token') or '')
    if not token:
        return False, '缺少 accessToken'

    ios2, en_name, numeric = await _region_fields(code_upper, auth)

    attrs = {
        'countryCode': [numeric],
        'countryFullName': [en_name],
        'countryName': [ios2],
    }
    body = {'attributes': attrs}
    try:
        async with _account_http_client(auth) as client:
            resp = await client.post(
                f'{billing_base(realm)}/console/login/account',
                json=body,
                headers=_billing_hdr(realm, auth),
            )
        code, data = _envelope(resp)
        if code == 0 or code == 200:
            return True, f'地区已提交（{code_upper}）'
        return False, f'提交地区失败 code={code}'
    except Exception as exc:  # noqa: BLE001
        return False, f'提交地区异常: {err_text(exc)}'


async def _region_fields(ios2: str, auth: dict | None = None) -> tuple[str, str, str]:
    """按 IOS2 短码查该地区的 (IOS2, EnName, Code)。

    地区列表来自上游接口 `/billing/area/get-country-code`（响应 data 是内嵌
    JSON 字符串）。查不到时退回 (IOS2, IOS2, IOS2)——提交非法值会被上游拒绝，
    好过替用户瞎猜一个。
    """
    fallback = (ios2, ios2, ios2)
    try:
        async with _account_http_client(auth or {}) as client:
            resp = await client.post(
                f'{billing_base(GLOBAL)}/billing/area/get-country-code',
                json={'filterForbidden': 1},
                headers=_hdr(GLOBAL),
            )
        code, data = _envelope(resp)
        if code != 0 or data is None:
            return fallback
        # 响应 data 可能是内嵌 JSON 字符串（上游脚本明确处理了这一层）
        inner = json.loads(data) if isinstance(data, str) else data
        if not isinstance(inner, dict):
            return fallback
        lst = ((inner.get('data') or {}).get('list')
               if isinstance(inner.get('data'), dict) else None) or []
        for item in lst:
            if isinstance(item, dict) and str(item.get('IOS2') or '').upper() == ios2:
                return (
                    str(item.get('IOS2') or ios2),
                    str(item.get('EnName') or ios2),
                    str(item.get('Code') or ios2),
                )
    except Exception:  # noqa: BLE001
        pass
    return fallback


async def claim_trial(auth: dict) -> tuple[bool, str]:
    """领取国际版的一次性 trial 加油包。

    14051 = 已领过，按幂等处理（算成功）。
    """
    realm = realm_of(auth)
    if realm != GLOBAL:
        return False, '国内版无 trial 加油包'
    token = str(auth.get('access_token') or '')
    if not token:
        return False, '缺少 accessToken'
    try:
        async with _account_http_client(auth) as client:
            resp = await client.post(
                f'{billing_base(realm)}/billing/ide/trial',
                json={},
                headers=_billing_hdr(realm, auth),
            )
        code, data = _envelope(resp)
        if code == 0:
            return True, 'trial 已领取'
        if code == 14051 or '14051' in str(data or ''):
            return True, 'trial 此前已领取（幂等）'
        return False, f'领取 trial 失败 code={code}'
    except Exception as exc:  # noqa: BLE001
        return False, f'领取 trial 异常: {err_text(exc)}'
