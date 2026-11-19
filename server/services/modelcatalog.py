"""模型目录：给「模型中心」页提供带细节的模型清单。

数据来源分两级，UI 会如实标注当前用的是哪一级：

  1. **腾讯模型接口**（首选）：用本地账号直连腾讯，能拿到显示名（`name`）、
     真实上下文（maxInputTokens）、最大输出、以及**推理档位**
     （`reasoning.supportedEfforts`）。这些字段上游的 `/v1/models` 会丢掉。
  2. **上游 /v1/models**（回退）：只给 id / 上下文 / 最大输出。腾讯接口调不通
     （账号全过期、网络问题）时用这一级，保证页面不空白。

为什么需要缓存：每次进页面都打腾讯接口既慢又容易被风控。这里缓存 5 分钟，
并在失败时做短暂的负缓存，避免连续失败时反复重试。

安全：不返回任何凭据；只暴露模型元数据。
"""
from __future__ import annotations

import asyncio
import json
import time

from .. import config
from .errtext import err_text
from . import tencent, wb2api, native_modality

# 成功缓存 5 分钟：模型清单变化很慢，没必要每次进页面都打腾讯
_TTL_OK = 300
# 失败负缓存 60 秒：避免连续失败时反复重试（尤其账号全过期时）
_TTL_FAIL = 60

# 缓存**按版本分键**：国内版与国际版的模型清单完全不同，若共用一份，
# 后拉取的那个会把前一个覆盖掉（两个版本的模型无法共存）。
_cache: dict[str, dict] = {}


def _slot(realm: str) -> dict:
    return _cache.setdefault(realm, {'at': 0.0, 'ttl': 0, 'payload': None})


# 系列归属：腾讯接口不返回，这里按模型 id 的前缀做**命名约定推导**。
# 仅为便于分组浏览，UI 上会标明是推导值；认不出的一律归入「其他」，
# 不做猜测性归类。
_SERIES_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (('glm',), '智谱 GLM'),
    (('deepseek',), 'DeepSeek'),
    (('kimi', 'moonshot'), 'Kimi'),
    (('minimax',), 'MiniMax'),
    (('hy', 'hunyuan'), '腾讯混元'),
    (('auto',), '自动选择'),
)


def series_of(model_id: str) -> str:
    """按 id 前缀推导系列名。认不出返回「其他」。"""
    mid = (model_id or '').lower()
    for prefixes, label in _SERIES_RULES:
        if mid.startswith(prefixes):
            return label
    return '其他'


def _pick_account() -> dict | None:
    """挑一个最可能可用的本地账号（优先剩余有效期长的、未过期的）。

    不做网络探测：那会把简单列表请求变成慢操作。选到不可用的账号时，
    调用方会再尝试下一个，最多试 `_MAX_TRIES` 个。
    """
    try:
        accounts = wb2api.list_auth_accounts()
    except Exception:  # noqa: BLE001
        return None
    alive = [a for a in accounts if not a.get('is_expired') and a.get('remain_seconds', 0) > 0]
    alive.sort(key=lambda a: a.get('remain_seconds', 0), reverse=True)
    return alive[0] if alive else None


def _load_token(filename: str) -> str:
    """读取指定 auth 文件的 accessToken。只在服务端内部使用，不外传。"""
    try:
        raw = wb2api.read_account_file(filename)
    except Exception:  # noqa: BLE001
        return ''
    return str((raw.get('auth') or {}).get('accessToken') or '')


