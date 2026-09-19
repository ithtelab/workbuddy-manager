"""OpenAI Responses API 兼容层（`/v1/responses`）。

为什么需要
----------
上游只有 OpenAI **Chat Completions** 接口。一批客户端（Codex、DeepSeek
Harness 的 `openai-responses` 协议、部分 OpenAI 官方 SDK 用法）只发
**Responses** 协议——请求体是 `input` 而不是 `messages`、`instructions` 是顶层
字段、工具是扁平形状；响应体是 `output` 数组、流式是一串
`response.output_text.delta` 事件。两者不是"参数改名"级别的差别，所以只能在
本层做双向翻译。

本模块只做**协议翻译**，其余（密钥鉴权、版本隔离、IP 管控、配额、限流、
日志与用量记账）**一律复用 gateway 既有实现**——多一个协议不该走一套新逻辑。
响应体里的模型名回填**用户请求的名字**（不是映射后的上游名），否则客户端会
认为「我请求的模型被换掉了」。

关于流式事件的形状
------------------
事件名与字段**不是猜的**，是对着客户端的解析实现逐条对齐的
（`@earendil-works/pi-ai` 的 `api/openai-responses*.js`，DeepSeek Harness 走它）。
其中两条硬性要求：

  · 流**必须以 `response.completed` 之类的事件收尾**——该实现若没见到终止事件
    会直接抛 `stream ended before a terminal response event`，客户端看到的是
    「失败」而不是「空回答」；所以收尾事件不能省。
  · 文本增量与 `output_item.added` 的 `output_index` **必须一致**——解析器按
    `output_index` 建立并检索"槽位"，对不上时增量被**静默丢弃**（表现为
    「有回复但内容为空」，比报错更难查）。

上游报错时**不**中途插入 SSE 错误帧，而是直接回非流式错误体（见 `_failed`）：
状态码得以保留，客户端按正常 HTTP 错误处理——这与本网关
「错误状态码要能穿过客户端折叠层」的修正是同一件事（issue #18）。
"""
from __future__ import annotations

import base64
import json
import logging
import time
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import config, keysvc
from . import gateway

logger = logging.getLogger('workbuddy.responses')

router = APIRouter(tags=['responses'])

# 单个请求最多翻译多少个 output item（文本/推理/工具各算一个）。设上限是因为
# 事件里的 index 由我们分配、与上游数组同长；上游给的畸形数据不该让我们无限
# 追加对象（正常一次回答只有几个 item）。
MAX_OUTPUT_ITEMS = 256


class CustomToolArgumentsError(ValueError):
    """Custom/freeform tool output cannot be executed through a function wrapper."""


def _custom_tool_names(body: dict) -> set[str]:
    tools = body.get('tools')
    if not isinstance(tools, list):
        return set()
    return {
        tool['name'] for tool in tools
        if isinstance(tool, dict)
        and tool.get('type') == 'custom'
        and isinstance(tool.get('name'), str)
        and tool['name']
    }


def _custom_input(raw: object) -> str:
    """Decode the temporary Chat wrapper used for a Responses custom tool.

    The wrapper is deliberately strict: a malformed or ambiguous custom call
    must fail instead of being emitted as an executable ordinary function call.
    """
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique_object) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        raise CustomToolArgumentsError(
            'Custom tool arguments must be a JSON object containing only string input.') from None
    if not isinstance(value, dict) or set(value) != {'input'} or not isinstance(value['input'], str):
        raise CustomToolArgumentsError(
            'Custom tool arguments must be a JSON object containing only string input.')
    return value['input']


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _event(name: str, payload: dict) -> bytes:
    """Responses 的 SSE 帧：`event:` 行 + 单行 data + 空行结尾。

    与 Chat Completions 的裸 `data:` 不同，这里**必须**带 `event:` 行——解析器
    按 `event:` 分派；只给 data 会让它拿不到类型而直接跳过。
    """
    return f'event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'.encode()


def _failed(message: str, status: int, err_type: str = 'api_error',
            code: str | None = None, hint: str | None = None) -> JSONResponse:
    """错误回非流式 JSON（带状态码），而不是 SSE 错误帧。

    流一旦以 200 开始，状态码就固定了——错误只能裹在事件里，客户端会把它当作
    「流正常结束」。所以错误一律在**开流之前**用真实状态码回掉。

    `hint` 透传上游的 `error.gateway_hint`（并列的可执行建议；为空时不写字段）。
    """
    return gateway._oai_error(message, status, err_type, code, hint)


def _upstream_error_text(data: object, resp: object = None,
                         *, fallback: str = '') -> str:
    """从上游错误体里取出人可读的原因（拿不到就退回原文/默认文案）。

    顺序是「越具体越优先」：OpenAI 形状的 error.message → 顶层 message →
    整段 JSON 摘要 → 未解析的响应原文 → 兜底文案。
    """
    if isinstance(data, dict) and data:
        err = data.get('error')
        if isinstance(err, dict):
            msg = err.get('message')
            if isinstance(msg, str) and msg.strip():
                return msg
        elif isinstance(err, str) and err.strip():
            return err
        msg = data.get('message')
        if isinstance(msg, str) and msg.strip():
            return msg
        return str(data)[:300]
    if isinstance(fallback, str) and fallback.strip():
        return fallback
    text = getattr(resp, 'text', '') if resp is not None else ''
    if isinstance(text, str) and text.strip():
        return text[:300]
    return '上游返回错误'


# ── 请求：Responses → Chat Completions ───────────────────────

def _text_of_parts(content: object) -> tuple[str, list[dict]]:
    """拆出 input 里的文本与图片，返回 (文本, OpenAI 内容块列表)。

    两种内容的走向不同：文本可以压平成字符串（上游对字符串最宽容），但只要
    出现图片就必须保留块数组结构，否则图片会丢。
    """
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return '', []
    texts: list[str] = []
    blocks: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = str(part.get('type') or '')
        if kind in ('input_text', 'output_text', 'text', 'summary_text'):
            text = part.get('text')
            if isinstance(text, str) and text:
                texts.append(text)
                blocks.append({'type': 'text', 'text': text})
        elif kind in ('input_image', 'image_url'):
            url = part.get('image_url') or part.get('url')
            # Responses 允许 image_url 是字符串或 {url: ...}
            if isinstance(url, dict):
                url = url.get('url')
            if isinstance(url, str) and url:
                blocks.append({'type': 'image_url', 'image_url': {'url': url}})
    return '\n'.join(texts), blocks


