"""密钥导出：把一把面板密钥生成为 cc-switch / ZCode 的配置片段。

**为什么只能在创建时导出**：面板只存密钥哈希（`keysvc.create_key` 里
`out['key'] = token` 是唯一一次明文），列表行里的 `prefix` 拼不出完整密钥。
因此本模块的入口只接受**调用方当场传进来的明文**，自己从不读库取密钥——
拿不到明文时函数无法凭空造出配置，这一点由签名强制。

**纯函数、无副作用**：只做「参数 → 配置 dict/文本」的转换，不写文件、
不碰客户端数据、不发网络。落盘/导入是后续步骤的事（见 docs 提案 §4），
这样导出功能可以先以最小风险合入。

**模型 id 用网关口径**（带 `cn:` / `global:` 前缀）：面板「模型中心」显示的是
腾讯返回的裸名（`glm-5.2`），而上游 `/v1/models` 与客户端实际调用用的是
带前缀的 id（`cn:glm-5.2`）。两者混用会让客户端选中模型后 404，所以这里
统一产出**带前缀**的 id，并在白名单为空时要求调用方给出完整清单。

**两种 base url 口径，不能混用**（同一个面板地址，三类客户端要的值不同）：

| 客户端 | 需要的值 | 原因 |
|---|---|---|
| cc-switch / claude | `http://host`（根，不带 `/v1`） | Anthropic SDK 自己拼 `/v1/messages` |
| cc-switch / codex  | `http://host/v1` | Codex 在 base url 后拼 `/responses` |
| ZCode（openai-responses） | `http://host/v1` | 同上 |

给 claude 填 `…/v1` 是最容易犯的错：实际请求变成 `…/v1/v1/messages`，
而面板上那个路径**存在但不是 POST 路由**，于是返回 **405** 而不是 404——
报错看起来像"方法不对"，很容易把人引到错误方向去查。见 `anthropic_base_url`。
"""
from __future__ import annotations

# 两个版本的前缀：与上游 /v1/models 的 id 约定一致（见 server/routers/gateway.py
# 与 keysvc._bare_model 的说明——`cn:` 是路由约定，`global:` 决定版本）。
REALM_PREFIX = {'cn': 'cn:', 'global': 'global:'}

# 支持的客户端。cc-switch 还要再分应用（claude / codex），ZCode 只有一个。
CLIENTS = ('ccswitch', 'zcode')
CCSWITCH_APPS = ('claude', 'codex')


def gateway_model_id(model: str, realm: str = 'cn') -> str:
    """把模型名补成**网关口径**（带版本前缀）。已带前缀的原样返回。

    两种前缀都要认：`cn:` 与 `global:` 都是合法输入（用户可能从上游清单里
    复制了带前缀的名字），但**不按 realm 改写已有前缀**——`global:` 决定
    路由，把一个 global 模型强行标成 cn 会变成另一个模型。
    """
    name = str(model or '').strip()
    if not name:
        raise ValueError('模型名不能为空')
    if name.startswith('cn:') or name.startswith('global:'):
        return name
    prefix = REALM_PREFIX.get(str(realm or 'cn').strip().lower(), REALM_PREFIX['cn'])
    return prefix + name


def gateway_base_url(public_base: str) -> str:
    """面板对外地址 → OpenAI 兼容 base url（**带 `/v1`**）。

    用于 codex / ZCode 这类 `openai-responses` 客户端：它们把 `/responses`
    拼在 base url 后面，所以这里要含 `/v1`（`…/v1` + `/responses` = 面板的
    `/v1/responses`）。
    """
    base = str(public_base or '').strip().rstrip('/')
    if not base:
        raise ValueError('面板对外地址不能为空')
    # 已经带 /v1 的不再重复追加（用户可能直接填了完整地址）
    return base if base.endswith('/v1') else base + '/v1'


def anthropic_base_url(openai_base_url: str) -> str:
    """OpenAI 兼容 base url → **Anthropic** 口径的 base url（**不带 `/v1`**）。

    Claude Code 走 Anthropic SDK，sdk 自己会在 base url 后面拼 `/v1/messages`，
    所以 `ANTHROPIC_BASE_URL` 必须是**根地址**：填成 `http://host/v1` 的话
    实际请求会变成 `http://host/v1/v1/messages`，面板上根本没有这个路由
    （实测 405，不是 404——更容易被误判成"方法不对"而查错方向）。

    真实的 cc-switch 配置也是这个口径：填的是**根地址 + 尾斜杠**
    （不是 `/v1`）。尾斜杠保留是因为 SDK 直接做字符串拼接，
    不补斜杠会得到 `http://hostv1/messages`。
    """
    base = str(openai_base_url or '').strip().rstrip('/')
    if not base:
        raise ValueError('面板对外地址不能为空')
    if base.endswith('/v1'):
        base = base[:-len('/v1')].rstrip('/')
    return base + '/'


