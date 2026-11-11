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

from .. import config, db, iputil, keysvc, upstreamsvc
from ..routers.security import get_config as get_security_config
from ..services import tap as tap_svc
# responses 只为复用推理凭据的编解码（`_encode_credential` / `_decode_credential`）：
# Anthropic 的 `signature` 与 Responses 的 `encrypted_content` 语义完全相同——
# 都是「客户端原样搬来搬去的不透明串」，两边编解码必须一致，否则同一轮对话在
# 两套协议间切换时凭据解不开。
from . import gateway, responses

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


def _err(message: str, status: int = 400, err_type: str = 'invalid_request_error',
         hint: str | None = None) -> JSONResponse:
    """Anthropic 风格的错误体。

    与 OpenAI 的区别不只是字段名：Anthropic 把错误包在 `{"type":"error","error":{...}}`
    里，客户端会按这个结构解析。共用 `_oai_error` 会让 SDK 读不到错误信息。

    `hint` 透传上游的 `error.gateway_hint`（与 message 并列的可执行建议）。放在
    Anthropic 的错误对象里——与上游在 OpenAI 形状下把它置于 `error` 内一致；
    为空时不写该字段（不编造）。
    """
    err: dict = {'type': err_type, 'message': message}
    if hint:
        err['gateway_hint'] = hint
    return JSONResponse({'type': 'error', 'error': err}, status_code=status)


def _as_int(value: object) -> int:
    """把上游给的数字字段安全地转成 int。

    上游是**外部进程**，`usage` 可能畸形（字符串、null、嵌套对象）。直接
    `int(...)` 会抛 ValueError，而它在**流式生成器内部**抛出会直接掐断流——
    客户端看到 terminated、连 `message_stop` 都收不到（gateway.py 里记录过
    这类事故）。转不动就按 0 计，不影响主流程。
    """
    if isinstance(value, bool):
        return 0
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _usage_fields(usage: dict) -> dict:
    """把上游 usage 映射成 **Anthropic 口径**的字段。

    ## 两个口径的方向是相反的（这里踩过，务必看清）

      · OpenAI / 上游：`prompt_tokens` **已包含**命中的缓存 token
        （另在 `prompt_cache_hit_tokens` 里给出命中数，供你展示摊销）；
      · Anthropic：`input_tokens` **不含**缓存——它把缓存拆成
        `cache_read_input_tokens`（本次命中读取）与
        `cache_creation_input_tokens`（本次写入缓存）两个独立字段，
        客户端统计总量时会把 `input + cache_read + cache_creation` 相加。

    所以**不能把 `prompt_tokens` 直接当 `input_tokens` 发出去**：那样客户端
    相加时缓存会被算两遍（实测 1024 命中会被计成 2048）。必须减去命中/写入量。

    这条口径差异是社区同学踩过之后反馈的（issue #39 的补充评论），
    本仓 `responses.py` 的 `_usage_object` 是从相反方向处理同一件事
    （OpenAI 语义要求 input **含**缓存，所以那边不减）——两处方向相反是**对的**，
    不要顺手「统一」。

    ## 字段名

    上游给的缓存字段名是 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` /
    `prompt_cache_write_tokens`；标准名 `cache_read_input_tokens` 在上游响应里
    恒为 0（实测量过），所以不能读那个。

    没给缓存字段时（多数国内版请求）退化为：input_tokens = prompt_tokens，
    两个缓存字段为 0 —— 与旧行为一致。
    """
    prompt = _as_int(usage.get('prompt_tokens'))
    completion = _as_int(usage.get('completion_tokens'))
    hit = _as_int(usage.get('prompt_cache_hit_tokens'))
    write = _as_int(usage.get('prompt_cache_write_tokens'))
    # 钳到非负：上游若给了畸形的命中数（> prompt），相减会得到负数，
    # 客户端拿到负的 input_tokens 会算出更离谱的上下文余量。
    return {
        'input_tokens': max(0, prompt - hit - write),
        'cache_read_input_tokens': hit,
        'cache_creation_input_tokens': write,
        'output_tokens': completion,
    }


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