# ── 推理内容的「凭据」编码 ────────────────────────────────
#
# 为什么要它：DeepSeek 在思考模式下要求客户端把上一轮的推理内容原样带回
# （不带就报 11155 `reasoning_content_missing`）。两套协议里，客户端回传的推理
# 块按规范应当带凭据：
#
#   · Responses：`store:false` 时带 `encrypted_content` 的 reasoning 项
#     （OpenAI 官方用它做无状态加密推理）；
#   · Anthropic：带 `signature` 的 `thinking` 块。
#
# 没有凭据时**不同客户端行为不同**（这点被 issue #37 的抓包纠正过，别想当然）：
# 实测 Codex 在 `encrypted_content` 为 null 时**并不会丢弃**整项，它靠 `summary`
# 里的明文照样把推理带了回来。所以凭据不是「唯一的回传载体」，而是「规范要求的
# 载体」——给上它更稳（客户端换版本、或多轮里 summary 被截断时仍能还原完整原文）。
#
# 这段历史值得记住：v1.0.51 只修了输入侧，v1.0.53 补了凭据，两次都以为修好了
# 却不生效——因为断言写的都是我们自己输出的东西。教训是**断言要盯着需要的行为**，
# 而不是自己的输出形状。
#
# 面板不需要真加密：这两个字段在协议里的语义就是「客户端原样搬来搬去的不透明
# 串」，我们做**可逆编码**即可（base64 包装明文）。用可逆而不是真加密还有个
# 好处：客户端把它们回传回来时，我们能直接还原出推理原文去满足腾讯的校验。
_CRED_PREFIX = 'wbm1:'


def _encode_credential(text: str) -> str:
    """把推理原文编码成可回传的凭据串。"""
    return _CRED_PREFIX + base64.urlsafe_b64encode(text.encode('utf-8')).decode('ascii')


def _decode_credential(raw: object) -> str | None:
    """解回推理原文；不是本面板编出来的（前缀不符 / 解码失败）返回 None。

    不能对任意串盲解：客户端可能回传**真·OpenAI** 的加密串（我们从没生成过），
    那种解不开也不该报错，交给调用方回落其它来源。
    """
    if not isinstance(raw, str) or not raw.startswith(_CRED_PREFIX):
        return None
    try:
        return base64.urlsafe_b64decode(raw[len(_CRED_PREFIX):].encode('ascii')).decode('utf-8')
    except Exception:  # noqa: BLE001
        return None


def attach_reasoning(msg: dict, text: str) -> None:
    """把推理文本挂到 assistant 消息上，**两个字段名都写**（`reasoning` + `reasoning_content`）。

    来历（社区报告，issue #37）：报告称腾讯**请求侧**校验读的是 `reasoning`
    而不是 `reasoning_content`，并给出 8 组对照实验——同一份出站 body 原样
    503（`11155`），仅补 `reasoning` 就 200。

    **我自己没能复现那份矩阵**（2026-09-18，真实账号直连腾讯、7 种构造全部
    200，含报告里说必然 503 的基线；带 tools、带 thinking=enabled+high、
    带会话连续性头、9 消息 3 assistant 回合的形态都试过）。所以「腾讯校验
    `reasoning`」这一点**尚无本仓的实测支持**，保留两个字段是**对冲**而非确证：

      · 多写一个上游不认的字段无害（未知字段被忽略）——代价为零；
      · 万一报告描述的形态在别的模型/部署上成立，我们已经被覆盖。

    已知为真、可依赖的两点（读上游源码得到，与本仓实验一致）：
      · `reasoning_content` 确实是**响应侧**的字段名（腾讯 SSE 回放推理用它）；
      · 上游的兜底 `backfillReasoningContent` **读 `reasoning`** 并把它复制到
        `reasoning_content`，且见到 `reasoning_content` 已存在就跳过。

    最后一点值得留意：正因为上游见到 `reasoning_content` 就跳过，**我们挂了
    这个字段反而会让上游那道兜底不生效**。这也是「两个都写」更稳的另一个理由
    ——万一字段名真有讲究，我们不依赖上游兜底也能过。

    文本为空时仍然挂上（字段存在本身有意义），但要清楚**空串救不了场**：
    真正的解法是不把推理文本丢掉，那是本模块其余逻辑在守的事。
    """
    msg['reasoning'] = text
    msg['reasoning_content'] = text


def _reasoning_text(item: dict) -> str:
    """从 reasoning item 里取出推理文本，用于回填 assistant 消息的推理字段。

    来源按可靠性排序：
      · `encrypted_content` —— 我们发出去、客户端原样带回的凭据，**优先**用它：
        它是唯一能还原出**完整**推理原文的载体（`summary` 可能被客户端截断）；
      · `summary` —— Responses 规范形态，`[{type:'summary_text', text:…}]`；
      · `content` —— 部分客户端把正文放这里，同样是 parts 数组；
      · `reasoning_content` / `reasoning` —— 已经是扁平字符串的形态。

    取不到文本时返回空串（而不是 None）：调用方据此仍会挂上字段（见
    `attach_reasoning` 对空串的说明），返回 None 会让调用方跳过回填。
    """
    decoded = _decode_credential(item.get('encrypted_content'))
    if decoded:
        return decoded
    for key in ('summary', 'content'):
        text, _ = _text_of_parts(item.get(key))
        if text:
            return text
    for key in ('reasoning_content', 'reasoning'):
        raw = item.get(key)
        if isinstance(raw, str) and raw:
            return raw
    return ''