# ── 推理档位静态兜底表（按版本分表，镜像上游 effort_catalog.go）──
#
# 来源：上游 2026-09-15 PR #92 引入的产品级兜底表（其注释注明照抄官方参考仓库
# 的 CODEBUDDY_FALLBACK_MODELS / WORKBUDDY_FALLBACK_MODELS）。腾讯接口对部分
# 模型不返回 supportedEfforts，而官方客户端确实支持多档强度——只依赖远端会让
# 模型中心显示「不支持推理」，与真实能力不符（用户报的 issue #8）。
#
# **两个版本的表绝不混用**：同一模型在两个版本下的档位可能不同
# （deepseek-v4.1-flash：国内版 low/high/max，国际版 high/max）。
# 这张表是「远端没给时的兜底」，远端给了就以远端为准（见 _decorate）。
#
# _EFFORT_EXTEND 是**产品能力已更新、模型接口元数据还没跟上**时的补档：
# 2026-10 issue #238，国际版官方客户端已能选 Max，但模型接口仍只回 high。
# 只在远端/兜底已有档位时追加，不会给未知模型凭空造档位。
_EFFORT_FALLBACK: dict[str, dict[str, dict]] = {
    'cn': {
        'deepseek-v4-flash': {'efforts': ['low', 'high', 'max']},
        'deepseek-v4.1-flash': {'efforts': ['low', 'high', 'max'], 'default': 'high'},
        'deepseek-v4-pro': {'efforts': ['low', 'high', 'xhigh'], 'default': 'high'},
        'hy4-preview': {'efforts': ['high'], 'default': 'high'},
        'hy4-preview-x': {'efforts': ['high']},
        'hy3': {'efforts': ['low', 'high'], 'default': 'high'},
        'hy3-x': {'efforts': ['low', 'high'], 'default': 'high'},
        'glm-5.3': {'efforts': ['low', 'high', 'max'], 'default': 'high'},
        'glm-5.3-flash': {'efforts': ['low', 'high', 'max'], 'default': 'high'},
        'glm-5.2': {'efforts': ['high', 'xhigh'], 'default': 'high'},
        'glm-5.1': {'efforts': ['medium']},
        'glm-5v-turbo': {'efforts': ['medium']},
        'kimi-k3-1': {'efforts': ['medium']},
        'kimi-k2.7': {'efforts': ['medium']},
        'kimi-k2.6': {'efforts': ['medium']},
        'minimax-m3': {'efforts': ['medium']},
    },
    'global': {
        'fast-model': {'efforts': ['medium']},
        'balanced-model': {'efforts': ['medium']},
        'primary-model': {'efforts': ['high']},
        'hy4-preview-f': {'efforts': ['high'], 'default': 'high'},
        'hy3': {'efforts': ['low', 'high'], 'default': 'high'},
        'deepseek-v4.1-flash': {'efforts': ['high', 'max'], 'default': 'high'},
        'gpt-6-astra': {'efforts': ['low', 'medium', 'high', 'xhigh', 'max'], 'default': 'high'},
        'gpt-5.6-sol': {'efforts': ['low', 'medium', 'high', 'xhigh', 'max'], 'default': 'high'},
        'gpt-5.6-terra': {'efforts': ['low', 'medium', 'high', 'xhigh', 'max'], 'default': 'high'},
        'gpt-5.6-luna': {'efforts': ['low', 'medium', 'high', 'xhigh', 'max'], 'default': 'high'},
        'gpt-5.5': {'efforts': ['low', 'medium', 'high', 'xhigh'], 'default': 'high'},
        'gpt-5.4': {'efforts': ['low', 'medium', 'high', 'xhigh'], 'default': 'high'},
        'gpt-5.3-codex': {'efforts': ['medium']},
        'gemini-3.5-flash': {'efforts': ['medium']},
        'glm-5.3': {'efforts': ['low', 'high', 'max'], 'default': 'high'},
        'glm-5.2': {'efforts': ['high', 'xhigh'], 'default': 'high'},
        'kimi-k3': {'efforts': ['medium']},
        'kimi-k2.6': {'efforts': ['medium']},
    },
}

_EFFORT_EXTEND: dict[str, dict[str, list[str]]] = {
    # issue #238：官方客户端已可选 Max，接口 supportedEfforts 仍为 ['high']。
    'global': {'deepseek-v4.1-flash': ['max']},
}


