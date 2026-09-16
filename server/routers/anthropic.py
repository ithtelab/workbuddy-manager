"""Anthropic Messages API 兼容层（`/v1/messages`）。

为什么需要
----------
上游只提供 OpenAI 兼容接口，但 Claude Code / Cursor / Cline / Zed 等一大批
客户端**只认 Anthropic 的 Messages 协议**。它们无法直接接本网关——不是配置
问题，而是协议不同：

  请求     · `system` 是顶层字段（不在 messages 里）
           · `max_tokens` **必填**（OpenAI 可选）
           · `content` 可以是字符串，也可以是 block 数组
           · 工具是 `{name, input_schema}`，没有 `type: function` 那层包装
  响应     · `content` 是 block 数组（文本与工具调用同处一个列表）
           · `stop_reason` 是 `end_turn` 而非 `stop`
  流式     · 带 `event:` 行的事件流（message_start / content_block_delta / …），
             与 OpenAI 的裸 `data: {...}` 完全不同

本模块只做**协议双向翻译**，其余（密钥鉴权、IP 管控、配额、限流、日志与
用量记账）**一律复用 gateway 既有实现**——那些是这个网关真正的价值，
不能因为多一个协议就走一套新逻辑。

架构
----
    客户端 ──Anthropic 协议──▶ [本模块：翻译] ──OpenAI 协议──▶ gateway 既有逻辑 ──▶ 上游

翻译层是纯函数（除流式状态机外无副作用），便于单测。
"""
from __future__ import annotations

import json
import logging
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import config, db, iputil, keysvc
from ..routers.security import get_config as get_security_config
from . import gateway

logger = logging.getLogger('workbuddy.anthropic')

router = APIRouter(tags=['anthropic'])

# OpenAI finish_reason → Anthropic stop_reason
_STOP_REASON = {
    'stop': 'end_turn',
    'length': 'max_tokens',
    'tool_calls': 'tool_use',
    'function_call': 'tool_use',
    'content_filter': 'end_turn',
}


def _err(message: str, status: int = 400, err_type: str = 'invalid_request_error') -> JSONResponse:
    """Anthropic 风格的错误体。

    与 OpenAI 的区别不只是字段名：Anthropic 把错误包在 `{"type":"error","error":{...}}`
    里，客户端会按这个结构解析。共用 `_oai_error` 会让 SDK 读不到错误信息。
    """
    return JSONResponse(
        {'type': 'error', 'error': {'type': err_type, 'message': message}},
        status_code=status,
    )


def _token_candidates(request: Request) -> list[str]:
    """列出请求里**所有**可能的令牌值（去重、保持优先级）。

    为什么不是「取一个」：客户端可能同时带**多份凭据**——实测遇到过
    Claude Code 既发 `x-api-key`（值属于另一个服务、67 字符的 `sk-…`）
    又发 `Authorization: Bearer`（值才是本网关的 `wbk_…`）。
    只取第一个就会拿到不相干的那份，表现为「明明配对了却 401」，
    而且从客户端侧完全看不出问题。所以这里返回候选列表，由调用方逐个验。

    取值来源：
      · `x-api-key` / `x-anthropic-api-key` —— Anthropic 官方 SDK 等
      · `Authorization` —— Claude Code 配 ANTHROPIC_AUTH_TOKEN 时发；
        带 `Bearer ` 前缀的剥掉，**不带前缀的也照收**（部分转发工具直接放裸 token）

    （注意：管理端的登录态**不认**这些头，那是另一回事。）
    """
    out: list[str] = []
    for header in ('x-api-key', 'x-anthropic-api-key'):
        value = request.headers.get(header, '').strip()
        if value and value not in out:
            out.append(value)
    auth = request.headers.get('authorization', '').strip()
    if auth:
        value = auth[7:].strip() if auth.lower().startswith('bearer ') else auth
        if value and value not in out:
            out.append(value)
    return out


def _resolve_key(request: Request):
    """从候选令牌里找出**能解析出密钥**的那一个。

    返回 `(key, token)`；都不行时 key 为 None、token 为最长候选（仅供日志判断）。
    """
    candidates = _token_candidates(request)
    for token in candidates:
        key = keysvc.resolve(token)
        if key is not None:
            return key, token
    return None, (max(candidates, key=len) if candidates else '')


