"""API 密钥管理接口。"""
from __future__ import annotations

import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import config, db, keysvc, security, upstreamsvc
from ..services import keyexport, keyimport, modelcatalog, modelmap
from ..iputil import client_ip

router = APIRouter(prefix='/api/keys', tags=['keys'])

# 校验失败时给用户的说明。放在写入**之前**拦，而不是存下去再说 ——
# 存坏数据的后果是「该密钥永久不可用」：`ip_matches` 对非法 CIDR 一律返回
# False（fail-closed，方向是对的），于是白名单里只要有一个写错的 CIDR，
# 这把密钥对**所有**来源 IP 都拒绝，而报错只说「不在白名单内」，
# 用户完全看不出是自己把 CIDR 写错了。宁可在这里拒掉并说清怎么写。
_CIDR_HINT = ('IP 白名单里有无法识别的条目：{bad}。\n'
              '  请写成单个 IP（1.2.3.4）或 CIDR（10.0.0.0/8）的形态。\n'
              '  留着它会让这把密钥**拒绝所有来源**（因为匹配不上任何 IP）。')


def _check_ip_allowlist(items: list[str] | None) -> None:
    """逐项校验 IP 白名单，非法即 400（附上该怎么写）。"""
    for raw in items or []:
        s = str(raw).strip()
        if not s:
            continue      # 空项由 keysvc 过滤掉，不算错
        try:
            ipaddress.ip_network(s, strict=False)
        except ValueError:
            raise HTTPException(status_code=400,
                                detail=_CIDR_HINT.format(bad=s)) from None


def _check_upstream(upstream_id: object) -> None:
    """校验密钥要绑定的上游存在（未绑定 / 默认上游 = None 或 0，直接放行）。

    为什么在这里拦而不是等转发时：绑一个不存在的 id，症状是**每一条请求都 503**
    （见 upstreamsvc.resolve_for_key 的「不静默回落」），而管理员在界面上看到的是
    「密钥建好了」。建的时候就报 400，比事后对着 503 排查便宜得多。

    默认上游（id=None）不是数据库里的一行，所以显式放过——它是合法取值。
    """
    if upstream_id in (None, '', 0, '0'):
        return
    if upstreamsvc.get_upstream(upstream_id) is None:
        raise HTTPException(status_code=400,
                            detail=f'选定的上游不存在（id={upstream_id}），它可能已被删除')


def _check_name(name: str | None) -> None:
    """名称不能只有空白。

    否则列表里会出现一行「没有名字」的密钥，管理员认不出它是干什么的、
    也不知道是自己误操作建的（实测可以建出一把 name='   ' 的密钥）。
    """
    if name is not None and not str(name).strip():
        raise HTTPException(status_code=400, detail='密钥名称不能只有空格')


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    # 绑定的上游（多上游 / 分组隔离）：null / 0 / 省略 = 默认上游。
    # 类型是 int 而不是 str：界面上是下拉选 id，存成整数才能在外键式查询里用。
    upstream_id: int | None = None
    expires_at: int | None = None
    max_ips: int = 0
    ip_allowlist: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    quota: int = 0
    # 积分额度（issue #27）：0 = 不限。与 token 额度各自独立，任一超限即拒绝。
    # 用 float：上游 credit 是小数（如 0.05 表示按倍率扣费）。
    quota_credit: float = 0
    # 版本归属：'' = 不限制（存量密钥的形态）。非 cn/global 的值由
    # keysvc._norm_realm 归一化成 ''——不报错，免得旧前端（不带该字段）被拒。
    realm: str = Field(default='', max_length=16)


class KeyPatch(BaseModel):
    name: str | None = None
    # 显式传 null 是**允许**的：把密钥改回默认上游（与 realm 同口径）。
    upstream_id: int | None = None
    enabled: bool | None = None
    expires_at: int | None = None
    max_ips: int | None = None
    ip_allowlist: list[str] | None = None
    models: list[str] | None = None
    quota: int | None = None
    quota_credit: float | None = None
    realm: str | None = None