def _decorate(items: list[dict], realm: str = 'cn') -> list[dict]:
    """规范化模型条目，并按三级规则补齐推理档位。

    三级（镜像上游 `EffortListing`，2026-09-15 PR #92 引入）：
      1. 远端返回了档位 → 用它（权威）；
      2. 远端没有 → 用**产品级静态兜底表**（按 realm 分表，见 _EFFORT_FALLBACK）；
      3. 两者皆无 → 空数组（界面显示「—」，不编造）。

    为什么必须有第 2 级：腾讯接口对部分模型（如 deepseek-v4.1-flash）不返回
    supportedEfforts，而官方客户端确实支持多档强度。只依赖远端会让模型中心
    显示「不支持推理」，与真实能力不符（用户报的 issue #8 即此）。

    兜底表**按版本分表且不混用**：同一模型在两个版本下的档位可能不同
    （deepseek-v4.1-flash：国内版三档、国际版 high/max），混用会把国际版
    显示成支持国内版的档位。

    远端元数据滞后于产品客户端时，`_EFFORT_EXTEND` 在已有档位上补差
    （issue #238）；只在 efforts 非空时追加，避免给未知模型造档位。
    """
    out = []
    for m in items:
        mid = str(m.get('id') or '')
        if not mid:
            continue
        remote = [str(x) for x in (m.get('efforts') or []) if x]
        default = str(m.get('default_effort') or '').strip()
        if remote:
            efforts, eff_default = remote, default
        else:
            cap = _EFFORT_FALLBACK.get(realm, {}).get(mid)
            efforts = list(cap['efforts']) if cap else []
            eff_default = str(cap.get('default') or '') if cap else ''
        if efforts:
            for extra in _EFFORT_EXTEND.get(realm, {}).get(mid, ()):
                if extra not in efforts:
                    efforts.append(extra)
        # 默认档必须在支持列表内才有效（镜像上游的 containsEffort 校验）
        if eff_default and eff_default not in efforts:
            eff_default = ''
        out.append({
            'id': mid,
            **native_modality.describe(mid),
            'name': m.get('name') or '',
            'context_length': int(m.get('context_length') or 0),
            'max_output_tokens': int(m.get('max_output_tokens') or 0),
            'efforts': efforts,
            'series': series_of(mid),
            # 默认推理档位；空 = 未声明（由上游自行回退到硬编码默认）
            'default_effort': eff_default,
            # 平台图片输入声明保留 true/false/unknown 与官方来源冲突。
            'supports_images': m.get('supports_images') if type(m.get('supports_images')) is bool else None,
            'image_input_conflict': m.get('image_input_conflict') is True,
            'image_input_sources': dict(m.get('image_input_sources') or {}),
            # ── 上游 2026-09-15 补齐的目录字段 ──
            # 模型描述（腾讯的 descriptionZh，中文）
            'description': str(m.get('description') or ''),
            # 积分倍率（如 "x0.05"）：同一 prompt 在不同模型上的扣费倍率，
            # 用户据此挑更省的模型。仅展示，不参与选号（与上游口径一致）。
            'credits': str(m.get('credits') or ''),
            'vendor': str(m.get('vendor') or ''),
            'tags': list(m.get('tags') or []),
            'is_default': bool(m.get('is_default')),
            'supports_reasoning': bool(m.get('supports_reasoning')),
            'supports_tool_call': bool(m.get('supports_tool_call')),
            'only_reasoning': bool(m.get('only_reasoning')),
            'reasoning_summary': str(m.get('reasoning_summary') or ''),
        })
    return out


async def catalog(realm: str = 'cn', force: bool = False) -> dict:
    """返回**指定版本**的模型清单与统计。结果带 source / fetched_at 等元信息。

    realm 区分国内版与国际版：两边的模型几乎不重叠（国际版以 gpt-* 等为主），
    必须分开取、分开缓存，否则会互相覆盖。
    """
    slot = _slot(realm)
    now = time.time()
    if not force and slot['payload'] is not None and now - slot['at'] < slot['ttl']:
        out = dict(slot['payload'])
        out['cached'] = True
        out['cache_age'] = int(now - slot['at'])
        return out

    result = await _build(realm, force=force)
    slot.update({
        'at': now,
        'ttl': _TTL_OK if result.get('source') == 'tencent' else _TTL_FAIL,
        'payload': result,
    })
    return dict(result, cached=False, cache_age=0)


