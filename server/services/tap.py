"""调用抓包（Trace Tap）：把经过网关的每一次调用留痕，供面板回放与对比。

## 这个功能解决什么

上游 `workbuddy2api` 是一个 OpenAI 兼容反代，客户端（Claude Code、Codex CLI、
Cursor、各类 SDK）把**完整的上下文**（system prompt、全部历史消息、工具定义）
压在每一次请求里发给它。面板过去只留下「谁在什么时候用了多少 token」——
于是「模型为什么这么答」「工具为什么没被调用」「这一轮比上一轮多了什么」
这些问题在面板上无从回答，只能去翻客户端自己的日志。

[claude-tap](https://github.com/liaohch3/claude-tap) 用本地代理解决同一个问题。
本项目的网关**本来就站在所有调用的必经之路上**，所以不再起第二个进程：
打开开关就能把同样的证据留在面板里，并且能直接和账号、密钥、用量对上。

## 与 request_logs 的分工

`request_logs` 是计量数据：字段固定、长期聚合、只增不删（按保留期滚动）。
抓包是排障证据：单行可达几百 KB，必须单独限容、单独清理。两者刻意不合并，
原因写在 `db.SCHEMA` 的建表注释里。

## 三条不变量

1. **不能影响转发**。所有入口都包在 try/except 里，且抓包在响应已经发给客户端
   之后才落库 —— 抓包失败最多丢一条证据，绝不能让用户请求失败。历史上出过
   「统计函数抛异常导致流式响应在收尾阶段中断」的事故（见 gateway._record）。
2. **不能留下明文密钥**。请求头里的 Authorization / x-api-key / Cookie 一律
   改写；正文里形如 `sk-…`、`Bearer …`、`"api_key": "…"` 的片段同样打码。
   抓包是本项目里唯一会存**完整对话内容**的地方，密钥混进去就再也没人看得见。
3. **不能无限增长**。每次请求体按 `max_body` 截断，会话数与保留天数都有上限，
   写入侧按周期清理（与 `request_logs` 同一个做法）。
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import time
from typing import Any, Iterable

from .. import config, db

# 设置项在 settings 表里的键名。用单键存整个配置对象，理由同其它模块：
# 新增一项不必再来一次迁移，读的时候与默认值合并即可。
SETTING_KEY = 'tap_config'

# 默认值。**默认关闭**：抓包会把完整对话内容落盘，属于用户应当主动开启的
# 能力（`WB_TAP_ENABLED=1` 可在部署层面改默认，适合专用排障实例）。
DEFAULT_MAX_BODY = 512 * 1024
DEFAULT_RETENTION_DAYS = 7
DEFAULT_MAX_SESSIONS = 300

# 同一会话的合并窗口（秒）。指纹相同但隔了很久的两段请求多半是「昨天那个任务
# 今天又开了一次」，并成一条会话会让「第 14 轮比第 13 轮多了什么」无从下眼。
_SESSION_IDLE_SECONDS = 12 * 3600

# 每写多少轮清理一次。与 request_logs 同思路：低流量部署靠「攒够次数」触发，
# 高流量部署不会每次写都扫一遍表。
_PRUNE_EVERY = 32

# 脱敏：请求头名（小写）。
_SECRET_HEADERS = frozenset({
    'authorization', 'proxy-authorization', 'x-api-key', 'api-key', 'apikey',
    'x-goog-api-key', 'cookie', 'set-cookie', 'x-auth-token', 'x-access-token',
    'anthropic-api-key', 'openai-api-key', 'x-stainless-api-key',
})

# 脱敏：正文里的令牌形态。顺序有意义 —— 先打码带前缀的完整形态，再收拾
# `"api_key": "…"` 这种键值对，避免前者被后者切碎后留下半个令牌。
_TOKEN_RULES = (
    re.compile(r'sk-[A-Za-z0-9_\-]{6,}'),
    re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}'),
    re.compile(r'(?i)\b(x-api-key|api[_-]?key|secret|password|token)'
               r'(\s*[:=]\s*"?)([A-Za-z0-9._\-]{8,})'),
    re.compile(r'AKIA[0-9A-Z]{16}'),
)

# 客户端家族：UA 归类。与 gateway.client_channel 同思路，但**不**复用那个函数 ——
# 它在 routers 里，services 反向导入 routers 会绕成环。归类口径只需粗到
# 「这轮是 Claude Code 还是 Codex」，两处各自维护一张表比制造循环导入便宜。
# 已知客户端的归类表。**顺序有意义**：先匹配到的赢（`claude-code` 里也含
# `claude`，但两者归一类，不冲突；而 `copilot` 不能出现在 vscode 之前，否则
# GitHub Copilot 之外的任何带 copilot 字样的 UA 都会被抢走）。
_CLIENT_RULES = (
    ('claude', ('claude-code', 'claude_cli', 'claude-cli', 'claude', 'anthropic')),
    ('codex', ('codex',)),
    # DeepSeek Harness（dsh）：UA 形如
    # `deepseek-harness/0.2.2-rc.1 (+https://github.com/deepseek-ai/deepseek-harness)`
    ('dsh', ('deepseek-harness', 'deepseek_harness')),
    ('cursor', ('cursor',)),
    ('vscode', ('vscode', 'visual studio code', 'github-copilot', 'copilot-chat')),
    ('cli', ('curl', 'wget', 'httpie', 'python-httpx', 'python-requests',
             'go-http-client', 'node', 'undici', 'postman', 'insomnia')),
)

_turn_writes = 0


# ── 配置 ─────────────────────────────────────────────────
def settings() -> dict[str, Any]:
    """当前抓包配置。DB 里的值优先，缺项回落到环境变量/默认值。"""
    raw = db.get_setting(SETTING_KEY, {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    enabled = raw.get('enabled')
    if enabled is None:
        enabled = config.TAP_ENABLED
    max_body = raw.get('max_body')
    if not isinstance(max_body, int) or max_body <= 0:
        max_body = config.TAP_MAX_BODY
    days = raw.get('retention_days')
    if not isinstance(days, int) or days <= 0:
        days = config.TAP_RETENTION_DAYS
    keep = raw.get('max_sessions')
    if not isinstance(keep, int) or keep <= 0:
        keep = config.TAP_MAX_SESSIONS
    return {
        'enabled': bool(enabled),
        # 夹到 [4 KiB, 8 MiB]：再小连 system prompt 都存不下，再大等于允许
        # 单条请求把库撑爆（面板跑在小内存机器上是常态）。
        'max_body': max(4096, min(int(max_body), 8 * 1024 * 1024)),
        'retention_days': max(1, min(int(days), 90)),
        'max_sessions': max(10, min(int(keep), 5000)),
    }


def save_settings(patch: dict[str, Any]) -> dict[str, Any]:
    """改配置。只接受已知字段，其余静默忽略（避免把任意键写进 settings）。"""
    cur = settings()
    merged = dict(cur)
    if 'enabled' in patch:
        merged['enabled'] = bool(patch['enabled'])
    for key in ('max_body', 'retention_days', 'max_sessions'):
        if key in patch:
            try:
                merged[key] = int(patch[key])
            except (TypeError, ValueError):
                continue
    db.set_setting(SETTING_KEY, merged)
    return settings()


def enabled() -> bool:
    """是否抓包。热路径每请求调一次，所以读设置而不是缓存（setting 读取很便宜）。"""
    try:
        return bool(settings()['enabled'])
    except Exception:  # noqa: BLE001 - 读配置失败按关闭处理，不能拦住请求
        return False


# ── 脱敏 ─────────────────────────────────────────────────
def _mask(value: object) -> str:
    """打码，**但保留可辨认的前缀**。

    为什么不是一律 `***`：抓包页要看的是「换没换凭证 / 用的是哪一把」。
    全打码等于把要对照的东西一起抹掉；保留前 6 位，认得出来、又不足以直接用。

    对 `Bearer xxx` / `Basic xxx` 这种带 scheme 的值要**分开处理**：直接取整串的
    前 6 个字符会得到 `Bearer` —— 因为 scheme 本身占满了，凭证一位都看不到，
    而这恰恰是最常见的一种头（实测第一版就是这个毛病）。
    """
    text = '' if value is None else str(value)
    if not text:
        return ''
    parts = text.split(' ', 1)
    if len(parts) == 2 and parts[0].lower() in ('bearer', 'basic', 'token', 'digest'):
        cred = parts[1]
        return f'{parts[0]} {cred[:6]}***' if len(cred) > 10 else f'{parts[0]} ***'
    head = text[:6]
    return f'{head}***' if len(text) > 10 else '***'


def redact_headers(headers: Iterable[tuple[str, str]] | None) -> dict[str, str]:
    """把请求头变成可入库的字典：敏感项打码，超长值截断。"""
    out: dict[str, str] = {}
    if not headers:
        return out
    for name, value in headers:
        key = str(name)
        out[key] = _mask(value) if key.lower() in _SECRET_HEADERS else str(value)[:512]
    return out


def scrub(text: str) -> str:
    """正文脱敏。**保持 JSON 可解析**：打码只替换值，不动结构。"""
    if not text:
        return text
    for rule in _TOKEN_RULES[:2]:
        text = rule.sub(lambda m: _mask(m.group(0)), text)
    text = _TOKEN_RULES[2].sub(lambda m: f'{m.group(1)}{m.group(2)}{_mask(m.group(3))}', text)
    text = _TOKEN_RULES[3].sub(lambda m: _mask(m.group(0)), text)
    return text


# ── 结构提取 ─────────────────────────────────────────────
def _content_text(content: object) -> str:
    """把 content 字段拉平成纯文本。

    四种形态都要认：字符串、OpenAI 的 `[{type:text,text}]`、Anthropic 的
    `[{type:text,text}]`（同名不同协议）、Responses 的 `[{type:input_text}]`。
    认不出就返回空串 —— 宁可少显示，也不能把 dict 的 repr 塞进界面。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ('text', 'content', 'input_text'):
                    val = item.get(key)
                    if isinstance(val, str):
                        parts.append(val)
                        break
                else:
                    if item.get('type') == 'image_url' or item.get('type') == 'image':
                        parts.append('[image]')
        return '\n'.join(parts)
    if isinstance(content, dict):
        for key in ('text', 'content'):
            val = content.get(key)
            if isinstance(val, str):
                return val
    return ''