def _convert_tools(tools: object) -> list[dict] | None:
    """Responses 的扁平工具定义 → Chat Completions 的嵌套形状。

    两种形状都收：规范的 `{type:'function', name, parameters}`，以及部分客户端
    混用的嵌套 `{type:'function', function:{...}}`。宁可宽容也不要因为一层包装
    差异就让整次请求 400。
    """
    if not isinstance(tools, list):
        return None
    out: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        inner = tool.get('function') if isinstance(tool.get('function'), dict) else tool
        name = inner.get('name')
        if not isinstance(name, str) or not name:
            continue
        spec: dict = {
            'type': 'function',
            'function': {
                'name': name,
                'description': str(inner.get('description') or ''),
                'parameters': inner.get('parameters')
                or {'type': 'object', 'properties': {}},
            },
        }
        if tool.get('type') == 'custom':
            # Chat Completions has no freeform/custom tool shape. Keep the
            # upstream call executable by using one strict string wrapper; the
            # response side restores the original custom_tool_call contract.
            spec['function']['description'] += (
                '\nPass the complete raw text for this custom tool in the input string. '
                'Do not encode it as function arguments inside that string. '
                'Any custom grammar is not enforced by this Chat Completions bridge.'
            )
            spec['function']['parameters'] = {
                'type': 'object',
                'properties': {
                    'input': {
                        'type': 'string',
                        'description': 'Complete raw custom tool input.',
                    },
                },
                'required': ['input'],
                'additionalProperties': False,
            }
            fmt = tool.get('format')
            if (
                isinstance(fmt, dict)
                and fmt.get('type') == 'grammar'
                and isinstance(fmt.get('definition'), str)
            ):
                spec['function']['description'] += (
                    '\nRequested grammar (guidance only):\n' + fmt['definition']
                )
        out.append(spec)
    return out or None


def _convert_tool_choice(choice: object) -> object:
    if isinstance(choice, str):
        return choice
    if isinstance(choice, dict):
        name = choice.get('name')
        if not name and isinstance(choice.get('function'), dict):
            name = choice['function'].get('name')
        if name:
            return {'type': 'function', 'function': {'name': str(name)}}
    return None