class WhitelistCheckIn(BaseModel):
    models: list[str] = Field(default_factory=list)
    realm: str = Field(default='', max_length=16)


@router.post('/check-models')
def check_models(body: WhitelistCheckIn,
                 user: dict = Depends(security.current_user)) -> dict:
    """检查模型白名单里哪些名字**匹配不到已知模型**（issue #46 的可选做法 2）。

    为什么要有个接口而不是前端自己比对：判据必须与调用侧**同一份**
    （`keysvc._bare_model` 的去 `cn:` 前缀、保留 `global:` 口径）。前端再实现
    一遍就是第二份事实来源，迟早漂移——而"两处口径不一致"正是 issue #46 的成因。

    **拿不到清单时不猜**（两种粒度）：

      · 两份清单都拿不到 → `checked: false`，前端如实显示「暂时无法校验」；
      · 只有一份拿得到 → **只判那一版的条目**，另一版的条目原样放过。因为两个版本
        的清单是分开取的，拿国内版清单去判 `global:xxx` 必然判成"找不到"——那是
        假警报，用户会去改一个本来正确的名字（比不提示更糟）。

    校验刻意**只读缓存、不发网络**：它挂在输入框失焦上，不该让一次上游慢响应把
    交互拖住；而且校验失败是可接受的（下次再看），打上游失败反而更糟。
    """
    names = [str(x).strip() for x in body.models if str(x).strip()]
    if not names:
        return {'checked': True, 'unknown': []}

    # 两个版本的清单**分别取、分别判**（见 docstring 里的假警报说明）
    known_by_realm: dict[str, set[str] | None] = {
        r: modelcatalog.cached_ids(r) for r in ('cn', 'global')
    }
    # 第三来源：qoder bridge 的模型清单（只读缓存，不发网络——与上面的口径
    # 一致，清单由「设置 → Qoder」页顺手暖）。qoder 模型名无前缀，落不进
    # 任何一个 realm 桶（C5），必须作为独立集合传下去，否则会被腾讯清单
    # 判成 unknown 假警报。未暖缓存时是 None → 判定式退化原行为。
    from ..services import qoder as qoder_svc
    extra = qoder_svc.cached_model_ids()
    if all(v is None for v in known_by_realm.values()) and extra is None:
        return {'checked': False, 'unknown': [],
                'reason': '暂时读不到模型清单（去「模型」页刷新一次再回来）'}

    # 只认真实别名：兜底那条（`*`）不是模型名，不该被当成"用户写的白名单项是对的"。
    aliases = modelmap.aliases(db.get_setting('model_map', {}) or {})
    unknown = keysvc.unknown_whitelist_entries(names, known_by_realm, aliases,
                                               extra_known=extra)
    return {'checked': True, 'unknown': unknown}


@router.get('')
def list_keys(user: dict = Depends(security.current_user)) -> list[dict]:
    return keysvc.list_keys()


@router.post('')
def create_key(body: KeyIn, request: Request,
               user: dict = Depends(security.require_admin)) -> dict:
    _check_name(body.name)
    _check_ip_allowlist(body.ip_allowlist)
    _check_upstream(body.upstream_id)
    created = keysvc.create_key(
        name=body.name,
        expires_at=body.expires_at,
        max_ips=body.max_ips,
        ip_allowlist=body.ip_allowlist,
        models=body.models,
        quota=body.quota,
        realm=body.realm,
        quota_credit=body.quota_credit,
        upstream_id=body.upstream_id,
    )
    # 密钥是拿额度用的凭证，发放必须留痕（含来源 IP）
    upstream_name = '默认上游'
    if created.get('upstream_id'):
        row = upstreamsvc.get_upstream(created['upstream_id'])
        upstream_name = row['name'] if row else f"#{created['upstream_id']}（已删除）"
    security.audit(user, 'create_key', str(created.get('name') or ''),
                   f"id={created.get('id')}；上游={upstream_name}；来源 {client_ip(request)}")
    return created