def _messages(body: object) -> list[dict]:
    if not isinstance(body, dict):
        return []
    msgs = body.get('messages')
    return [m for m in msgs if isinstance(m, dict)] if isinstance(msgs, list) else []


def _system_text(body: object) -> str:
    """取 system 提示词。OpenAI 放 messages[0]，Anthropic 放顶层 system。"""
    if not isinstance(body, dict):
        return ''
    sys_val = body.get('system')
    if isinstance(sys_val, (str, list, dict)):
        text = _content_text(sys_val)
        if text:
            return text
    for msg in _messages(body):
        if str(msg.get('role')) in ('system', 'developer'):
            return _content_text(msg.get('content'))
    return ''


def _first_user_text(body: object) -> str:
    for msg in _messages(body):
        if str(msg.get('role')) == 'user':
            return _content_text(msg.get('content'))
    return ''


def _tool_names(body: object) -> list[str]:
    if not isinstance(body, dict):
        return []
    tools = body.get('tools')
    if not isinstance(tools, list):
        return []
    names: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get('function') if isinstance(tool.get('function'), dict) else tool
        name = fn.get('name')
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _fingerprint(body: object, protocol: str) -> str:
    """会话指纹：system + 首条 user 消息 + 协议。

    为什么是这两段：一次 agent 任务里，system 与「用户最初那句话」在整个任务
    期间不变，中间的工具往返只是往后追加。所以同一任务的每次请求指纹相同，
    而换个任务必然不同 —— 不需要客户端上报任何 run id。
    """
    raw = '\x00'.join((protocol, _system_text(body), _first_user_text(body)))
    return hashlib.sha1(raw.encode('utf-8', 'replace')).hexdigest()[:20]


def _client_family(ua: str | None) -> str:
    """UA → 客户端家族。

    **认不出来时不再统一叫 `other`**：UA 的第一个 product token 本身就是客户端
    名（`deepseek-harness/0.2.2-rc.1` → `deepseek-harness`）。早先一律回落成
    `other`，界面上就只剩一个「其它」——用户看得出来 UA 里明明写着是什么客户端，
    面板却说不知道（实测反馈）。回落成 token 之后，新客户端不需要改代码也能被
    正确显示；真正的空 UA 才记 `unknown`。
    """
    text = (ua or '').strip()
    if not text:
        return 'unknown'
    low = text.lower()
    for family, needles in _CLIENT_RULES:
        if any(n in low for n in needles):
            return family
    token = re.split(r'[/\s]', text)[0].lower()
    token = re.sub(r'[^a-z0-9._-]', '', token)[:24]
    return token or 'other'


def _client_version(ua: str | None) -> str:
    """从 UA 的第一个 product token 里取版本号（`name/1.2.3` → `1.2.3`）。

    抓包页把它显示在客户端名后面：同一个客户端的不同版本行为可能不同（协议字段、
    工具调用形态都变过），只写名字在排障时不够用。
    """
    if not ua:
        return ''
    first = ua.strip().split(' ')[0]
    if '/' not in first:
        return ''
    return re.sub(r'[^0-9A-Za-z.\-]', '', first.split('/', 1)[1])[:24]


def request_digest(body: object) -> dict:
    """请求体的**可比摘要**（在截断前算）。

    为什么不直接比 body：请求体常有两三 MB，而单条存储上限默认 512 KB ——
    截断把两轮都切在**同一个字节位置**，抢救出的前缀逐字相同，「与上一轮对比」
    于是永远说「完全一致」，那是假信息（实测：325 条消息的会话，真实差异全在
    末尾几十条里）。

    摘要只保留「结构」不保留「全文」：消息逐条记 role + 内容哈希（16 位），
    工具记名字，参数记键值。它与截断无关，因此永远可比；体积只有几 KB。

    哈希用 sha1 而非全文：全文会让摘要本身变成另一份「截断体」，就白做了。
    """
    if not isinstance(body, dict):
        return {'kind': 'other'}
    digest: dict[str, Any] = {'kind': 'object'}
    msgs = body.get('messages')
    if isinstance(msgs, list):
        digest['messages'] = [
            {
                'role': str(m.get('role')) if isinstance(m, dict) else '?',
                'h': _short_hash(m.get('content') if isinstance(m, dict) else m),
                # 工具调用也算进去：agent 的每一轮差异往往就在这里
                'tc': _tool_call_names(m) if isinstance(m, dict) else [],
            }
            for m in msgs
        ]
    tools = body.get('tools')
    if isinstance(tools, list):
        digest['tools'] = sorted(
            str((t.get('function') or t).get('name'))
            for t in tools if isinstance(t, dict))
    params = _param_view(body)
    digest['params'] = {k: _short_hash(v) for k, v in params.items()}
    digest['model'] = body.get('model') if isinstance(body.get('model'), str) else ''
    digest['n'] = len(msgs) if isinstance(msgs, list) else 0
    return digest


def _short_hash(value: object) -> str:
    if value is None:
        return ''
    if isinstance(value, str):
        raw = value
    else:
        try:
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            raw = str(value)
    return hashlib.sha1(raw.encode('utf-8', 'replace')).hexdigest()[:16]


def _tool_call_names(msg: dict) -> list[str]:
    calls = msg.get('tool_calls')
    if not isinstance(calls, list):
        return []
    out: list[str] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        fn = call.get('function') if isinstance(call.get('function'), dict) else {}
        name = fn.get('name') or call.get('name')
        if isinstance(name, str):
            out.append(name)
    return out


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _preview(text: str, limit: int = 160) -> str:
    flat = ' '.join(text.split())
    return flat[:limit]


# ── 响应重建 ─────────────────────────────────────────────
# 单条流式响应的原始缓冲上限（字节）。四条协议路径共用一个值：抓包要重建出
# 「模型说了什么」就必须拿到完整帧，但上游一旦吐出异常流（没有换行、死循环
# 重发），无限累积会先把面板的内存吃掉。超出后只停止累积，不打断转发。
RAW_LIMIT = 4 * 1024 * 1024


class RawStream:
    """流式原始响应缓冲。

    为什么不直接让各路由自己 `''.join(chunks)`：三条协议路径各写一遍「累积 +
    封顶」必然会漏一处（漏的表现是某条协议抓到的响应永远是空的，而界面看起来
    只是「模型没说话」）。封顶值也只有一处，调它不必翻三个文件。

    刻意不做解码：调用方已经为转发解码过一次，这里只收字符串，避免同一段字节
    在热路径上 decode 两遍。
    """

    __slots__ = ('parts', 'kept', 'on')

    def __init__(self, on: bool | None = None) -> None:
        # 抓包关闭时这个缓冲必须整体失效：流式响应每个 chunk 都会进来，
        # 无脑累积等于给每条长流白存最多 4 MB（默认关闭是大多数部署的常态）。
        self.on = enabled() if on is None else bool(on)
        self.parts: list[str] = []
        self.kept = 0

    def feed(self, text: str) -> None:
        if not self.on or self.kept >= RAW_LIMIT:
            return
        self.parts.append(text)
        self.kept += len(text)

    def text(self) -> str:
        return ''.join(self.parts) if self.on else ''


def _merge_tool_delta(state: dict[int, dict], call: dict) -> None:
    idx = call.get('index')
    if not isinstance(idx, int):
        idx = len(state)
    slot = state.setdefault(idx, {'id': '', 'name': '', 'arguments': ''})
    if isinstance(call.get('id'), str) and call['id']:
        slot['id'] = call['id']
    fn = call.get('function') if isinstance(call.get('function'), dict) else {}
    if isinstance(fn.get('name'), str) and fn['name']:
        slot['name'] = fn['name']
    args = fn.get('arguments')
    if isinstance(args, str):
        slot['arguments'] += args
    elif isinstance(args, dict):
        slot['arguments'] += json.dumps(args, ensure_ascii=False)