def to_chat_request(body: dict, custom_tool_names: set[str] | None = None) -> dict:
    """Responses 请求体 → Chat Completions 请求体。"""
    custom_tool_names = custom_tool_names or _custom_tool_names(body)
    # `stream` 必须**转告上游**：上游靠这个字段决定是回 SSE 还是回一次性 JSON。
    # 漏掉它会得到一个"看起来很成功"的结果——网关按流式解析，上游却回了 JSON，
    # 于是一个 data 帧都解析不出来，客户端只收到 response.created + completed 的
    # 空回答（不报错、不重试）。本项含在 E2E 里专门守着。
    out: dict = {'model': body.get('model'), 'stream': bool(body.get('stream'))}

    messages: list[dict] = []

    # instructions 是 Responses 里承载 system 的字段（不在 input 里）
    instructions = body.get('instructions')
    if isinstance(instructions, str) and instructions.strip():
        messages.append({'role': 'system', 'content': instructions})
    elif isinstance(instructions, list):
        text, _ = _text_of_parts(instructions)
        if text:
            messages.append({'role': 'system', 'content': text})

    raw_input = body.get('input')
    if isinstance(raw_input, str):
        if raw_input.strip():
            messages.append({'role': 'user', 'content': raw_input})
    elif isinstance(raw_input, list):
        # 连续的 function_call 要合并进**同一条** assistant 消息：OpenAI 要求
        # 一次 assistant 回合里的多个工具调用同属一条消息，拆成多条会让上游
        # 在「上一个工具结果还没回」的校验上直接 400。
        pending_calls: list[dict] = []
        # 待挂到下一条 assistant 消息上的推理文本（见下面 `kind == 'reasoning'`）。
        # 用 None 表示「没见到 reasoning item」，空串表示「见到了但没文本」——
        # 两者必须区分：上游的触发条件是**字段存在**，空串一样能触发它的回填。
        #
        # **累积**而不是覆盖：客户端可能把一段推理拆成多个 reasoning item 下发，
        # 只留最后一个会把前文丢掉（自审发现的边界）。
        pending_reasoning: str | None = None

        def take_reasoning() -> str | None:
            """取走累积的推理文本并把状态清空。"""
            nonlocal pending_reasoning
            if pending_reasoning is None:
                return None
            value, pending_reasoning = pending_reasoning, None
            return value

        def flush_calls() -> None:
            if pending_calls:
                msg: dict = {
                    'role': 'assistant',
                    'content': None,
                    'tool_calls': list(pending_calls),
                }
                # 工具调用回合也可能带推理（模型先思考再调工具），一并保留，
                # 否则这类多轮同样会因缺痕迹被腾讯拒。
                pending = take_reasoning()
                if pending is not None:
                    attach_reasoning(msg, pending)
                messages.append(msg)
                pending_calls.clear()

        for item in raw_input:
            if not isinstance(item, dict):
                continue
            kind = str(item.get('type') or '')
            if kind in ('function_call', 'custom_tool_call'):
                if kind == 'custom_tool_call' and not isinstance(item.get('input'), str):
                    raise CustomToolArgumentsError(
                        'Custom tool history input must be a string.')
                name = str(item.get('name') or '')
                raw_arguments = (
                    json.dumps({'input': item.get('input')}, ensure_ascii=False)
                    if kind == 'custom_tool_call'
                    else item.get('arguments')
                )
                pending_calls.append({
                    'id': str(item.get('call_id') or item.get('id') or ''),
                    'type': 'function',
                    'function': {
                        'name': name,
                        # Responses 的 arguments 已经是字符串；对象则序列化
                        'arguments': raw_arguments
                        if isinstance(raw_arguments, str)
                        else json.dumps(raw_arguments or {}, ensure_ascii=False),
                    },
                })
                continue

            flush_calls()

            if kind in ('function_call_output', 'custom_tool_call_output'):
                # 与 Anthropic 层同一个坑（那个 PR #25 报了）：tool 消息的 content
                # 在 OpenAI 协议里只能是字符串，**放不下结构化图片**。
                # 只取文字会让工具输出的图片**静默丢失**（模型看不到图）；
                # 而把整段内容 repr 成字符串更糟 —— 图片 base64 会被当文本分词，
                # 一张几 MB 的图就能算出上百万 token，直接撑爆上下文。
                # 所以：文字留在 tool 消息里，图片提取出来并入紧随其后的 user 消息。
                output = item.get('output')
                out_text, out_images = _split_tool_output(output)
                messages.append({
                    'role': 'tool',
                    'tool_call_id': str(item.get('call_id') or ''),
                    'content': out_text,
                })
                # 图片必须紧跟其后（顺序即语义：它们属于这次工具调用）
                for chunk in _images_as_user_messages(out_images):
                    messages.append(chunk)
                continue
            if kind == 'reasoning':
                # reasoning item **不能丢**：它是 DeepSeek 多轮对话的一致性凭据。
                #
                # 背景（社区反馈的 11155 死循环）：腾讯要求「上一轮的推理内容必须在
                # 后续请求里回传」，缺了就是 400 `11155 reasoning_content_missing`
                # → 该账号被记为失败 → 连续失败触发降级冷却 → 池里没有可用账号 →
                # 客户端反复重试，最终变成与模型无关的 503 死循环。
                #
                # 所以这里把 reasoning item 的文本挂到 assistant 消息上，
                # **两个字段名都写**（`reasoning` + `reasoning_content`）——
                # 原因见 `attach_reasoning`：请求侧校验读的是 `reasoning`，
                # 只写 `reasoning_content` 等于没写（issue #37 的 8 组对照实验）。
                #
                # 为什么不是原样透传整个 item：`{type:'reasoning', summary:[…]}` 是
                # OpenAI 专有形状，Chat Completions 不认；只有扁平的字符串是
                # 两侧都认的形态。
                #
                # 挂载规则（**宽容**，不假设顺序）：累积起来，落到**下一条**
                # assistant 消息上；连续多段则**拼接**。
                #
                # 早先的写法是「只认紧随其后的 assistant，否则丢弃」，依据是「顺序上
                # reasoning 总在它对应的回复之前」。这个假设没有任何东西保证，而且
                # 一旦不成立，后果是这个文件专门在修的 11155 —— 客户端明明带回了
                # 凭据，我们却把内容丢在半路。宁可挂到一条不相关的 assistant 上
                # （腾讯只要求「assistant 消息上有这个字段」，多余内容无害），
                # 也不要丢掉。
                text = _reasoning_text(item)
                if text:
                    pending_reasoning = (
                        text if pending_reasoning is None else pending_reasoning + text
                    )
                else:
                    # 客户端发了 reasoning 项、但里面**没有可用文本**（空 summary /
                    # 空 content / 只有空的 encrypted_content）。这种畸形输入下我们
                    # 无从还原推理内容，于是**不挂字段**，并记一条 WARN。
                    #
                    # 早先这里挂空串，理由是「字段存在就能满足校验」。但社区实测
                    # （issue #37 的取值矩阵）表明**空串仍会被拒**、非空才过 ——
                    # 若该结论成立，挂空串让我们从「没痕迹」变成「有痕迹但内容为空」，
                    # 反而更糟；若该结论不成立（本仓复现不出，见该 issue 的讨论），
                    # 挂空串与不挂在结果上又没差别（上游兜底本就会补空串）。
                    # 两种情况都指向同一结论：**挂空串是无收益的风险**，故不挂。
                    #
                    # 记 WARN 是因为这条路径此前完全静默：真遇到 11155 时，
                    # 用户与我们都看不到「是哪个客户端发了空推理项」，只能猜。
                    logger.warning(
                        '客户端发来的 reasoning 项无可提取文本（type=%s keys=%s）——'
                        '该轮将不带推理内容，若上游报 11155 请把此日志一并提供',
                        item.get('type'), sorted(item.keys()),
                    )
                continue

            # message（或没写 type 的 {role, content} —— 宽容处理）
            role = str(item.get('role') or 'user')
            if role == 'developer':
                role = 'system'
            text, blocks = _text_of_parts(item.get('content'))
            if blocks and any(b.get('type') == 'image_url' for b in blocks):
                msg: dict | None = {'role': role, 'content': blocks}
            elif text:
                msg = {'role': role, 'content': text}
            else:
                msg = None
            if msg is not None:
                # 只在 assistant 消息上挂推理 —— 腾讯的校验针对 assistant 回合。
                # 客户端把 reasoning 放在别处（少见）时不硬塞，免得造出上游不认的组合。
                #
                # 不是 assistant 时**保留**待挂状态，留给后面那条 assistant：
                # 中间隔着 user 的异常顺序（客户端消息穿插）也要能送到，
                # 丢掉就又是一次 11155。
                if role == 'assistant':
                    pending = take_reasoning()
                    if pending is not None:
                        attach_reasoning(msg, pending)
                messages.append(msg)
        flush_calls()

        # 收尾：推理项排在**最后一条 assistant 之后**时，循环里没有「下一条
        # assistant」可以挂，它仍是待挂状态。回填到已发出的最后一条 assistant 上
        # ——它本来就是这一轮的推理（顺序异常不影响归属）。
        # 这也是自审发现的边界：原实现直接丢弃，客户端带回了凭据却仍触发 11155。
        if pending_reasoning is not None:
            for prev in reversed(messages):
                if prev.get('role') == 'assistant':
                    attach_reasoning(prev, take_reasoning())
                    break

    out['messages'] = messages

    if body.get('max_output_tokens') is not None:
        out['max_tokens'] = body['max_output_tokens']
    for key in ('temperature', 'top_p'):
        if body.get(key) is not None:
            out[key] = body[key]

    tools = _convert_tools(body.get('tools'))
    if tools:
        out['tools'] = tools
    choice = _convert_tool_choice(body.get('tool_choice'))
    if choice is not None:
        out['tool_choice'] = choice

    reasoning = body.get('reasoning')
    if isinstance(reasoning, dict) and isinstance(reasoning.get('effort'), str):
        out['reasoning_effort'] = reasoning['effort']

    # 只透传**上游认识**的字段。`store` / `include` / `prompt_cache_key` /
    # `reasoning` 这些是 OpenAI 专有的，透过去只会换来一个 400。
    # （`stream` 不在其列——它必须转告上游，见函数开头。）
    return out


def _tool_output_text(output: object) -> str:
    """function_call_output 的 output 可以是字符串，也可以是内容块数组。"""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        text, _ = _text_of_parts(output)
        return text
    if output is None:
        return ''
    return str(output)


def _split_tool_output(output: object) -> tuple[str, list[dict]]:
    """拆出工具输出的 (文字, 图片块)。

    与 Anthropic 层同口径（见 `to_chat_request` 里 tool_result 那段）：
    tool 消息的 content 只能是字符串，图片要提升为后续 user 消息的图像块，
    否则就会「静默丢图」（只取文字）或「撑爆上下文」（repr 整段含 base64）。
    """
    if isinstance(output, list):
        text, blocks = _text_of_parts(output)
        images = [b for b in blocks if b.get('type') == 'image_url']
        if not text and images:
            text = '[图片]'   # 上游要求 tool content 非空
        return text, images
    return _tool_output_text(output), []


def _images_as_user_messages(images: list[dict]) -> list[dict]:
    """把提升出来的图片包成 user 消息（放进 content 块数组）。"""
    if not images:
        return []
    return [{'role': 'user', 'content': images}]