def _authorize(request: Request, model: str | None, *,
               mapped: str | None = None) -> tuple[dict | None, str, JSONResponse | None]:
    """完整鉴权：密钥 → 全局 IP 管控 → 密钥约束 → 限流。返回 `(key, ip, error)`。

    mapped：`model` 经「模型映射」后的名字，版本归属判它（issue #47，
    见 `keysvc.validate` 的说明）。与 `gateway._authorize` 同名同义。

    ⚠️ 这套检查与 `gateway._authorize` **必须保持同序同项**——同一把密钥在两个
    协议下得出不同结论是最难查的一类问题。之所以没直接复用：本层要遍历**多个
    候选令牌**（客户端可能同时带着别的服务的凭据），而 `_authorize` 只接单个 token。
    改任一侧时请同步另一侧。

    之所以抽成函数：`count_tokens` 早先只挑了个能解析的密钥就放行，
    把停用/过期/配额/限流**全漏了**——重复实现的两份逻辑一旦漂移，漏的就是安全项。
    """
    ip = iputil.client_ip(request)
    ua = request.headers.get('user-agent')
    path = request.url.path

    key, token = _resolve_key(request)
    if not token:
        logger.warning('未取到令牌，收到的请求头: %s', _header_names(request))
        return None, ip, _err('缺少 API Key：请在 x-api-key 或 Authorization: Bearer 中提供', 401, 'authentication_error')
    if key is None:
        # 「配了却 401」几乎只能靠这行定位：候选个数/长度说明客户端发了什么，
        # 前缀是密钥的**公开部分**（面板列表里就显示它），便于比对是哪一把；
        # 再往后不记，避免把凭据写进日志。
        # 只在本网关前缀（wbk_）上打前缀——客户端可能同时带了别的服务的凭据
        # （实测遇到过 sk- 开头的），那种串的前缀不该被抄进本项目的日志。
        hint = token[:12] if token.startswith(keysvc.TOKEN_PREFIX) else '(非本网关前缀)'
        logger.warning(
            '令牌无法解析：候选=%d 个，最长 %d 位、前缀=%r；x-api-key 长度=%d，authorization 长度=%d；请求头: %s',
            len(_token_candidates(request)), len(token), hint,
            len(request.headers.get('x-api-key', '')),
            len(request.headers.get('authorization', '')),
            _header_names(request),
        )
        return None, ip, _err('API Key 无效', 401, 'authentication_error')

    # 供记账用（IP 拦截早于下面的映射归一化，这里先算一份）
    _mapped_for_log = gateway._map_model(model) if mapped is None else mapped

    sec = get_security_config()
    if sec.get('enabled'):
        rules = [
            {'kind': r['kind'], 'cidr': r['cidr']}
            for r in db.query('SELECT kind, cidr FROM ip_rules')
        ]
        if not iputil.evaluate(ip, rules, sec.get('mode', 'blacklist')):
            gateway._log_ip(ip, path, True, ua, 'ip_blocked')
            gateway._record(key, ip, model or '', _mapped_for_log or '', 403, 0, 0, 0, ua, 'IP 被拦截', False,
                            account_expected=False)
            return None, ip, _err(f'来源 IP {ip} 被安全策略拦截', 403, 'permission_error')

    # 注意：`blocked=False` 的这一行现在默认**不写库**（审计日志只留拦截，
    # 见 gateway._log_ip 的说明）——保留调用是为了 WB_AUDIT_ALL_ACCESS=1 时
    # 与 gateway 行为一致，不多一条分叉。
    gateway._log_ip(ip, path, False, ua)

    # 映射只算一次：拒绝路径也要用它记账（口径见 gateway._authorize 的说明——
    # 日志的 realm 按实际要用的那个名字归档，别名不带前缀，按请求名记会归错栏）。
    mapped = gateway._map_model(model) if mapped is None else mapped

    # model 为 None 时按「模型发现类请求」处理：跳过版本与模型白名单
    # （没有 model 就无从判定版本），但停用/过期/配额/IP 这些照常校验。
    reason = keysvc.validate(key, ip, model, is_model_list=(model is None),
                             mapped_model=mapped)
    if reason:
        # 与 gateway._authorize 同口径：状态码来自 keysvc，不再一律 403
        # （403 会被客户端显示成「API 密钥无效」，掩盖真实原因）。
        status = getattr(reason, 'status', 403)
        gateway._record(key, ip, model or '', mapped or '', status, 0, 0, 0, ua, reason, False,
                        account_expected=False)
        # 也记进安全页的入站日志：走 /v1/messages 的客户端被拒时，此前在
        # 「IP 访问日志」里**完全看不到**（只有 gateway 那条路记），两套协议
        # 的审计口径不一致。原因码复用 gateway 的归类，避免两处写法漂移。
        gateway._log_ip(ip, path, True, ua, gateway._key_reject_code(reason, model is None))
        return None, ip, _err(reason, status, getattr(reason, 'err_type', 'permission_error'))

    limited, _count = gateway._rate_limited(key)
    if limited:
        msg = f'请求过于频繁（{gateway.RATE_WINDOW}s 内超过 {gateway.RATE_MAX_PER_MIN} 次）'
        gateway._record(key, ip, model or '', mapped or '', 429, 0, 0, 0, ua, msg, False,
                        account_expected=False)
        gateway._log_ip(ip, path, True, ua, 'rate_limited')
        return None, ip, _err(msg, 429, 'rate_limit_error')

    return key, ip, None


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