def reconstruct(text: str | None = None, payload: object | None = None) -> dict:
    """把上游响应还原成「模型说了什么」。

    上游三种协议一律以 OpenAI 形态回应，所以这里只有一个解析器：流式按 SSE
    逐帧吃 `choices[0].delta`，非流式直接读 `choices[0].message`。工具调用的
    参数是跨帧拼接的（`arguments` 逐段到达），必须按 index 归并 —— 不归并就会
    变成一堆半截 JSON，界面看着像模型说胡话。
    """
    out: dict[str, Any] = {
        'text': '', 'reasoning': '', 'tool_calls': [], 'finish': None,
        'events': 0, 'done': False,
    }
    if isinstance(payload, dict):
        choices = payload.get('choices')
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            msg = choices[0].get('message') or {}
            if isinstance(msg, dict):
                out['text'] = _content_text(msg.get('content'))
                reasoning = msg.get('reasoning_content') or msg.get('reasoning')
                out['reasoning'] = reasoning if isinstance(reasoning, str) else ''
                calls = msg.get('tool_calls')
                if isinstance(calls, list):
                    state: dict[int, dict] = {}
                    for i, call in enumerate(calls):
                        if isinstance(call, dict):
                            _merge_tool_delta(state, {**call, 'index': i})
                    out['tool_calls'] = [state[k] for k in sorted(state)]
            out['finish'] = choices[0].get('finish_reason')
            out['events'] = 1
            out['done'] = True
        return out

    if not text:
        return out

    chunks: list[str] = []
    reasoning: list[str] = []
    state: dict[int, dict] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith('data:'):
            continue
        raw = line[5:].strip()
        if not raw:
            continue
        if raw == '[DONE]':
            out['done'] = True
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        out['events'] += 1
        choices = obj.get('choices')
        if not isinstance(choices, list) or not choices:
            continue
        choice = choices[0]
        if not isinstance(choice, dict):
            continue
        delta = choice.get('delta') if isinstance(choice.get('delta'), dict) else {}
        piece = delta.get('content')
        if isinstance(piece, str) and piece:
            chunks.append(piece)
        think = delta.get('reasoning_content') or delta.get('reasoning')
        if isinstance(think, str) and think:
            reasoning.append(think)
        calls = delta.get('tool_calls')
        if isinstance(calls, list):
            for call in calls:
                if isinstance(call, dict):
                    _merge_tool_delta(state, call)
        if choice.get('finish_reason'):
            out['finish'] = choice['finish_reason']

    out['text'] = ''.join(chunks)
    out['reasoning'] = ''.join(reasoning)
    out['tool_calls'] = [state[k] for k in sorted(state)]
    return out


def client_reconstruct(client_text: str | None, protocol: str) -> dict | None:
    """从**客户端视角**的响应里还原「模型说了什么」。

    为什么不能直接复用 `reconstruct`：那个函数读的是**上游**的 OpenAI 形态。
    Anthropic / Responses 两条协议回给客户端的是翻译后的报文 —— 事件名、字段名、
    工具参数所在位置全不一样。两边各解析一次，用户才能在「客户端视角」里看到
    自己真正收到的东西，而不是网关看到的原始形态。

    非流式（JSON）与流式（SSE）两种都认：先按 JSON 试，失败再按 SSE 逐帧读。
    """
    if not client_text:
        return None
    out: dict[str, Any] = {
        'text': '', 'reasoning': '', 'tool_calls': [], 'finish': None,
        'events': 0, 'done': False,
    }

    # 非流式：整份 JSON 报文
    try:
        doc = json.loads(client_text)
    except ValueError:
        doc = None
    if isinstance(doc, dict):
        if protocol == 'anthropic':
            for block in doc.get('content') or []:
                if not isinstance(block, dict):
                    continue
                kind = block.get('type')
                if kind == 'text' and isinstance(block.get('text'), str):
                    out['text'] += block['text']
                elif kind == 'thinking' and isinstance(block.get('thinking'), str):
                    out['reasoning'] += block['thinking']
                elif kind == 'tool_use':
                    out['tool_calls'].append({
                        'id': str(block.get('id') or ''),
                        'name': str(block.get('name') or ''),
                        'arguments': json.dumps(block.get('input') or {},
                                                ensure_ascii=False),
                    })
            out['finish'] = doc.get('stop_reason')
            out['done'] = True
            out['events'] = 1
            return out
        if protocol == 'responses':
            for item in doc.get('output') or []:
                if not isinstance(item, dict):
                    continue
                if item.get('type') == 'message':
                    for part in item.get('content') or []:
                        if isinstance(part, dict) and isinstance(part.get('text'), str):
                            out['text'] += part['text']
                elif item.get('type') == 'function_call':
                    out['tool_calls'].append({
                        'id': str(item.get('call_id') or ''),
                        'name': str(item.get('name') or ''),
                        'arguments': str(item.get('arguments') or ''),
                    })
                elif item.get('type') == 'reasoning':
                    for part in item.get('summary') or []:
                        if isinstance(part, dict) and isinstance(part.get('text'), str):
                            out['reasoning'] += part['text']
            out['finish'] = doc.get('status')
            out['done'] = True
            out['events'] = 1
            return out
        # chat 非流式：与上游同形
        return reconstruct(payload=doc)

    # 流式：逐帧读（两种协议的帧名不同，但都是 `data: {json}`）
    calls: dict[int, dict] = {}
    for line in client_text.splitlines():
        line = line.strip()
        if not line.startswith('data:'):
            continue
        raw = line[5:].strip()
        if not raw or raw == '[DONE]':
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        out['events'] += 1
        etype = obj.get('type')
        if protocol == 'anthropic':
            if etype == 'content_block_start':
                block = obj.get('content_block') or {}
                if isinstance(block, dict) and block.get('type') == 'tool_use':
                    idx = int(obj.get('index') or 0)
                    calls[idx] = {'id': str(block.get('id') or ''),
                                  'name': str(block.get('name') or ''),
                                  'arguments': ''}
            elif etype == 'content_block_delta':
                delta = obj.get('delta') or {}
                if isinstance(delta, dict):
                    if isinstance(delta.get('text'), str):
                        out['text'] += delta['text']
                    if isinstance(delta.get('thinking'), str):
                        out['reasoning'] += delta['thinking']
                    if isinstance(delta.get('partial_json'), str):
                        idx = int(obj.get('index') or 0)
                        slot = calls.setdefault(idx, {'id': '', 'name': '', 'arguments': ''})
                        slot['arguments'] += delta['partial_json']
            elif etype == 'message_delta':
                delta = obj.get('delta') or {}
                if isinstance(delta, dict) and delta.get('stop_reason'):
                    out['finish'] = delta['stop_reason']
            elif etype == 'message_stop':
                out['done'] = True
        elif protocol == 'responses':
            if etype == 'response.output_text.delta' and isinstance(obj.get('delta'), str):
                out['text'] += obj['delta']
            elif etype == 'response.reasoning_text.delta' and isinstance(obj.get('delta'), str):
                out['reasoning'] += obj['delta']
            elif etype == 'response.output_item.added':
                item = obj.get('item') or {}
                if isinstance(item, dict) and item.get('type') == 'function_call':
                    idx = int(obj.get('output_index') or 0)
                    calls[idx] = {'id': str(item.get('call_id') or ''),
                                  'name': str(item.get('name') or ''),
                                  'arguments': ''}
            elif etype == 'response.function_call_arguments.delta':
                idx = int(obj.get('output_index') or 0)
                slot = calls.setdefault(idx, {'id': '', 'name': '', 'arguments': ''})
                if isinstance(obj.get('delta'), str):
                    slot['arguments'] += obj['delta']
            elif etype in ('response.completed', 'response.failed', 'response.incomplete'):
                out['done'] = True
                resp = obj.get('response') or {}
                if isinstance(resp, dict) and resp.get('status'):
                    out['finish'] = resp['status']
        else:
            # 兜底：按 OpenAI 帧读（chat 透传时走不到这里）
            sub = reconstruct(text='data: ' + raw)
            out['text'] += sub['text']
            out['reasoning'] += sub['reasoning']
    out['tool_calls'] = [calls[k] for k in sorted(calls)]
    return out


# 辅助调用的 system 特征。目前只收「会话标题生成」——它是 dsh / Codex 这类客户端
# 在每轮开始时都会发的独立请求，system prompt 与主循环完全不同。
#
# 为什么值得单独认：它有自己的 system，指纹必然与主会话不同，于是**一次对话在
# 列表里显示成两条**（一条 1 轮的"标题"、一条 N 轮的主会话）。用户看到的就是
# 「为什么有两条记录」——实测反馈。
_AUX_PATTERNS = (
    ('title', (re.compile(r'create a concise title', re.I),
               re.compile(r'generate the session title', re.I),
               re.compile(r'\bsession title\b', re.I))),
    # 上下文压缩（compaction）：同样是客户端在会话中途发的**独立请求**，
    # system 换成压缩专用提示词、通常不带工具。判据刻意收紧到
    # 「compaction / 对这段对话做摘要」这类明确措辞：泛化的 summar* 会把
    # 「帮我总结这篇文章」这种正常请求也吞进来，那是误伤。
    ('compact', (re.compile(r'\bcompaction\b', re.I),
                 re.compile(r'compact (the|this) conversation', re.I),
                 re.compile(r'summar(?:y|ize) (of )?(the )?conversation so far', re.I),
                 re.compile(r'conversation summary', re.I))),
)