def _pick_models(models: list[str] | None, realm: str, fallback: str | None) -> list[str]:
    """规范化模型清单；空清单时至少给一个默认模型（否则配置不可用）。

    这里是**最后一道**兜底：正常路径上，调用方（`routers/keys.py` 的
    `_export_models`）已经保证了清单非空，并在取不到时说清该去做什么。
    真落到这句错误，说明调用方既没拿到清单、也没给默认模型——文案仍要可照做，
    不要只丢一句"清单为空"让人去猜。
    """
    out = [gateway_model_id(m, realm) for m in (models or []) if str(m or '').strip()]
    if not out:
        if not fallback:
            raise ValueError('没有可用的模型：请为该密钥填写模型白名单后重试')
        out = [gateway_model_id(fallback, realm)]
    # 去重但保留顺序（上游清单本身有先后含义，客户端按 modelOrder 展示）
    seen: set[str] = set()
    return [m for m in out if not (m in seen or seen.add(m))]


def first_model(models: list[str] | None, realm: str,
                default_model: str | None = None) -> str:
    """这份配置实际会用的主模型（网关口径）。

    抽出来是因为「一键导入」走深链时要把同一个模型名塞进 URL 参数，而真正
    决定它的是 `_pick_models`——两处各推一遍迟早会漂移，所以只留一个出口。
    """
    return _pick_models(models, realm, default_model)[0]


def to_ccswitch(*, token: str, base_url: str, app: str, name: str,
                models: list[str] | None = None, realm: str = 'cn',
                default_model: str | None = None) -> dict:
    """生成 cc-switch 的 `providers.settings_config`（字典，调用方再 json.dumps）。

    结构取自 cc-switch 库中真实数据（2026-09-24 实测）：
      · claude：`{"env": {"ANTHROPIC_AUTH_TOKEN": ..., "ANTHROPIC_BASE_URL": ...}}`
      · codex ：`{"auth": {"OPENAI_API_KEY": ...}, "config": "<TOML 文本>"}`

    codex 的 base_url / model 写在 TOML 文本里而不是独立字段——这是 cc-switch
    自带 provider 的实际形态，照抄以免客户端读不到。
    """
    if app not in CCSWITCH_APPS:
        raise ValueError(f'app 必须是 {CCSWITCH_APPS} 之一，收到 {app!r}')
    if not str(token or '').strip():
        raise ValueError('密钥不能为空')
    if not str(name or '').strip():
        raise ValueError('供应商名不能为空')

    picked = _pick_models(models, realm, default_model)
    chosen = picked[0]

    if app == 'claude':
        return {'env': {
            'ANTHROPIC_AUTH_TOKEN': token,
            # 根地址而非 /v1：SDK 自己拼 /v1/messages（见 anthropic_base_url）
            'ANTHROPIC_BASE_URL': anthropic_base_url(base_url),
            'ANTHROPIC_MODEL': chosen,
            # 三个档位都指向同一模型：面板按模型名路由，没有 haiku/sonnet/opus
            # 的概念；留空会让 cc-switch 回退到官方模型名而打到错误的端点。
            'ANTHROPIC_DEFAULT_HAIKU_MODEL': chosen,
            'ANTHROPIC_DEFAULT_SONNET_MODEL': chosen,
            'ANTHROPIC_DEFAULT_OPUS_MODEL': chosen,
        }}

    # codex：TOML 文本。转义双引号以免模型名里出现引号时破坏 TOML。
    def _q(value: str) -> str:
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'

    lines = [
        'model_provider = "workbuddy"',
        f'model = {_q(chosen)}',
        '',
        '[model_providers]',
        '[model_providers.workbuddy]',
        f'name = {_q(name)}',
        # 上游是 OpenAI 兼容的 Responses 形态（config.api.type 一致）
        'wire_api = "responses"',
        'requires_openai_auth = true',
        f'base_url = {_q(base_url)}',
        '',
    ]
    return {'auth': {'OPENAI_API_KEY': token}, 'config': '\n'.join(lines)}


def to_zcode(*, token: str, base_url: str, name: str, provider_id: str,
             models: list[str] | None = None, realm: str = 'cn',
             default_model: str | None = None,
             context_windows: dict[str, int] | None = None) -> dict:
    """生成 ZCode 的供应商配置片段（可直接并入 provider_config.json）。

    返回**片段**而非整份文件：整份文件里还有用户自己的其它供应商与顺序，
    导出端不该替用户决定 providerOrder 的全量内容。调用方（或未来的一键导入）
    负责合并，合并键是 `providerId`。
    """
    if not str(provider_id or '').strip():
        raise ValueError('providerId 不能为空')
    if not str(token or '').strip():
        raise ValueError('密钥不能为空')
    if not str(name or '').strip():
        raise ValueError('供应商名不能为空')

    picked = _pick_models(models, realm, default_model)
    ctx = context_windows or {}
    rules = []
    for model_id in picked:
        props: dict[str, int] = {}
        window = ctx.get(model_id)
        if window:      # 拿不到上下文长度就不写，让客户端用自己的默认值
            props['contextWindow'] = int(window)
        rules.append({'modelId': model_id, 'providerId': provider_id,
                      'config': {'properties': props}})

    return {
        'providerRule': {
            'providerId': provider_id,
            'providerName': name,
            'config': {
                'group': 'standard-personal',
                'access': {'type': 'api-key', 'apiKey': token},
                # 与 CC-Switch 的 codex 一致：上游是 Responses 兼容形态
                'api': {'type': 'openai-responses', 'baseUrl': base_url},
                'personalModelIds': list(picked),
                'modelOrder': list(picked),
            },
        },
        'providerModelRules': rules,
    }