def _estimate_tokens(text: str) -> int:
    """按字符类别粗略估算 token 数（供 `count_tokens`，估不到上游分词器）。

    分开算的理由见 `count_tokens` 的说明：统一「字符数 / 3」对中文是严重低估，
    而低估会让客户端以为还能塞更多、真实请求却在发出时被上游以「上下文过长」
    拒绝，且用户看不出是估算接口给了错数字。高估才是安全方向。

      · CJK（汉字 / 假名 / 韩文）：约 1 字符 1 token（保守取整字符数）；
      · 其余：约 4 字符 1 token。

    只做数量级估算，**不追求精确**——它唯一的作用是让客户端留够余量。
    """
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        cp = ord(ch)
        # 汉字（含扩展 A/B）、日文假名、韩文音节与字母、CJK 标点与全角符号。
        # 判据用码位区间而不是 `unicodedata.east_asian_width`：后者对全角标点
        # 与韩文 Hangul Jamo 的分类在不同 Python 版本下有差异，区间判定稳定。
        if (0x3000 <= cp <= 0x9FFF or 0xAC00 <= cp <= 0xD7AF
                or 0xF900 <= cp <= 0xFAFF or 0xFF00 <= cp <= 0xFFEF
                or 0x20000 <= cp <= 0x2FA1F):
            cjk += 1
        else:
            other += 1
    return cjk + (other + 3) // 4


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
        tool_images: list[dict] = []  # 工具结果里的图片 → 提升到后续 user 消息
        # 本条的推理文本（thinking 块）→ 挂到本条的 assistant 消息上，交给上游。
        # None = 没见到 thinking 块；空串 = 见到了但拿不到明文（两者必须区分：
        # 上游的兜底逻辑按**字段存在与否**判断有无痕迹，空串也算存在）。
        #
        # 实际写哪些字段由 `responses.attach_reasoning` 决定 —— 请求侧校验读的是
        # `reasoning`（不是 `reasoning_content`，见那里的说明）。
        thinking_text: str | None = None

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
            elif kind in ('thinking', 'redacted_thinking'):
                # Anthropic 的推理块与 Responses 的 reasoning item 是同一个东西，
                # **不能丢**：腾讯对 DeepSeek 要求多轮回传推理内容，丢了会被拒
                # （11155 reasoning_content_missing）→ 该账号记一次失败 → 连续失败
                # 触发降级冷却 → 池里无可用账号 → 客户端重试变成与模型无关的 503
                # 死循环。详见 responses.py 里同类处理的说明。
                #
                # 解析按可靠性排序：`signature` 是我们发出去、客户端原样带回的凭据，
                # **优先**解它——它是唯一能还原完整推理原文的载体（`thinking` 明文
                # 可能被客户端截断）。解不开再回落明文。
                decoded = responses._decode_credential(block.get('signature'))
                if not decoded:
                    # `redacted_thinking` 只有加密串（`data`），拿不到明文；但**字段
                    # 存在**就是上游补丁的触发条件，所以带上密文比丢掉安全（它是密文，
                    # 不含可识别的自然语言指纹）。
                    raw_think = block.get('thinking')
                    if not isinstance(raw_think, str) or not raw_think:
                        raw_think = block.get('data')
                    decoded = raw_think if isinstance(raw_think, str) else ''
                # **累积**而不是覆盖：一条 assistant 里可能有多个 thinking 块
                # （分段推理 / redacted + 明文并存），只留最后一个会把前文丢掉。
                # 与 responses.py 的处理保持一致——两套协议面对的是同一份推理内容，
                # 行为不该因客户端选了哪个协议而不同。
                thinking_text = decoded if thinking_text is None else thinking_text + decoded
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
                # OpenAI 的 role:tool 消息 content 只能是字符串，图片物理上无处安放。
                # 旧写法在「只有图片、没有文字」时落到 str(content) 兜底，会把整段
                # base64 连同结构标记压成 Python repr 文本发给上游——实测 3.3MB 的
                # 图被上游按 234 万 token 计数（prompt too long: 2349045 > 1048576），
                # 上下文瞬间撑爆且会话无法恢复。
                # 正确做法：tool 消息只留文字，图片提升到紧随其后的 user 消息。
                raw_result = block.get('content')
                result_text = _text_of(raw_result)
                result_images: list[dict] = []
                if isinstance(raw_result, list):
                    for sub in raw_result:
                        if isinstance(sub, dict) and sub.get('type') == 'image':
                            url = _image_url(sub.get('source'))
                            if url:
                                result_images.append(
                                    {'type': 'image_url', 'image_url': {'url': url}})
                if not result_text and not result_images:
                    # 既无文字也无图片：保留原兜底，未知形态仍以文本透出
                    result_text = str(raw_result or '')
                elif not result_text:
                    result_text = '[图片]'  # 上游要求 tool content 非空
                tool_images.extend(result_images)
                tool_results.append({
                    'role': 'tool',
                    'tool_call_id': str(block.get('tool_use_id') or ''),
                    'content': result_text,
                })

        # 工具结果必须先于本条的其余内容（它们对应上一轮 assistant 的调用）
        messages.extend(tool_results)
        # 工具结果里的图片提升到这里：并入同一条 user 消息，与人类上传的图片
        # 同等对待，上游（workbuddy2api → CodeBuddy）实测能识别为视觉输入。
        parts = tool_images + parts

        # 客户端发了 thinking 块、但里面没有可用内容（畸形输入）时记一条 WARN，
        # 值**保留**空串 —— 交给 attach_reasoning 补占位（口径见那里）。
        if thinking_text == '':
            # 空值**保留**（不置回 None）：`attach_reasoning` 见到空值会补一个
            # 空格交出去 —— 上游校验 len(reasoning)>0 且不 trim，空格过闸、
            # 空串不过（口径与上游 2026-09-19 commit 5657229 一致）。
            # 置 None 则整条推理字段都不挂，等于把「有痕迹」退回「没痕迹」。
            #
            # 记 WARN：畸形输入（客户端发来空的 thinking 块）此前完全静默，
            # 真遇到 11155 时看不出是哪个客户端这么发的。
            logger.warning(
                '客户端发来的 thinking 块无可提取内容 —— 将按上游口径补占位，'
                '若上游报 11155 请把此日志一并提供',
            )

        if tool_calls:
            msg: dict = {'role': 'assistant', 'tool_calls': tool_calls}
            text = '\n'.join(p['text'] for p in parts if p.get('type') == 'text')
            # 有工具调用时 content 通常是空的，但保留文本更稳（部分上游要求非 null）
            msg['content'] = text or None
            if thinking_text is not None:
                responses.attach_reasoning(msg, thinking_text)
            messages.append(msg)
        elif parts or thinking_text is not None:
            # 只有一个纯文本块时压平为字符串——多数上游对字符串更宽容
            if len(parts) == 1 and parts[0].get('type') == 'text':
                msg = {'role': role, 'content': parts[0]['text']}
            elif parts:
                msg = {'role': role, 'content': parts}
            else:
                # 本条只有 thinking 块、没有正文：仍要落一条 assistant 消息把痕迹
                # 带上，否则这段推理无处安放、等于又丢掉了。
                msg = {'role': role, 'content': None}
            # 只在 assistant 消息上挂 —— 腾讯的校验针对 assistant 回合；
            # 挂在别处会造成上游不认的组合，那比不挂更糟。
            if thinking_text is not None and role == 'assistant':
                responses.attach_reasoning(msg, thinking_text)
            messages.append(msg)

    out['messages'] = messages

    if body.get('max_tokens') is not None:
        out['max_tokens'] = body['max_tokens']
    for src, dst in (('temperature', 'temperature'), ('top_p', 'top_p')):
        if body.get(src) is not None:
            out[dst] = body[src]
    # 推理档位（issue #39）：Anthropic 的 output_config.effort / thinking.budget_tokens
    # → 上游的 reasoning_effort。不映射的话用户选了档位却拿到默认档，且无从自查。
    effort = _reasoning_effort(body)
    if effort:
        out['reasoning_effort'] = effort
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
        elif kind == 'none':
            out['tool_choice'] = 'none'

        # `disable_parallel_tool_use` 是 Anthropic 的「这一轮只准调一个工具」开关，
        # 藏在 tool_choice 里而不是顶层。漏掉它，客户端以为并发被禁了、实际没禁：
        # 上游可能一次回多个 tool_use，客户端的串行编排会拿到意料之外的结果。
        # 只在显式要求禁用时映射——Anthropic 的默认（未给该字段）就是允许并发，
        # 而 OpenAI 侧默认同样是允许，因此不写字段即语义一致。
        if choice.get('disable_parallel_tool_use') is True:
            out['parallel_tool_calls'] = False
    return out