async def _build(realm: str, force: bool = False) -> dict:
    """依次尝试：腾讯接口（多个账号）→ 上游 /v1/models。

    候选账号**只取该版本的**——否则可能拿国际版账号去问国内版端点
    （或反过来），既取不到模型也平白多一次失败。
    """
    errors: list[str] = []
    # 最多试 3 个账号：单个账号可能恰好凭证失效，但没必要把所有账号都试一遍
    try:
        candidates = [
            a for a in wb2api.list_auth_accounts()
            if a.get('realm') == realm
            and not a.get('is_expired') and a.get('remain_seconds', 0) > 0
        ]
        candidates.sort(key=lambda a: a.get('remain_seconds', 0), reverse=True)
    except Exception as exc:  # noqa: BLE001
        candidates = []
        errors.append(f'读取本地账号失败: {err_text(exc)}')
    if not candidates:
        errors.append(f'没有可用的{"国际版" if realm == "global" else "国内版"}账号')

    for acct in candidates[:3]:
        token = _load_token(str(acct.get('file') or ''))
        if not token:
            continue
        ok, data = await tencent.fetch_models({'access_token': token, 'realm': realm,
                                               'domain': acct.get('domain', ''),
                                               'proxy': acct.get('proxy', '')})
        if ok and isinstance(data, list) and data:
            return {
                'models': _decorate(data, realm),
                'source': 'tencent',
                'source_label': '腾讯模型接口（含显示名与推理档位）',
                'via': acct.get('nickname') or acct.get('uid') or '',
                'errors': errors,
            }
        errors.append(f'{acct.get("nickname") or acct.get("uid")}: {data}')

    # 回退：上游 /v1/models（字段少，但至少保证页面有内容）。
    # 注意上游的清单里带 `cn:` / `global:` 前缀，必须按版本挑出属于本版本的那些，
    # 否则两个版本的模型会混在一页里。
    ok, data = await wb2api.get_models()
    if ok:
        items = data if isinstance(data, list) else (
            data.get('data') if isinstance(data, dict) else None
        )
        if isinstance(items, list) and items:
            picked = [_strip_realm_prefix(m) for m in items if _belongs(m, realm)]
            picked = [_map_upstream_model_fields(m) for m in picked if m.get('id')]
            if picked:
                return {
                    'models': _decorate(picked, realm),
                    'source': 'upstream',
                    'source_label': '上游 /v1/models（无显示名；推理档位取上游透出值）',
                    'via': 'workbuddy2api',
                    'errors': errors,
                }
            errors.append(f'上游清单里没有{"国际版" if realm == "global" else "国内版"}条目')
    else:
        errors.append(f'上游 /v1/models 失败: {data}')

    return {
        'models': [],
        'source': 'none',
        'source_label': '暂无可用的模型数据来源',
        'via': '',
        'errors': errors,
    }


# 上游 /v1/models 的字段名 → 我们内部统一的字段名。
# 上游 2026-09-15 起大幅补齐了这些字段（PR 见其 commit 318182a/31e3b45/b67f061），
# 此前 /v1/models 只有 id/context_length/max_output_tokens，所以我们才要直连腾讯。
_UPSTREAM_FIELD_MAP = {
    'reasoning_supported_efforts': 'efforts',
    'reasoning_default_effort': 'default_effort',
    'reasoning_summary': 'reasoning_summary',
}


def _map_upstream_model_fields(m: dict) -> dict:
    """把上游 `/v1/models` 的字段名映射成我们内部统一的形状。

    两处来源不同、名字不同，必须在入口处收敛，否则 _decorate 要认两套命名：
      * 推理档位：上游叫 `reasoning_supported_efforts`（它的命名），
        腾讯接口叫 `reasoning.supportedEfforts`（我们直连时解析出来的）；
      * 其余字段（name/description/credits/tags/vendor/能力标志）**两边同名**，
        直接透传即可。

    历史上这里只映射了档位字段——那时上游 /v1/models 里没有别的字段可映射。
    """
    out = dict(m)
    for src, dst in _UPSTREAM_FIELD_MAP.items():
        if src in m:
            if dst == 'efforts':
                out[dst] = [str(x) for x in (m.get(src) or []) if x]
            else:
                out[dst] = str(m.get(src) or '')
    if 'supports_images' in m:
        out['supports_images'] = m.get('supports_images') if type(m.get('supports_images')) is bool else None
        out['image_input_sources'] = {'upstream_models': out['supports_images']}
    return out


def _strip_realm_prefix(m: dict) -> dict:
    """去掉上游模型名的 `cn:` / `global:` 前缀，返回新 dict。

    上游 `/v1/models` 输出的名字带版本前缀（网关侧约定），界面展示时不需要，
    而且同一个裸名可能在两个版本各有一份；保留前缀会导致显示成 `cn:glm-5.2`。
    """
    mid = str(m.get('id') or '')
    for pref in ('cn:', 'global:'):
        if mid.lower().startswith(pref):
            mid = mid[len(pref):]
            break
    return {**m, 'id': mid}