def _aux_kind(body: object) -> str:
    """这条请求是不是辅助调用（标题生成等）。返回 kind，普通请求返回 'main'。

    判据用「system 特征 + 没有工具」双保险：只匹配文案，客户端换个说法就漏；
    只看"没工具"，普通的纯聊天请求也会被误判成辅助调用。
    """
    system = _system_text(body)
    if not system:
        return 'main'
    if _tool_names(body):
        return 'main'
    for kind, patterns in _AUX_PATTERNS:
        if any(p.search(system) for p in patterns):
            return kind
    return 'main'


def _norm_text(text: str) -> str:
    """把两段文本归一成「可以直接比」的形态。

    现场细节（不改就会对不上）：客户端的标题请求是把人类消息**序列化进 JSON
    字符串**的，所以我方看到的是字面量 `\\n\\n`；而主会话的首条消息在入库时
    （`_clean`）已经把换行压成了空格。同一段话在两边长得完全不同，直接 `in`
    必然失败 —— 归一化要同时处理「字面量转义」与「空白折叠」。
    """
    flat = (text or '').replace('\\n', ' ').replace('\\r', ' ').replace('\\t', ' ')
    return re.sub(r'\s+', ' ', flat).strip()


def _link_parent(first_user: str, protocol: str, key_id: int | None,
                 now: int, exclude_id: int) -> int | None:
    """把辅助调用挂回它所属的主会话。

    判据是**内容包含**，不是时间窗：标题请求会把人类消息原文塞进自己的 user
    内容里（实测 `Generate the session title from this JSON array … [{"seq":13,
    "text":"…"}]`），所以主会话的「首条 user 消息」必然是这段文本的子串。
    时间窗（10 分钟）只是把扫描范围收窄，不作为判据本身 —— 并发跑多个会话时
    时间窗会同时命中多条，包含匹配不会。
    """
    if not first_user:
        return None
    rows = db.query(
        'SELECT id, first_user FROM tap_sessions '
        "WHERE COALESCE(kind, 'main') = 'main' AND id != ? AND protocol = ? "
        'AND COALESCE(key_id, 0) = COALESCE(?, 0) AND updated_at >= ? '
        'ORDER BY updated_at DESC LIMIT 50',
        (exclude_id, protocol, key_id, now - 600),
    )
    needle = _norm_text(first_user)
    if len(needle) < 8:
        return None
    for row in rows:
        candidate = _norm_text(row['first_user'] or '')
        if not candidate:
            continue
        # 由长到短试几种前缀：客户端可能把人类消息**截断**后再塞进标题请求
        # （实测遇到过只保留首句的情况），所以只比整条必然对不上。短到 16 个字
        # 仍然足够具体（中文 16 字基本不会撞车），再短就不可靠了。
        # 下限放到 6 个字：真实的首条消息经常很短（「继续」「hi」「看下这个」），
        # 卡在 16 字会让这些会话永远挂不上辅助调用（实测：15 字的中文标题就漏了）。
        # 6 字以上 + 同密钥 + 同协议 + 10 分钟窗，误挂的概率已经很低。
        for size in (120, 60, 30, 16, 8, 6):
            sig = candidate[:size]
            if len(sig) >= 6 and sig in needle:
                return int(row['id'])
    return None


# ── 写入 ─────────────────────────────────────────────────
def _session_id(fingerprint: str, key_id: int | None, client: str,
                protocol: str, model: str | None, now: int,
                ua: str | None = None, first_user: str = '',
                kind: str = 'main') -> int:
    row = db.query_one(
        'SELECT id FROM tap_sessions WHERE fingerprint = ? '
        'AND COALESCE(key_id, 0) = COALESCE(?, 0) AND protocol = ? '
        'AND updated_at >= ? ORDER BY updated_at DESC LIMIT 1',
        (fingerprint, key_id, protocol, now - _SESSION_IDLE_SECONDS),
    )
    if row:
        # 首轮没有 UA（或旧记录还没有这一列）时，用后续轮补齐 —— UA 是事实，
        # client 只是从它派生的分类，能补就补。
        db.execute(
            'UPDATE tap_sessions SET client_ua = COALESCE(client_ua, ?), '
            'first_user = COALESCE(first_user, ?) WHERE id = ?',
            (_clean(ua, 200), _clean(first_user, 4000), row['id']),
        )
        return int(row['id'])
    session_id = db.execute(
        'INSERT INTO tap_sessions(fingerprint, key_id, client, client_ua, protocol, '
        'model, started_at, updated_at, turns, prompt_tokens, completion_tokens, '
        'first_user, kind) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?)',
        (fingerprint, key_id, client, _clean(ua, 200), protocol,
         _clean(model, 128), now, now, _clean(first_user, 4000), kind),
    )
    if kind != 'main':
        parent = _link_parent(first_user, protocol, key_id, now, session_id)
        if parent:
            db.execute('UPDATE tap_sessions SET parent_id = ? WHERE id = ?',
                       (parent, session_id))
    return session_id


def _clean(value: object, limit: int) -> str | None:
    """外部文本入库前的清洗：去掉控制字符并截断。

    与 db._clean 同口径（那里处理的是日志字段）。这里再包一层是因为抓包的
    文本来源更多：请求头、UA、上游错误体，都可能带换行与控制字符 —— 存进库
    再原样渲染，日志/详情页就会出现伪造出来的额外行。
    """
    if value is None:
        return None
    text = str(value).replace('\r', ' ').replace('\n', ' ')
    text = ''.join(ch for ch in text if ch >= ' ' or ch == '\t')
    return text[:limit]


def snapshot(body: object) -> object:
    """客户端原始请求体的快照，供抓包的「客户端视角」使用。

    **关闭抓包时直接返回原对象**，不做深拷贝：`_chat` / `/v1/messages` /
    `/v1/responses` 都在转发前置阶段调用它，而大上下文请求几十万 token 的
    JSON 深拷贝一次就是几十毫秒与等量内存 —— 抓包默认关闭，不能让它给正常
    转发加这份成本。开启时才拷贝，且必须早于任何改写。
    """
    if not enabled():
        return body
    import copy
    try:
        return copy.deepcopy(body)
    except Exception:  # noqa: BLE001 - 快照失败不该影响请求
        return body