def _thinking_enabled(body: dict) -> bool:
    """请求是否启用了扩展思考。

    Anthropic 的形状是 `thinking: {'type': 'enabled', 'budget_tokens': N}`；
    关闭时客户端会写 `{'type': 'disabled'}` 或干脆不带这个字段。两种都算没启用。

    **`adaptive` 同样算启用**：新版 Claude Code 对「模型自适应决定思考量」的模型
    发 `{'type': 'adaptive', 'display': ...}`（无 `budget_tokens`）。而它判断一个
    模型是否支持 adaptive 的依据是**模型名是否在它的官方能力表里**——本地网关
    后面挂的 `cn:deepseek-v4-flash` 这类第三方模型名一律查不到，于是**全部**回落
    到 `adaptive`。只认 `enabled` 会让这些请求的推理在网关这一跳被整段丢弃：
    上游照常思考（`output_config.effort` 也照常传），但客户端一个 `thinking_delta`
    都收不到，表现为「思考过程不可见、`thinking_tokens` 恒为 0」。
    两种 type 对网关的语义相同——客户端能接受 thinking 块——因此都回。

    宽容处理**畸形值**：不是 dict、或缺 type，都按「没启用」—— 宁可少回一个
    thinking 块（客户端拿不到凭据时只是多花点 token 重新思考），也不要对着
    一个不认这种块的客户端硬塞。
    """
    think = body.get('thinking')
    if not isinstance(think, dict):
        return False
    return str(think.get('type') or '').lower() in ('enabled', 'adaptive')


# Anthropic 的 `output_config.effort` → 上游 `reasoning_effort` 的档位映射。
#
# 上游的档位命名取自腾讯 CodeBuddy 官方客户端，其**从低到高**是
# `off < minimal < low < medium < high < xhigh < max`（见 workbuddy2api 的
# `payload.go: effortRank`，`effort_catalog.go` 里各模型声明的也正是这套）。
# 也就是说 **`xhigh` 与 `max` 是两个不同的档**，`max` 更强。
#
# Anthropic 侧只用到 low/medium/high/max。名字重合的直接同名透传；`xhigh` 也接受
# （它本就是上游的合法档，客户端可能直接给出）。
#
# **不要把 `max` 降成 `xhigh`**：那会让「选最高档」实际拿到次高档，正是这个映射
# 要消除的「设了没用」。真正的「该模型支持哪些档」由上游自己的降级管线处理
# （`normalizeReasoningEffort` 会按 `effortRank` 降到 ≤ 请求档的最高支持档），
# 我们不重复实现那张表——重复就是两份事实来源。
_EFFORT_MAP = {
    'low': 'low',
    'medium': 'medium',
    'high': 'high',
    'xhigh': 'xhigh',
    'max': 'max',
}


def _reasoning_effort(body: dict) -> str | None:
    """把 Anthropic 侧的推理档位映射成上游的 `reasoning_effort`（无则 None）。

    两个来源，**优先级**：`output_config.effort`（显式档位）> `thinking.budget_tokens`
    （按预算分档）。

    为什么必须读它们（issue #39）：客户端（如 Claude Code）用它来控制思考深度，
    而此前 `to_openai_request` 把 `thinking` 整个吃掉、只用于「要不要回 thinking
    块」，`output_config` 更是**完全没读**。于是用户选了 high、实际发出去的是
    上游默认档——用户看到的现象是「调了档位但模型思考深度没变」，没法自查。

    `budget_tokens` → 档位的分界按官方文档的量级取：1024 是最小可用预算，
    4k 以下算 low、16k 以下算 medium、64k 以下算 high，再往上 max。
    精度不重要——它是「用户愿意花多少」的粗略表达，而上游还会按模型能力降级。
    """
    effort = None
    cfg = body.get('output_config')
    if isinstance(cfg, dict):
        raw = cfg.get('effort')
        if isinstance(raw, str) and raw.strip():
            effort = _EFFORT_MAP.get(raw.strip().lower())

    if effort is None:
        think = body.get('thinking')
        budget = think.get('budget_tokens') if isinstance(think, dict) else None
        if isinstance(budget, bool):
            budget = None
        if isinstance(budget, (int, float)) and budget > 0:
            if budget < 4096:
                effort = 'low'
            elif budget < 16384:
                effort = 'medium'
            elif budget < 65536:
                effort = 'high'
            else:
                # 最高一档是 `max`（不是 `xhigh`）：预算 ≥64k 表达的是「不限思考」，
                # 对应上游最强档。给 `xhigh` 会让这种请求拿不到应有的深度。
                effort = 'max'
    return effort


def _thinking_block(text: str) -> dict:
    """Anthropic 的 thinking 块：`thinking` 给人看，`signature` 供客户端回传。

    两个字段缺一不可（见 responses.py 里 `_encode_credential` 的完整说明）：
      · 只有 `thinking` → 客户端没有可回传的签名，**整个块被丢弃**，下一轮不带
        推理痕迹，DeepSeek 报 11155（issue #36 报的正是这个）；
      · 只有 `signature` → 界面上看不到思考过程。

    `signature` 用与 Responses `encrypted_content` 相同的编解码——同一轮对话在
    两套协议间切换时凭据仍能解开。
    """
    return {
        'type': 'thinking',
        'thinking': text,
        'signature': responses._encode_credential(text),
    }