@router.patch('/{key_id}')
def update_key(key_id: int, body: KeyPatch, user: dict = Depends(security.require_admin)) -> dict:
    # 只校验本次真的提交了的字段（PATCH 是部分更新，`exclude_unset` 语义）
    patch = body.model_dump(exclude_unset=True)
    if 'name' in patch:
        _check_name(patch['name'])
    if 'ip_allowlist' in patch:
        _check_ip_allowlist(patch['ip_allowlist'])
    if 'upstream_id' in patch:
        _check_upstream(patch['upstream_id'])
    updated = keysvc.update_key(key_id, patch)
    if not updated:
        raise HTTPException(status_code=404, detail='密钥不存在')
    return updated


@router.post('/{key_id}/reset-usage')
def reset_usage(key_id: int, user: dict = Depends(security.require_admin)) -> dict:
    # 不存在的 id 应报 404，而不是静默成功：否则前端会提示「已重置」，
    # 而实际什么都没发生（密钥可能已被别人删掉，页面上却看着还在）。
    if not keysvc.reset_usage(key_id):
        raise HTTPException(status_code=404, detail='密钥不存在')
    return {'ok': True}


@router.delete('/{key_id}')
def delete_key(key_id: int, request: Request,
               user: dict = Depends(security.require_admin)) -> dict:
    if not keysvc.delete_key(key_id):
        raise HTTPException(status_code=404, detail='密钥不存在')
    security.audit(user, 'delete_key', str(key_id), f'来源 {client_ip(request)}')
    return {'ok': True}


class ExportIn(BaseModel):
    """导出请求。`token` 必须是**创建时返回的明文**（见 /export 的说明）。"""
    client: str = Field(max_length=16)
    token: str = Field(min_length=1)
    # cc-switch 还要指定应用；ZCode 忽略该字段
    app: str = Field(default='', max_length=16)
    # 面板对外地址（可选）。留空则按本次请求推导。
    base_url: str = Field(default='', max_length=256)
    provider_name: str = Field(default='', max_length=64)
    # 模型清单（可选）。留空则按密钥白名单 / 目录缓存 / 实时拉取推导，见 _export_models
    models: list[str] = Field(default_factory=list)
    default_model: str = Field(default='', max_length=128)


def _derive_base_url(request: Request, override: str) -> str:
    """面板对外地址：优先显式传入，否则按请求推导。

    推导用 `request.base_url`（Starlette 会看 X-Forwarded-* 头），
    子路径部署时再补上 `config.BASE_PATH`——否则生成的 baseUrl 会缺前缀，
    客户端调用全部 404。
    """
    from .. import config as _config
    if override.strip():
        return override.strip().rstrip('/')
    base = str(request.base_url).rstrip('/')
    if _config.BASE_PATH and not base.endswith(_config.BASE_PATH):
        base += _config.BASE_PATH
    return base


def _empty_models_detail(realm: str, result: dict | None) -> str:
    """模型清单为空时给**可照做**的说明（区分两种成因，见 `_export_models`）。

    为什么要分开：用户要做的事完全不同——「没有账号」得先去「账号」页加账号
    （该版本下一个模型都调不了，光填白名单绕不过去），「拉取失败」则只需稍后
    重试或手填白名单。合成一句「模型清单为空」，用户只能靠猜，且容易误以为是
    密钥出了问题（实测就有人因此把密钥删掉重建，白折腾一遍）。
    """
    label = '国际版' if realm == 'global' else '国内版'
    errors = [str(e) for e in ((result or {}).get('errors') or [])]
    if any('没有可用的' in e for e in errors):
        return (f'面板当前没有可用的{label}账号，无法得知该版本有哪些模型。'
                f'请先到「账号」页添加一个{label}账号，'
                f'或在这把密钥里填写模型白名单后重试。')
    return (f'暂时取不到{label}模型清单（已尝试实时拉取）。'
            f'请稍后重试，或在这把密钥里填写模型白名单。')