def capture(*, body: object, headers: Iterable[tuple[str, str]] | None,
            key: dict | None, ua: str | None, path: str, protocol: str,
            status: int, latency_ms: int | None = None,
            first_token_ms: int | None = None, stream: bool = False,
            text: str | None = None, payload: object | None = None,
            usage: dict | None = None, error: str | None = None,
            upstream_body: object | None = None,
            upstream_headers: object | None = None,
            trace_id: str | None = None,
            client_response: object | None = None) -> int | None:
    """记录一轮调用。返回 turn id，未抓包时返回 None。

    **两端都存**，因为「客户端发的」与「网关发出去的」本来就是两份报文：

    * `body` —— 客户端**原样**发来的请求体（调用方必须在改写**之前**深拷贝一份）；
    * `upstream_body` —— 网关改写后真正发往上游的形态（模型映射、`max_tokens`
      钳制、图片归一、`stream_options` 注入都发生在这条路径上）。

    响应同理：`text` / `payload` 是**上游**原文，`client_response` 是**回给
    客户端**的报文（Chat 是透传；Anthropic / Responses 是翻译后的形态）。少了
    任何一边，排查「转换层吃了什么字段」都只能靠猜。

    调用方必须保证异常不外溢：本函数自己兜底，但抓包在请求收尾路径上，
    多一层保险不值得计较。
    """
    global _turn_writes
    try:
        if not enabled():
            return None
        cfg = settings()
        now = int(time.time())
        view_body = upstream_body if isinstance(upstream_body, dict) else body
        fingerprint = _fingerprint(view_body, protocol)
        client = _client_family(ua)
        model = None
        if isinstance(body, dict) and isinstance(body.get('model'), str):
            model = body['model']
        kind = _aux_kind(view_body)
        session_id = _session_id(fingerprint, key['id'] if key else None,
                                 client, protocol, model, now, ua,
                                 _first_user_text(view_body), kind)
        row = db.query_one('SELECT turns FROM tap_sessions WHERE id = ?', (session_id,))
        seq = int(row['turns']) + 1 if row else 1

        recon = reconstruct(text=text, payload=payload)
        usage_obj = usage if isinstance(usage, dict) else {}

        # 摘要要在**截断前**算：截断后的 body 已经不是全量，摘要就失去意义。
        digest = request_digest(view_body)

        request_doc = {
            'headers': redact_headers(headers),
            # 网关发给上游的那套头（Content-Type、Authorization…）。**必须一起留**：
            # 客户端头里的 Authorization 是它自己的网关密钥，上游头里的是上游密钥，
            # 两份都对不上时，「凭证到底换了没换」光看 body 永远看不出来。
            'upstream_headers': redact_headers(
                upstream_headers.items() if isinstance(upstream_headers, dict)
                else upstream_headers),
            # `body` 是**客户端原样**发出的体（调用方在改写前快照）；
            # `upstream_body` 是网关改写后的体。相同时只在上面存一份。
            'body': body,
        }
        if upstream_body is not None and upstream_body is not body:
            request_doc['upstream_body'] = upstream_body
        request_text = scrub(json.dumps(request_doc, ensure_ascii=False, default=str))
        request_text, cut_request = _clip(request_text, cfg['max_body'])

        response_text = ''
        cut_response = False
        if text is not None:
            response_text, cut_response = _clip(scrub(text), cfg['max_body'])
        elif payload is not None:
            response_text, cut_response = _clip(
                scrub(json.dumps(payload, ensure_ascii=False, default=str)),
                cfg['max_body'])

        # 客户端视角：只有翻译层（Anthropic / Responses）才与上游不同。Chat 是
        # 透传，**不重复存一份** —— 界面按「透传」直接复用上游原文，省一半空间。
        client_text = ''
        if client_response is not None:
            raw_client = (client_response if isinstance(client_response, str)
                          else json.dumps(client_response, ensure_ascii=False,
                                          default=str))
            client_text, cut_client = _clip(scrub(raw_client), cfg['max_body'])
            cut_response = cut_response or cut_client

        recon_text, cut_recon = _clip(
            json.dumps(recon, ensure_ascii=False, default=str), cfg['max_body'])

        pt = _as_int(usage_obj.get('prompt_tokens'))
        ct = _as_int(usage_obj.get('completion_tokens'))
        # 缓存命中段与费用：与 request_logs 同口径，界面上两处数字才对得上。
        hit = usage_obj.get('prompt_cache_hit_tokens')
        miss = usage_obj.get('prompt_cache_miss_tokens')
        try:
            cost = db._estimate_cost(model, pt, ct, hit, miss, now)
        except Exception:  # noqa: BLE001 - 价目表缺失不该影响抓包
            cost = None

        turn_id = db.execute(
            'INSERT INTO tap_turns(session_id, seq, ts, path, protocol, model, '
            'mapped_model, status, stream, latency_ms, first_token_ms, '
            'prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens, '
            'cost, request_json, request_digest, response_text, client_response_text, '
            'recon_json, usage_json, error, truncated, trace_id) '
            'VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (session_id, seq, now, _clean(path, 128), protocol, _clean(model, 128),
             _clean(model, 128), status, 1 if stream else 0, latency_ms,
             first_token_ms, pt, ct, _as_int(hit), _as_int(miss), cost,
             request_text, json.dumps(digest, ensure_ascii=False, default=str),
             response_text, client_text or None, recon_text,
             json.dumps(usage_obj, ensure_ascii=False, default=str)[:8192],
             _clean(error, 500), 1 if (cut_request or cut_response or cut_recon) else 0,
             _clean(trace_id, 64)),
        )
        db.execute(
            'UPDATE tap_sessions SET updated_at = ?, turns = ?, '
            'prompt_tokens = prompt_tokens + ?, completion_tokens = completion_tokens + ?, '
            'model = COALESCE(?, model) WHERE id = ?',
            (now, seq, pt, ct, _clean(model, 128), session_id),
        )
        _turn_writes += 1
        if _turn_writes >= _PRUNE_EVERY:
            _turn_writes = 0
            prune()
        return turn_id
    except Exception:  # noqa: BLE001 - 抓包是旁路，任何异常都不能影响请求
        return None


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def prune() -> dict[str, int]:
    """按保留期与会话数上限清理。幂等，写入侧周期调用，也可手动触发。"""
    cfg = settings()
    cutoff = int(time.time()) - int(cfg['retention_days']) * 86400
    old = db.execute('DELETE FROM tap_turns WHERE ts < ?', (cutoff,))
    keep = int(cfg['max_sessions'])
    rows = db.query(
        'SELECT id FROM tap_sessions ORDER BY updated_at DESC LIMIT -1 OFFSET ?',
        (keep,),
    )
    dropped = 0
    for row in rows:
        dropped += db.execute('DELETE FROM tap_turns WHERE session_id = ?', (row['id'],))
        db.execute('DELETE FROM tap_sessions WHERE id = ?', (row['id'],))
    # 空会话：只剩壳的会话在列表里是一行什么都点不开的条目。
    db.execute('DELETE FROM tap_sessions WHERE id NOT IN (SELECT session_id FROM tap_turns)')
    return {'turns': old + dropped}


# ── 查询 ─────────────────────────────────────────────────
def backfill_aux() -> int:
    """按已入库的请求体重新判定会话类型并补上归属（旧数据一次性修正）。

    两遍：先把每条会话的 `first_user` 补齐（这一列是后加的，存量记录为空），
    再判定类型并做包含匹配 —— 否则归属查询永远拿 NULL 去比，必然对不上。
    """
    changed = 0
    rows = db.query('SELECT id, kind, parent_id, protocol, key_id, first_user '
                    'FROM tap_sessions')
    filled: dict[int, str] = {}
    for row in rows:
        first_user = row['first_user'] or ''
        if not first_user:
            turn_row = db.query_one(
                'SELECT request_json FROM tap_turns WHERE session_id = ? '
                'ORDER BY seq ASC LIMIT 1', (row['id'],))
            if turn_row:
                doc, ok = _loads(turn_row['request_json'])
                if ok and isinstance(doc, dict):
                    first_user = _first_user_text(doc.get('upstream_body') or doc.get('body'))
                    if first_user:
                        db.execute('UPDATE tap_sessions SET first_user = ? WHERE id = ?',
                                   (_clean(first_user, 4000), row['id']))
        filled[int(row['id'])] = first_user

    # 第二遍：判定类型 + 归属。先把所有主会话定下来（它们可能是辅助调用的父），
    # 再处理辅助调用。
    pending: list[tuple[int, str, str, str | None, int | None, int]] = []
    for row in rows:
        stored = row['kind'] or 'main'
        # 已经是辅助调用、但还没挂上父的（上一版回填留下的），也要补一次归属；
        # 挂上了的就不重复算。
        if stored != 'main' and row['parent_id'] is not None:
            continue
        turn_row = db.query_one(
            'SELECT request_json, ts FROM tap_turns WHERE session_id = ? '
            'ORDER BY seq ASC LIMIT 1', (row['id'],))
        if not turn_row:
            continue
        doc, ok = _loads(turn_row['request_json'])
        if not ok or not isinstance(doc, dict):
            continue
        body = doc.get('upstream_body') or doc.get('body')
        kind = stored if stored != 'main' else _aux_kind(body)
        if kind == 'main':
            continue
        pending.append((int(row['id']), kind, str(filled.get(int(row['id'])) or ''),
                        row['protocol'], row['key_id'], int(turn_row['ts'])))
    for session_id, kind, first_user, protocol, key_id, ts in pending:
        # key_id 必须一起传：归属查询按「同一把密钥」过滤，传 None 会退化成
        # 「key_id 为 0 的那些会话」，一条都对不上。
        parent = _link_parent(first_user, str(protocol or 'chat'), key_id, ts + 1,
                              session_id)
        db.execute('UPDATE tap_sessions SET kind = ?, parent_id = ? WHERE id = ?',
                   (kind, parent, session_id))
        changed += 1
    return changed


def backfill_clients() -> int:
    """按入库的 UA 重新归类旧会话。返回改动条数。

    为什么需要：`client` 是**派生**值（从 UA 算出来的）。分类规则一改（例如
    早先把认不出的 UA 一律记成 `other`），历史记录不会自己变对 —— 而它们恰好是
    用户已经看着的那批。UA 原文一直存在 `tap_turns.request_json` 里，所以可以
    原地重算，不必重新抓。
    """
    changed = 0
    rows = db.query('SELECT id, client, client_ua FROM tap_sessions')
    for row in rows:
        ua = row['client_ua']
        if not ua:
            turn_row = db.query_one(
                'SELECT request_json FROM tap_turns WHERE session_id = ? '
                'ORDER BY seq ASC LIMIT 1', (row['id'],))
            if turn_row:
                doc, ok = _loads(turn_row['request_json'])
                if ok and isinstance(doc, dict):
                    headers = doc.get('headers') or {}
                    if isinstance(headers, dict):
                        for key, value in headers.items():
                            if str(key).lower() == 'user-agent':
                                ua = str(value)
                                break
        if not ua:
            continue
        family = _client_family(ua)
        # 分类没变也要补写 UA：旧记录有 UA 原文却缺 `client_ua`，缺了版本号，
        # 界面上就只能显示一个光秃秃的客户端名。
        if family == row['client'] and row['client_ua']:
            continue
        db.execute('UPDATE tap_sessions SET client = ?, client_ua = ? WHERE id = ?',
                   (family, _clean(ua, 200), row['id']))
        changed += 1
    return changed


def stats() -> dict:
    row = db.query_one(
        'SELECT COUNT(*) AS sessions, '
        'COALESCE(SUM(turns), 0) AS turns, '
        'COALESCE(SUM(prompt_tokens), 0) AS pt, '
        'COALESCE(SUM(completion_tokens), 0) AS ct, '
        'MIN(started_at) AS first_at, MAX(updated_at) AS last_at FROM tap_sessions')
    return {
        'sessions': int(row['sessions'] or 0),
        'turns': int(row['turns'] or 0),
        'prompt_tokens': int(row['pt'] or 0),
        'completion_tokens': int(row['ct'] or 0),
        'first_at': row['first_at'],
        'last_at': row['last_at'],
    }