def to_anthropic_response(data: dict, model: str, *, thinking: bool = False) -> dict:
    """OpenAI 非流式响应 → Anthropic 响应体。

    thinking=True 时在最前面加一个带 `signature` 的 thinking 块（推理原文 +
    可回传凭据）。见 `_thinking_block` 的说明。
    """
    choice = (data.get('choices') or [{}])[0] if isinstance(data.get('choices'), list) else {}
    message = choice.get('message') or {}
    usage = data.get('usage') or {}

    content: list[dict] = []
    reasoning = message.get('reasoning_content')
    if thinking and isinstance(reasoning, str) and reasoning:
        content.append(_thinking_block(reasoning))
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
        # 口径转换见 _usage_fields：Anthropic 的 input 不含缓存，
        # 直接发 prompt_tokens 会让客户端把缓存算两遍。
        'usage': _usage_fields(usage),
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

    def __init__(self, model: str, *, thinking: bool = False) -> None:
        self.model = model
        # 请求里是否启用了思考（`thinking.type == 'enabled'`）。只有启用时才回
        # thinking 块：没启用的客户端不会处理这种块，多出来反而可能被当成异常
        # （issue #36 的建议，也是 Anthropic 官方行为——不开思考就没有该块）。
        self.thinking = thinking
        self.msg_id = 'msg_' + uuid.uuid4().hex[:20]
        self.started = False
        self.finished = False
        self.text_index: int | None = None       # 当前打开的文本块
        self.tool_index: int | None = None       # 当前打开的工具块
        self.think_index: int | None = None      # 当前打开的思考块
        self.think_buf = ''                      # 思考全文（用于算签名）
        self.next_index = 0
        self.tool_seen = False
        # 是否见过正文增量：首字延迟据此判定（用「第一个含正文的 delta」，而不是
        # 「第一个 chunk」——后者会把建连时间也算进去，数字偏小且失真）
        self.saw_content = False
        self.input_tokens = 0
        self.output_tokens = 0
        # 缓存两段（Anthropic 把它们与 input 并列，见 _usage_fields）
        self.cache_read = 0
        self.cache_write = 0

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

    def _close_think(self) -> list[bytes]:
        """关掉思考块。**先把签名发完，再 stop**。

        顺序不能反：`content_block_stop` 之后到达的 delta 会被客户端丢弃
        （块已经关闭），签名就白发了 —— 而签名正是客户端回传推理的唯一凭据，
        丢了它等于整个修复失效（issue #36）。
        """
        if self.think_index is None:
            return []
        index, self.think_index = self.think_index, None
        out = [_event('content_block_delta', {
            'type': 'content_block_delta',
            'index': index,
            'delta': {
                'type': 'signature_delta',
                'signature': responses._encode_credential(self.think_buf),
            },
        })]
        out += self._close(index)
        return out

    def _open_think(self) -> list[bytes]:
        self.think_index = self.next_index
        self.next_index += 1
        return [_event('content_block_start', {
            'type': 'content_block_start',
            'index': self.think_index,
            'content_block': {'type': 'thinking', 'thinking': ''},
        })]

    def feed(self, obj: dict) -> list[bytes]:
        """喂一个 OpenAI SSE 的 data 对象，返回要下发的事件。"""
        out: list[bytes] = []
        # 已经收尾过就不再吐事件：上游若在带 finish_reason 的帧之后**继续发内容帧**
        # （它自己有 bug，或被打穿），客户端会收到 message_stop 之后的事件，
        # 而那些块永远等不到 content_block_stop —— 协议被污染。
        if self.finished:
            return out
        if not self.started:
            out += self._start_message()

        # 非流式写法的 usage 也可能出现在末尾帧
        usage = obj.get('usage')
        if isinstance(usage, dict):
            # 只累积**原始**数值，口径转换留到发送时（_usage_fields）——
            # 因为 Anthropic 的 input 要减掉缓存，而这里是「上游原始值」。
            self.input_tokens = _as_int(usage.get('prompt_tokens')) or self.input_tokens
            self.output_tokens = _as_int(usage.get('completion_tokens')) or self.output_tokens
            self.cache_read = _as_int(usage.get('prompt_cache_hit_tokens')) or self.cache_read
            self.cache_write = _as_int(usage.get('prompt_cache_write_tokens')) or self.cache_write

        choices = obj.get('choices')
        if not isinstance(choices, list) or not choices:
            return out
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get('delta') or choice.get('message') or {}
        if not isinstance(delta, dict):
            delta = {}

        # 推理增量（上游的 `reasoning_content`）→ thinking 块。
        # 只有请求里启用了思考才回：没启用的客户端不处理这种块。
        # 位置在文本之前——与上游给增量的顺序一致（先思考后正文）。
        reasoning = delta.get('reasoning_content')
        if self.thinking and isinstance(reasoning, str) and reasoning:
            if self.think_index is None:
                out += self._open_think()
            self.think_buf += reasoning
            out.append(_event('content_block_delta', {
                'type': 'content_block_delta',
                'index': self.think_index,
                'delta': {'type': 'thinking_delta', 'thinking': reasoning},
            }))

        text = delta.get('content')
        if isinstance(text, str) and text:
            self.saw_content = True
            # 切到正文前先把思考块关掉（含发签名）——思考与正文是两个块，
            # 顺序保证签名在对应的正文之前送达。
            out += self._close_think()
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
            # 文本块与工具块不能并存：切到工具前先关文本；
            # 思考块同理（它的签名要在关闭前发完）
            out += self._close_think()
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
        # 思考块先关（含发签名）：顺序与开块顺序一致，也让签名一定落在 stop 之前
        out += self._close_think()
        out += self._close(self.text_index)
        out += self._close(self.tool_index)
        self.text_index = self.tool_index = None

        reason = finish_reason
        # 出现过工具调用但上游没给 finish_reason 时，按 tool_use 收尾更贴近实际
        if reason is None and force and self.tool_seen:
            reason = 'tool_calls'
        # message_delta 发的是**累计 usage**，必须把 input_tokens 一并带上。
        #
        # 此前只发了 output_tokens —— 而 Anthropic 规范里 input_tokens 应当出现在
        # 这里（message_start 那一刻上游还没给 usage，所以那里必然是 0，真实值
        # 只能在末尾补）。少了它，客户端**整条流里再也看不到真实的输入量**，
        # 只能回退成按字符估算（中文会被低估约 1.5 倍），上下文余量也跟着算错。
        # 社区实测：网关侧统计 7447 万，客户端只显示 3648 万，差了整整一倍
        # （issue #39）。
        out.append(_event('message_delta', {
            'type': 'message_delta',
            'delta': {'stop_reason': _STOP_REASON.get(str(reason), 'end_turn'), 'stop_sequence': None},
            'usage': _usage_fields({
                'prompt_tokens': self.input_tokens,
                'completion_tokens': self.output_tokens,
                'prompt_cache_hit_tokens': self.cache_read,
                'prompt_cache_write_tokens': self.cache_write,
            }),
        }))
        out.append(_event('message_stop', {'type': 'message_stop'}))
        return out