def _header_names(request: Request) -> list[str]:
    """收到的请求头**名字**（不含值）。

    鉴权失败时写进日志：客户端到底把令牌放在哪个头里，是这类「配了却 401」
    问题唯一可靠的线索——且只记名字，不会把凭据写进日志。
    """
    try:
        return sorted({k.lower() for k in request.headers.keys()})
    except Exception:  # noqa: BLE001
        return []


def _text_of(content: object) -> str:
    """把 Anthropic 的 content（字符串或 block 数组）压成纯文本。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    out: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get('type') == 'text':
            out.append(str(block.get('text') or ''))
    return '\n'.join(p for p in out if p)


def _image_url(source: object) -> str | None:
    """Anthropic 图片块 → data URL。不认识的形态返回 None（由调用方跳过）。"""
    if not isinstance(source, dict):
        return None
    if source.get('type') == 'base64':
        media = str(source.get('media_type') or 'image/png')
        data = str(source.get('data') or '')
        return f'data:{media};base64,{data}' if data else None
    if source.get('type') == 'url':
        url = str(source.get('url') or '')
        return url or None
    return None


def to_openai_request(body: dict) -> dict:
    """Anthropic 请求体 → OpenAI 请求体。

    最绕的一处是**工具结果**：Anthropic 把它当作 user 消息里的一个 block，
    而 OpenAI 要求它是**独立的 `role: tool` 消息**。因此一条 Anthropic 消息
    可能被拆成多条 OpenAI 消息（工具结果在前，剩余文本在后）。
    """
    out: dict = {'model': body.get('model'), 'stream': bool(body.get('stream'))}

    messages: list[dict] = []

    # system 是顶层字段 → 转成首条 system 消息
    system = body.get('system')
    if system:
        text = _text_of(system) if not isinstance(system, str) else system
        if text:
            messages.append({'role': 'system', 'content': text})

    for raw in body.get('messages') or []:
        if not isinstance(raw, dict):
            continue
        role = raw.get('role')
        content = raw.get('content')

        if isinstance(content, str):
            messages.append({'role': role, 'content': content})
            continue
        if not isinstance(content, list):
            continue

        parts: list[dict] = []      # 文本 / 图片（当前消息的常规内容）
        tool_calls: list[dict] = []  # assistant 发起的工具调用
        tool_results: list[dict] = []  # user 回传的工具结果 → 要拆成独立消息

        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get('type')
            if kind == 'text':
                parts.append({'type': 'text', 'text': str(block.get('text') or '')})
            elif kind == 'image':
                url = _image_url(block.get('source'))
                if url:
                    parts.append({'type': 'image_url', 'image_url': {'url': url}})
            elif kind == 'tool_use':
                tool_calls.append({
                    'id': str(block.get('id') or f'call_{uuid.uuid4().hex[:12]}'),
                    'type': 'function',
                    'function': {
                        'name': str(block.get('name') or ''),
                        # OpenAI 的 arguments 是**字符串**，Anthropic 的 input 是对象
                        'arguments': json.dumps(block.get('input') or {}, ensure_ascii=False),
                    },
                })
            elif kind == 'tool_result':
                tool_results.append({
                    'role': 'tool',
                    'tool_call_id': str(block.get('tool_use_id') or ''),
                    'content': _text_of(block.get('content')) or str(block.get('content') or ''),
                })

        # 工具结果必须先于本条的其余内容（它们对应上一轮 assistant 的调用）
        messages.extend(tool_results)

        if tool_calls:
            msg: dict = {'role': 'assistant', 'tool_calls': tool_calls}
            text = '\n'.join(p['text'] for p in parts if p.get('type') == 'text')
            # 有工具调用时 content 通常是空的，但保留文本更稳（部分上游要求非 null）
            msg['content'] = text or None
            messages.append(msg)
        elif parts:
            # 只有一个纯文本块时压平为字符串——多数上游对字符串更宽容
            if len(parts) == 1 and parts[0].get('type') == 'text':
                messages.append({'role': role, 'content': parts[0]['text']})
            else:
                messages.append({'role': role, 'content': parts})

    out['messages'] = messages

    if body.get('max_tokens') is not None:
        out['max_tokens'] = body['max_tokens']
    for src, dst in (('temperature', 'temperature'), ('top_p', 'top_p')):
        if body.get(src) is not None:
            out[dst] = body[src]
    if body.get('stop_sequences'):
        out['stop'] = body['stop_sequences']
    if isinstance(body.get('metadata'), dict) and body['metadata'].get('user_id'):
        out['user'] = str(body['metadata']['user_id'])

    tools = body.get('tools')
    if isinstance(tools, list) and tools:
        out['tools'] = [
            {
                'type': 'function',
                'function': {
                    'name': str(t.get('name') or ''),
                    'description': str(t.get('description') or ''),
                    'parameters': t.get('input_schema') or {'type': 'object', 'properties': {}},
                },
            }
            for t in tools
            if isinstance(t, dict) and t.get('name')
        ]

    choice = body.get('tool_choice')
    if isinstance(choice, dict):
        kind = choice.get('type')
        if kind == 'auto':
            out['tool_choice'] = 'auto'
        elif kind == 'any':
            out['tool_choice'] = 'required'
        elif kind == 'tool' and choice.get('name'):
            out['tool_choice'] = {
                'type': 'function',
                'function': {'name': str(choice['name'])},
            }
    return out


def to_anthropic_response(data: dict, model: str) -> dict:
    """OpenAI 非流式响应 → Anthropic 响应体。"""
    choice = (data.get('choices') or [{}])[0] if isinstance(data.get('choices'), list) else {}
    message = choice.get('message') or {}
    usage = data.get('usage') or {}

    content: list[dict] = []
    text = message.get('content')
    if isinstance(text, str) and text:
        content.append({'type': 'text', 'text': text})

    for call in message.get('tool_calls') or []:
        if not isinstance(call, dict):
            continue
        fn = call.get('function') or {}
        raw_args = fn.get('arguments')
        try:
            parsed = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else {}
        except (ValueError, TypeError):
            # 上游给了非法 JSON：原样塞进一个占位字段，别让整次调用失败
            parsed = {'_raw': raw_args}
        content.append({
            'type': 'tool_use',
            'id': str(call.get('id') or f'toolu_{uuid.uuid4().hex[:12]}'),
            'name': str(fn.get('name') or ''),
            'input': parsed if isinstance(parsed, dict) else {'_raw': parsed},
        })

    return {
        'id': 'msg_' + uuid.uuid4().hex[:20],
        'type': 'message',
        'role': 'assistant',
        'model': model,
        'content': content,
        'stop_reason': _STOP_REASON.get(str(choice.get('finish_reason')), 'end_turn'),
        'stop_sequence': None,
        'usage': {
            'input_tokens': int(usage.get('prompt_tokens') or 0),
            'output_tokens': int(usage.get('completion_tokens') or 0),
        },
    }


def _event(name: str, payload: dict) -> bytes:
    """Anthropic SSE 事件：带 `event:` 行，且**两道换行**结尾。"""
    return f'event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'.encode()


class _StreamTranslator:
    """OpenAI SSE → Anthropic 事件流的转换状态机。

    为什么需要状态机而不是逐块替换：Anthropic 的流是**有结构**的——每个内容块
    必须先 `content_block_start`、增量若干次 `content_block_delta`、再
    `content_block_stop`，且块有递增的 `index`；整条消息还要用
    `message_start` / `message_delta` / `message_stop` 包起来。
    而 OpenAI 只给一串无结构的 delta，**没有任何"边界"信息**，所以边界只能由
    我们在遇到内容类型切换或流结束时自己推断。

    文本与工具调用的增量语义也不同：
      · 文本        → `text_delta`
      · 工具调用参数 → `input_json_delta`，且参数是**分片**下发的
        （`{"loc` + `ation": ...}`），必须原样透传片段、由客户端拼接。
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.msg_id = 'msg_' + uuid.uuid4().hex[:20]
        self.started = False
        self.finished = False
        self.text_index: int | None = None       # 当前打开的文本块
        self.tool_index: int | None = None       # 当前打开的工具块
        self.next_index = 0
        self.tool_seen = False
        # 是否见过正文增量：首字延迟据此判定（用「第一个含正文的 delta」，而不是
        # 「第一个 chunk」——后者会把建连时间也算进去，数字偏小且失真）
        self.saw_content = False
        self.input_tokens = 0
        self.output_tokens = 0

    def _start_message(self) -> list[bytes]:
        self.started = True
        return [_event('message_start', {
            'type': 'message_start',
            'message': {
                'id': self.msg_id,
                'type': 'message',
                'role': 'assistant',
                'model': self.model,
                'content': [],
                'stop_reason': None,
                'stop_sequence': None,
                'usage': {'input_tokens': self.input_tokens, 'output_tokens': 0},
            },
        })]

    def _close(self, index: int | None) -> list[bytes]:
        if index is None:
            return []
        return [_event('content_block_stop', {'type': 'content_block_stop', 'index': index})]

    def feed(self, obj: dict) -> list[bytes]:
        """喂一个 OpenAI SSE 的 data 对象，返回要下发的事件。"""
        out: list[bytes] = []
        if not self.started:
            out += self._start_message()

        # 非流式写法的 usage 也可能出现在末尾帧
        usage = obj.get('usage')
        if isinstance(usage, dict):
            self.input_tokens = int(usage.get('prompt_tokens') or self.input_tokens or 0)
            self.output_tokens = int(usage.get('completion_tokens') or self.output_tokens or 0)

        choices = obj.get('choices')
        if not isinstance(choices, list) or not choices:
            return out
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get('delta') or choice.get('message') or {}
        if not isinstance(delta, dict):
            delta = {}

        text = delta.get('content')
        if isinstance(text, str) and text:
            self.saw_content = True
            # 从工具块切回文本时，先把工具块关掉
            if self.tool_index is not None:
                out += self._close(self.tool_index)
                self.tool_index = None
            if self.text_index is None:
                self.text_index = self.next_index
                self.next_index += 1
                out.append(_event('content_block_start', {
                    'type': 'content_block_start',
                    'index': self.text_index,
                    'content_block': {'type': 'text', 'text': ''},
                }))
            out.append(_event('content_block_delta', {
                'type': 'content_block_delta',
                'index': self.text_index,
                'delta': {'type': 'text_delta', 'text': text},
            }))

        for call in delta.get('tool_calls') or []:
            if not isinstance(call, dict):
                continue
            fn = call.get('function') or {}
            # 文本块与工具块不能并存：切到工具前先关文本
            if self.text_index is not None:
                out += self._close(self.text_index)
                self.text_index = None

            name = fn.get('name')
            if name:
                # 新工具：关掉上一个，开一个新的
                if self.tool_index is not None:
                    out += self._close(self.tool_index)
                self.tool_index = self.next_index
                self.next_index += 1
                self.tool_seen = True
                out.append(_event('content_block_start', {
                    'type': 'content_block_start',
                    'index': self.tool_index,
                    'content_block': {
                        'type': 'tool_use',
                        'id': str(call.get('id') or f'toolu_{uuid.uuid4().hex[:12]}'),
                        'name': str(name),
                        'input': {},
                    },
                }))

            args = fn.get('arguments')
            if isinstance(args, str) and args and self.tool_index is not None:
                # 分片原样透传，拼接交给客户端（我们无从判断 JSON 何时完整）
                out.append(_event('content_block_delta', {
                    'type': 'content_block_delta',
                    'index': self.tool_index,
                    'delta': {'type': 'input_json_delta', 'partial_json': args},
                }))

        finish = choice.get('finish_reason')
        if finish:
            out += self.finish(str(finish))
        return out

    def finish(self, finish_reason: str | None = None, *, force: bool = False) -> list[bytes]:
        """收尾：关掉打开的块，发 message_delta + message_stop（幂等）。"""
        if self.finished:
            return []
        self.finished = True
        out: list[bytes] = []
        if not self.started:
            out += self._start_message()
        out += self._close(self.text_index)
        out += self._close(self.tool_index)
        self.text_index = self.tool_index = None

        reason = finish_reason
        # 出现过工具调用但上游没给 finish_reason 时，按 tool_use 收尾更贴近实际
        if reason is None and force and self.tool_seen:
            reason = 'tool_calls'
        out.append(_event('message_delta', {
            'type': 'message_delta',
            'delta': {'stop_reason': _STOP_REASON.get(str(reason), 'end_turn'), 'stop_sequence': None},
            'usage': {'output_tokens': self.output_tokens},
        }))
        out.append(_event('message_stop', {'type': 'message_stop'}))
        return out