# ── 响应：Chat Completions → Responses ───────────────────────

def _usage_object(usage: dict) -> dict:
    """usage 字段按 Responses 口径映射。

    `input_tokens` 在 OpenAI 语义里**已包含**命中缓存的 token，缓存数单独放在
    `input_tokens_details.cached_tokens`——客户端会把它从 input 里减掉再显示，
    所以不能在这里先减。
    """
    prompt = _as_int(usage.get('prompt_tokens'))
    completion = _as_int(usage.get('completion_tokens'))
    details = usage.get('prompt_tokens_details')
    cached = _as_int(details.get('cached_tokens')) if isinstance(details, dict) else 0
    out_details = usage.get('completion_tokens_details')
    reasoning = _as_int(out_details.get('reasoning_tokens')) if isinstance(out_details, dict) else 0
    return {
        'input_tokens': prompt,
        'input_tokens_details': {'cached_tokens': cached},
        'output_tokens': completion,
        'output_tokens_details': {'reasoning_tokens': reasoning},
        'total_tokens': prompt + completion,
    }


def _message_item(text: str, item_id: str) -> dict:
    return {
        'type': 'message',
        'id': item_id,
        'status': 'completed',
        'role': 'assistant',
        'content': [{'type': 'output_text', 'text': text, 'annotations': []}],
    }


def _function_call_item(call_id: str, item_id: str, name: str, arguments: str) -> dict:
    return {
        'type': 'function_call',
        'id': item_id,
        'call_id': call_id,
        'name': name,
        'arguments': arguments,
        'status': 'completed',
    }


def _custom_call_item(call_id: str, item_id: str, name: str, raw_input: str) -> dict:
    return {
        'type': 'custom_tool_call',
        'id': item_id,
        'call_id': call_id,
        'name': name,
        'input': raw_input,
    }


def _reasoning_item(text: str, item_id: str) -> dict:
    """推理输出项：`summary` 给人看，`encrypted_content` 供客户端回传。

    两个字段都要有，少任何一个都会出问题（见 `_encode_credential` 的说明）：
      · 只有 `summary` → 客户端在 `store:false` 下没有可回传的凭据，丢掉整项；
      · 只有 `encrypted_content` → 界面上看不到思考过程。
    """
    return {
        'type': 'reasoning',
        'id': item_id,
        'summary': [{'type': 'summary_text', 'text': text}],
        'encrypted_content': _encode_credential(text),
    }


def _normalize_tool_arguments(raw: object) -> str:
    """工具参数统一成字符串（Responses 里就是字符串）。

    非法 JSON **原样保留**：这是上游给的内容，改成 `{}` 会让客户端以为
    「模型决定不传参」，反而把问题藏起来。
    """
    if isinstance(raw, str):
        return raw
    if raw is None:
        return '{}'
    return json.dumps(raw, ensure_ascii=False)


def to_responses_object(
    data: dict,
    model: str,
    resp_id: str,
    custom_tool_names: set[str] | None = None,
) -> dict:
    """Chat Completions 非流式响应 → Responses 响应体。"""
    custom_tool_names = custom_tool_names or set()
    choice = (data.get('choices') or [{}])[0] if isinstance(data.get('choices'), list) else {}
    message = choice.get('message') or {}
    finish = str(choice.get('finish_reason') or 'stop')

    output: list[dict] = []
    # 推理项放在最前（与流式的顺序一致：思考在正文之前）。它**必须带上凭据**
    # `encrypted_content`，否则客户端在 `store:false` 下会把它整个丢掉，
    # 下一轮请求就不带推理痕迹 → DeepSeek 报 11155（issue #36）。
    reasoning = message.get('reasoning_content')
    if isinstance(reasoning, str) and reasoning:
        output.append(_reasoning_item(reasoning, 'rs_' + uuid.uuid4().hex[:20]))
    calls = [call for call in message.get('tool_calls') or [] if isinstance(call, dict)]
    custom_calls = [
        call for call in calls
        if isinstance(call.get('function'), dict)
        and call['function'].get('name') in custom_tool_names
    ]
    if custom_calls and finish not in ('stop', 'tool_calls', 'length'):
        raise CustomToolArgumentsError(
            'Custom tool response ended without a complete tool turn.')
    # A length-truncated custom call must never be exposed as executable output.
    suppress_tools = bool(custom_calls) and finish == 'length'
    for call in custom_calls:
        _custom_input((call.get('function') or {}).get('arguments'))
    for call in ([] if suppress_tools else calls):
        if not isinstance(call, dict):
            continue
        fn = call.get('function') or {}
        name = str(fn.get('name') or '')
        is_custom = name in custom_tool_names
        output.append(_custom_call_item(
            str(call.get('id') or f'call_{uuid.uuid4().hex[:12]}'),
            'ctc_' + uuid.uuid4().hex[:20],
            name,
            _custom_input(fn.get('arguments')),
        ) if is_custom else _function_call_item(
            str(call.get('id') or f'call_{uuid.uuid4().hex[:12]}'),
            'fc_' + uuid.uuid4().hex[:20],
            name,
            _normalize_tool_arguments(fn.get('arguments')),
        ))
    text = message.get('content')
    if isinstance(text, str) and text:
        output.append(_message_item(text, 'msg_' + uuid.uuid4().hex[:20]))

    status, incomplete = ('completed', None) if finish != 'length' else (
        'incomplete', {'reason': 'max_output_tokens'})
    return {
        'id': resp_id,
        'object': 'response',
        'created_at': int(time.time()),
        'status': status,
        'model': model,
        'output': output,
        'output_text': text if isinstance(text, str) else '',
        'parallel_tool_calls': True,
        'error': None,
        'incomplete_details': incomplete,
        'usage': _usage_object(data.get('usage') or {}),
    }


# ── 流式：Chat Completions SSE → Responses 事件流 ──────────────