# SSE 解析缓冲的上限。正常 SSE 是「一行一条 data:」，缓冲里始终只留半行；
# 如果上游持续吐出不含换行的数据（实现有 bug、或上游被换成了别的东西），
# 缓冲会一直长下去直至吃光内存——所以到上限就中止转发并落一条日志。
# 1 MiB 远大于任何正常的单行 SSE（含大段 tool_use 参数也只几十 KB）。
MAX_SSE_BUFFER = 1 << 20


@router.post('/v1/messages')
async def messages(request: Request):
    """Anthropic Messages API。"""
    body, err = await gateway._read_json_body(request)
    if err:
        # 保留网关注入的状态码语义：413（体太大）与 400（JSON 非法）是两回事。
        # 一律写死 400 会让客户端在「体太大」时误判为格式问题——它只会原样重试，
        # 而不会去压缩上下文或调大上限（gateway 那条路给的就是 413 + 调参建议）。
        if getattr(err, 'status_code', 0) == 413:
            return _err('请求体超过上限：请压缩上下文或附件。'
                        '该上限由本网关设置（环境变量 WB_GATEWAY_MAX_BODY_MB）', 413)
        return _err('请求体不是合法 JSON 对象')

    model = body.get('model')
    if not isinstance(model, str) or not model.strip():
        return _err('model 必须是字符串')
    # 客户端原样的 Anthropic 请求体（快照要在 clamp_max_tokens 之前：那一步会
    # 就地改 `max_tokens`，之后就看不出客户端原本要了多少）。
    client_body = tap_svc.snapshot(body)
    # max_tokens 在 Anthropic 协议里是**必填项**，缺失即 400（与 OpenAI 不同）
    if body.get('max_tokens') is None:
        return _err('缺少必填字段 max_tokens')
    # 越界的 max_tokens 会被上游包装成 503（见 gateway.clamp_max_tokens 的说明）。
    # 在必填性校验之后压回上限，转换层就能拿到已收敛的值。
    gateway.clamp_max_tokens(body)

    # 鉴权：与 gateway._authorize 同一套检查、同一顺序（见该函数说明）
    # 映射先算：版本归属判的是**映射后**的实际模型名（issue #47）
    mapped = gateway._map_model(model)
    key, ip, auth_err = _authorize(request, model, mapped=mapped)
    if auth_err:
        return auth_err

    ua = request.headers.get('user-agent')
    stream = bool(body.get('stream'))
    # 客户端是否启用了扩展思考（Anthropic 的 `thinking: {type:'enabled'}`）。
    # 只有启用时才在响应里回 thinking 块——没启用的客户端不处理这种块，
    # 多出来可能被当成协议异常（issue #36 的建议，也与官方行为一致）。
    want_thinking = _thinking_enabled(body)

    try:
        payload = to_openai_request(body)
    except Exception as exc:  # noqa: BLE001
        gateway._record(key, ip, model, mapped or '', 400, 0, 0, 0, ua, str(exc), False,
                        account_expected=False)
        return _err(f'请求转换失败：{exc}')

    # 思考档位（issue #319）：客户端选了档位，日志里却一直是「—」，因为只有
    # chat 路径把 reasoning_effort 传给了 _record。这里从**已转换的 payload**
    # 取——to_openai_request 已经把 Anthropic 的 output_config / thinking
    # 映射成上游的 reasoning_effort，取它才是「这次真正发出去的值」。
    effort = payload.get('reasoning_effort')
    effort = effort.strip()[:16] if isinstance(effort, str) and effort.strip() else None

    if mapped:
        payload['model'] = mapped
    if stream:
        payload.setdefault('stream_options', {})
        if isinstance(payload['stream_options'], dict):
            payload['stream_options'].setdefault('include_usage', True)

    # 上游按密钥解析（多上游 / 分组隔离）；绑定缺失或停用时明确报错，不回落。
    try:
        upstream = upstreamsvc.resolve_for_key(key)
    except upstreamsvc.UpstreamUnavailable as exc:
        gateway._record(key, ip, model, mapped or '', 503, 0, 0, 0, ua, str(exc), False,
                        account_expected=False)
        # anthropic 的错误体没有 code 字段（只有 type），故第三参给 type
        return _err(str(exc), 503, 'api_error')
    url = f'{upstream["base_url"]}/v1/chat/completions'
    # 转发头只构造一次：既给 httpx，也给抓包（双视角的一半）
    up_headers = gateway._upstream_headers(upstream)
    # 关联键：让上游的出站转储能精确对回这一轮（见 gateway 同名注释）
    # 关联键只服务抓包：关闭时不发这个头，交给上游自己按会话生成 32 位 ID。
    # 开着时也用 32 位 hex（与上游/官方客户端同形），16 位会改变出站头的形状。
    trace_id = uuid.uuid4().hex if tap_svc.enabled() else None
    if trace_id:
        up_headers['X-Trace-ID'] = trace_id
    started = time.time()

    if not stream:
        try:
            async with config.http_client(config.UPSTREAM_TIMEOUT, connect=5) as client:
                resp = await client.post(url, json=payload,
                                         headers=up_headers)
            latency = int((time.time() - started) * 1000)
            usage: dict = {}
            try:
                data = resp.json()
                usage = data.get('usage') or {}
            except Exception:  # noqa: BLE001
                data = None
            # 整份 usage 交过去（而不是只取 credit）：扣费与提示词缓存三段
            # 都在这一份里，分开取就会出现「某条协议缓存永远是空的」（issue #69）。
            gateway._record(
                key, ip, model, mapped or '', resp.status_code,
                _as_int(usage.get('prompt_tokens')), _as_int(usage.get('completion_tokens')),
                latency, ua, None if resp.status_code < 400 else str(data)[:500], False,
                usage=usage, effort=effort,
            )
            # 抓包：body 是 Anthropic 原形（客户端意图），payload 是转换后真正
            # 发往上游的 OpenAI 体 —— 排查「转换层吃了什么字段」要靠后者。
            # 客户端视角：Anthropic 客户端实际收到的报文（翻译后的形态）。
            # 与 `payload`（上游原文）是两份东西，抓包页要能对着看。
            client_doc = None
            if resp.status_code < 400:
                try:
                    client_doc = to_anthropic_response(
                        data if isinstance(data, dict) else {}, model,
                        thinking=want_thinking)
                except Exception:  # noqa: BLE001 - 翻译失败不该影响抓包
                    client_doc = None
            tap_svc.capture(
                body=client_body, headers=request.headers.items(), key=key, ua=ua,
                path='/v1/messages', protocol='anthropic',
                status=resp.status_code, latency_ms=latency, stream=False,
                payload=data, usage=usage,
                error=None if resp.status_code < 400 else str(data)[:500],
                upstream_body=payload, upstream_headers=up_headers,
                trace_id=trace_id, client_response=client_doc,
            )
            if resp.status_code >= 400:
                msg = ''
                if isinstance(data, dict):
                    err_obj = data.get('error')
                    msg = str(err_obj.get('message') if isinstance(err_obj, dict) else err_obj) or str(data)[:300]
                else:
                    msg = resp.text[:300]
                return _err(msg, resp.status_code, 'api_error',
                            hint=gateway._error_hint(data))
            # 复用上面为抓包算好的那份：翻译一次就够，重复翻译既慢又可能
            # 因为两次取值不同（例如时间戳字段）而在界面上与实际的响应对不上。
            return JSONResponse(client_doc if client_doc is not None else
                                to_anthropic_response(
                                    data if isinstance(data, dict) else {},
                                    model, thinking=want_thinking))
        except Exception as exc:  # noqa: BLE001
            latency = int((time.time() - started) * 1000)
            gateway._record(key, ip, model, mapped or '', 502, 0, 0, latency, ua, str(exc), False,
                            account_expected=gateway._account_expected_after_error(exc))
            tap_svc.capture(
                body=client_body, headers=request.headers.items(), key=key, ua=ua,
                path='/v1/messages', protocol='anthropic', status=502,
                latency_ms=latency, stream=False, error=str(exc),
                upstream_body=payload, upstream_headers=up_headers, trace_id=trace_id,
            )
            return _err(f'上游不可用：{exc}', 502, 'api_error')

    # ── 流式 ────────────────────────────────────────────────
    client = config.http_client(config.UPSTREAM_TIMEOUT, connect=5)
    try:
        req = client.build_request('POST', url, json=payload,
                                   headers=up_headers)
        resp = await client.send(req, stream=True)
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        latency = int((time.time() - started) * 1000)
        gateway._record(key, ip, model, mapped or '', 502, 0, 0, latency, ua, str(exc), True,
                        account_expected=gateway._account_expected_after_error(exc))
        return _err(f'上游不可用：{exc}', 502, 'api_error')

    status_code = resp.status_code

    # 抓包用的原始流缓冲（`pending` 会被逐行消费，存不出完整响应，同 gateway）
    raw_stream = tap_svc.RawStream()
    # 回给客户端的那条流（Anthropic 事件）。与 `raw_stream` 分开：那份是上游的
    # OpenAI 形态，这份才是客户端手里拿到的报文 —— 抓包页的「客户端视角」用它。
    client_stream = tap_svc.RawStream()

    async def gen():
        usage: dict = {}
        pending = ''
        translator = _StreamTranslator(model, thinking=want_thinking)
        error_text: str | None = None
        first_token_ms: int | None = None
        abort = False   # 中途出错需中止转发（缓冲超限 / 上游回 error 帧）

        try:
            async for chunk in resp.aiter_bytes():
                piece = chunk.decode('utf-8', errors='ignore')
                raw_stream.feed(piece)
                pending += piece

                if status_code >= 400:
                    # 有数据就留一份：早先要等到 4000 字节才取，而常见的上游错误体
                    # 只有几百字节 → error_text 恒为 None，错误被**静默丢弃**，
                    # 客户端收到「正常但内容为空」的回答，连重试都不会触发。
                    if error_text is None and pending.strip():
                        error_text = pending[:500]
                    # 上游报错时也可能持续吐数据，缓冲同样要设上限
                    if len(pending) > MAX_SSE_BUFFER:
                        break
                    continue

                if len(pending) > MAX_SSE_BUFFER:
                    logger.warning('SSE 缓冲超过 %d 字节仍未见换行，中止转发（上游响应形态异常）',
                                   MAX_SSE_BUFFER)
                    error_text = '上游响应异常：数据流缺少分隔'
                    abort = True
                    break

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

                    # 上游可能**中途**回一个 error 帧（形如 {"error":{...}}，没有
                    # choices）。整帧丢弃会让客户端只收到 message_start 加一个空
                    # 回答——必须显式转成 Anthropic 的 error 事件并中止。
                    err_obj = obj.get('error')
                    if isinstance(err_obj, dict) and not obj.get('choices'):
                        msg = err_obj.get('message')
                        error_text = str(msg if msg else err_obj)[:500]
                        # 上游在 error 帧里可能带 gateway_hint（并列的可执行建议，
                        # 上游 StreamHint 变体的产物）。拼在 message 后面一起发给
                        # 客户端——SSE 的 error 事件只有 message 一个字段位，
                        # 丢掉它等于让「该怎么办」这句建议消失。
                        mid_hint = gateway._error_hint(obj)
                        if mid_hint:
                            error_text = f'{error_text}（{mid_hint}）'[:500]
                        abort = True
                        break

                    frame_usage = obj.get('usage')
                    if isinstance(frame_usage, dict):
                        usage.update(frame_usage)

                    for event in translator.feed(obj):
                        client_stream.feed(event.decode('utf-8', 'ignore'))
                        yield event

                    if translator.saw_content and first_token_ms is None:
                        first_token_ms = int((time.time() - started) * 1000)

                if abort:
                    break

            # 收尾。**顺序要紧**：error 必须发在 message_stop **之前**。
            # 多数 SDK（含 Anthropic 官方库）把 message_stop 当作流的终止信号，
            # 读到它就结束迭代、不再读后续事件——先发 stop 后发 error 等于那个
            # 错误永远不会被客户端看到，客户端仍表现为「成功但空回复」，比报错
            # 更难排查。
            #
            # 两种情况都会走到这里，所以只写一遍：
            #   · 上游直接报错（status >= 400）—— error_text 是响应体；
            #   · 状态码 200 但**过程中**出错（缓冲超限 / 上游中途回 error 帧）——
            #     此时客户端拿到的 stop_reason 是 end_turn，即一个「完整但空/
            #     残缺」的回答：不重试、不报错，排障时毫无线索；截断点若落在
            #     input_json_delta 中间，还会拿半截 JSON 去执行工具调用。
            if error_text:
                err_event = _event('error', {
                    'type': 'error',
                    'error': {'type': 'api_error', 'message': error_text},
                })
                client_stream.feed(err_event.decode('utf-8', 'ignore'))
                yield err_event
            # 仍补一套收尾事件（含 message_start，若流尚未开始）：部分客户端期
            # 待流以 message_stop 结束，缺了会一直等（挂住）。error 已先发出，
            # 不会再被吞掉。
            for event in translator.finish(None, force=True):
                client_stream.feed(event.decode('utf-8', 'ignore'))
                yield event
        finally:
            await resp.aclose()
            await client.aclose()
            latency = int((time.time() - started) * 1000)
            gateway._record(
                key, ip, model, mapped or '', status_code,
                _as_int(usage.get('prompt_tokens')), _as_int(usage.get('completion_tokens')),
                latency, ua, error_text, True,
                usage=usage, first_token=first_token_ms, effort=effort,
            )
            tap_svc.capture(
                body=client_body, headers=request.headers.items(), key=key, ua=ua,
                path='/v1/messages', protocol='anthropic', status=status_code,
                latency_ms=latency, first_token_ms=first_token_ms, stream=True,
                text=raw_stream.text(), usage=usage, error=error_text,
                upstream_body=payload, upstream_headers=up_headers,
                trace_id=trace_id, client_response=client_stream.text() or None,
            )

    return StreamingResponse(gen(), status_code=200, media_type='text/event-stream')