def _iter_sse_lines(pending: str):
    """把缓冲切分为完整的 SSE 行；返回 (已消费的完整行, 剩余缓冲)。"""
    while '\n' in pending:
        line, pending = pending.split('\n', 1)
        yield line.rstrip('\r')
    return pending


@router.post('/v1/messages')
async def messages(request: Request):
    """Anthropic Messages API。"""
    body, err = await gateway._read_json_body(request)
    if err:
        # 上游格式错误：这里换成 Anthropic 风格，客户端才读得懂
        return _err('请求体不是合法 JSON 对象')

    model = body.get('model')
    if not isinstance(model, str) or not model.strip():
        return _err('model 必须是字符串')
    # max_tokens 在 Anthropic 协议里是**必填项**，缺失即 400（与 OpenAI 不同）
    if body.get('max_tokens') is None:
        return _err('缺少必填字段 max_tokens')

    # 鉴权沿用网关那套（密钥 / IP 管控 / 配额 / 限流），令牌来源两种都认
    key, token = _resolve_key(request)
    if not token:
        logger.warning('未取到令牌，收到的请求头: %s', _header_names(request))
        return _err('缺少 API Key：请在 x-api-key 或 Authorization: Bearer 中提供', 401, 'authentication_error')
    if key is None:
        # 「配了却 401」几乎只能靠这行定位：候选个数/长度说明客户端发了什么，
        # 前缀是密钥的**公开部分**（面板列表里就显示它），用于比对是哪一把；
        # 再往后不记，避免把凭据写进日志。
        logger.warning(
            '令牌无法解析：候选=%d 个，最长 %d 位、前缀=%r；x-api-key 长度=%d，authorization 长度=%d；请求头: %s',
            len(_token_candidates(request)), len(token), token[:12],
            len(request.headers.get('x-api-key', '')),
            len(request.headers.get('authorization', '')),
            _header_names(request),
        )
        return _err('API Key 无效', 401, 'authentication_error')

    ip = iputil.client_ip(request)
    ua = request.headers.get('user-agent')
    path = request.url.path

    sec = get_security_config()
    if sec.get('enabled'):
        rules = [
            {'kind': r['kind'], 'cidr': r['cidr']}
            for r in db.query('SELECT kind, cidr FROM ip_rules')
        ]
        if not iputil.evaluate(ip, rules, sec.get('mode', 'blacklist')):
            gateway._log_ip(ip, path, True, ua)
            gateway._record(key, ip, model, '', 403, 0, 0, 0, ua, 'IP 被拦截', False)
            return _err(f'来源 IP {ip} 被安全策略拦截', 403, 'permission_error')

    gateway._log_ip(ip, path, False, ua)
    reason = keysvc.validate(key, ip, model)
    if reason:
        gateway._record(key, ip, model, '', 403, 0, 0, 0, ua, reason, False)
        return _err(reason, 403, 'permission_error')

    limited, _count = gateway._rate_limited(key)
    if limited:
        msg = f'请求过于频繁（{gateway.RATE_WINDOW}s 内超过 {gateway.RATE_MAX_PER_MIN} 次）'
        gateway._record(key, ip, model, '', 429, 0, 0, 0, ua, msg, False)
        return _err(msg, 429, 'rate_limit_error')

    stream = bool(body.get('stream'))

    try:
        payload = to_openai_request(body)
    except Exception as exc:  # noqa: BLE001
        gateway._record(key, ip, model, '', 400, 0, 0, 0, ua, str(exc), False)
        return _err(f'请求转换失败：{exc}')

    mapped = gateway._map_model(model)
    if mapped:
        payload['model'] = mapped
    if stream:
        payload.setdefault('stream_options', {})
        if isinstance(payload['stream_options'], dict):
            payload['stream_options'].setdefault('include_usage', True)

    url = f'{config.WB2API_BASE}/v1/chat/completions'
    started = time.time()

    if not stream:
        try:
            async with config.http_client(config.UPSTREAM_TIMEOUT, connect=5) as client:
                resp = await client.post(url, json=payload, headers=gateway._upstream_headers())
            latency = int((time.time() - started) * 1000)
            usage: dict = {}
            try:
                data = resp.json()
                usage = data.get('usage') or {}
            except Exception:  # noqa: BLE001
                data = None
            gateway._record(
                key, ip, model, mapped or '', resp.status_code,
                int(usage.get('prompt_tokens') or 0), int(usage.get('completion_tokens') or 0),
                latency, ua, None if resp.status_code < 400 else str(data)[:500], False,
                credit=gateway._usage_credit(usage),
            )
            if resp.status_code >= 400:
                msg = ''
                if isinstance(data, dict):
                    err_obj = data.get('error')
                    msg = str(err_obj.get('message') if isinstance(err_obj, dict) else err_obj) or str(data)[:300]
                else:
                    msg = resp.text[:300]
                return _err(msg, resp.status_code, 'api_error')
            return JSONResponse(to_anthropic_response(data if isinstance(data, dict) else {}, model))
        except Exception as exc:  # noqa: BLE001
            latency = int((time.time() - started) * 1000)
            gateway._record(key, ip, model, mapped or '', 502, 0, 0, latency, ua, str(exc), False)
            return _err(f'上游不可用：{exc}', 502, 'api_error')

    # ── 流式 ────────────────────────────────────────────────
    client = config.http_client(config.UPSTREAM_TIMEOUT, connect=5)
    try:
        req = client.build_request('POST', url, json=payload, headers=gateway._upstream_headers())
        resp = await client.send(req, stream=True)
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        latency = int((time.time() - started) * 1000)
        gateway._record(key, ip, model, mapped or '', 502, 0, 0, latency, ua, str(exc), True)
        return _err(f'上游不可用：{exc}', 502, 'api_error')

    status_code = resp.status_code

    async def gen():
        usage: dict = {}
        pending = ''
        translator = _StreamTranslator(model)
        error_text: str | None = None
        first_token_ms: int | None = None

        try:
            async for chunk in resp.aiter_bytes():
                pending += chunk.decode('utf-8', errors='ignore')

                if status_code >= 400:
                    if len(pending) > 4000:
                        error_text = pending[:500]
                    continue

                # 逐行解析并翻译。**不能**先把 pending 交给 gateway._scan_sse：
                # 那个函数会消费掉所有完整行、只返回残缺缓冲，这里就拿不到数据了。
                # usage 与首字延迟因此在本循环内自行提取。
                while '\n' in pending:
                    line, pending = pending.split('\n', 1)
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

                    frame_usage = obj.get('usage')
                    if isinstance(frame_usage, dict):
                        usage.update(frame_usage)

                    for event in translator.feed(obj):
                        yield event

                    if translator.saw_content and first_token_ms is None:
                        first_token_ms = int((time.time() - started) * 1000)

            if status_code >= 400:
                # 上游直接报错：把错误翻成 Anthropic 事件，别让客户端空等
                for event in translator.finish(None, force=True):
                    yield event
                if error_text:
                    yield _event('error', {
                        'type': 'error',
                        'error': {'type': 'api_error', 'message': error_text},
                    })
            else:
                for event in translator.finish(None, force=True):
                    yield event
        finally:
            await resp.aclose()
            await client.aclose()
            latency = int((time.time() - started) * 1000)
            gateway._record(
                key, ip, model, mapped or '', status_code,
                int(usage.get('prompt_tokens') or 0), int(usage.get('completion_tokens') or 0),
                latency, ua, error_text, True,
                credit=gateway._usage_credit(usage), first_token=first_token_ms,
            )

    return StreamingResponse(gen(), status_code=200, media_type='text/event-stream')