class _StreamTranslator:
    """把无结构的 Chat Completions delta 重建成 Responses 的有结构事件流。

    需要状态机的原因：Responses 的流是**有边界**的——每个输出项要先
    `output_item.added`、增量若干次、再 `output_item.done`，且带递增的
    `output_index`；而上游只给一串无结构的 delta，边界只能由我们在内容类型
    切换或流结束时自己推断（这点与 Anthropic 翻译层同理）。

    推理内容（`reasoning_content`）单独成一个 reasoning 输出项：它与正文是
    两个 item，混在一起会让客户端的思考区与正文区错位。
    """

    def __init__(
        self,
        model: str,
        resp_id: str,
        custom_tool_names: set[str] | None = None,
    ) -> None:
        self.model = model
        self.resp_id = resp_id
        self.created_at = int(time.time())
        self.started = False
        self.finished = False
        self.next_index = 0
        self.items: list[dict] = []

        self.reason_index: int | None = None
        self.reason_id = ''
        self.reason_buf = ''

        self.text_index: int | None = None
        self.text_id = ''
        self.text_buf = ''

        # 上游工具序号 → 输出项（上游可能并发给多个工具，各自编号）
        self.tools: dict[int, dict] = {}
        self.tool_order: list[int] = []

        self.saw_content = False
        self.finish_reason: str | None = None
        self.usage: dict = {}
        self.custom_tool_names = custom_tool_names or set()
        self.failed = False

    # ── 事件构造 ──
    def _created(self) -> list[bytes]:
        self.started = True
        return [_event('response.created', {
            'type': 'response.created',
            'response': {
                'id': self.resp_id,
                'object': 'response',
                'created_at': self.created_at,
                'status': 'in_progress',
                'model': self.model,
                'output': [],
            },
        })]

    def _open_reason(self) -> list[bytes]:
        self.reason_index = self.next_index
        self.next_index += 1
        self.reason_id = 'rs_' + uuid.uuid4().hex[:20]
        return [_event('response.output_item.added', {
            'type': 'response.output_item.added',
            'output_index': self.reason_index,
            'item': {
                'type': 'reasoning',
                'id': self.reason_id,
                'summary': [],
            },
        })]

    def _close_reason(self) -> list[bytes]:
        if self.reason_index is None:
            return []
        index, self.reason_index = self.reason_index, None
        # 收尾时的 item 与 `output_item.added` 的那个**不是同一个形状**：
        # 凭据 `encrypted_content` 只能在这里补上——它要等推理全文到齐才能算出。
        # 客户端在 `output_item.done` 上取这个字段（issue #36：此前从没发过，
        # 于是客户端拿不到凭据，下一轮不带推理痕迹，上游报 11155）。
        item = _reasoning_item(self.reason_buf, self.reason_id)
        self.items.append(item)
        return [_event('response.output_item.done', {
            'type': 'response.output_item.done',
            'output_index': index,
            'item': item,
        })]

    def _open_text(self) -> list[bytes]:
        self.text_index = self.next_index
        self.next_index += 1
        self.text_id = 'msg_' + uuid.uuid4().hex[:20]
        index = self.text_index
        return [
            _event('response.output_item.added', {
                'type': 'response.output_item.added',
                'output_index': index,
                'item': {
                    'type': 'message',
                    'id': self.text_id,
                    'status': 'in_progress',
                    'role': 'assistant',
                    'content': [],
                },
            }),
            _event('response.content_part.added', {
                'type': 'response.content_part.added',
                'output_index': index,
                'content_index': 0,
                'item_id': self.text_id,
                'part': {'type': 'output_text', 'text': '', 'annotations': []},
            }),
        ]

    def _close_text(self) -> list[bytes]:
        if self.text_index is None:
            return []
        index, self.text_index = self.text_index, None
        part = {'type': 'output_text', 'text': self.text_buf, 'annotations': []}
        item = _message_item(self.text_buf, self.text_id)
        self.items.append(item)
        return [
            _event('response.output_text.done', {
                'type': 'response.output_text.done',
                'output_index': index,
                'content_index': 0,
                'item_id': self.text_id,
                'text': self.text_buf,
            }),
            _event('response.content_part.done', {
                'type': 'response.content_part.done',
                'output_index': index,
                'content_index': 0,
                'item_id': self.text_id,
                'part': part,
            }),
            _event('response.output_item.done', {
                'type': 'response.output_item.done',
                'output_index': index,
                'item': item,
            }),
        ]

    def _open_tool(self, seq: int, call_id: str, name: str) -> list[bytes]:
        index = self.next_index
        self.next_index += 1
        state = {
            'index': index,
            'id': 'fc_' + uuid.uuid4().hex[:20],
            'call_id': call_id,
            'name': name,
            'args': '',
        }
        self.tools[seq] = state
        self.tool_order.append(seq)
        # A custom tool's name may arrive in several upstream fragments.
        # Buffer tool events until the final name/arguments can be validated.
        if self.custom_tool_names:
            return []
        return [_event('response.output_item.added', {
            'type': 'response.output_item.added',
            'output_index': index,
            'item': {
                'type': 'function_call',
                'id': state['id'],
                'call_id': call_id,
                'name': name,
                # 必须给空串（不能省略）：客户端据此决定是否开始累积参数
                'arguments': '',
                'status': 'in_progress',
            },
        })]

    def _close_tools(self) -> list[bytes]:
        out: list[bytes] = []
        custom_inputs = {
            seq: _custom_input(state['args'])
            for seq, state in self.tools.items()
            if state['name'] in self.custom_tool_names
        }
        for seq in self.tool_order:
            state = self.tools.get(seq)
            if not state or state.get('closed'):
                continue
            state['closed'] = True
            custom = seq in custom_inputs
            value = custom_inputs[seq] if custom else state['args']
            field = 'input' if custom else 'arguments'
            family = 'custom_tool_call_input' if custom else 'function_call_arguments'
            item = (_custom_call_item if custom else _function_call_item)(
                state['call_id'], state['id'], state['name'], value)
            self.items.append(item)
            if self.custom_tool_names:
                out.append(_event('response.output_item.added', {
                    'type': 'response.output_item.added',
                    'output_index': state['index'],
                    'item': {**item, field: '', 'status': 'in_progress'},
                }))
                out.append(_event(f'response.{family}.delta', {
                    'type': f'response.{family}.delta',
                    'output_index': state['index'],
                    'item_id': state['id'],
                    'delta': value,
                }))
            out += [
                _event(f'response.{family}.done', {
                    'type': f'response.{family}.done',
                    'output_index': state['index'],
                    'item_id': state['id'],
                    field: value,
                }),
                _event('response.output_item.done', {
                    'type': 'response.output_item.done',
                    'output_index': state['index'],
                    'item': item,
                }),
            ]
        return out

    # ── 主循环 ──
    def feed(self, obj: dict) -> list[bytes]:
        """喂一个上游 SSE 的 data 对象，返回要下发的事件。"""
        out: list[bytes] = []
        # 收尾后不再吐事件：上游若在 finish_reason 之后继续发内容帧（它自己有
        # bug，或被打穿），客户端会收到终止事件之后的事件，协议被污染。
        if self.finished:
            return out
        if not self.started:
            out += self._created()

        usage = obj.get('usage')
        if isinstance(usage, dict):
            self.usage.update(usage)

        choices = obj.get('choices')
        if not isinstance(choices, list) or not choices:
            return out
        choice = choices[0] if isinstance(choices[0], dict) else {}
        delta = choice.get('delta') or choice.get('message') or {}
        if not isinstance(delta, dict):
            delta = {}

        reasoning = delta.get('reasoning_content')
        if isinstance(reasoning, str) and reasoning:
            if self.reason_index is None:
                out += self._close_text()
                out += self._open_reason()
            self.reason_buf += reasoning
            out.append(_event('response.reasoning_summary_text.delta', {
                'type': 'response.reasoning_summary_text.delta',
                'output_index': self.reason_index,
                'item_id': self.reason_id,
                'delta': reasoning,
            }))

        text = delta.get('content')
        if isinstance(text, str) and text:
            self.saw_content = True
            if self.reason_index is not None:
                out += self._close_reason()
            if self.text_index is None:
                out += self._open_text()
            self.text_buf += text
            out.append(_event('response.output_text.delta', {
                'type': 'response.output_text.delta',
                'output_index': self.text_index,
                'content_index': 0,
                'item_id': self.text_id,
                'delta': text,
            }))

        for call in delta.get('tool_calls') or []:
            if not isinstance(call, dict):
                continue
            seq = _as_int(call.get('index'))
            fn = call.get('function') or {}
            name = fn.get('name')
            if seq not in self.tools:
                if len(self.tools) >= MAX_OUTPUT_ITEMS:
                    logger.warning('工具调用数量超过 %d，忽略后续项', MAX_OUTPUT_ITEMS)
                    continue
                if self.text_index is not None:
                    out += self._close_text()
                if self.reason_index is not None:
                    out += self._close_reason()
                out += self._open_tool(
                    seq,
                    str(call.get('id') or f'call_{uuid.uuid4().hex[:12]}'),
                    str(name or ''),
                )
            state = self.tools[seq]
            if self.custom_tool_names:
                # The first fragment is already stored by _open_tool. Later
                # fragments may continue a split name; repeated full names
                # are ignored so they do not become "BashBash".
                if state.get('seen_fragment'):
                    if isinstance(name, str) and name != state['name']:
                        state['name'] += name
                    if isinstance(call.get('id'), str):
                        state['call_id'] = call['id']
                state['seen_fragment'] = True
            # 分片参数原样透传，拼接交给客户端
            args = fn.get('arguments')
            if isinstance(args, str) and args:
                state['args'] += args
                if not self.custom_tool_names:
                    out.append(_event('response.function_call_arguments.delta', {
                        'type': 'response.function_call_arguments.delta',
                        'output_index': state['index'],
                        'item_id': state['id'],
                        'delta': args,
                    }))

        finish = choice.get('finish_reason')
        if finish:
            out += self.finish(str(finish))
        return out

    def finish(self, finish_reason: str | None = None, *, force: bool = False) -> list[bytes]:
        """收尾：关掉打开的输出项并下发终止事件（幂等）。"""
        if self.finished:
            return []
        self.finished = True
        self.finish_reason = finish_reason
        out: list[bytes] = []
        if not self.started:
            out += self._created()

        out += self._close_text()
        out += self._close_reason()

        # 上游出现过工具调用却没给 finish_reason 时，仍按工具收尾更贴近实际：
        # 漏掉 arguments.done 会让客户端的参数累积停在半截。
        if force and finish_reason is None and self.tools:
            finish_reason = 'tool_calls'
        try:
            has_custom = any(
                state['name'] in self.custom_tool_names
                for state in self.tools.values()
            )
            if has_custom and finish_reason not in ('stop', 'tool_calls', 'length'):
                raise CustomToolArgumentsError(
                    'Custom tool response ended without a complete tool turn.')
            if self.custom_tool_names and any(
                not state['name'] for state in self.tools.values()
            ):
                raise CustomToolArgumentsError(
                    'Upstream tool call has no complete tool name.')
            if finish_reason == 'length' and has_custom:
                # A complete wrapper at a length stop is still incomplete and
                # must not be exposed as an executable custom call.
                for state in self.tools.values():
                    if state['name'] in self.custom_tool_names:
                        _custom_input(state['args'])
            else:
                out += self._close_tools()
        except CustomToolArgumentsError as exc:
            self.failed = True
            out.append(_event('response.failed', {
                'type': 'response.failed',
                'response': {
                    'id': self.resp_id,
                    'object': 'response',
                    'created_at': self.created_at,
                    'status': 'failed',
                    'model': self.model,
                    'output': self.items,
                    'error': {
                        'code': 'invalid_custom_tool_arguments',
                        'message': str(exc),
                    },
                    'usage': _usage_object(self.usage),
                },
            }))
            return out

        if finish_reason == 'length':
            status, event_name = 'incomplete', 'response.incomplete'
            incomplete_details: dict | None = {'reason': 'max_output_tokens'}
        else:
            status, event_name = 'completed', 'response.completed'
            incomplete_details = None

        out.append(_event(event_name, {
            'type': event_name,
            'response': {
                'id': self.resp_id,
                'object': 'response',
                'created_at': self.created_at,
                'status': status,
                'model': self.model,
                'output': self.items,
                'incomplete_details': incomplete_details,
                'error': None,
                'usage': _usage_object(self.usage),
            },
        }))
        return out