@router.post('/v1/messages/count_tokens')
async def count_tokens(request: Request):
    """粗略估算输入 token 数。

    Claude Code 等客户端会先调这个接口来决定上下文还能塞多少。我们拿不到
    上游的分词器，**只能给粗略估算**。

    ## 估算口径：按字符类别分开算（不是统一「字符数 / 3」）

    早先这里是 `(字符数 + 2) // 3`，而注释同时写着「宁可高估」——**两者是矛盾的**：
    除 3 对 ASCII 大致合理（英文约 4 字符/token），但对中日韩文是**严重低估**
    （汉字通常 1–1.5 字符/token，除 3 会把一段中文算成实际的 1/3）。用户报的
    症状正是「估算偏低」（issue #39）：中文调用方按这个数留余量，真实请求却超限。

    高估是安全方向（客户端少塞一点，请求仍能过），低估会让请求在真正发出时被
    上游以「上下文过长」拒绝，且用户完全看不出是估算接口给了错数字。

    所以按脚本分开加权：
      · CJK（汉字 / 假名 / 韩文）按 ~1 字符 1 token；
      · 其余（拉丁、代码、JSON）按 ~4 字符 1 token；
      · 每个 block 再加一点结构开销（role、分隔符等），因为真实分词时
        消息边界会引入额外 token，纯按字符数算必然偏低。
    """
    # 体积上限：这个端点同样对外开放，不能成为绕过网关请求体上限的后门
    # （gateway._read_json_body 逐块累加、不信任 Content-Length，正是为此）。
    body, err = await gateway._read_json_body(request)
    if err:
        if getattr(err, 'status_code', 0) == 413:
            return _err('请求体超过上限：请精简后再试', 413)
        return _err('请求体不是 JSON 对象')

    # 完整鉴权：早先这里只挑了个「能解析的密钥」就放行，把停用 / 过期 / 配额 /
    # IP 白名单 / 限流**全漏了**——被吊销的密钥照样能调这个端点。现在与
    # /v1/messages 走同一条路径（`_authorize`）。
    # count_tokens 允许不带 model（那是常态），故 model 为 None 时按「模型发现
    # 类请求」处理：跳过版本与模型白名单，其余约束照常生效。
    model = body.get('model') if isinstance(body.get('model'), str) else None
    # 同样是「映射先于鉴权」：客户端在发消息前先数 token，用的还是别名——
    # 若这里按请求名判版本，配了别名映射的密钥会在这一步就被打回（issue #47）
    _mapped = gateway._map_model(model)
    _key, _ip, auth_err = _authorize(request, model, mapped=_mapped)
    if auth_err:
        return auth_err

    total = 0
    blocks = 0
    system = body.get('system')
    if system:
        blocks += 1
        total += _estimate_tokens(
            _text_of(system) if not isinstance(system, str) else system)
    for msg in body.get('messages') or []:
        if isinstance(msg, dict):
            blocks += 1
            total += _estimate_tokens(_text_of(msg.get('content')))
    for tool in body.get('tools') or []:
        if isinstance(tool, dict):
            blocks += 1
            total += _estimate_tokens(json.dumps(tool, ensure_ascii=False))
    # 每块 4 token 的结构开销：消息/工具边界在真实分词里都要额外占位。
    # 这部分是「宁可高估」的落点之一。
    return JSONResponse({'input_tokens': max(1, total + blocks * 4)})