def list_sessions(page: int = 1, size: int = 20, days: int | None = None,
                  client: str | None = None, q: str | None = None,
                  include_aux: bool = False) -> dict:
    where: list[str] = []
    args: list[Any] = []
    # 辅助调用（会话标题生成等）默认不单列：它们属于某条主会话，混在列表里
    # 会让人以为「一次对话被记了两条」（实测反馈）。要看就显式打开。
    if not include_aux:
        where.append("COALESCE(s.kind, 'main') = 'main'")
    if days:
        where.append('updated_at >= ?')
        args.append(int(time.time()) - max(1, min(int(days), 90)) * 86400)
    if client and client not in ('all', ''):
        where.append('client = ?')
        args.append(client)
    if q:
        where.append('(model LIKE ? OR fingerprint LIKE ?)')
        args.extend([f'%{q}%', f'%{q}%'])
    page = max(1, min(int(page), 100_000))
    size = max(1, min(int(size), 200))
    clause = ('WHERE ' + ' AND '.join(where)) if where else ''
    total = int(db.query_one(
        f'SELECT COUNT(*) AS c FROM tap_sessions s {clause}', args)['c'] or 0)
    # `turns` 一律从 tap_turns **实算**，不用会话表里的汇总列。
    # 教训（2026-11-08）：库损坏后恢复出的汇总列还在、明细行全丢，列表于是显示
    # 「33 轮」而每一轮都点不开 —— 界面上看起来像功能坏了，其实是数字在撒谎。
    rows = db.query(
        f'SELECT s.*, k.name AS key_name, '
        f'(SELECT COUNT(*) FROM tap_turns t WHERE t.session_id = s.id) AS actual_turns, '
        f'(SELECT COUNT(*) FROM tap_sessions a WHERE a.parent_id = s.id) AS aux_count '
        f'FROM tap_sessions s '
        f'LEFT JOIN api_keys k ON k.id = s.key_id {clause} '
        f'ORDER BY s.updated_at DESC LIMIT ? OFFSET ?',
        (*args, size, (page - 1) * size),
    )
    return {
        'total': total,
        'items': [
            {
                'id': r['id'],
                'fingerprint': r['fingerprint'],
                'key_id': r['key_id'],
                'key_name': r['key_name'],
                'client': r['client'],
                'client_ua': r['client_ua'],
                'kind': r['kind'],
                'parent_id': r['parent_id'],
                'protocol': r['protocol'],
                'model': r['model'],
                'started_at': r['started_at'],
                'updated_at': r['updated_at'],
                'turns': r['actual_turns'],
                # 挂在这条会话底下的辅助调用（标题 / 压缩）次数。辅助调用默认
                # 不单列，但完全看不见也不行 —— 用户会以为「标题没被记下来」。
                'aux_count': r['aux_count'] or 0,
                'prompt_tokens': r['prompt_tokens'],
                'completion_tokens': r['completion_tokens'],
            }
            for r in rows
        ],
    }


def session(session_id: int) -> dict | None:
    """会话详情：会话头 + 每一轮的摘要（不含正文，正文单条取）。"""
    row = db.query_one(
        'SELECT s.*, k.name AS key_name FROM tap_sessions s '
        'LEFT JOIN api_keys k ON k.id = s.key_id WHERE s.id = ?', (session_id,))
    if not row:
        return None
    turns = db.query(
        'SELECT id, seq, ts, status, stream, latency_ms, first_token_ms, model, '
        'mapped_model, prompt_tokens, completion_tokens, cache_hit_tokens, '
        'cache_miss_tokens, cost, error, truncated, protocol, '
        'LENGTH(request_json) AS request_bytes, '
        'LENGTH(response_text) AS response_bytes FROM tap_turns '
        'WHERE session_id = ? ORDER BY seq ASC', (session_id,))
    return {
        'id': row['id'],
        'fingerprint': row['fingerprint'],
        'key_id': row['key_id'],
        'key_name': row['key_name'],
        'client': row['client'],
        'client_ua': row['client_ua'],
        'kind': row['kind'],
        'parent_id': row['parent_id'],
        'protocol': row['protocol'],
        'model': row['model'],
        'started_at': row['started_at'],
        'updated_at': row['updated_at'],
        'turns': len(turns),
        # 名下挂着的辅助调用：界面上可以直接跳过去，不必先去列表打开「显示辅助调用」。
        'aux': [
            {'id': c['id'], 'kind': c['kind'], 'ts': c['ts'], 'model': c['model']}
            for c in db.query(
                # 会话表没有 `ts` 列（只有 started_at / updated_at）：辅助调用
                # 按创建时刻排序，用 started_at 即可。
                'SELECT id, kind, started_at AS ts, model FROM tap_sessions '
                'WHERE parent_id = ? ORDER BY started_at ASC LIMIT 50', (session_id,))
        ],
        'prompt_tokens': row['prompt_tokens'],
        'completion_tokens': row['completion_tokens'],
        'items': [
            {
                'id': t['id'],
                'seq': t['seq'],
                'ts': t['ts'],
                'status': t['status'],
                'stream': bool(t['stream']),
                'latency_ms': t['latency_ms'],
                'first_token_ms': t['first_token_ms'],
                'model': t['model'],
                'mapped_model': t['mapped_model'],
                'prompt_tokens': t['prompt_tokens'],
                'completion_tokens': t['completion_tokens'],
                'cache_hit_tokens': t['cache_hit_tokens'],
                'cache_miss_tokens': t['cache_miss_tokens'],
                'cost': t['cost'],
                'error': t['error'],
                'truncated': bool(t['truncated']),
                'protocol': t['protocol'],
                'request_bytes': t['request_bytes'] or 0,
                'response_bytes': t['response_bytes'] or 0,
            }
            for t in turns
        ],
    }


def _loads(text: str | None) -> tuple[object | None, bool]:
    """解析入库的 JSON 文本。返回 (解析结果, 是否可解析)。

    截断过的行必然解析不了 —— 这正是 `truncated` 存在的原因：界面据此
    展示原文，而不是把半截 JSON 解析出的空对象当成「这一轮什么都没发」。
    """
    if not text:
        return None, False
    try:
        return json.loads(text), True
    except ValueError:
        return None, False


def turn(turn_id: int) -> dict | None:
    row = db.query_one('SELECT * FROM tap_turns WHERE id = ?', (turn_id,))
    if not row:
        return None
    request, request_ok = _loads(row['request_json'])
    recon, _ = _loads(row['recon_json'])
    usage, _ = _loads(row['usage_json'])
    response, response_ok = _loads(row['response_text'])
    # 客户端视角的响应：Chat 是透传（直接复用上游原文），翻译层才有独立报文。
    # 存量行没有这一列（NULL）—— 界面按「未采集」显示，不拿上游原文冒充。
    protocol = str(row['protocol'] or 'chat')
    client_text = row['client_response_text'] if 'client_response_text' in row.keys() else None
    if client_text:
        client_doc, client_ok = _loads(client_text)
        client_recon = client_reconstruct(client_text, protocol)
    elif protocol == 'chat':
        client_doc, client_ok = response, response_ok
        client_recon = recon
    else:
        client_doc, client_ok, client_recon = None, False, None
    return {
        'id': row['id'],
        'session_id': row['session_id'],
        'seq': row['seq'],
        'ts': row['ts'],
        'path': row['path'],
        'protocol': row['protocol'],
        'model': row['model'],
        'mapped_model': row['mapped_model'],
        'status': row['status'],
        'stream': bool(row['stream']),
        'latency_ms': row['latency_ms'],
        'first_token_ms': row['first_token_ms'],
        'prompt_tokens': row['prompt_tokens'],
        'completion_tokens': row['completion_tokens'],
        'cache_hit_tokens': row['cache_hit_tokens'],
        'cache_miss_tokens': row['cache_miss_tokens'],
        'cost': row['cost'],
        'error': row['error'],
        'truncated': bool(row['truncated']),
        'request': request,
        'request_ok': request_ok,
        # 截断时 request_json 本身就是原文（diff 的兜底路径要用它）；
        # 未截断时 request_raw 为 None，界面直接用解析好的 request。
        'request_raw': None if request_ok else row['request_json'],
        'request_text': row['request_json'],
        # 摘要（截断前算的）：diff 在截断场景改用它
        'request_digest': _loads(row['request_digest'])[0]
        if 'request_digest' in row.keys() else None,
        'response': response,
        'response_ok': response_ok,
        'response_raw': row['response_text'] if (not response_ok or row['stream']) else None,
        'recon': recon or {'text': '', 'reasoning': '', 'tool_calls': []},
        'usage': usage or {},
        'trace_id': row['trace_id'] if 'trace_id' in row.keys() else None,
        # ── 两端视角 ──────────────────────────────────────────
        # `upstream_*` 是网关与上游之间那一跳，`client_*` 是客户端手里那份。
        # 对 Chat 两者相同（透传），对 Anthropic / Responses 是两份不同报文。
        'upstream_response': response,
        'upstream_response_ok': response_ok,
        'upstream_recon': recon or {'text': '', 'reasoning': '', 'tool_calls': []},
        'client_response': client_doc,
        'client_response_ok': client_ok,
        'client_response_raw': client_text if (client_text and not client_ok) else None,
        'client_recon': client_recon,
        'passthrough': protocol == 'chat' or not client_text,
        # 按协议解读：客户端视角的那份报文用哪种协议解析（chat 透传时同上）
        'client_protocol': 'chat' if protocol == 'chat' else protocol,
    }