def _export_models(body: ExportIn, realm: str) -> list[str]:
    """决定这份配置里写哪些模型。

    优先级（都在**网关口径**下产出，keyexport 负责补前缀）：

      1. 调用方显式传入的 `models`（前端可带密钥白名单，最准）；
      2. 目录缓存里该版本的清单（`cached_ids` 只读缓存，不发网络）；
      3. 缓存**为空**时实时拉一次；仍为空则报 409 并说清该做什么。

    第 3 步为什么要发网络：模型清单缓存是**进程内内存**，面板一重启就空了，而
    只有打开「模型中心」页才会去拉。于是「重启 → 建密钥 → 点导出」这条最普通的
    路径必然失败，且报出的原因与密钥毫无关系、用户无法自查（实测：面板重启后
    导出连续 400「模型清单为空且未指定默认模型」，用户反复重建密钥也没用）。
    导出是**一次性交互**，点一次多等一两秒，比直接报错好得多；最坏是腾讯接口
    超时（`WB_TENCENT_TIMEOUT`，默认 15 秒），期间前端已有等待态。

    **不要**把这个"取不到就拉一次"挪到「保存密钥时校验模型名」那条路
    （见 `check_models`）：它挂在输入框失焦上，一次上游慢响应就会把交互拖住，
    那边必须保持只读缓存。
    """
    if body.models:
        return list(body.models)
    cached = modelcatalog.cached_ids(realm)
    if cached:
        return sorted(cached)
    result = modelcatalog.fetch_ids_blocking(realm)
    cached = modelcatalog.cached_ids(realm)
    if cached:
        return sorted(cached)
    # 409 而不是 400：这不是"请求写错了"，而是"当前状态取不到模型"——
    # 去加个账号或稍后重试就能成功，可重试性正是 409 该表达的（同 _REFUSAL_STATUS）。
    raise HTTPException(status_code=409,
                        detail=_empty_models_detail(realm, result))


def _normalise_client(raw: str) -> str:
    client = str(raw or '').strip().lower()
    if client not in keyexport.CLIENTS:
        raise HTTPException(status_code=400,
                            detail=f'client 必须是 {keyexport.CLIENTS} 之一')
    return client


def _normalise_app(client: str, raw: str) -> str:
    """cc-switch 必须指明写到哪个应用；ZCode 只有一个身份，忽略该字段。"""
    if client != 'ccswitch':
        return ''
    app = str(raw or '').strip().lower()
    if app not in keyexport.CCSWITCH_APPS:
        raise HTTPException(
            status_code=400,
            detail=f'cc-switch 必须指定 app（{keyexport.CCSWITCH_APPS}）')
    return app


def _config_context(body: ExportIn, request: Request) -> dict:
    """导出与导入共用的参数解析：版本、面板地址、供应商名、模型清单。

    两条路共用同一份，是为了让「导出的能用、导入的不能用」这种漂移没有机会
    出现——客户端配置的字段口径（尤其 base url 带不带 `/v1`，见 keyexport
    的模块说明）在复制粘贴中最容易走样，只留一份就不存在走样。
    """
    resolved = keysvc.resolve(body.token)
    realm = str((resolved or {}).get('realm') or '').strip() or 'cn'
    if realm not in ('cn', 'global'):
        realm = 'cn'
    return {
        'resolved': resolved,
        'realm': realm,
        # 这里给的是 **OpenAI 口径**（带 /v1）。claude 要的根地址由
        # keyexport.anthropic_base_url 在生成时去掉，别在这里分流。
        'base_url': keyexport.gateway_base_url(_derive_base_url(request, body.base_url)),
        'name': body.provider_name.strip()
                or f"WorkBuddy {((resolved or {}).get('name') or 'key')}",
        'models': _export_models(body, realm),
        'default_model': body.default_model.strip() or None,
        'prefix': str((resolved or {}).get('prefix') or 'manual'),
    }


