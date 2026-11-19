"""AstrBot OpenAPI 通知通道（QQ 官方机器人 / 任意已接入的平台）。

为什么经 AstrBot 中转，而不是在管理端直连 QQ 官方 API：QQ 官方机器人要
AppID / AppSecret、IP 白名单、access_token 定时刷新、主动消息权限，以及
「群主动推送需要缓存 msg_id」这类协议细节——它们全部属于机器人运行时，
不该复制进一个监控面板。AstrBot 4.18+ 的 OpenAPI 恰好把这件事收敛成三样
配置：**HTTP 地址 + 开发者访问密钥 + UMO（发给谁）**。

    管理端 ──POST /api/v1/im/message──▶ AstrBot ──▶ QQ

协议要点（见 docs/qq-bot.md）：

* 鉴权：`Authorization: Bearer abk_...`（也接受 `X-API-Key`），密钥在
  AstrBot 的「设置 → OpenAPI」创建，**必须有 `im` scope**，否则 403；
* 消息体：`{"umo": "...", "message": [{"type": "plain", "text": "..."}]}`，
  纯字符串也可以，但数组形式更稳（不同版本对字符串的处理有过差异）；
* UMO 形如 `平台适配器id:消息类型:会话ID`，QQ 官方群聊是
  `qq_official:GroupMessage:<group_openid>`，私聊是
  `qq_official:FriendMessage:<openid>`。`group_openid` 只能从群消息事件里
  拿到，所以首次配置必须先在目标群里 @ 一次机器人。

**密钥绝不进日志**：这里所有错误文本都会先做一次脱敏（把 api_key 换成
`***`），因为 httpx 的异常消息可能带上完整请求头；上层写审计时也只记
base_url 与 umo。
"""
from __future__ import annotations

import json
import time
from urllib.parse import urlparse

from .. import config

# QQ 单条文本的实际上限随场景变化（群/私聊、是否 markdown 都不同）。
# 取一个保守值并显式截断：超长时宁可少发几行，也不要整条被平台拒收——
# 告警的价值在于「到达」，而不是「一字不漏」。
MAX_TEXT = 1800

_LANE = 'watchdog-qq'


def api_base(base_url: str) -> str:
    """把用户填的地址归一化成 `…/api/v1`。

    三种常见填法都要能work：`https://host:6185`、`https://host:6185/api`、
    `https://host:6185/api/v1/`。不要求用户记住 OpenAPI 的挂载路径。
    """
    base = str(base_url or '').strip().rstrip('/')
    if not base:
        return ''
    # 有人会把文档里的完整端点整段粘进来；去掉端点本身再归一化，
    # 否则会拼成 `.../im/message/api/v1` 这种必然 404 的地址。
    for suffix in ('/im/message', '/im/bots'):
        if base.endswith(suffix):
            base = base[:-len(suffix)].rstrip('/')
            break
    if base.endswith('/api/v1'):
        return base
    if base.endswith('/api'):
        return base + '/v1'
    return base + '/api/v1'


def _redact(text: object, secret: str) -> str:
    out = str(text or '')
    if secret and secret in out:
        out = out.replace(secret, '***')
    return out


def _error(message: str, *, status: int = 0, secret: str = '') -> dict:
    return {'ok': False, 'error': _redact(message, secret), 'status': status}


def _excerpt(resp) -> str:
    """响应体摘要：界面/审计只需要「大概是哪类错」，不需要整页 HTML。"""
    try:
        text = resp.text
    except Exception:  # noqa: BLE001
        return ''
    return text[:300].replace('\n', ' ').strip()


def _parse_json(resp) -> object:
    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError):
        return None


async def send_message(qq: dict, text: str, *, timeout: float | None = None) -> dict:
    """发一条纯文本消息。失败一律返回 `{ok: False, error}`，不抛异常。

    为什么不抛：一轮检测里 QQ 只是通知通道之一，通道挂了不该让整轮结果变成
    异常——邮件、状态落库、界面展示都还要继续。
    """
    if not qq.get('enabled'):
        return {'ok': False, 'skipped': 'QQ 通知未启用'}
    base = api_base(qq.get('base_url') or '')
    key = str(qq.get('api_key') or '')
    umo = str(qq.get('umo') or '').strip()
    if not base or not key or not umo:
        return {'ok': False, 'skipped': 'QQ 配置不完整（地址 / 密钥 / UMO）'}
    parsed = urlparse(base)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return _error('AstrBot 地址必须是 http(s) 绝对地址')

    body = str(text or '').strip()
    if not body:
        return _error('消息内容为空')
    if len(body) > MAX_TEXT:
        body = body[:MAX_TEXT - 12] + '\n…（已截断）'

    payload = {'umo': umo, 'message': [{'type': 'plain', 'text': body}]}
    headers = {'Authorization': f'Bearer {key}'}
    tmo = timeout or float(qq.get('timeout_seconds') or 15)
    url = base + '/im/message'
    try:
        async with config.http_client(tmo, lane=_LANE) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except Exception as exc:  # noqa: BLE001
        return _error(f'{exc.__class__.__name__}: {exc}', secret=key)

    if resp.status_code >= 400:
        return _error(f'AstrBot HTTP {resp.status_code}: {_excerpt(resp)}',
                      status=resp.status_code, secret=key)
    data = _parse_json(resp)
    if isinstance(data, dict) and data.get('status') == 'error':
        return _error(f"AstrBot 返回错误：{data.get('message') or data}",
                      status=resp.status_code, secret=key)
    return {
        'ok': True,
        'sent_at': int(time.time()),
        'umo': umo,
        'status': resp.status_code,
        'response': _excerpt(resp),
    }


async def list_bots(qq: dict, *, timeout: float | None = None) -> dict:
    """`GET /im/bots`：确认地址 / 密钥 / scope 是否可用。

    比「直接发一条」更适合做「检测连接」——它不依赖 UMO 是否正确，能把
    「密钥没 im 权限」和「UMO 填错了」两类失败分开，报错才可操作。
    顺带返回平台适配器 id，用户据此拼 UMO。
    """
    base = api_base(qq.get('base_url') or '')
    key = str(qq.get('api_key') or '')
    if not base or not key:
        return {'ok': False, 'error': '请先填写 AstrBot 地址与开发者访问密钥'}
    parsed = urlparse(base)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return _error('AstrBot 地址必须是 http(s) 绝对地址')

    tmo = timeout or float(qq.get('timeout_seconds') or 15)
    try:
        async with config.http_client(tmo, lane=_LANE) as client:
            resp = await client.get(base + '/im/bots',
                                    headers={'Authorization': f'Bearer {key}'})
    except Exception as exc:  # noqa: BLE001
        return _error(f'{exc.__class__.__name__}: {exc}', secret=key)

    if resp.status_code >= 400:
        return _error(f'AstrBot HTTP {resp.status_code}: {_excerpt(resp)}',
                      status=resp.status_code, secret=key)
    data = _parse_json(resp)
    bots = data.get('data') if isinstance(data, dict) else None
    if isinstance(data, dict) and isinstance(data.get('bots'), list):
        bots = data['bots']
    if not isinstance(bots, list):
        bots = []
    return {'ok': True, 'status': resp.status_code, 'bots': bots}