def _summary(body: object) -> dict:
    """请求体的结构摘要：消息数、工具数、system 长度、各角色条数。

    存不进 `tap_turns` 是因为它随每次请求变化，而这里算的是**当次**的视图；
    放在 API 里现算，数据库就能少一列。
    """
    msgs = _messages(body)
    roles: dict[str, int] = {}
    for msg in msgs:
        role = str(msg.get('role') or 'unknown')
        roles[role] = roles.get(role, 0) + 1
    return {
        'messages': len(msgs),
        'roles': roles,
        'tools': len(_tool_names(body)),
        'system_chars': len(_system_text(body)),
        'tool_names': _tool_names(body)[:60],
    }


def turn_view(turn_id: int) -> dict | None:
    """详情视图：在 turn() 之上补上摘要，省得前端自己数。"""
    data = turn(turn_id)
    if not data:
        return None
    body = None
    if isinstance(data['request'], dict):
        body = data['request'].get('upstream_body') or data['request'].get('body')
    data['summary'] = _summary(body)
    return data


def delete_session(session_id: int) -> bool:
    db.execute('DELETE FROM tap_turns WHERE session_id = ?', (session_id,))
    return bool(db.execute('DELETE FROM tap_sessions WHERE id = ?', (session_id,)))


def clear() -> dict:
    turns = db.execute('DELETE FROM tap_turns')
    db.execute('DELETE FROM tap_sessions')
    return {'ok': True, 'turns': turns}


# ── 对比（claude-tap 的 diff 视图）────────────────────────
def _msg_view(msg: dict) -> dict:
    return {
        'role': str(msg.get('role') or 'unknown'),
        'preview': _preview(_content_text(msg.get('content'))),
        'chars': len(_content_text(msg.get('content'))),
    }


def _tool_map(body: object) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(body, dict):
        return out
    tools = body.get('tools')
    if not isinstance(tools, list):
        return out
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get('function') if isinstance(tool.get('function'), dict) else tool
        name = fn.get('name')
        if isinstance(name, str) and name:
            out[name] = json.dumps(fn, ensure_ascii=False, sort_keys=True)
    return out


def _param_view(body: object) -> dict[str, str]:
    """需要对比的顶层参数。messages/tools/system 各自单独比，不重复计入。"""
    if not isinstance(body, dict):
        return {}
    skip = {'messages', 'input', 'tools', 'system', 'stream_options'}
    return {
        k: json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)[:400]
        for k, v in body.items() if k not in skip
    }


def _diff_from_digest(digest_a: dict, digest_b: dict,
                      turn_a: dict, turn_b: dict) -> dict:
    """用摘要比两轮（截断场景）。

    摘要的 messages 是逐条 `{role, h(内容哈希), tc(工具调用名)}`，因此能精确
    指出「新增了哪几条、改了哪几条」——这正是截断丢掉的部分。
    """
    msgs_a = digest_a.get('messages') or []
    msgs_b = digest_b.get('messages') or []
    common = min(len(msgs_a), len(msgs_b))
    changed = [
        {'index': i, 'role': str(msgs_b[i].get('role')),
         'before': _digest_preview(msgs_a[i]), 'after': _digest_preview(msgs_b[i])}
        for i in range(common)
        if msgs_a[i].get('h') != msgs_b[i].get('h')
        or msgs_a[i].get('role') != msgs_b[i].get('role')
    ]
    added = [
        {'index': i, 'role': str(msgs_b[i].get('role')),
         'preview': _digest_preview(msgs_b[i]), 'chars': 0}
        for i in range(common, len(msgs_b))
    ]
    removed = [
        {'index': i, 'role': str(msgs_a[i].get('role')),
         'preview': _digest_preview(msgs_a[i])}
        for i in range(common, len(msgs_a))
    ]
    params_a = digest_a.get('params') or {}
    params_b = digest_b.get('params') or {}
    params = [
        {'key': k, 'before': params_a.get(k), 'after': params_b.get(k)}
        for k in sorted(set(params_a) | set(params_b))
        if params_a.get(k) != params_b.get(k)
    ]
    tools_a = digest_a.get('tools') or []
    tools_b = digest_b.get('tools') or []
    return {
        'before': {'id': turn_a['id'], 'seq': turn_a['seq'], 'ts': turn_a['ts'],
                   'model': turn_a.get('model')},
        'after': {'id': turn_b['id'], 'seq': turn_b['seq'], 'ts': turn_b['ts'],
                  'model': turn_b.get('model')},
        'params': params,
        'system': {
            'changed': False,
            'before_chars': 0, 'after_chars': 0, 'before': '', 'after': '',
        },
        'messages': {
            'before_total': len(msgs_a), 'after_total': len(msgs_b),
            'common': common, 'added': added[:50], 'removed': removed[:50],
            'changed': changed[:50],
        },
        'tools': {
            'added': sorted(set(tools_b) - set(tools_a))[:50],
            'removed': sorted(set(tools_a) - set(tools_b))[:50],
            'changed': [],
        },
        'summary': {
            'params': len(params), 'system': 0, 'added': len(added),
            'removed': len(removed), 'changed': len(changed),
            'tools': len(set(tools_b) - set(tools_a)) + len(set(tools_a) - set(tools_b)),
        },
        # 截断下用的是摘要：界面要说明「比的是摘要」，否则用户会以为看到的是全文
        'from_digest': True,
    }


def _digest_preview(msg: dict) -> str:
    """摘要里一条消息的可读摘要：角色 + 工具调用名 + 哈希前缀。"""
    role = str(msg.get('role') or '?')
    tools = msg.get('tc') or []
    h = str(msg.get('h') or '')[:8]
    if tools:
        return f'{role} · {", ".join(str(t) for t in tools[:3])} · {h}'
    return f'{role} · {h}'


def _body_of(turn_row: dict) -> object:
    """取这一轮的请求体（用于 diff）。

    **截断的轮次必须也能比**：请求体超过 `max_body` 时 `request_json` 解析不了，
    `request` 是 None —— 两个 None 相比会「完全一致」，那是**假信息**（实测
    用户看到的正是这个）。所以这里退一步：从 `request_raw` 里切出能解析的
    body 前缀（`salvage_json_prefix`），至少能比出「多了哪些消息」。
    """
    req = turn_row.get('request')
    if isinstance(req, dict):
        body = req.get('upstream_body') or req.get('body')
        if body is not None:
            return body
    raw = turn_row.get('request_raw') or turn_row.get('request_text')
    if raw:
        return salvage_json_prefix(raw)
    return None


def salvage_json_prefix(raw: str) -> object | None:
    """从（可能被截断的）入库原文里切出能解析的请求体前缀。

    入库结构是 `{headers, body, upstream_body}`，截断发生在 body 中段。做法：
    找最后一个 `"body": {`（**取最后一次** —— headers 的值里也可能出现
    "body" 字样），把 body 之前那段补齐解析得到 headers；body 本身逐字符扫描，
    记录字符串边界与括号栈，切到最后一个完整闭合点并补齐右括号。
    """
    if not raw:
        return None
    needle = '"body": {'
    at = -1
    scan = 0
    while True:
        pos = raw.find(needle, scan)
        if pos < 0:
            break
        at = pos
        scan = pos + 1
    if at < 0:
        return _json_prefix(raw)
    body_raw = raw[at + len(needle) - 1:]
    return _json_prefix(body_raw)


def _json_prefix(text: str) -> object | None:
    """切出 JSON 文本里可解析的最长前缀（补齐未闭合的括号）。

    **切点必须落在「字符串之外」**：截断常发生在某个字符串中间（例如
    `"name": "bas`），此时切出来的前缀里有一个**未闭合的字符串**，
    `JSON.parse` 必然失败 —— 整个抢救作废，界面上请求体就是空的（实测：
    minimax 那条 512 KB 截断记录，body 完全显示不出来）。

    所以：先扫出「不在字符串里」的逗号/括号位置，只在这些位置切；再把
    切点回退到**最近一个完整闭合的容器**上 —— 宁可少给几 KB，也不能给出一段
    解析不了的文本。
    """
    trimmed = text.strip()
    if not trimmed or trimmed[0] not in '{[':
        return None
    stack: list[str] = []
    in_string = False
    escaped = False
    # 安全切点：每个元素结束时（对象/数组的闭合处），且不在字符串里
    safe_ends: list[int] = []
    last_comma = -1
    for i, ch in enumerate(trimmed):
        if in_string:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch in '{[':
            stack.append(ch)
            continue
        if ch in '}]':
            if stack:
                stack.pop()
            safe_ends.append(i + 1)
            if not stack:
                break
            continue
        if ch == ',' and stack:
            last_comma = i
    # 兜底切点：最后一个「在字符串外的逗号」——它前面是一个完整元素
    if last_comma > 0:
        safe_ends.append(last_comma)
    if not safe_ends:
        return None
    # 从后往前试：越靠后给的内容越全，但必须是能解析的
    for end in sorted(set(safe_ends), reverse=True):
        if end <= 0:
            continue
        prefix = trimmed[:end]
        # 补齐未闭合的括号（按扫描到的栈）
        depth = _bracket_depth(trimmed, end)
        for ch in reversed(depth):
            prefix += '}' if ch == '{' else ']'
        prefix = prefix.rstrip().rstrip(',').rstrip()
        if not prefix:
            continue
        try:
            return json.loads(prefix)
        except ValueError:
            continue
    return None