def _belongs(m: dict, realm: str) -> bool:
    """上游清单里的这一条是否属于指定版本。

    带前缀的按前缀判定；不带前缀的（老版本上游）默认算国内版，
    与国际版的「裸名默认 CN」口径一致。
    """
    mid = str(m.get('id') or '').lower()
    if mid.startswith('global:'):
        return realm == 'global'
    if mid.startswith('cn:'):
        return realm == 'cn'
    return realm == 'cn'


def invalidate(realm: str | None = None) -> None:
    """清空缓存（手动刷新、账号变动后调用）。不传 realm 则全清。"""
    if realm is None:
        _cache.clear()
        return
    _cache.pop(realm, None)


def cached_ids(realm: str) -> set[str] | None:
    """**缓存里**的模型 id 集合；没有可用缓存时返回 None。

    只读缓存、**不发网络请求**：调用方是「保存密钥」这类路径，不能因为要校验
    模型名就等一次上游往返（上游慢或挂着时，保存会被拖住甚至超时）。
    拿不到就返回 None，让调用方**跳过校验**——宁可这次不提示，也不要给假警报。

    缓存里是 `_decorate` 之后的条目，id 形态与 `/v1/models` 一致（`global:`
    前缀保留、`cn:` 已由上游侧归一）。判据的归一化交给 `keysvc._bare_model`，
    这里只负责取值。
    """
    slot = _cache.get(realm)
    payload = (slot or {}).get('payload')
    models = (payload or {}).get('models') if isinstance(payload, dict) else None
    if not models:
        return None
    return {str(m.get('id') or '') for m in models if isinstance(m, dict) and m.get('id')}


def fetch_ids_blocking(realm: str) -> dict:
    """**同步**拉一次该版本的模型目录，返回 `catalog()` 的结果（给同步路由用）。

    为什么要这个同步桥：本应用的路由多数写成 `def`，跑在线程池里——这是刻意的，
    为的是让 sqlite、子进程、写盘这些阻塞动作不去占用事件循环。而 `catalog()`
    是 async，所以这里在线程内自建一个临时事件循环跑一次。

    为什么自建循环是安全的（不是碰运气）：
      * 上游的 HTTP 客户端全部是「每次调用新建 + `async with` 用完即弃」
        （见 `config.http_client`），**不绑定任何事件循环**，所以临时循环既不会
        与主循环抢资源，也不会复用跨循环的连接池；
      * 线程池里的工作线程本身没有运行中的循环，`asyncio.run` 不会撞上
        「已有事件循环」的限制；
      * `asyncio.run` 的信号处理只在主线程生效，工作线程里是空操作。

    若当前线程**已经**在跑事件循环（正常不会发生：同步路由在线程池线程里），
    就不敢自建第二个循环，直接返回一个"没拉到"的结果，让调用方按"取不到"处理
    ——宁可少一个模型名，也不要在这里把请求卡死。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return {'models': [], 'source': 'none', 'source_label': '', 'via': '',
                'errors': ['当前上下文已有事件循环，未实时拉取模型清单']}
    return asyncio.run(catalog(realm))


def summarize(models: list[dict]) -> dict:
    """统计卡数据。全部由清单真实计算，不含推测项。

    容忍未经过 `_decorate` 的原始条目（缺 series 时现场推导），
    这样调用方不必先确保装饰过，统计口径也不会因入口不同而漂移。
    """
    ids = [m.get('id', '') for m in models]
    reasoning = [m for m in models if m.get('efforts')]
    # 「大上下文」按 128K 起算（常见分档线），仅作浏览辅助
    large = [m for m in models if (m.get('context_length') or 0) >= 131072]
    max_ctx = max((m.get('context_length') or 0 for m in models), default=0)
    series = sorted({m.get('series') or series_of(str(m.get('id') or '')) for m in models})
    return {
        'total': len(models),
        'reasoning': len(reasoning),
        'large_context': len(large),
        'max_context': max_ctx,
        'series': series,
        'unique_ids': len(set(ids)),
    }