# SSE 解析缓冲上限（与 Anthropic 层同口径）：上游若持续吐不含换行的数据，
# 缓冲会一直长下去直至吃光内存。
MAX_SSE_BUFFER = 1 << 20


async def _handle(request: Request) -> JSONResponse | StreamingResponse:
    """两个注册路径共用（`/v1/responses` 与 `/responses`）。"""
    body, err = await gateway._read_json_body(request)
    if err:
        return err

    model = body.get('model')
    if not isinstance(model, str) or not model.strip():
        return _failed('model 必须是字符串', 400, 'invalid_request_error', 'invalid_model')

    # 鉴权：与 gateway._authorize 同一套检查、同一顺序（含版本隔离与配额）
    key, ip, auth_err = gateway._authorize(request, model)
    if auth_err:
        return auth_err

    ua = request.headers.get('user-agent')
    stream = bool(body.get('stream'))
    custom_tool_names = _custom_tool_names(body)

    try:
        payload = to_chat_request(body, custom_tool_names)
    except Exception as exc:  # noqa: BLE001
        gateway._record(key, ip, model, '', 400, 0, 0, 0, ua, str(exc), False)
        return _failed(f'请求转换失败：{exc}', 400)

    if not payload.get('messages'):
        return _failed('input 为空：Responses 请求必须带 input 或 instructions',
                       400, 'invalid_request_error', 'empty_input')

    mapped = gateway._map_model(model)
    if mapped:
        payload['model'] = mapped
    if stream:
        payload.setdefault('stream_options', {})
        if isinstance(payload['stream_options'], dict):
            payload['stream_options'].setdefault('include_usage', True)

    resp_id = 'resp_' + uuid.uuid4().hex[:24]
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
                _as_int(usage.get('prompt_tokens')), _as_int(usage.get('completion_tokens')),
                latency, ua, None if resp.status_code < 400 else str(data)[:500], False,
                credit=gateway._usage_credit(usage),
            )
            if resp.status_code >= 400:
                return _failed(_upstream_error_text(data, resp), resp.status_code,
                               hint=gateway._error_hint(data))
            if not isinstance(data, dict):
                # 200 但响应体不是 JSON：不能当作「成功但空回答」返回——那正是
                # 最难排查的一种表现（客户端不重试、不报错）。原样把上游的响应
                # 内容作为错误暴露出来，让问题可见。
                return _failed('上游返回了无法解析的响应：' + resp.text[:300],
                               502, 'api_error', 'upstream_invalid_body')
            return JSONResponse(
                to_responses_object(data, model, resp_id, custom_tool_names)
            )
        except CustomToolArgumentsError as exc:
            return _failed(
                str(exc), 502, 'api_error', 'invalid_custom_tool_arguments')
        except Exception as exc:  # noqa: BLE001
            latency = int((time.time() - started) * 1000)
            gateway._record(key, ip, model, mapped or '', 502, 0, 0, latency, ua, str(exc), False)
            return _failed(f'上游不可用：{exc}', 502)

    # ── 流式 ──
    client = config.http_client(config.UPSTREAM_TIMEOUT, connect=5)
    try:
        req = client.build_request('POST', url, json=payload, headers=gateway._upstream_headers())
        resp = await client.send(req, stream=True)
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        latency = int((time.time() - started) * 1000)
        gateway._record(key, ip, model, mapped or '', 502, 0, 0, latency, ua, str(exc), True)
        return _failed(f'上游不可用：{exc}', 502)

    # 上游直接报错：**开流之前**用真实状态码回掉。
    # 若开始流式（HTTP 200 已定），状态码就没法再改了——错误只能裹进事件里，
    # 而客户端会把「流正常结束」当成成功，错误就显得像「空回答」。
    if resp.status_code >= 400:
        raw = b''
        try:
            async with resp:
                raw = await resp.aread()
        except Exception:  # noqa: BLE001
            raw = b''
        finally:
            await client.aclose()
        text = raw.decode('utf-8', errors='replace')[:500]
        parsed = None
        try:
            parsed = json.loads(text)
        except ValueError:
            pass
        latency = int((time.time() - started) * 1000)
        gateway._record(key, ip, model, mapped or '', resp.status_code, 0, 0, latency,
                        ua, text, True)
        return _failed(_upstream_error_text(parsed, None, fallback=text),
                       resp.status_code, hint=gateway._error_hint(parsed))

    async def gen():
        pending = ''
        translator = _StreamTranslator(
            model, resp_id, custom_tool_names=custom_tool_names)
        error_text: str | None = None
        first_token_ms: int | None = None

        try:
            async for chunk in resp.aiter_bytes():
                pending += chunk.decode('utf-8', errors='ignore')

                if len(pending) > MAX_SSE_BUFFER:
                    logger.warning('SSE 缓冲超过 %d 字节仍未见换行，中止转发', MAX_SSE_BUFFER)
                    error_text = '上游响应异常：数据流缺少分隔'
                    break

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

                    # 上游可能**中途**回 error 帧（形如 {"error":{...}}，无 choices）
                    err_obj = obj.get('error')
                    if isinstance(err_obj, dict) and not obj.get('choices'):
                        msg = err_obj.get('message')
                        error_text = str(msg if msg else err_obj)[:500]
                        # 带上上游的 gateway_hint（可执行建议）。收尾时以
                        # response.failed 的 error.message 发给客户端，那里只有
                        # 一个 message 字段位，丢掉它就等于建议消失。
                        mid_hint = gateway._error_hint(obj)
                        if mid_hint:
                            error_text = f'{error_text}（{mid_hint}）'[:500]
                        break

                    for event in translator.feed(obj):
                        yield event

                    if translator.saw_content and first_token_ms is None:
                        first_token_ms = int((time.time() - started) * 1000)

                if error_text:
                    break

            if error_text:
                # 收尾事件已发出就不能再报错了（客户端已按成功处理），所以
                # 先发终止事件之外的失败事件、**不**走 finish()
                yield _event('response.failed', {
                    'type': 'response.failed',
                    'response': {
                        'id': resp_id,
                        'object': 'response',
                        'created_at': translator.created_at,
                        'status': 'failed',
                        'model': model,
                        'output': translator.items,
                        'error': {'code': 'upstream_error', 'message': error_text},
                    },
                })
            else:
                for event in translator.finish(None, force=True):
                    yield event
        finally:
            await resp.aclose()
            await client.aclose()
            latency = int((time.time() - started) * 1000)
            gateway._record(
                key, ip, model, mapped or '', 200, 0, 0, latency, ua, error_text, True,
                credit=gateway._usage_credit(translator.usage), first_token=first_token_ms,
            )

    return StreamingResponse(gen(), status_code=200, media_type='text/event-stream')


@router.post('/v1/responses')
async def responses_v1(request: Request):
    """OpenAI Responses API（SDK 的 baseURL 带 `/v1` 时走这里）。"""
    return await _handle(request)


@router.post('/responses')
async def responses_root(request: Request):
    """OpenAI SDK 的 `responses.create` 是 `{baseURL}/responses`：
    baseURL 只填到域名（不含 `/v1`）时走的是这条路径。"""
    return await _handle(request)