def _bracket_depth(text: str, upto: int) -> list[str]:
    """文本前 ``upto`` 个字符处仍未闭合的括号（栈）。"""
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in text[:upto]:
        if in_string:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch in '{[':
            stack.append(ch)
        elif ch in '}]' and stack:
            stack.pop()
    return stack


def diff(before_turn_id: int, after_turn_id: int) -> dict | None:
    """两轮请求的结构化差异。

    为什么用结构而不是统一 diff 文本：agent 的一轮对话里，90% 的字节是上次
    已经看过的历史消息，行级 diff 会把新增的两条工具结果埋在几千行「未变」里。
    按「参数 / system / 消息 / 工具」四块比，用户一眼就能看到这一轮到底多了什么。
    """
    a = turn(before_turn_id)
    b = turn(after_turn_id)
    if not a or not b:
        return None
    body_a, body_b = _body_of(a), _body_of(b)

    # 截断的两轮：body 前缀逐字相同（切在同一字节位置），必须改用**摘要**比。
    # 摘要在截断前算好（request_digest），与截断无关。
    digest_a = a.get('request_digest')
    digest_b = b.get('request_digest')
    if a.get('truncated') or b.get('truncated'):
        if isinstance(digest_a, dict) and isinstance(digest_b, dict):
            return _diff_from_digest(digest_a, digest_b, a, b)

    params_a, params_b = _param_view(body_a), _param_view(body_b)
    params = [
        {'key': key, 'before': params_a.get(key), 'after': params_b.get(key)}
        for key in sorted(set(params_a) | set(params_b))
        if params_a.get(key) != params_b.get(key)
    ]

    sys_a, sys_b = _system_text(body_a), _system_text(body_b)
    system = {
        'changed': sys_a != sys_b,
        'before_chars': len(sys_a),
        'after_chars': len(sys_b),
        'before': _preview(sys_a, 400),
        'after': _preview(sys_b, 400),
    }

    msgs_a = [_msg_view(m) for m in _messages(body_a)]
    msgs_b = [_msg_view(m) for m in _messages(body_b)]
    common = min(len(msgs_a), len(msgs_b))
    changed = [
        {'index': i, 'role': msgs_b[i]['role'],
         'before': msgs_a[i]['preview'], 'after': msgs_b[i]['preview']}
        for i in range(common)
        if msgs_a[i] != msgs_b[i]
    ]
    added = [
        {'index': i, 'role': msgs_b[i]['role'], 'preview': msgs_b[i]['preview'],
         'chars': msgs_b[i]['chars']}
        for i in range(common, len(msgs_b))
    ]
    removed = [
        {'index': i, 'role': msgs_a[i]['role'], 'preview': msgs_a[i]['preview']}
        for i in range(common, len(msgs_a))
    ]

    tools_a, tools_b = _tool_map(body_a), _tool_map(body_b)
    tools = {
        'added': sorted(set(tools_b) - set(tools_a))[:50],
        'removed': sorted(set(tools_a) - set(tools_b))[:50],
        'changed': [
            {'name': name, 'before': tools_a[name][:200], 'after': tools_b[name][:200]}
            for name in sorted(set(tools_a) & set(tools_b))
            if tools_a[name] != tools_b[name]
        ][:50],
    }

    return {
        'before': {'id': a['id'], 'seq': a['seq'], 'ts': a['ts'], 'model': a['model']},
        'after': {'id': b['id'], 'seq': b['seq'], 'ts': b['ts'], 'model': b['model']},
        'params': params,
        'system': system,
        'messages': {
            'before_total': len(msgs_a),
            'after_total': len(msgs_b),
            'common': common,
            'added': added[:50],
            'removed': removed[:50],
            'changed': changed[:50],
        },
        'tools': tools,
        'summary': {
            'params': len(params),
            'system': 0 if not system['changed'] else 1,
            'added': len(added),
            'removed': len(removed),
            'changed': len(changed),
            'tools': len(tools['added']) + len(tools['removed']) + len(tools['changed']),
        },
    }


# ── 导出（自包含 HTML）────────────────────────────────────
_EXPORT_CSS = """
:root{color-scheme:light dark}
body{margin:0;padding:24px;font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",
"PingFang SC","Microsoft YaHei",sans-serif;background:#f6f7f9;color:#1a1a1a}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:24px 0 8px}
.meta{color:#666;font-size:12px;margin-bottom:16px}
.turn{background:#fff;border:1px solid #e3e5e8;border-radius:10px;margin:10px 0;
overflow:hidden}
.turn>summary{cursor:pointer;padding:10px 14px;font-weight:600;list-style:none}
.turn>summary::-webkit-details-marker{display:none}
.row{display:flex;gap:10px;flex-wrap:wrap;padding:0 14px 10px;font-size:12px;color:#555}
.tag{background:#eef1f5;border-radius:999px;padding:1px 8px}
pre{margin:0;padding:12px 14px;background:#fbfbfd;border-top:1px solid #eee;
overflow:auto;max-height:480px;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;
white-space:pre-wrap;word-break:break-word}
.err{color:#b42318}
@media (prefers-color-scheme:dark){
body{background:#111;color:#e7e7e7}.turn{background:#1a1a1a;border-color:#2c2c2c}
pre{background:#141414;border-color:#262626;color:#cfcfcf}.meta{color:#9a9a9a}
.tag{background:#242424;color:#c9c9c9}
}
"""


def _esc(value: object) -> str:
    return html.escape('' if value is None else str(value))


def export_html(session_id: int) -> str | None:
    """把一个会话导成自包含 HTML。

    与 claude-tap 的「一键导出证据」对齐：对方拿到这一个文件就能看完整上下文，
    不需要登录本面板，也不需要再查库。刻意做成**纯静态**（无脚本、无外链）——
    导出的东西经常是贴到 issue 或发给同事的，带脚本的 HTML 会先被安全策略拦下。
    """
    data = session(session_id)
    if not data:
        return None
    parts: list[str] = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
        f'<title>WorkBuddy Trace #{data["id"]}</title>',
        f'<style>{_EXPORT_CSS}</style></head><body>',
        f'<h1>调用抓包 #{data["id"]}</h1>',
        f'<div class="meta">客户端 {_esc(data["client"])} · 协议 '
        f'{_esc(data["protocol"])} · 模型 {_esc(data["model"])} · '
        f'{len(data["items"])} 轮 · '
        f'Token 入 {data["prompt_tokens"]} / 出 {data["completion_tokens"]}</div>',
    ]
    for item in data['items']:
        full = turn(item['id']) or {}
        recon = full.get('recon') or {}
        req = full.get('request') or {}
        req_body = req.get('upstream_body') or req.get('body')
        parts.append('<details class="turn">')
        parts.append(
            f'<summary>#{item["seq"]} · {_esc(item["model"])} · '
            f'HTTP {_esc(item["status"])} · {_esc(item["latency_ms"])} ms'
            f'{" · 截断" if item["truncated"] else ""}</summary>')
        parts.append(
            '<div class="row">'
            f'<span class="tag">入 {item["prompt_tokens"] or 0}</span>'
            f'<span class="tag">出 {item["completion_tokens"] or 0}</span>'
            f'<span class="tag">首字 {item["first_token_ms"] or "—"} ms</span>'
            f'<span class="tag">请求 {item["request_bytes"]} B</span>'
            f'<span class="tag">响应 {item["response_bytes"]} B</span>'
            '</div>')
        if item['error']:
            parts.append(f'<pre class="err">{_esc(item["error"])}</pre>')
        if recon.get('text') or recon.get('reasoning'):
            parts.append('<pre><strong>模型输出</strong>\n'
                         f'{_esc(recon.get("text") or "")}</pre>')
        if recon.get('tool_calls'):
            parts.append('<pre><strong>工具调用</strong>\n'
                         f'{_esc(json.dumps(recon["tool_calls"], ensure_ascii=False, indent=2))}</pre>')
        # 客户端视角：翻译层（Anthropic / Responses）回给客户端的报文与上游不同。
        # 导出是拿去对外的证据，只带上游那一半等于让对方看不到"我到底收到了什么"。
        client_recon = full.get('client_recon') or {}
        if not full.get('passthrough') and client_recon.get('text'):
            parts.append('<pre><strong>客户端收到的文本</strong>\n'
                         f'{_esc(client_recon.get("text") or "")}</pre>')
        parts.append('<pre><strong>请求</strong>\n'
                     f'{_esc(json.dumps(req_body, ensure_ascii=False, indent=2, default=str)[:20000])}</pre>')
        if full.get('response_raw'):
            parts.append('<pre><strong>上游原文</strong>\n'
                         f'{_esc(str(full["response_raw"])[:20000])}</pre>')
        parts.append('</details>')
    parts.append('</body></html>')
    return ''.join(parts)