def _client_settings(*, client: str, app: str, token: str, ctx: dict,
                     provider_id: str) -> dict:
    """产出该客户端的配置（cc-switch 的 settings_config / ZCode 的片段）。"""
    if client == 'ccswitch':
        return keyexport.to_ccswitch(
            token=token, base_url=ctx['base_url'], app=app, name=ctx['name'],
            models=ctx['models'], realm=ctx['realm'],
            default_model=ctx['default_model'])
    return keyexport.to_zcode(
        token=token, base_url=ctx['base_url'], name=ctx['name'],
        # 用密钥前缀做 providerId：同一把密钥重复导入时能对上同一个供应商
        # （upsert 而非追加），前缀本身就是公开信息。
        provider_id=provider_id, models=ctx['models'], realm=ctx['realm'],
        default_model=ctx['default_model'])


@router.post('/export')
def export_key(body: ExportIn, request: Request,
               user: dict = Depends(security.require_admin)) -> dict:
    """把一把密钥导出为 cc-switch / ZCode 的配置片段（**只读、无副作用**）。

    **为什么要传明文 token 而不是 key_id**：面板只存哈希，库里拿不回明文；
    只有创建密钥的响应里那一次有。所以本端点不查库、不接受 key_id——
    调用方（前端）把刚拿到的明文传进来，服务端只做格式转换。

    为什么不写客户端文件：写盘/入库属「一键导入」，涉及客户端竞态与部署形态
    （面板可能在服务器上）。导出**无副作用**，所以先合入、默认可用；
    导入是 `POST /import-local`，默认关闭（见下方的三重门）。

    404 语义：本端点不按 id 查库，故不存在 404 分支；参数非法一律 400。
    """
    client = _normalise_client(body.client)
    app = _normalise_app(client, body.app)
    ctx = _config_context(body, request)
    provider_id = f"workbuddy-{ctx['prefix']}"

    try:
        settings = _client_settings(client=client, app=app, token=body.token,
                                    ctx=ctx, provider_id=provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    payload = ({'app_type': app, 'name': ctx['name'], 'settings_config': settings}
               if client == 'ccswitch'
               else {'name': ctx['name'], 'provider': settings})

    # 导出会把明文密钥交给客户端，属敏感操作：留痕（不记密钥本身）
    security.audit(user, 'export_key', ctx['prefix'],
                   f'client={client}；模型 {len(ctx["models"]) or 1} 个；'
                   f'来源 {client_ip(request)}')
    return {'client': client, 'base_url': ctx['base_url'], 'realm': ctx['realm'],
            'models': ctx['models'], 'name': ctx['name'], **payload}


class ImportIn(ExportIn):
    """本机导入：参数与导出完全一致，只多三个开关。

    `set_current`  —— 是否把导入的供应商设为当前；
    `mode`         —— auto（能走客户端官方深链就走）/ deeplink / direct；
    `close_running`—— 直写时，客户端正在运行就替用户关掉、写完再拉起来。
    """

    set_current: bool = False
    mode: str = 'auto'
    close_running: bool = False


IMPORT_MODES = ('auto', 'deeplink', 'direct')

# 拒绝写入的原因码 → HTTP 状态。`not_installed` 是 404（本机没有这个东西），
# 其余是 409（当前状态不允许，改完状态可以重试）——状态码本身就该能表达
# 「这个错误重试有没有意义」。
_REFUSAL_STATUS = {'not_installed': 404, 'client_running': 409,
                   'cannot_detect': 409, 'bad_format': 409,
                   'cannot_stop': 409, 'deeplink_unavailable': 409,
                   'cannot_open': 409}


def _is_loopback(request: Request) -> bool:
    try:
        return ipaddress.ip_address(client_ip(request)).is_loopback
    except ValueError:
        return False


def _require_local_caller(request: Request) -> None:
    """本机导入的两道前置门：开关已开 + 调用方来自本机。"""
    if not config.LOCAL_IMPORT_ENABLED:
        raise HTTPException(
            status_code=403,
            detail='本机导入未启用。它会把密钥直接写进本机的 cc-switch / ZCode '
                   '配置，故默认关闭：请在面板的 .env 里设 WB_LOCAL_IMPORT=1 '
                   '后重启面板。')
    if not _is_loopback(request):
        raise HTTPException(
            status_code=403,
            detail='本机导入只允许从面板所在的机器访问（请用 '
                   'http://127.0.0.1:<面板端口> 打开面板）。远程部署请改用'
                   '「导出配置」手动导入——在服务器上写 cc-switch / ZCode 的'
                   '配置不会影响你本机的客户端。')


@router.get('/import-local/status')
def import_local_status(request: Request,
                        user: dict = Depends(security.require_admin)) -> dict:
    """本机导入的可用状态（**只读**，供界面提前提示，免得点完才知道不行）。

    开关关着时也返回 200（`enabled: false`）而不是 403：界面要能据此**隐藏**
    这一块，报错会让用户以为是故障。
    """
    if not config.LOCAL_IMPORT_ENABLED:
        return {'enabled': False, 'local_caller': _is_loopback(request),
                'clients': []}
    local = _is_loopback(request)
    return {
        'enabled': True,
        'local_caller': local,
        # 远程访问时不去探测服务器上的客户端：那与用户本机无关，
        # 报出来只会让人误以为"已安装、可以导入"。
        'clients': keyimport.status_all() if local else [],
    }


@router.get('/import-local/detect')
def import_local_detect(request: Request, client: str = 'ccswitch',
                        deep: bool = True,
                        user: dict = Depends(security.require_admin)) -> dict:
    """自动检测客户端装在哪（面板的「自动检测」按钮调它）。

    为什么需要它：客户端装在哪台机器上完全看用户——绿色版可能解压在
    `E:\\cc swich\\`（目录名还拼错了），安装版在 `%LOCALAPPDATA%\\Programs\\…`。
    写死任何一个都会在别人机器上失效。检测顺序是"可信度从高到低"：环境变量覆盖
    → 正在跑的进程 → 注册表里的协议处理器（`ccswitch://` 指向谁）→ 上次的结果
    → 常见安装位 →（`deep=true` 时）受限扫描。命中就缓存到面板数据目录。

    只读文件系统与注册表，不写用户的任何配置——写盘只发生在 `/import-local`。
    """
    _require_local_caller(request)
    return keyimport.detect(_normalise_client(client), deep=bool(deep))


@router.post('/import-local')
def import_local(body: ImportIn, request: Request,
                 user: dict = Depends(security.require_admin)) -> dict:
    """把刚创建的密钥**写进本机**的 cc-switch / ZCode 配置（提案 §9.3）。

    与 `/export` 共用 `_client_settings`，两条路产出的配置完全一致。

    **两条导入路径**（见 keyimport 模块开头）：

      · `deeplink`（首选）：把配置拼成 cc-switch 官方的 `ccswitch://v1/import`
        链接交给操作系统。客户端自己校验、入库、弹确认框——我们既不用关它，
        也不用赌它的表结构（`endpointAutoSelect` 之类语义未公开的开关就绕开了）。
      · `direct`（回退）：直接改它的配置/数据库。写入那一刻客户端必须是退出的，
        所以会在 `client_closed` 里把"关掉—写完—拉起来"这一串做掉。

    `mode='auto'`（默认）时能走深链就走深链。

    **刻意不回显密钥**：明文只在「创建密钥」那一次响应里出现，这一条能少一个
    泄露面就少一个（深链走的是同一条原则——链接只在内存里拼，不进响应体）。

    **重复导入是 upsert**：provider id 由密钥前缀推导（见 keyimport），
    同一把密钥反复导入只更新那一项，不会在客户端里堆出一串同名供应商。
    """
    _require_local_caller(request)
    mode = str(body.mode or 'auto').strip().lower()
    if mode not in IMPORT_MODES:
        raise HTTPException(status_code=400,
                            detail=f'mode 必须是 {IMPORT_MODES} 之一，收到 {mode!r}')
    client = _normalise_client(body.client)
    app = _normalise_app(client, body.app)
    ctx = _config_context(body, request)
    provider_id = (f"workbuddy-{ctx['prefix']}" if client == 'zcode'
                   else keyimport.ccswitch_provider_id(ctx['prefix'], app))

    try:
        settings = _client_settings(client=client, app=app, token=body.token,
                                    ctx=ctx, provider_id=provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    # 端点行的 URL 与 settings_config 的 base url 保持同一族
    # （claude 根地址、codex 带 /v1）：实测 cc-switch 现有 provider
    # 就是「端点 = base url 去尾斜杠」，两处不一致会被当成配错了。
    endpoint_url = (keyexport.anthropic_base_url(ctx['base_url'])
                    if app == 'claude' else ctx['base_url'])

    use_deeplink = (client == 'ccswitch' and mode in ('auto', 'deeplink')
                    and keyimport.deeplink_supported('ccswitch'))
    if mode == 'deeplink' and not use_deeplink:
        raise HTTPException(
            status_code=409,
            detail='这台机器用不了客户端官方的深链导入（只支持 cc-switch，'
                   '且它得注册过 ccswitch:// 协议）。请改用「导出配置」手动导入，'
                   '或把导入方式切回「自动」。')

    lifecycle = None
    try:
        if use_deeplink:
            result = keyimport.import_ccswitch_deeplink(
                app=app, name=ctx['name'], settings_config=settings,
                token=body.token, base_url=endpoint_url,
                model=_main_model(ctx), set_current=body.set_current)
        else:
            # 直写：写盘那一刻客户端必须是退出的，否则它随后的保存会整份覆盖。
            with keyimport.client_closed(client,
                                         close_running=body.close_running) as lc:
                if client == 'ccswitch':
                    result = keyimport.import_ccswitch(
                        settings_config=settings, app=app, name=ctx['name'],
                        provider_id=provider_id, endpoint_url=endpoint_url,
                        set_current=body.set_current)
                else:
                    result = keyimport.import_zcode(
                        fragment=settings, set_current=body.set_current)
            lifecycle = lc.as_dict()
    except keyimport.ImportRefused as exc:
        raise HTTPException(status_code=_REFUSAL_STATUS.get(exc.reason, 409),
                            detail=exc.message) from None

    method = 'deeplink' if use_deeplink else 'direct'
    # 写用户本机的凭据文件（或把凭据交给客户端）与导出同级敏感：审计里同样不记密钥。
    security.audit(user, 'import_key_local', ctx['prefix'],
                   f'client={client}{"/" + app if app else ""}；{method}；'
                   f'{result.get("action")}；模型 {len(ctx["models"]) or 1} 个；'
                   f'来源 {client_ip(request)}')
    out = {'client': client, 'app': app, 'realm': ctx['realm'],
           'base_url': ctx['base_url'], 'model_count': len(ctx['models']) or 1,
           'method': method, 'lifecycle': lifecycle}
    out.update(result)
    return out


def _main_model(ctx: dict) -> str:
    """这次配置实际用的主模型（走深链时要把它塞进 URL，口径必须和配置里一致）。"""
    try:
        return keyexport.first_model(ctx['models'], ctx['realm'],
                                     ctx.get('default_model'))
    except ValueError:
        return ''
