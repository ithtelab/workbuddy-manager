"""内置聊天测试台：用登录会话直接试调上游，无需先发一把 API 密钥。

为什么单独做一条通道、而不是让页面直接打 `/v1/chat/completions`：
网关那条路要求 `Bearer wbk_...`，而测试台的使用者还没建密钥（正是想先试一下）。
这里用管理端登录态鉴权，服务端注入上游 api_key 转发，因此**不经过**密钥/IP/配额
校验——它只用于调试，不是对外入口。

消耗的积分会随响应一起返回（上游末帧 `usage.credit`），界面在右下角实时显示。
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import config, security, upstreamsvc
from ..iputil import client_ip
from . import gateway

router = APIRouter(prefix='/api/playground', tags=['playground'])


class ChatIn(BaseModel):
    """测试台请求。messages 直接透传给上游，不做内容校验。"""
    model: str = Field(min_length=1, max_length=128)
    messages: list[dict] = Field(min_length=1, max_length=200)
    # 推理档位：off/minimal/low/medium/high/xhigh/max；空串表示不指定
    reasoning_effort: str = Field(default='', max_length=16)
    stream: bool = True
    temperature: float | None = None
    max_tokens: int | None = None
    # 版本：cn / global。**必须显式指定**——上游的裸模型名默认走国内版，
    # 测试台若只在界面上切了版本、请求却发裸名，会选到国内版账号池，
    # 出现「切到国际版却调用国内模型」的错配。这里据此给模型名加前缀。
    realm: str = Field(default='cn', max_length=16)


def _qualified_model(model: str, realm: str) -> str:
    """给模型名加版本前缀，让上游显式选域。

    上游请求侧语法是 `[realm:]model`（小写），前缀不传上游 body。
    已经有前缀的（用户手输）不重复添加；国内版不加前缀以兼容裸名默认行为。
    """
    m = (model or '').strip()
    if m.lower().startswith(('cn:', 'global:')):
        return m
    if realm == 'global':
        return f'global:{m}'
    return m


@router.post('/chat')
async def chat(body: ChatIn, request: Request, user: dict = Depends(security.require_admin)):
    """转发一次对话到上游。

    管理员限定：测试台会**真实消耗积分**，而默认部署里存在一个弱口令的 guest
    账号，放开给所有登录用户等于把额度暴露出去。
    """
    # 测试台没有下游密钥（管理员会话直连），固定走默认上游 —— 与从前一致。
    # 走 upstreamsvc 而不是直接读 config：默认上游的取值只有一处事实来源。
    upstream = upstreamsvc.default_upstream()
    ip = client_ip(request)
    started = time.time()
    realm = 'global' if str(body.realm).strip().lower() == 'global' else 'cn'
    model_sent = _qualified_model(body.model, realm)
    payload: dict = {
        'model': model_sent,
        'messages': body.messages,
        'stream': body.stream,
    }
    if body.reasoning_effort:
        payload['reasoning_effort'] = body.reasoning_effort
    if body.temperature is not None:
        payload['temperature'] = body.temperature
    if body.max_tokens is not None:
        payload['max_tokens'] = body.max_tokens
    if body.stream:
        # 让上游在末帧返回 usage（含 credit），界面据此显示本次消耗
        payload['stream_options'] = {'include_usage': True}

    headers = gateway._upstream_headers(upstream)
    url = f'{upstream["base_url"]}/v1/chat/completions'

    client = config.http_client(config.UPSTREAM_TIMEOUT, connect=5)
    try:
        req = client.build_request('POST', url, json=payload, headers=headers)
        resp = await client.send(req, stream=True)
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        gateway._record(
            None, ip, model_sent, '', 502, 0, 0,
            int((time.time() - started) * 1000),
            request.headers.get('user-agent'), str(exc), body.stream,
        )
        raise HTTPException(status_code=502, detail=f'上游不可用：{exc}') from exc

    if not body.stream:
        try:
            raw = await resp.aread()
        finally:
            await resp.aclose()
            await client.aclose()
        # 非流式同样要记账（否则测试台的这部分开销查不到）
        usage: dict = {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and isinstance(parsed.get('usage'), dict):
                usage = parsed['usage']
        except Exception:  # noqa: BLE001
            pass
        # 整份 usage 交过去（而不是只取 credit）：扣费与提示词缓存三段都在这一份里
        gateway._record(
            None, ip, model_sent, '', resp.status_code,
            int(usage.get('prompt_tokens') or 0),
            int(usage.get('completion_tokens') or 0),
            int((time.time() - started) * 1000),
            request.headers.get('user-agent'), None, False,
            usage=usage,
        )
        return StreamingResponse(iter([raw]), media_type='application/json',
                                 status_code=resp.status_code)

    async def gen():
        """转发 SSE，并在收尾时记录日志与用量。

        测试台会**真实消耗积分**，若不记账，用户在「请求日志」与「用量」里
        查不到这部分开销，账目就对不上。这里复用网关的 `_record`：
        key_id 传 None（测试台没有对外密钥），日志页会显示成「—」，
        因此仍可区分「测试台调用」与「某个密钥的调用」。
        """
        usage: dict = {}
        pending = ''
        error_text: str | None = None
        first_token_ms: int | None = None
        try:
            async for chunk in resp.aiter_bytes():
                if resp.status_code >= 400:
                    pending += chunk.decode('utf-8', errors='ignore')
                    if len(pending) > 4000:
                        error_text = pending[:500]
                    yield chunk
                    continue
                pending += chunk.decode('utf-8', errors='ignore')
                pending, saw_content = gateway._scan_sse(pending, usage)
                if saw_content and first_token_ms is None:
                    first_token_ms = int((time.time() - started) * 1000)
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()
            gateway._record(
                None, ip, model_sent, '', resp.status_code,
                int(usage.get('prompt_tokens') or 0),
                int(usage.get('completion_tokens') or 0),
                int((time.time() - started) * 1000),
                request.headers.get('user-agent'), error_text, True,
                usage=usage,
                first_token=first_token_ms,
            )

    return StreamingResponse(gen(), status_code=resp.status_code,
                             media_type=resp.headers.get('content-type', 'text/event-stream'))


@router.get('/models')
async def playground_models(
    realm: str = 'cn',
    user: dict = Depends(security.current_user),
) -> dict:
    """测试台的模型下拉：复用模型目录，带上可用的推理档位。

    与「模型中心」同源，因此界面上的档位与实际模型能力一致，不会出现
    「选了一个该模型不支持的档位、被上游静默降级」的困惑。
    按版本取：国际版的模型与国内版几乎不重叠。
    """
    from ..services import modelcatalog

    r = 'global' if str(realm).strip().lower() == 'global' else 'cn'
    data = await modelcatalog.catalog(r)
    return {
        'models': [
            {'id': m['id'], 'name': m.get('name') or m['id'],
             'efforts': m.get('efforts') or [], 'series': m.get('series') or ''}
            for m in (data.get('models') or [])
        ],
        'source': data.get('source'),
        'source_label': data.get('source_label'),
        'realm': r,
    }