@router.post('/v1/messages/count_tokens')
async def count_tokens(request: Request):
    """粗略估算输入 token 数。

    Claude Code 等客户端会先调这个接口来决定上下文还能塞多少。我们拿不到
    上游的分词器，**只能给粗略估算**——所以这里明确按「字符数 / 3」返回，
    宁可高估（客户端会保守地留更多余量），也不能低估导致真实请求超限。
    返回结构按协议要求带 `input_tokens`。

    鉴权与 `/v1/messages` 一致：它虽然不产生上游调用、不消耗额度，但同样是对外
    接口——裸着会让任何人都能借它判断网关是否存活、探测部署规模，也与其余端点
    的「一律先验密钥」不一致。
    """
    key, token = _resolve_key(request)
    if not token:
        logger.warning('count_tokens 未取到令牌，收到的请求头: %s', _header_names(request))
        return _err('缺少 API Key：请在 x-api-key 或 Authorization: Bearer 中提供', 401, 'authentication_error')
    if key is None:
        logger.warning('count_tokens 令牌无法解析：候选=%d 个，最长 %d 位；请求头: %s',
                       len(_token_candidates(request)), len(token), _header_names(request))
        return _err('API Key 无效', 401, 'authentication_error')

    body = None
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    if not isinstance(body, dict):
        return _err('请求体必须是 JSON 对象')

    total = 0
    system = body.get('system')
    if system:
        total += len(_text_of(system) if not isinstance(system, str) else system)
    for msg in body.get('messages') or []:
        if isinstance(msg, dict):
            total += len(_text_of(msg.get('content')))
    for tool in body.get('tools') or []:
        if isinstance(tool, dict):
            total += len(json.dumps(tool, ensure_ascii=False))
    return JSONResponse({'input_tokens': max(1, (total + 2) // 3)})
