"""账号管理：列表、扫码添加、签到、测试、刷新、删除、重启上游。"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import shutil
import time
from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile

from .. import config, db, security, upstreamsvc
from ..services import (
    credits as creditsvc, modelcatalog, reload, tasklog, taskrun, tencent, wb2api,
)
from ..services.realm import realm_of, supports_checkin

logger = logging.getLogger('workbuddy.accounts')

router = APIRouter(prefix='/api', tags=['accounts'])

_ACCOUNT_UPLOAD_MAX_BYTES = 2 * 1024 * 1024
_ACCOUNT_UPLOAD_DEFAULT_TTL = 30 * 24 * 60 * 60


def _uploaded_account(raw: object, filename: str) -> dict:
    """Normalize supported exported account JSON into ``write_auth_file`` input."""
    if not isinstance(raw, dict):
        raise ValueError('账号条目必须是对象')

    account = raw.get('account') if isinstance(raw.get('account'), dict) else raw
    auth = raw.get('auth') if isinstance(raw.get('auth'), dict) else raw

    def value(*keys: str) -> object:
        for source in (auth, account, raw):
            for key in keys:
                if source.get(key) not in (None, ''):
                    return source[key]
        return ''

    uid = str(value('uid', 'user_id')).strip()
    access_token = str(value('accessToken', 'access_token')).strip()
    if not uid:
        raise ValueError('缺少 uid')
    if not access_token:
        raise ValueError('缺少 accessToken')

    expires = value('expiresAt', 'expires_at', 'expiresIn', 'expires_in')
    try:
        if expires in (None, ''):
            # Some account exporters only preserve the opaque tokens and created_at.
            # Keep those files importable; the exact expiry is unavailable in that format.
            expires_at = int(time.time()) + _ACCOUNT_UPLOAD_DEFAULT_TTL
        else:
            expires_at = int(expires)
    except (TypeError, ValueError) as exc:
        raise ValueError('expiresAt 必须是数字') from exc
    if expires_at <= 0:
        raise ValueError('缺少有效 expiresAt')

    return {
        'uid': uid,
        'access_token': access_token,
        'refresh_token': str(value('refreshToken', 'refresh_token') or ''),
        'expires_at': expires_at,
        'domain': str(value('domain') or ''),
        'realm': str(value('realm') or ''),
        'enterprise_id': str(value('enterpriseId', 'enterprise_id') or ''),
        'nickname': str(value('nickname', 'name') or ''),
        'device_token': str(raw.get('device_token') or ''),
        'proxy': str(raw.get('proxy') or ''),
        '_filename': filename,
    }


def _uploaded_entries(raw: object) -> list[object]:
    """Expand common single-account and multi-account export shapes."""
    if isinstance(raw, dict) and isinstance(raw.get('accounts'), list):
        entries = raw['accounts']
    elif isinstance(raw, list):
        entries = raw
    else:
        entries = [raw]
    if not entries:
        raise ValueError('JSON 中没有账号条目')
    return entries


@router.post('/accounts/upload')
async def upload_accounts(
    files: list[UploadFile] = File(...),
    upstream_id: int | None = Query(None),
    user: dict = Depends(security.require_session_admin),
) -> dict:
    """Import one or more account JSON files, preserving the existing account flow."""
    group = _group(upstream_id)
    auth_dir = _require_dir(group)
    uploaded: list[dict] = []
    failed: list[dict] = []

    for upload in files:
        name = upload.filename or '未命名文件'
        try:
            if not name.lower().endswith('.json'):
                raise ValueError('只支持 .json 文件')
            content = await upload.read(_ACCOUNT_UPLOAD_MAX_BYTES + 1)
            if len(content) > _ACCOUNT_UPLOAD_MAX_BYTES:
                raise ValueError('文件不能超过 2 MB')
            raw = json.loads(content.decode('utf-8-sig'))
            for entry in _uploaded_entries(raw):
                try:
                    account = _uploaded_account(entry, name)
                    filename, existed = tencent.write_auth_file(account, auth_dir)
                    # Preserve per-account metadata that write_auth_file intentionally does not
                    # accept from the login flow.
                    path = auth_dir / filename
                    saved = json.loads(path.read_text(encoding='utf-8'))
                    if account['device_token']:
                        saved['device_token'] = account['device_token']
                    if account['proxy']:
                        saved['proxy'] = account['proxy']
                    tencent._atomic_write_json(path, saved)
                    reload.request_reload_or_restart(account['uid'], upstream=_reload_target(group))
                    uploaded.append({'file': filename, 'uid': account['uid'], 'updated': existed})
                except (ValueError, OSError, KeyError, TypeError) as exc:
                    failed.append({'file': name, 'message': str(exc) or '账号条目无效'})
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, OSError) as exc:
            failed.append({'file': name, 'message': str(exc) or '文件格式无效'})

    return {'ok': not failed, 'uploaded': uploaded, 'failed': failed}

# ── 服务端侧的授权轮询（用户反馈：被遮挡窗口的定时器节流）──────────────
#
# 前端每 2 秒轮询一次「授权好了没」，而浏览器对**被遮挡**（不是「不可见」）的窗口
# 会节流定时器：实测降成约 1 分钟一次，而 `document.hidden` 仍是 false、
# visibilitychange 也不触发——代码里那个兜底因此完全失效。
#
# 所以把「问腾讯」这件事搬到服务端（后台任务每 2 秒一次），前端只来读结果：
# 检测节奏与页面是否被遮挡无关，顺带避免多个窗口重复问腾讯。
_LOGIN_POLL_SECONDS = 2.0
_LOGIN_POLL_MAX = 300.0          # 后台最多盯 5 分钟，之后当过期处理
_LOGIN_RESULT_TTL = 900.0        # 终态结果留 15 分钟，够前端被节流后回来取
_login_polls: dict[str, dict] = {}       # state → 响应（终态含成功结果）
_login_tasks: dict[str, asyncio.Task] = {}
_login_regions: dict[str, str] = {}      # state → 国际版地区（可能晚于 auth/start 到达）
_login_owner: dict[str, str] = {}        # state → 发起人，用于收掉同一用户的旧轮询


def _owner_key(user: dict, upstream_id: int | None) -> str:
    return f"{user.get('username') or ''}|{upstream_id if upstream_id is not None else ''}"


def _cancel_other_login_polls(state: str, owner: str) -> None:
    """同一用户又发了一张新码：把旧码的后台轮询收掉。

    不发新码就换码的路径只有一条 —— 用户在弹窗里改地区（前端会重新申请）。
    不收的话，每改一次地区就多一个轮询在替一张废码问腾讯，5 分钟内都在跑，
    而「避免重复调用腾讯」正是把轮询搬到服务端的理由之一。
    同一用户的多扇窗口同理：只保留最新的那张码有后台轮询；旧的窗口靠自己的
    前端轮询照常能拿到结果（那条路径一直可用，只是不享受后台加速）。
    """
    for st, task in list(_login_tasks.items()):
        if st == state or _login_owner.get(st) != owner:
            continue
        if task.done():
            # 已跑完的别动：它的终态结果前端可能还没来取（结果留 TTL 那么久）
            continue
        task.cancel()
        _login_tasks.pop(st, None)
        _login_polls.pop(st, None)
        _login_regions.pop(st, None)
        _login_owner.pop(st, None)
        logger.info('同一用户重新发码，旧轮询已收掉（state=%s）', st)


def _prune_login_polls() -> None:
    # 清掉过期结果（state 是短命的，但别让它无限增长）
    now = time.time()
    for st in [k for k, v in _login_polls.items()
               if now - float(v.get('_at') or 0) > _LOGIN_RESULT_TTL]:
        _login_polls.pop(st, None)
        _login_tasks.pop(st, None)
        _login_regions.pop(st, None)
        _login_owner.pop(st, None)


def _today_start() -> int:
    """本地时区「今天 0 点」的 epoch 秒 —— 签到状态按**自然日**判定。

    两件事都要求自然日口径：

      · 腾讯侧签到就是按自然日算的（重复签到时它回 10001「今日已签到」）；
      · 界面要回答的是「今天签没签」，不是「最近 24 小时签没签」。

    为什么用本地时间而不是 UTC：容器 TZ=Asia/Shanghai（见 docker-compose.yml），
    这里若按 UTC 取当天 0 点，中国时间每天 08:00 之前会被算成「昨天」——
    表现为早上刚签完，界面又说没签。
    """
    now = datetime.datetime.now()
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def _group(upstream_id: int | None) -> dict:
    """解析这次操作作用于哪个**分组**：None / 0 = 默认分组，否则查上游记录。

    账号相关接口有十几个，每一个都要回答同一个问题——「这次操作读哪个目录、
    问哪个上游实例」。集中在一处：默认分组的定义、未知分组的报错、空目录的
    判定只有一份；多分组特性最容易错的地方就是某条路径忘了带分组、静默落到
    默认分组上（那会让「B 组的账号」显示成 A 组的，判断全错）。
    """
    if not isinstance(upstream_id, int) or upstream_id == 0:
        # 非 int 一律当「默认分组」：直呼路由函数（测试 / 内部复用）时拿到的是
        # FastAPI 的 Query 默认哨兵而不是 None——放过它就会误报「分组不存在」。
        return upstreamsvc.default_upstream()
    row = upstreamsvc.get_upstream(upstream_id)
    if row is None:
        raise HTTPException(status_code=404, detail='分组不存在（可能已被删除）')
    return row


def _group_dir(group: dict) -> Path | None:
    """分组的本地账号目录；未配置时返回 None（该分组只做密钥转发）。"""
    raw = str(group.get('auth_dir') or '').strip()
    return Path(raw) if raw else None


def _require_dir(group: dict) -> Path:
    """要求该分组可管理账号：没有本地账号目录就明确拒绝。

    为什么必须拒绝而不是落到默认目录：那会把 B 组的账号加到 A 组去（或相反），
    而两种结果在界面上都「看起来成功了」——隔离失效且无人察觉。宁可报错。
    """
    d = _group_dir(group)
    if d is None:
        raise HTTPException(
            status_code=409,
            detail=(f"分组「{group['name']}」没有配置本地账号目录，无法在面板里管理"
                    "它的账号；请到「设置 → 上游」为它填写账号目录（与那套上游实例"
                    "的 auths 目录一致）"),
        )
    return d


def _reload_target(group: dict) -> dict | None:
    """转给 reload 模块的分组参数：默认分组传 None（既有行为逐字不变）。

    默认上游的启停走 WB2API_MODE / 启停脚本那套（native 部署依赖它），
    只有非默认分组才按「容器名」重启。
    """
    return None if group.get('is_default') else group


@router.get('/proxies')
def available_proxies(user: dict = Depends(security.current_user)) -> dict:
    return {'routes': sorted(config.proxy_routes()), 'default': config.DEFAULT_ACCOUNT_PROXY}


@router.put('/accounts/{filename}/proxy')
async def account_set_proxy(
    filename: str, body: dict = Body(...),
    upstream_id: int | None = Query(None),
    user: dict = Depends(security.require_admin),
) -> dict:
    name = body.get('proxy')
    if not isinstance(name, str):
        raise HTTPException(status_code=400, detail='proxy 必须是线路名或空字符串')
    name = name.strip()
    group = _group(upstream_id)
    try:
        config.account_proxy({'proxy': name})
        result = wb2api.set_account_proxy(filename, name, _require_dir(group))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail='账号文件不存在') from exc
    creditsvc.invalidate()
    scheduled = reload.request_restart(_reload_target(group))
    return {'ok': True, **result, 'reload_triggered': scheduled}


@router.get('/accounts')
async def list_accounts(upstream_id: int | None = Query(None),
                        user: dict = Depends(security.current_user)) -> dict:
    """账号列表：本地授权信息 + 上游运行时状态（含积分余额）。

    积分（credits）优先使用上游 /status 的值：它是上游调度时写入的快照，
    与账号可用性判定一致，开销也小。前端可用「刷新积分」触发实时查询。

    upstream_id 指定**分组**（缺省 = 默认分组）：账号文件读该分组的目录，
    积分与运行状态问该分组的上游实例。两者必须一致——读 A 组的文件、问 B 组
    的状态，界面会把「别的组的账号」标成在线/离线，判断全错。
    """
    group = _group(upstream_id)
    base_dir = _group_dir(group)
    # 没有本地目录的分组**不能**去读默认目录：那会把默认池的账号显示成这个
    # 分组的账号——与「移动时落错目录」是同一类串组错误。列表给空，由
    # manageable 标记让界面说明「该分组未配置本地账号目录」。
    accounts = wb2api.list_auth_accounts(base_dir) if base_dir else []
    status = await wb2api.get_status(base_url=group['base_url'],
                                     api_key=upstreamsvc.forward_api_key(group))
    wb2api.merge_pool_status(accounts, status)
    # 备注随列表一次带回（issue #67）：按 uid 取，没有备注的账号给空串而不是缺字段
    # —— 前端两处视图（手机卡片 / 桌面表格）都直接读它，缺字段会多一处判空。
    notes = db.account_notes()
    # 今日签到状态随列表一次带回（和备注同一个理由：两处视图都直接读它）。
    # 数据源是本端签到记录 —— 腾讯对「今天已签过」回 10001 且我们照记，
    # 所以「本端签过」与「今天已签到」在这里是同一件事。上游自动签到不产
    # 逐账号记录（它只打一行汇总），所以首次进入面板时可能显示未签到，
    # 手动点一次拿到 10001 后就归位了 —— 这一点在界面上如实说明。
    done_today = db.checkin_done_since(_today_start())
    for a in accounts:
        uid = str(a.get('uid') or '')
        a['note'] = notes.get(uid, '')
        a['checkin_today'] = done_today.get(uid)
    synced = sum(1 for a in accounts if a.get('credits') is not None)
    return {
        'total': len(accounts),
        'accounts': accounts,
        'pool_synced': synced,
        'pool_available': bool(status.get('connected')),
        # 分组上下文：前端据此标注「本列表来自哪个分组」，并决定能否在这里
        # 管理账号（没有本地目录的分组只能看，不能加 / 移 / 删）。
        'upstream': {
            'id': group['id'],
            'name': group['name'],
            'is_default': bool(group['is_default']),
        },
        'auth_dir': str(base_dir or ''),
        'manageable': base_dir is not None,
    }


@router.get('/status')
async def upstream_status(upstream_id: int | None = Query(None),
                          user: dict = Depends(security.current_user)) -> dict:
    group = _group(upstream_id)
    return await wb2api.get_status(base_url=group['base_url'],
                                   api_key=upstreamsvc.forward_api_key(group))


def _strip_cn_prefix(m: dict) -> dict:
    """去掉模型 id 的 `cn:` 前缀，**保留 `global:`**（见调用处注释）。"""
    mid = str(m.get('id') or '')
    if mid.lower().startswith('cn:'):
        return {**m, 'id': mid[3:]}
    return m


@router.get('/models')
async def models(
    realm: str | None = None,
    user: dict = Depends(security.current_user),
) -> dict:
    """上游可用模型列表。

    返回结构化对象而非裸数组，是为了带上 source：上游在动态拉取失败时会
    回退到**内置静态表**（老版本写死的，数量少得多），两者外观一样。前端
    据此如实标注来源，避免让人误以为是自己账号/配置有问题。

    realm 非空时只返回该版本的条目：上游清单里带 `cn:` / `global:` 前缀，
    不过滤会把两个版本的模型混在一起显示。
    """
    ok, data = await wb2api.get_models()
    if not ok:
        raise HTTPException(status_code=502, detail=str(data))
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and isinstance(data.get('data'), list):
        items = data['data']
    else:
        items = []
    source_items = items
    if realm:
        r = 'global' if str(realm).strip().lower() == 'global' else 'cn'
        items = [m for m in items if modelcatalog._belongs(m, r)]
    # 去掉 `cn:` 前缀：那是**上游的路由约定**，不是模型本身的名字。
    # 界面要显示的是「腾讯自带的模型名」（glm-5.2 / deepseek-v4.1-flash），
    # 前面挂个 cn: 既难看又让人以为得照着写。
    #
    # ⚠️ 只去 `cn:`，**绝不能动 `global:`**：上游 resolveModel 只认 `[realm:]model`
    # 协议——取第一个 `:` 前段恰为 cn/global 才剥离，**其余一律当裸名（= 国内版）**。
    # 所以裸名走国内版本来就成立（存量客户端一直如此），但**国际版一旦失去
    # `global:` 前缀就会被路由到国内账号池**（模型名对不上，必然报错）。
    # 曾用过 modelcatalog._strip_realm_prefix()，它把两种前缀都去掉了——那会让
    # 界面上的国际版模型名变成「不可用」，实测发现后改为只去 cn:。
    items = [_strip_cn_prefix(m) for m in items]
    return {
        'models': items,
        'source': wb2api.models_source(source_items),
        'count': len(items),
    }


@router.post('/auth/start')
async def auth_start(
    realm: str | None = Query(
        None,
        description='国内版 cn / 国际版 global；也可用 JSON body 传 {"realm": "..."}',
    ),
    region: str | None = Query(
        None,
        description='国际版地区代码（如 HK）；也可用 JSON body 传 {"region": "..."}',
    ),
    upstream_id: int | None = Query(None),
    body: dict | None = Body(None),
    user: dict = Depends(security.require_admin),
) -> dict:
    """发起扫码登录。realm 决定国内版 / 国际版端点（缺省国内版）。

    **参数来源要同时接受 body 与 query**：前端 post() 把参数放在 JSON body 里，
    而早先这里声明的是普通标量参数 —— FastAPI 对标量默认按 **query** 解析，
    body 里的 realm 被静默忽略、恒回落到默认值 'cn'。后果是：切到「国际版」
    点添加账号，拿到的仍是国内版二维码（`copilot.tencent.com`），且**不报错**。
    现同时接受两处，body 优先（与前端一致），query 保留兼容旧调用方。

    region 也在这里收：后台轮询（见 `_poll_login_background`）要用它做地区注册，
    而它**不能只靠前端轮询带上来** —— 窗口被遮挡时前端可能一次都不补，那条
    路径正是这个后台轮询存在的原因。用户在弹窗里改地区会重新发码，所以这里
    拿到的就是当前那张码对应的地区。
    """
    # 分组在这里只做**提前校验**：真正的落盘发生在 auth/poll（那里也要带
    # 同一个分组）。先拒掉不存在的分组，用户不会扫完码才发现目标分组没了。
    _group(upstream_id)
    raw = realm
    if isinstance(body, dict) and body.get('realm') is not None:
        raw = str(body.get('realm'))
    r = 'global' if str(raw or '').strip().lower() == 'global' else 'cn'
    raw_region = region
    if isinstance(body, dict) and body.get('region') is not None:
        raw_region = str(body.get('region'))
    reg = str(raw_region or '').strip() or None
    try:
        proxy_name = config.DEFAULT_ACCOUNT_PROXY
        if isinstance(body, dict) and 'proxy' in body:
            if not isinstance(body['proxy'], str):
                raise HTTPException(status_code=400, detail='proxy 必须是线路名或空字符串')
            proxy_name = body['proxy'].strip()
        try:
            config.account_proxy({'proxy': proxy_name})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        out = await tencent.start_login(r, proxy_name) if proxy_name else await tencent.start_login(r)
        # 起后台轮询：前端是否被节流都不影响检测节奏（见文件上方说明）
        st = str(out.get('state') or '')
        if st:
            if reg:
                _login_regions[st] = reg
            if st not in _login_tasks or _login_tasks[st].done():
                _prune_login_polls()
                _login_polls.pop(st, None)
                _cancel_other_login_polls(st, _owner_key(user, upstream_id))
                _login_owner[st] = _owner_key(user, upstream_id)
                _login_tasks[st] = asyncio.create_task(
                    _poll_login_background(st, r, reg, upstream_id, user))
        return out
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get('/auth/poll')
async def auth_poll(
    state: str,
    realm: str | None = None,
    region: str | None = None,
    upstream_id: int | None = Query(None),
    user: dict = Depends(security.require_admin),
) -> dict:
    """轮询扫码结果；成功则落盘并触发上游重载。

    region：国际版账号的地区代码（如 HK）。国际版新号必须先完成地区注册，
    否则聊天会报 14017；地区由用户在弹窗里选，这里只负责提交，不替他决定。
    """
    if not state:
        return {'status': 'invalid'}

    # 后台已经跑出终态：直接返回那一份，**不再问腾讯**。
    # 只对终态短路：失败态（落盘失败等）要继续走下面的原路径，那里有
    # `provisioned` 重试语义（用户点「重试」时重新落盘，而不是重扫二维码）。
    cached = _login_polls.get(state)
    if cached and cached.get('status') not in (None, 'waiting'):
        # 去掉内部记账字段（`_at`）再返回，别把实现细节漏给前端
        return {k: v for k, v in cached.items() if not k.startswith('_')}

    # 地区可能比 auth/start 更晚到（用户在弹窗里选、或另一扇窗口带上来）：
    # 记下来给后台轮询用 —— 国际版漏了地区注册，聊天会报 14017。
    if region:
        _login_regions[state] = region
    group = _group(upstream_id)

    # 落盘成功后 state 会在下面被丢弃，但如果**落盘失败**（issue #26），
    # 前端会继续轮询同一个 state 让用户重试 —— 那条路径不能把签到、领 trial
    # 这些**有副作用**的动作重跑一遍（会重复写签到记录、重复调腾讯接口）。
    # 所以记一笔「这个 state 已经做过供给」，重试时直接跳到落盘。
    provisioned = _provisioned_states.get(state)

    r = None
    if realm is not None:
        r = 'global' if str(realm).strip().lower() == 'global' else 'cn'
    result = await tencent.poll_login(state, r)
    if result.get('status') != 'ready':
        return result

    realm_of_result = result.get('realm') or 'cn'
    if provisioned:
        # 已经供给过：只补落盘（上次就是败在这一步），其余一律不重做
        return _save_and_finish(result, realm_of_result, region_msg='', state=state,
                                group=group)

    # 探测 dict：billing 域身份头（X-User-Id / X-Domain 等）需要这些字段，
    # 与 _auth_dict 同构；device_token 此刻还没有（落盘时由外部写入）
    auth_probe = {
        'access_token': result['access_token'],
        'uid': result['uid'],
        'enterprise_id': result.get('enterprise_id', ''),
        'realm': realm_of_result,
        'domain': result.get('domain', ''),
        'proxy': result.get('proxy', ''),
    }

    # 国际版：先做地区注册（未注册会导致后续聊天报 14017）
    region_msg = ''
    if realm_of_result == 'global':
        if region:
            ok_r, region_msg = await tencent.submit_region(auth_probe, region)
        else:
            done, why = await tencent.registration_status(auth_probe)
            if not done:
                region_msg = '尚未完成地区注册，请在弹窗中选择地区后重试（否则聊天会报 14017）'
        # 注册完成后再领一次性 trial（失败不阻断，已领过按幂等）
        if region_msg == '' or region_msg.startswith('地区已提交'):
            ok_t, trial_msg = await tencent.claim_trial(auth_probe)
            if ok_t:
                region_msg = f'{region_msg}；{trial_msg}'.strip('；')

    # 自动签到（幂等，不阻断落盘）。国际版无签到体系，checkin 会直接跳过
    code, message = await tencent.checkin(auth_probe, realm_of_result)
    db.add_checkin_log(
        str(result.get('uid', '')), str(result.get('nickname', '')),
        'add', code in (0, 10001), code, message,
    )

    # 供给（签到 / trial）做完了才允许重试时跳过它们 —— 标记要在**动副作用之前**
    # 落位，否则「签到成功但标记没写」的窗口里重试仍会重跑一次。
    _mark_provisioned(state)
    creditsvc.invalidate(str(result.get('uid', '')))
    return _save_and_finish(result, realm_of_result, region_msg, state,
                            group=group)


def _mark_provisioned(state: str) -> None:
    """记下「这个 state 已完成签到/trial 等有副作用的供给」。

    只在内存里、且随 state 一起被 TTL 回收：它只需覆盖「落盘失败 → 用户重试」
    这个短窗口（发码起 5 分钟内）；服务重启后 state 本身也失效，标记没有意义。
    """
    _provisioned_states[state] = time.time()
    # 顺手清掉过期的，避免长期运行后无限增长（与 state 同一有效期）
    cutoff = time.time() - tencent.STATE_TTL
    for k in [k for k, at in _provisioned_states.items() if at < cutoff]:
        _provisioned_states.pop(k, None)


async def _poll_login_background(state: str, realm: str, region: str | None,
                                  upstream_id: int | None,
                                  user: dict) -> None:
    """替前端把授权轮询跑完，直到出现终态（见文件上方说明）。

    复用 `auth_poll` 本身：那条路径已经处理了地区注册 / trial / 签到 / 落盘与
    各种错误分支，重写一份必然漂移。跑出终态就存住，前端来读即可。
    """
    deadline = time.time() + _LOGIN_POLL_MAX
    while time.time() < deadline:
        # 地区每轮取一次最新的：auth_start 时可能还没有（用户要先看到二维码
        # 才会去选地区），而地区登记必须在**落盘前**完成，否则国际版新号
        # 聊天报 14017。
        region = _login_regions.get(state) or region
        try:
            resp = await auth_poll(state=state, realm=realm, region=region,
                                   upstream_id=upstream_id, user=user)
        except HTTPException as exc:
            # 落盘失败这类：不存终态，让前端走原路径（那里有「重试即重新落盘」的
            # 语义，且要保留 state 不丢）。
            logger.warning('后台轮询遇到可重试的错误（等前端重试）: %s', exc.detail)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning('后台轮询失败（不影响前端轮询）: %s', exc)
            return
        if resp.get('status') != 'waiting':
            _login_polls[state] = {**resp, '_at': time.time()}
            return
        await asyncio.sleep(_LOGIN_POLL_SECONDS)
    logger.info('后台轮询到时（%ss）：按过期处理', int(_LOGIN_POLL_MAX))


# ── 系统自动禁用（disabled）的解除 ────────────────────────────────
#
# 上游把「账号不可用」拆成**两位独立的开关**：
#
#   · `manual_disabled` —— 运维主动摘的（面板的「临时停用」）；
#   · `disabled`        —— 系统自动禁的（12153 连败 3 次、或 11140 被封）。
#
# 两者由不同的端点解除：`enable` 只清前者，`revive` 只清后者（上游 admin.go
# 原话：「若账号仍被系统自动禁用，它不会因此回到选号池——那需要 revive」）。
#
# 面板此前**只调 disable/enable、从不调 revive**，于是被自动禁用的账号在面板里
# 没有任何操作能救回来 —— 重新登录只换凭证（上游 upsert 保留 disabled）、签到被
# `!e.disabled` 挡掉、一次成功又要求先被选中（自锁）、时间也不会让它过期。
# 用户报的「国际版重新登录后连通性测试通过、账号池就是调不动」就是这条。


async def _revive_if_disabled(uid: str, *, base_url: str | None = None,
                              api_key: str | None = None) -> tuple[bool, str, bool | None]:
    """账号若被上游**系统自动禁用**，解除它。

    返回 `(是否真的解除了, 说明文案, 之后是否仍被禁用)` —— 第三位是**三态**：
    `True` 仍禁用 / `False` 确认已回到池子 / `None` 读不到状态（不知道）。
    为什么不是 bool：把「读不到」当 `False` 会让调用方**无条件报成功**
    （「已启用」），而那时我们其实什么都没确认 —— 正是这个功能要消灭的误导。
    调用方必须显式处理 `None`。

    为什么抽成一个函数：有三处要用同一套判断 —— 面板的「启用」按钮、重新登录后的
    自动恢复、以及将来可能的批量修复。写三遍必然漂移。

    为什么拿 `/status` 而不是 admin 端点的回显做判据：回显里的 `disabled` 字段是
    较新版本才有的，而 `/status` 一直带着它，且它正是**界面显示的那一份数据** ——
    用同一个来源判断，才不会出现「接口说好了、界面还红着」。
    """
    still = await wb2api.account_disabled(uid, base_url=base_url, api_key=api_key)
    if still is None:
        return False, '读不到该账号在上游的状态（上游不可达，或它还没进入账号池）', None
    if not still:
        return False, '', False
    ok, msg, code = await wb2api.revive_account(uid, base_url=base_url, api_key=api_key)
    if not ok:
        if code == 'no_route':
            return False, ('上游未启用管理接口，无法解除系统禁用 —— 请到'
                           '「设置 → 账号管理接口」开启后重试'), True
        return False, f'解除系统禁用失败：{msg}', True
    # 再确认一次：接口回 200 ≠ 真的回到池子（见 revive_account 的说明）。
    # 这次读不到就如实返回 None（不知道），不要 `bool(None)` 谎报「已清干净」。
    after = await wb2api.account_disabled(uid, base_url=base_url, api_key=api_key)
    return True, msg, after


# 重新登录后的自动恢复。等待上限比热加载窗口（约 5 秒）宽一档：账号文件刚落盘时
# 上游可能还没扫到，这时 `/status` 里读不到它（返回 None），要再等一轮。
_REVIVE_WAIT_SECONDS = 20.0
_REVIVE_POLL_SECONDS = 2.0

# 在跑的自动恢复任务。**必须持有强引用**：asyncio 只保留对任务的弱引用，
# `create_task()` 的结果没人接住时，任务可能在执行途中被 GC 掉 —— 症状是
# 「这次修复有时不生效」，且完全不报错。与上面的 `_login_tasks` 同一套路。
_revive_tasks: set[asyncio.Task] = set()


async def _revive_after_login(uid: str, group: dict) -> None:
    """登录成功后的后台收尾：等账号入池，若仍被系统禁用就自动解除。

    **为什么重新登录应当自动恢复**：登录在语义上就是「刷新账号状态」—— 用户重新
    授权了一次，凭证是全新的，上游那句「需重新登录」的禁用理由已经消失，没有道理
    还让它留在池外。上游自己不做这件事（upsert 只换凭证），所以由面板补上。
    """
    base_url = group.get('base_url')
    api_key = upstreamsvc.forward_api_key(group)
    deadline = time.monotonic() + _REVIVE_WAIT_SECONDS
    while time.monotonic() < deadline:
        still = await wb2api.account_disabled(uid, base_url=base_url, api_key=api_key)
        if still is None:
            await asyncio.sleep(_REVIVE_POLL_SECONDS)   # 还没被上游收录，再等等
            continue
        if not still:
            return                                      # 没被禁用，无需处理
        ok, msg, _ = await wb2api.revive_account(uid, base_url=base_url, api_key=api_key)
        logger.info('重新登录后自动解除系统禁用 uid=%s：%s%s',
                    uid[:8], '成功' if ok else '失败', f'（{msg}）' if msg else '')
        return
    logger.info('重新登录后等待账号入池超时，跳过自动解除禁用 uid=%s', uid[:8])


def _schedule_revive_after_login(uid: str, group: dict) -> None:
    """调度上面那个后台任务。

    登录**已经成功**（账号文件确实落盘了），所以这里任何失败都不该反过来影响
    登录结果 —— 拿不到事件循环、排不上任务都静默跳过（测试直呼路由函数、
    或在没有循环的线程里调用，都是常见情形）。

    整段都包在 try 里，不只包 `get_running_loop`：排任务那一步抛出来同样会
    顺着 `_save_and_finish` 冒到 `auth_poll`，把一次**已经成功**的登录变成 500。
    """
    uid = str(uid or '').strip()
    if not uid:
        return
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(_revive_after_login(uid, group))
        # 持有强引用：asyncio 只保留弱引用，没人接住的任务可能在执行途中被 GC 掉，
        # 症状是「有时不生效」且不报错（与 _login_tasks 同一套路）。
        if task is not None:
            _revive_tasks.add(task)
            task.add_done_callback(_revive_tasks.discard)
    except Exception:  # noqa: BLE001 —— 排不上就算了，登录本身已经成功
        logger.debug('调度「登录后自动解除系统禁用」失败（不影响登录结果）',
                     exc_info=True)


def _save_and_finish(result: dict, realm_of_result: str, region_msg: str,
                     state: str, group: dict | None = None) -> dict:
    """落盘 + 收尾（重载上游、丢弃 state）。落盘失败时抛出**可执行**的提示。

    单独抽出来是因为它有两条进入路径：首次供给后、以及「上次就是败在落盘」
    的重试。两条路径都必须给出同样的引导，且都**不丢 state**（issue #26）。
    """
    # 落盘是这一步里**唯一真正关键**的动作：成功才算账号加进来了。
    # 把它包起来是为了给出可执行的提示 —— 失败的一个常见成因是 auths 目录
    # 权限不对（宝塔/1Panel 用 root 装、却以别的 uid 跑），而那个错误的原文
    # 只有一行 PermissionError，用户看不出「该 chown 哪个目录」。
    group = group or upstreamsvc.default_upstream()
    auth_dir = _group_dir(group)
    try:
        filename, existed = tencent.write_auth_file(result, auth_dir)
    except ValueError as exc:
        # uid 形态异常（`write_auth_file` 会校验后才拼文件名）。这不是权限问题，
        # 给「去 chown」的提示会把用户引到错方向 —— 如实说明是上游返回的数据异常。
        raise HTTPException(
            status_code=500,
            detail=(
                f'账号已授权成功，但返回的账号标识形态异常，已拒绝写入：{exc}。'
                f'这通常是上游接口返回了非预期的数据；请重试一次，'
                f'若持续出现请把本条信息反馈给我们。'
            ),
        ) from exc
    except OSError as exc:
        # 不 drop state：用户此刻重试（或前端再轮询一次）应当能成功，
        # 而不是拿到「二维码已失效」被引导去重新扫码（issue #26 的现象）。
        raise HTTPException(
            status_code=500,
            detail=(
                f'账号已授权成功，但写入账号文件失败：{exc}。'
                f'请检查 {auth_dir or config.AUTH_DIR} 的目录权限（容器部署见 compose 里 '
                f'chown 10001:10001 的说明），修好后**直接重试本弹窗**即可，'
                f'不需要重新扫码。'
            ),
        ) from exc

    # 账号已确实落盘，这时才丢 state（成功路径的收尾）
    tencent.drop_state(state)
    _provisioned_states.pop(state, None)

    # 让上游加载新账号：**先等它的 auths 热加载，超时才重启**（后台执行，
    # 不阻塞本次响应）。此前无脑重启，而重启期间上游 /status 不可用 —— 前端
    # 收到成功立刻刷新账号列表，那一刷会一直挂到容器起来，用户看到的是
    # 「登录成功却要等 20-30 秒」。详见 reload.request_reload_or_restart。
    reload.request_reload_or_restart(str(result.get('uid') or ''),
                                     upstream=_reload_target(group))
    # 重新登录 = 刷新账号状态：若它此前被系统禁用（12153 连败 / 11140 被封），
    # 这时后台自动解除 —— 否则「重新登录」救不回账号，用户报的就是这条。
    _schedule_revive_after_login(str(result.get('uid') or ''), group)

    return {
        'status': 'success',
        'uid': result['uid'],
        'nickname': result['nickname'],
        'realm': realm_of_result,
        'updated': existed,
        'file': filename,
        'region_note': region_msg,
    }


def _auth_dict(raw: dict) -> dict:
    """把授权文件内容整理成探测 / 查询积分所需的字段（含版本，供端点分派）。

    device_token 是**每号**的设备风控 token（auth 文件顶层 device_token 键，
    与 workbuddy2api 的解析位置一致）；缺省留空，由 realm.device_token_for
    回退到上游 config 的全局值或文件。
    """
    acct = raw.get('account') or {}
    auth = raw.get('auth') or {}
    return {
        'access_token': auth.get('accessToken', ''),
        'uid': acct.get('uid', ''),
        'enterprise_id': acct.get('enterpriseId', ''),
        'domain': auth.get('domain', ''),
        'realm': auth.get('realm'),
        'device_token': str(raw.get('device_token') or ''),
        'proxy': str(raw.get('proxy') or ''),
    }


def _load(filename: str, auth_dir: Path | None = None) -> dict:
    try:
        return wb2api.read_account_file_any(filename, auth_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail='账号文件不存在') from exc


@router.post('/accounts/{filename}/checkin')
async def account_checkin(filename: str, upstream_id: int | None = Query(None),
                          user: dict = Depends(security.require_admin)) -> dict:
    raw = _load(filename, _require_dir(_group(upstream_id)))
    acct = raw.get('account') or {}
    auth = raw.get('auth') or {}
    token = auth.get('accessToken', '')
    uid = str(acct.get('uid', ''))
    nickname = str(acct.get('nickname', ''))

    if not token:
        db.add_checkin_log(uid, nickname, 'manual', False, None, '该账号无有效 accessToken')
        return {'code': -1, 'message': '该账号无有效 accessToken'}

    # 今天已经签过就不再打上游：腾讯对重复签到回 10001（幂等成功，不是错误），
    # 但每点一次都是一次真实 RPC，而且会在签到记录里堆出一串「今日已签到」，
    # 把真正的失败记录挤出视线（线上实测：同一个账号一天被记了 11 条）。
    # 这里就地返回、不写日志，让「签到记录」保持「每次实际动作一条」。
    done_at = db.checkin_done_since(_today_start()).get(uid)
    if done_at is not None:
        return {
            'code': 10001,
            'already': True,
            'message': '今日已签到，无需重复',
            'checkin_today': done_at,
            'credits': None,
            'expiries': [],
        }

    # 传完整 auth dict：billing 域要带 X-User-Id 等身份头（对齐上游 BillingHeaders）
    code, message = await tencent.checkin({
        'access_token': token,
        'uid': uid,
        'enterprise_id': str(acct.get('enterpriseId') or ''),
        'domain': str(auth.get('domain') or ''),
        'realm': auth.get('realm'),
        'device_token': str(raw.get('device_token') or ''),
        'proxy': str(raw.get('proxy') or ''),
    }, realm_of(auth))
    # 0 = 签到成功；10001 = 今日已签到，同样视为成功；
    # -2 = 该版本无签到体系（国际版），不是失败，也不该记成失败
    ok = code in (0, 10001)
    db.add_checkin_log(uid, nickname, 'manual', ok, code, message)

    # 签到后顺带查实时积分：上游只在它自己的定时任务里刷新 credits，
    # 手动签到不会带动它更新，所以这里主动查一次返回给前端。
    credits: int | float | None = None
    expiries: list[dict] = []
    if ok:
        # 签到会改变余额，先失效缓存再查实时值
        creditsvc.invalidate(uid)
        _, credits, _, _, _, expiries = await creditsvc.get_credits(
            _auth_dict(raw), nickname=str(acct.get('nickname') or ''),
        )

    return {
        'code': code,
        'message': message,
        # 10001 = 腾讯说「今天已签过」：这也是成功，但和「本次刚签上」在提示语上
        # 要分开说 —— 否则用户会以为自己的点击真的又签了一次。
        'already': code == 10001,
        'credits': credits,
        'expiries': expiries,
    }


@router.get('/accounts/{filename}/credits')
async def account_credits(
    filename: str,
    force: bool = False,
    upstream_id: int | None = Query(None),
    user: dict = Depends(security.require_admin),
) -> dict:
    """查询单个账号的实时积分余额与套餐到期时间（直接向腾讯查询，带 60s 缓存）。

    force=true 可绕过缓存强制查询。
    expiries: [{'at': epoch 秒, 'amount': 额度}]，按到期时间升序，只含仍有余额的套餐。
    """
    raw = _load(filename, _require_dir(_group(upstream_id)))
    acct = raw.get('account') or {}
    ok, value, message, cached, age, expiries = await creditsvc.get_credits(
        _auth_dict(raw), force=force, nickname=str(acct.get('nickname') or ''),
    )
    return {
        'ok': ok,
        'credits': value,
        'message': message,
        'cached': cached,
        'cache_age': age,
        'expiries': expiries,
    }


@router.post('/accounts/refresh-credits')
async def refresh_all_credits(
    force: bool = True,
    upstream_id: int | None = Query(None),
    user: dict = Depends(security.current_user),
) -> dict:
    """并发查询所有账号的积分，返回 {uid: credits} 与每条是否来自缓存。

    上游 /status 的 credits 只在它定时任务时更新，可能滞后数小时；
    本接口直接向腾讯查询。force=true（默认）用于「刷新积分」按钮，
    强制绕过 60 秒缓存；force=false 用于页面加载，命中缓存时不重复请求腾讯。
    无论哪种，都回传 cached / cache_age，前端据此标注「实时 / 缓存」。

    ## 权限（issue #56）

    `force=false`（页面加载那条路）**任何已登录用户都能调**：它读取的是
    账号余额，属于只读信息，只读账号同样应当看到**同一份**数字。
    此前整个接口都是 admin-only，于是只读账号的页面加载静默 403，界面
    回退到上游 `/status` 的快照值——那是「上游上次调度这个账号时记下的」，
    可能滞后数小时、也可能还是 0，用户看到的就是「积分不对 / 有两个显示 0」。

    `force=true`（显式点「刷新积分」）仍然只有管理员能调：它会**强制**绕过
    缓存、对所有账号发起一次真实查询，属于「主动触发外部调用」的动作，
    不该由只读账号驱动。
    """
    if force and str(user.get('role') or '') != 'admin':
        raise HTTPException(status_code=403, detail='刷新积分需要管理员权限')

    base_dir = _require_dir(_group(upstream_id))
    accounts = wb2api.list_auth_accounts(base_dir)

    async def one(
        acc: dict,
    ) -> tuple[str, int | float | None, str, bool, int | None, list[dict]]:
        try:
            raw = wb2api.read_account_file(acc['file'], base_dir)
        except Exception as exc:  # noqa: BLE001
            return acc['uid'], None, f'读取失败: {exc}', False, None, []
        ok, value, message, cached, age, expiries = await creditsvc.get_credits(
            _auth_dict(raw), force=force, nickname=str(acc.get('nickname') or ''),
        )
        return acc['uid'], value if ok else None, message, cached, age, expiries

    results = await asyncio.gather(*(one(a) for a in accounts)) if accounts else []

    credits_map: dict[str, int | float | None] = {}
    meta: dict[str, dict] = {}
    failed: list[str] = []
    for uid, credits, message, cached, age, expiries in results:
        credits_map[uid] = credits
        meta[uid] = {
            'cached': cached,
            'cache_age': age,
            'message': message,
            'expiries': expiries,
        }
        if credits is None:
            failed.append(f'{uid[:12]}: {message}')

    return {
        'total': len(accounts),
        'succeeded': len(accounts) - len(failed),
        'credits': credits_map,
        'meta': meta,
        'failed': failed,
    }


def _checkin_semaphore() -> asyncio.Semaphore:
    """限制签到并发数：太高容易触发腾讯风控，太低又会拖到前端超时。"""
    return asyncio.Semaphore(config.CHECKIN_CONCURRENCY)


@router.post('/accounts/checkin-all')
async def checkin_all(upstream_id: int | None = Query(None),
                      user: dict = Depends(security.require_admin)) -> dict:
    """对所有账号执行一次签到，并逐条记录结果。

    上游的自动签到只在失败时打日志、成功静默，且没有可触发的 HTTP 接口；
    这里用管理端自己的签到实现补齐「可手动触发 + 可追溯」。

    并发执行（上限见 WB_CHECKIN_CONCURRENCY）：逐个 await 时，
    几十个账号叠加腾讯 RPC 耗时会超过前端 60 秒超时——前端报失败、
    后端却还在跑，用户容易重复点击。并发后总耗时约等于最慢的单个账号。
    """
    base_dir = _require_dir(_group(upstream_id))
    accounts = wb2api.list_auth_accounts(base_dir)
    done_today = db.checkin_done_since(_today_start())
    sem = _checkin_semaphore()

    async def one(acc: dict) -> dict:
        filename = acc['file']
        uid = acc.get('uid', '')
        nickname = acc.get('nickname', '')
        try:
            raw = wb2api.read_account_file(filename, base_dir)
            auth = raw.get('auth') or {}
            token = auth.get('accessToken', '')
        except Exception as exc:  # noqa: BLE001
            msg = f'读取失败: {exc}'
            db.add_checkin_log(uid, nickname, 'manual-batch', False, None, msg)
            return {'nickname': nickname, 'ok': False, 'message': msg}

        if not token:
            msg = '无有效 accessToken'
            db.add_checkin_log(uid, nickname, 'manual-batch', False, None, msg)
            return {'nickname': nickname, 'ok': False, 'message': msg}

        # 国际版没有签到体系：直接跳过（不记失败，也不打上游请求）
        if not supports_checkin(realm_of(auth)):
            msg = '国际版无签到体系，已跳过'
            db.add_checkin_log(uid, nickname, 'manual-batch', False, -2, msg)
            return {'nickname': nickname, 'ok': False, 'skipped': True,
                    'code': -2, 'message': msg}

        # 今日已签到：跳过（理由同单账号签到 —— 重复点是白打的 RPC，
        # 还会在签到记录里堆出一串「今日已签到」把失败记录挤下去）。
        # 结果里照报，界面才能显示「N 个今日已签到」而不是让它们凭空消失。
        if uid and uid in done_today:
            return {'nickname': nickname, 'ok': True, 'already': True,
                    'code': 10001, 'message': '今日已签到，已跳过'}

        async with sem:
            # 传完整 auth dict：billing 域要带 X-User-Id 等身份头
            code, message = await tencent.checkin({
                'access_token': token,
                'uid': str((raw.get('account') or {}).get('uid') or uid),
                'enterprise_id': str((raw.get('account') or {}).get('enterpriseId') or ''),
                'domain': str(auth.get('domain') or ''),
                'realm': auth.get('realm'),
                'device_token': str(raw.get('device_token') or ''),
                'proxy': str(raw.get('proxy') or ''),
            }, realm_of(auth))
        ok = code in (0, 10001)
        db.add_checkin_log(uid, nickname, 'manual-batch', ok, code, message)
        return {'nickname': nickname, 'ok': ok, 'code': code, 'message': message}

    results = await asyncio.gather(*(one(a) for a in accounts)) if accounts else []
    # 两类账号都不进 `total` 分母，各自单独报数：
    #   · skipped：国际版没有签到体系，既不会成功也不是失败。算进 total 会显示成
    #     「5/6 成功」，用户以为漏签了一个号、反复去点——而它永远是「已跳过」。
    #   · already：今日已签到，本次压根没打上游。算进 total 会把「无需重复」说成
    #     「刚签成功」，用户会以为这次点击真的又签了一次。
    # 于是 total 的含义收敛成「本次真正发起并需要结果的账号数」，
    # succeeded 自然就是「这次真签上了几个」。
    applicable = [r for r in results if not r.get('skipped')]
    already = [r for r in applicable if r.get('already')]
    attempted = [r for r in applicable if not r.get('already')]
    succeeded = sum(1 for r in attempted if r['ok'])
    return {
        'total': len(attempted),
        'succeeded': succeeded,
        'already': len(already),
        'skipped': len(results) - len(applicable),
        'results': list(results),
    }


@router.get('/checkin-logs')
def checkin_logs(
    limit: int = 200,
    uid: str | None = None,
    offset: int = 0,
    days: int | None = None,
    realm: str | None = None,
    user: dict = Depends(security.current_user),
) -> dict:
    """签到记录（分页）：**本端触发的 + 上游自动签到的**统一视图。

    为什么要合并：签到记录原先只写本端触发的（手动 / 批量 / 添加账号），而上游
    定时签到的结果由日志采集器写进 task_logs。于是「自动签到」在签到记录里
    永远看不到——用户反馈的「自动签到不显示」就是这个。

    上游对签到成功是静默的（只打一行 `checkin done: total=.. ok=..` 汇总 + 失败明细），
    所以自动签到侧提供的是**每轮汇总/异常行**，不是逐账号成功明细；这一点在
    界面上如实标注，不假装有更细的数据。

    两张表各自条数都不大，按 ts 归并后在 Python 侧分页，避免为跨表分页写
    UNION + 双重 LIMIT 的复杂 SQL。

    候选量必须覆盖到「当前页的末尾」，不能固定取前 N 条：合并是按时间排序的，
    若只取各表最近的 500 条，落在 500 名之后的记录会永远翻不到，而 total 又是
    真实全量——界面会显示「共 810 条」却翻不出后面 300 条。因此按 offset+limit
    取候选（各表都取这么多，够覆盖最坏情况：全部记录都来自同一张表）。
    """
    start = max(0, int(offset))
    size = min(500, max(1, int(limit)))
    # 各表都取到 start+size，保证合并后第 start..start+size 条一定在候选里
    want = start + size

    # 版本筛选是**按行**做的（日志表没有 realm 列），被筛掉的往往是窗口里的大多数
    # 行——另一个版本的记录。候选量因此要放大到单表上限，与 /task-logs 重算 stats
    # 时同一做法。不放大会有两个后果：本版的记录被另一版挤出窗口（页面上「本版
    # 一条都没有」），以及下面按窗口统计的 total 偏小（issue #51 的次要问题）。
    realm_map = _realm_uid_filter(realm)
    if realm_map is not None:
        want = max(want, 2000)

    local = db.list_checkin_logs(limit=want, uid=uid, offset=0, days=days)
    local_total = db.count_checkin_logs(uid=uid, days=days)
    local_items = [{**r, 'auto': False} for r in local]

    # 上游自动签到（采集器落库的 kind='checkin'）
    auto_rows = db.list_task_logs(limit=want, uid=uid, kind='checkin', offset=0, days=days)
    auto_total = db.count_task_logs(uid=uid, kind='checkin', days=days)
    auto_items = [
        {
            # 与 checkin_logs 的 id 空间不同，加偏移前缀避免前端 key 冲突
            'id': 10**9 + int(r['id']),
            'ts': r['ts'],
            'uid': r['uid'],
            'nickname': r.get('nickname') or '',
            'source': 'auto',
            'kind': 'checkin',
            # 汇总行不代表单个账号成功，success 仅用于界面着色，不参与判定
            'success': r.get('level') != 'error',
            'code': None,
            'message': r.get('message_cn') or r.get('message') or '',
            'auto': True,
        }
        for r in auto_rows
    ]

    merged = sorted(local_items + auto_items, key=lambda x: x['ts'], reverse=True)

    # 按版本过滤（realm 为空则不过滤，保持既有调用行为）
    if realm_map is not None:
        merged = [it for it in merged if _uid_matches_realm(str(it.get('uid') or ''), realm_map, realm)]

    # total 必须与**列表同一口径**（issue #51 次要问题）：此前它统计在版本过滤之前，
    # 界面上「共 6 条 · 仅显示最近 2 条」而列表只有 2 条，用户以为记录丢了。
    # 过滤生效时用过滤后的条数（窗口已放大到单表上限，见上），未过滤时保持原值。
    total = len(merged) if realm_map is not None else local_total + auto_total
    unfiltered_total = local_total + auto_total

    # 自动侧的行也要解析昵称（上游只带 uid 前 8 位）；本端的已有昵称
    resolve_nick = _nickname_resolver()
    for it in merged:
        if not it.get('nickname'):
            it['nickname'] = resolve_nick(str(it.get('uid') or ''))

    start = max(0, int(offset))
    end = start + min(500, max(1, int(limit)))
    return {
        'items': merged[start:end],
        'total': total,
        # 分别给出，便于界面说明「本端 N 条 / 自动 M 条」。
        #
        # **这两个数故意不按版本过滤**（与上面的 `total` 不同）：界面用它说明
        # 「清空会删掉多少条」，而清空是整表操作、不分版本——按版本过滤会让提示
        # 少说数量。两个口径各有用途，别把它们「统一」掉。
        'local_total': local_total,
        'auto_total': auto_total,
        # 两个版本合计的条数（版本筛选生效时，`total` 只数当前版本）
        'unfiltered_total': unfiltered_total,
    }


@router.post('/checkin-logs/clear')
def clear_checkin_logs(user: dict = Depends(security.require_session_admin)) -> dict:
    db.clear_checkin_logs()
    return {'ok': True}


def _nickname_resolver() -> object:
    """构造 uid → 昵称的解析函数。

    上游 2026-09-12 起把日志里的 uid 截成前 8 位，而账号表里是完整 uuid，
    无法直接相等匹配，因此按前缀解析；前缀命中多个账号（理论可能）时不猜，
    保留 uid 原文。签到记录与任务记录都用它，避免两处口径不一致。
    """
    nick_by_uid: dict[str, str] = {}
    nick_by_prefix: dict[str, str] = {}
    ambiguous: set[str] = set()
    try:
        # 默认目录 + 各分组登记的账号目录：日志里的 uid 可能来自任何一个池，
        # 只扫默认目录会让分组账号在日志里显示成裸 uid。
        dirs: list[Path | None] = [None]
        for up in upstreamsvc.list_upstreams(include_default=False):
            raw_dir = str(up.get('auth_dir') or '').strip()
            if raw_dir:
                dirs.append(Path(raw_dir))
        for base_dir in dirs:
            for acc in wb2api.list_auth_accounts(base_dir):
                auid = str(acc.get('uid') or '')
                nick = str(acc.get('nickname') or '')
                if not auid or not nick:
                    continue
                nick_by_uid[auid] = nick
                prefix = auid[:8]
                if prefix in nick_by_prefix and nick_by_prefix[prefix] != nick:
                    ambiguous.add(prefix)
                else:
                    nick_by_prefix[prefix] = nick
    except Exception:  # noqa: BLE001
        pass

    def resolve(uid: str) -> str:
        if not uid:
            return ''
        if uid in nick_by_uid:
            return nick_by_uid[uid]
        prefix = uid[:8]
        return '' if prefix in ambiguous else nick_by_prefix.get(prefix, '')

    return resolve


def _realm_uid_filter(realm: str | None) -> dict[str, str] | None:
    """构造「哪些 uid 属于该版本」的映射；realm 为空返回 None（不过滤）。

    日志表里只有 uid，没有 realm 列（本方案选择不改数据库），所以按
    账号表把 uid 映射到版本再筛。上游日志里的 uid 是前 8 位，这里同时
    支持完整 uid 与前缀两种匹配。

    注意：账号已被删除时其历史日志无从判断版本，一律按「不属于任何版本」
    处理——宁可少显示，也不要把国际版账号的日志挂到国内版视图下。
    """
    if not realm:
        return None
    r = 'global' if str(realm).strip().lower() == 'global' else 'cn'
    try:
        accounts = wb2api.list_auth_accounts()
    except Exception:  # noqa: BLE001
        return {}
    return {
        str(a.get('uid')): str(a.get('realm') or 'cn')
        for a in accounts
        if a.get('uid')
    }


def _uid_matches_realm(uid: str, realm_map: dict[str, str] | None, realm: str) -> bool:
    """该日志行的 uid 是否属于指定版本（realm_map 为 None 时恒 True）。

    **uid 为空的行属于所有版本**（issue #51）：轮次汇总行（`checkin done:
    total=.. ok=..`）与脚本行不属于任何账号，是「这一轮跑没跑」的唯一凭据。
    按「空 uid 不属于任何版本」处理会让它们在**每个**版本视图下都被筛掉——
    用户看到的现象是「自动签到没跑」，而实际是记录被藏了。

    与下面那段「删号的历史日志宁可不显示」不冲突：那种行的 uid **非空**
    （属于某个真实账号，只是账号已不在表里），仍按前缀匹配的老规矩处理。
    """
    if realm_map is None:
        return True
    u = str(uid or '')
    if not u:
        return True
    want = 'global' if str(realm).strip().lower() == 'global' else 'cn'
    full = realm_map.get(u)
    if full is not None:
        return full == want
    # 上游只给前 8 位：前缀唯一命中才算，命中多个视为不确定、不展示
    hits = {v for k, v in realm_map.items() if k[:8] == u[:8]}
    return len(hits) == 1 and hits.pop() == want


@router.get('/task-logs')
def task_logs(
    limit: int = 200,
    offset: int = 0,
    uid: str | None = None,
    kind: str | None = None,
    days: int | None = None,
    realm: str | None = None,
    user: dict = Depends(security.current_user),
) -> dict:
    """上游自动任务留痕（猫猫旅行 / 活跃上报 / 自动签到 / 保活）。

    上游把这些结果打在容器日志里，容器重建即丢失；本接口读取的是
    后台采集器解析后落库的记录，因此能长期保留并统计积分收益。

    分页返回：列表只取当前页，`total` 为**当前筛选下**的总数，
    概览 `stats` 也按同一时间范围统计，保证数字与列表一致。
    """
    logs = db.list_task_logs(limit=limit, uid=uid, kind=kind, offset=offset, days=days)

    # 按版本过滤（realm 为空则不过滤）。日志表无 realm 列，按账号表映射 uid
    realm_map = _realm_uid_filter(realm)
    if realm_map is not None:
        logs = [r for r in logs
                if _uid_matches_realm(str(r.get('uid') or ''), realm_map, realm)]

    resolve_nick = _nickname_resolver()
    for row in logs:
        # 结果文案中文化：数据库留英文原文（排查要看上游原话），
        # 接口额外给出 message_cn 供界面展示
        row['message_cn'] = tasklog.translate_message(row.get('message', ''))
        row['nickname'] = resolve_nick(str(row.get('uid') or ''))

    # `total` 是**当前筛选下**的条数（列表分页要用），而 `stats` 是**各类型**的
    # 汇总（筛选栏要用它显示每个类型的条数与总条数）。
    #
    # 两者口径必须分开，不能一起按 kind 过滤 —— 这里踩过坑（issue #35）：
    # 原先 realm 分支用 `list_task_logs(kind=kind, ...)` 重算 stats，于是
    # `stats.total` 变成了「当前筛选下」的条数。界面用 `stats.total > 0` 决定
    # 筛选栏是否渲染，点进一个没有记录的类型时它就变成 0 → **整条筛选栏消失**，
    # 用户再也切不回「全部」，只能刷新页面。
    # 也就是说：筛选栏的可见性绝不能依赖筛选结果本身。
    stats = db.task_log_stats(days=days)
    total = db.count_task_logs(uid=uid, kind=kind, days=days)
    if realm_map is not None:
        # 版本过滤只能按行做（日志表没有 realm 列）。注意这里**不带 kind**：
        # 要的就是「该版本下所有类型」的分布。
        all_rows = db.list_task_logs(limit=2000, uid=uid, kind=None, offset=0, days=days)
        kept = [r for r in all_rows
                if _uid_matches_realm(str(r.get('uid') or ''), realm_map, realm)]
        by_kind: dict[str, dict] = {}
        for r in kept:
            k = str(r.get('kind') or '')
            slot = by_kind.setdefault(k, {'count': 0, 'credits': 0})
            slot['count'] += 1
            slot['credits'] += int(r.get('credits') or 0)
        stats = {
            'by_kind': by_kind,
            'total': len(kept),
            'total_credits': sum(v['credits'] for v in by_kind.values()),
        }
        # total 同样要按版本口径重算（带 kind），与列表一致
        scoped = ([r for r in kept if str(r.get('kind') or '') == kind]
                  if kind else kept)
        total = len(scoped)

    return {
        'logs': logs,
        'total': total,
        'stats': stats,
        'kinds': tasklog.KIND_LABELS,
        'collector': tasklog.state(),
    }


@router.post('/task-logs/collect')
async def collect_task_logs(user: dict = Depends(security.require_admin)) -> dict:
    """立即采集一次（不等后台轮询），便于刚跑完任务就看结果。"""
    parsed, added = await tasklog._collect_once()
    return {'ok': True, 'parsed': parsed, 'added': added}


@router.post('/task-logs/clear')
def clear_task_logs(user: dict = Depends(security.require_session_admin)) -> dict:
    db.clear_task_logs()
    return {'ok': True}


# 已完成「有副作用的供给」（签到 / 国际版 trial）的扫码 state。
#
# 为什么需要（issue #26 的连带问题）：落盘失败时我们**故意不丢 state**，好让
# 用户重试不用重新扫码；但前端是每 2 秒轮询同一个 state，而 poll_login 每次都
# 会返回 ready —— 不记一笔的话，每次轮询都会重跑签到与领 trial：重复写签到
# 记录、重复调腾讯接口。仅供「落盘失败 → 重试」这个短窗口使用，随 state 的
# 5 分钟 TTL 一起过期（服务重启后 state 本身也失效，标记无意义）。
_provisioned_states: dict[str, float] = {}


# ── 成长任务一键执行（issue #19）─────────────────────────────
# 调用上游自带的 scripts/task_runner.py。三种模式按风险分级，见 taskrun 模块注释：
# preview 只读 / claim 幂等领奖 / full 点亮+领奖（会伪造上报）。
# 全部仅管理员可用 —— 这些操作会对账号发起真实写请求。


@router.get('/task-run')
def task_run_status(user: dict = Depends(security.require_admin)) -> dict:
    """当前/上次执行状态与输出尾部（界面轮询）。"""
    return taskrun.status()


@router.post('/task-run')
async def task_run_start(
    body: dict = Body(...),
    user: dict = Depends(security.require_admin),
) -> dict:
    """启动一次执行。body: {mode, target}。

    `full`（点亮 + 领奖）会伪造活跃上报，因此要求显式传 `confirm: true` ——
    与「领奖」区分开，避免手滑点到风险最高的那个。
    """
    mode = str(body.get('mode') or '').strip()
    target = str(body.get('target') or 'ALL').strip() or 'ALL'
    if mode not in ('preview', 'claim', 'full'):
        raise HTTPException(status_code=400, detail='模式只能是 preview / claim / full')
    if mode == 'full' and body.get('confirm') is not True:
        raise HTTPException(
            status_code=400,
            detail='「点亮任务」会向腾讯发送活跃上报（造画布、连发对话等），'
                   '请先确认：该操作有风控风险，建议先用「预览」看清将要做的事。',
        )
    # 走线程池版本：start() 内部会解析脚本路径（脚本缺失时还要 fork docker
    # 提取，最长 60 秒），本接口是 async，同步跑它会冻住整个事件循环——
    # 连带把对外网关一起卡住。
    ok, msg = await taskrun.start_async(mode, target)
    if not ok:
        # 前置拒绝（脚本缺失/已有任务在跑/参数不合法）用 409 表达「状态冲突」，
        # 与「请求本身有错」（400）区分开。
        raise HTTPException(status_code=409, detail=msg)
    return {'ok': True, 'message': msg}


@router.post('/task-run/stop')
async def task_run_stop(user: dict = Depends(security.require_admin)) -> dict:
    stopped = await taskrun.stop()
    return {'ok': stopped, 'message': '已停止' if stopped else '当前没有正在执行的任务'}


@router.get('/task-claim-schedule')
def task_claim_schedule_get(user: dict = Depends(security.require_admin)) -> dict:
    """定时领奖配置（仅幂等领奖，不含点亮）。"""
    return taskrun.get_schedule()


@router.put('/task-claim-schedule')
def task_claim_schedule_put(
    body: dict = Body(...),
    user: dict = Depends(security.require_admin),
) -> dict:
    """保存定时领奖配置。"""
    try:
        return taskrun.set_schedule(body.get('enabled'), body.get('hours'))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get('/upstream/logs')
def upstream_logs(limit: int = 200, user: dict = Depends(security.current_user)) -> dict:
    """上游容器日志中与自动任务相关的行（原始日志）。

    上游只在失败时打日志、成功大多静默（旅行与活跃上报除外，它们会记录
    积分与服务端判据）。结构化、可长期保留的记录见 /api/task-logs。
    """
    lines = wb2api.read_container_logs(limit=limit)
    keywords = ('checkin', 'keepalive', 'user-resource', 'travel', 'activity')
    interesting = [
        # 去掉 docker --timestamps 前缀（上游自己已带秒级时间，重复显示反而难读）
        tasklog.strip_docker_ts(ln)
        for ln in lines
        if any(k in ln.lower() for k in keywords)
    ]
    return {'available': lines != [], 'lines': interesting, 'total': len(lines)}


@router.post('/accounts/{filename}/test')
async def account_test(filename: str, upstream_id: int | None = Query(None),
                       user: dict = Depends(security.require_admin)) -> dict:
    raw = _load(filename, _require_dir(_group(upstream_id)))
    acct = raw.get('account') or {}
    auth = raw.get('auth') or {}
    # 探测需要 uid / enterpriseId / domain 以复刻上游请求头，
    # device_token 用于 X-Device-Token（上游 chat 域同样注入）
    ok, message = await tencent.probe_account({
        'access_token': auth.get('accessToken', ''),
        'uid': acct.get('uid', ''),
        'enterprise_id': acct.get('enterpriseId', ''),
        'domain': auth.get('domain', ''),
        'realm': auth.get('realm'),
        'device_token': str(raw.get('device_token') or ''),
        'proxy': str(raw.get('proxy') or ''),
    })
    return {'ok': ok, 'message': message}


@router.post('/accounts/{filename}/refresh')
async def account_refresh(filename: str, upstream_id: int | None = Query(None),
                          user: dict = Depends(security.require_admin)) -> dict:
    """刷新该账号的 accessToken（issue #40）。

    **这里真的去续期了**。此前这个端点只是 `reload.restart_now()`——触发一次
    上游容器重载，而重载既不会刷新 token、也不会改变任何凭证。用户以为按钮会
    续期、实际什么也没发生（issue #40 报的正是这个），属于**承诺与行为不符**。

    上游没有对外暴露刷新接口（其路由表见 `tencent.refresh_token` 的说明），
    刷新只发生在它自己的保活排程与选号路径里。所以这里直接实现协议本身。

    流程：读账号 → 调腾讯刷新接口 → **原子写回**新 token（保留 device_token
    等未知字段）→ 触发一次上游重载让新凭证立刻生效（不重载的话上游要等
    auths 热加载轮询，旧上游甚至要等重启）。
    """
    group = _group(upstream_id)
    base_dir = _require_dir(group)
    raw = _load(filename, base_dir)
    auth = raw.get('auth') or {}
    acct = raw.get('account') or {}
    if not auth.get('accessToken'):
        return {'ok': False, 'message': '该账号无有效 accessToken'}
    if not str(auth.get('refreshToken') or '').strip():
        return {'ok': False, 'message': '该账号没有 refreshToken —— 只能重新扫码或登录'}

    ok, message, fields = await tencent.refresh_token({
        'access_token': auth.get('accessToken', ''),
        'refresh_token': auth.get('refreshToken', ''),
        'uid': acct.get('uid', ''),
        'enterprise_id': acct.get('enterpriseId', ''),
        'domain': auth.get('domain', ''),
        'realm': auth.get('realm'),
        'device_token': str(raw.get('device_token') or ''),
        'proxy': str(raw.get('proxy') or ''),
    })
    if not ok:
        return {'ok': False, 'message': message}

    try:
        tencent.update_auth_tokens(filename, fields, base_dir)
    except (ValueError, FileNotFoundError, OSError) as exc:
        # 刷新成功但写不进去：如实说清。**不能报成功**——用户以为续期了，
        # 而磁盘上还是旧 token，重启后又变回过期状态，比直接失败更难查。
        return {'ok': False,
                'message': f'{message}，但写入账号文件失败：{exc}（有效期未保存）'}

    # restart_now() 返回 (ok, message) 二元组，必须解包：直接当布尔用会因为
    # 非空元组恒为真，从而在重载失败时仍报「已重载生效」（且 reload_triggered
    # 会变成数组、与前端声明的 boolean 不符）。
    reloaded, reload_error = await reload.restart_now(upstream=_reload_target(group))
    if reloaded:
        tail = '，上游已重载生效'
    elif group['is_default'] or str(group.get('container') or '').strip():
        tail = f'；上游重载失败：{reload_error}，请在宿主机重启上游容器'
    else:
        # 分组没登记容器名：不猜也不冒报错——上游的 auths 热加载会自己收录
        tail = f'；{reload_error}；新凭证将由该分组的上游热加载（约 5 秒）自动收录'
    return {
        'ok': True,
        'message': message + tail,
        'reload_triggered': reloaded,
        'expires_at': fields.get('expires_at'),
    }


@router.post('/accounts/{filename}/clear-cooling')
async def account_clear_cooling(
    filename: str,
    upstream_id: int | None = Query(None),
    user: dict = Depends(security.require_admin),
) -> dict:
    """强制退出账号级冷却、熔断/降权和模型级限流状态。

    上游没有提供清除运行态冷却的管理接口，而 state.json 每 5 秒会被内存
    Flush 覆盖。因此这个动作必须：停上游 → 原子修改目标账号 → 启上游 →
    验证实时状态。只改目标 uid，不碰凭证、积分、禁用位或其它账号。
    """
    group = _group(upstream_id)
    if not group['is_default']:
        # 清冷却要「停上游 → 改 state.json → 启上游」。分组的登记信息里没有
        # state.json 的路径（只有地址 / 密钥 / 账号目录），去改默认实例的
        # state.json 是**改错对象**——那种「成功」比失败更危险。明确拒绝。
        raise HTTPException(
            status_code=409,
            detail=('该分组暂不支持从面板清除冷却（需要该实例的 state.json 路径）；'
                    '可等它自然冷却，或在宿主机上重启该分组的上游实例'),
        )
    try:
        raw = wb2api.read_account_file_any(filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail='账号文件不存在') from exc
    uid = str((raw.get('account') or {}).get('uid') or '').strip()
    if not uid:
        raise HTTPException(status_code=400, detail='该账号文件缺少 uid，无法定位上游状态')

    ok, message, detail = await wb2api.force_clear_account_cooling(uid)
    return {'ok': ok, 'message': message, **(detail or {})}


@router.put('/accounts/{filename}/note')
async def account_set_note(
    filename: str,
    body: dict = Body(...),
    upstream_id: int | None = Query(None),
    user: dict = Depends(security.require_admin),
) -> dict:
    """给账号写一句备注（issue #67）——比如「张叔叔」「备用号」「给小李用的」。

    为什么需要：用手机号邀请注册的账号，昵称往往认不出是谁，删号时不知道该删哪个。

    存法见 `db.account_notes` 的注释：**按 uid** 存在本端库里（不写进上游的账号
    文件——那是上游按自己 schema 读写的文件，塞自定义字段会被它覆盖或超出 schema）。
    uid 是账号的稳定标识，所以临时停用（改文件名）不会让备注丢。

    空串 = 删除备注（不留空行）。
    """
    try:
        raw = wb2api.read_account_file_any(
            filename, _require_dir(_group(upstream_id)))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail='账号文件不存在') from exc
    uid = str((raw.get('account') or {}).get('uid') or '').strip()
    if not uid:
        raise HTTPException(status_code=400, detail='该账号文件缺少 uid，无法保存备注')

    # 先把**所有空白收起成单个空格**（审查补）：备注在列表里是单行展示 + 悬停看
    # 全文，粘进来的多行文本（或中间一串空格）会让它看起来像坏数据；顺带把
    # 「只有换行/空格」的输入归成空串 = 清除。
    #
    # 截断而不是拒绝：备注是给人看的短文本，粘多了不该报错丢掉整句。
    # 上限取 100 字符（界面上也是这个 maxLength），够写清是谁/做什么用。
    note = ' '.join(str(body.get('note') or '').split())[:100]
    if note:
        db.set_account_note(uid, note)
    else:
        db.delete_account_note(uid)
    return {'ok': True, 'uid': uid, 'note': note}


@router.delete('/accounts/{filename}')
async def account_delete(filename: str, upstream_id: int | None = Query(None),
                         user: dict = Depends(security.require_session_admin)) -> dict:
    group = _group(upstream_id)
    try:
        removed = wb2api.delete_auth_account(filename, _require_dir(group))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail='账号文件不存在')
    reload.request_restart(upstream=_reload_target(group))
    return {'success': True}


@router.post('/accounts/{filename}/move')
async def account_move(
    filename: str,
    body: dict = Body(...),
    upstream_id: int | None = Query(None),
    user: dict = Depends(security.require_session_admin),
) -> dict:
    """把一个账号**转移**到另一个分组（「默认分组的账号移进各组」的核心动作）。

    语义与边界：
      · 源分组 = upstream_id（缺省默认分组）；目标 = body['to_upstream_id']；
      · 移动的是**账号文件本身**（含 `.json.disabled` 形态），凭证一个字节不改；
      · 目标已有同名文件时拒绝（409）——覆盖等于丢掉目标那份凭据；
      · 两侧都必须是登记过本地账号目录的分组（否则明确 409，绝不猜路径）。
    生效方式：文件落到目标目录后，目标上游的 auths 热加载（约 5 秒）会收录
    它；这里走 request_reload_or_restart——新上游不必等，旧版本回退重启。
    """
    src = _group(upstream_id)
    to_raw = body.get('to_upstream_id')
    if 'to_upstream_id' not in body or to_raw is None:
        raise HTTPException(
            status_code=400,
            detail='to_upstream_id 必填（要移动到哪个分组；0 = 默认分组）',
        )
    try:
        # 0 是**显式**的「默认分组」写法；「没填」（None / 键缺失）必须报错 ——
        # 两者语义不同，把「没填」当默认池会把账号悄悄移错组。
        to_id = None if to_raw in (0, '0') else int(to_raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail='to_upstream_id 必须是分组 id') from None
    dst = _group(to_id)
    same = (src.get('id') is None and dst.get('id') is None) or (
        src.get('id') is not None and src.get('id') == dst.get('id'))
    if same:
        raise HTTPException(status_code=400, detail='源分组与目标分组相同，无需移动')
    src_dir = _require_dir(src)
    dst_dir = _require_dir(dst)

    # 找到磁盘上的真实文件（两种形态都试，与 read_account_file_any 同口径）
    name = str(filename)
    candidates = ([name, name[: -len('.disabled')]] if name.endswith('.disabled')
                  else [name, name + '.disabled'])
    src_path: Path | None = None
    for cand in candidates:
        cand_path = wb2api._safe_file(cand, src_dir)
        if cand_path.exists():
            src_path = cand_path
            break
    if src_path is None:
        raise HTTPException(status_code=404, detail='账号文件不存在')

    dst_path = dst_dir / src_path.name
    if dst_path.exists():
        raise HTTPException(
            status_code=409,
            detail=(f'目标分组「{dst["name"]}」里已有同名文件 {src_path.name}；'
                    '请先处理它（改名或删除）再移动，避免覆盖掉那份凭据'),
        )
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_path), str(dst_path))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f'移动失败：{exc}') from exc

    uid = ''
    try:
        moved = json.loads(dst_path.read_text(encoding='utf-8'))
        uid = str((moved.get('account') or {}).get('uid') or '')
    except Exception:  # noqa: BLE001 —— 读不出 uid 不影响移动，只是少了重载收尾
        uid = ''
    reload.request_reload_or_restart(uid, upstream=_reload_target(dst))
    return {
        'ok': True,
        'file': src_path.name,
        'uid': uid,
        'from': {'id': src['id'], 'name': src['name']},
        'to': {'id': dst['id'], 'name': dst['name']},
    }


@router.post('/restart')
async def restart(upstream_id: int | None = Query(None),
                  user: dict = Depends(security.require_session_admin)) -> dict:
    group = _group(upstream_id)
    ok, message = await reload.restart_now(upstream=_reload_target(group))
    return {'ok': ok, 'message': message}


def _fallback_why(bit_code: str, disabled: bool, group: dict | None = None) -> str:
    """回退到改名方式时，把「为什么没走状态位」说到可操作（issue #45 追问）。

    `no_route` 有两种成因，界面上必须分得开：

      · 配置里**没开** → 给出开关位置；
      · 配置里**已开** → 说明运行中的上游没加载到它：上游只在启动时读这个开关，
        改完必须**重启容器**；若已重启仍如此，就是镜像太旧（早于 2026-09-19）。
        这条文案里带上**面板实际读的配置路径**——用户手改的常常是另一个文件
        （实测反馈：「明明上游已经打开了 admin.enabled 还是不行」）。

    `disabled` 决定文案的落点：**停用**要说清代价并给出「重新停用」的下一步；
    **启用**时账号已经恢复，再说「再重新停用」是说不通的（用户点的是启用），
    只提示「以后想让它保留签到与保活，去哪儿开开关」。
    """
    if bit_code != 'no_route':
        return ''
    if disabled:
        tail = '已改用改名方式：账号将完全退出账号池，签到与保活也会一并停止。'
        next_step = ('若要保留签到与保活，请到「设置 → 账号管理接口」开启后'
                     '重启上游容器，再重新停用')
    else:
        tail = '已改用改名方式启用（该方式下账号退出账号池，任务也不执行）。'
        next_step = ('若希望以后停用时保留签到与保活，请到「设置 → 账号管理接口」'
                     '开启后重启上游容器')
    if group is not None and not group.get('is_default'):
        # 分组的上游配置文件路径不在登记信息里：这里**不能**引用默认实例的
        # 配置路径（用户照着改的是另一个文件，改完仍无效）。
        return (f'（该分组「{group["name"]}」的上游未提供管理接口：' + tail
                + '；如需「只摘流量、任务照常」的语义，请在这个分组自己的上游'
                '实例里开启 admin 后重启它）')
    enabled, where = wb2api.admin_enabled_in_config()
    if enabled:
        return (f'（上游配置里已开启管理接口（{where}），但运行中的上游没有提供它：'
                '上游只在启动时读这个开关，改完配置需要重启上游容器才生效；'
                '若已重启仍如此，说明上游镜像早于 2026-09-19。' + tail + '）')
    if enabled is None:
        return f'（{where}。' + tail + next_step + '）'
    return '（该上游未启用管理接口，' + tail + next_step + '）'


@router.post('/accounts/{filename}/disabled')
async def account_set_disabled(
    filename: str,
    body: dict = Body(...),
    upstream_id: int | None = Query(None),
    user: dict = Depends(security.require_admin),
) -> dict:
    """临时停用 / 启用一个账号（issue #21、#45）。

    两种机制，**优先用上游状态位**：

      · `manual_disabled`（上游 2026-09-19 起的新接口）——语义是「对话流量摘除」
        而非「账号冻结」：不被选中转发，但**签到 / token 保活 / 猫猫旅行照常执行**，
        凭证与积分都是活的。这才是「先停一会儿」想要的语义。
      · **改文件名**（加 `.disabled` 后缀，本面板旧实现）——账号完全退出账号池，
        连排程任务都不再跑。副作用过大：积分不再增长、token 不再续期，回来时
        可能已经过期。仅作为回退。

    为什么必须保留回退：上游那组接口**默认不注册**（`admin.enabled` 默认 false，
    关闭时一律 404——其注释写明是「不向外暴露管理面」的有意设计），旧版本更是
    根本没有。没有回退的话，这些部署上「停用」按钮会直接失效。

    body: {disabled: bool, reload: bool, reason: str}。
    `reload` 默认 true，**只对回退的改名路径有意义**（旧上游不监听文件变化，
    不重载就不生效；新上游有 5 秒热加载，重载只是为了不等那 5 秒）。
    状态位路径不需要重载——它走的是上游自己的接口。要批量操作时可先传 false。
    """
    if not isinstance(body.get('disabled'), bool):
        raise HTTPException(status_code=400, detail='disabled 必须是布尔值')
    disabled = bool(body['disabled'])
    reason = str(body.get('reason') or '').strip() or '面板手动停用'
    group = _group(upstream_id)
    base_dir = _require_dir(group)

    # uid 用于调上游的状态位接口。用 any 版本读：用户手里的文件名可能与磁盘
    # 形态不一致（刚停用/刚启用时界面仍持旧名），只按一个名字读会解析不出 uid，
    # 于是状态位那条路被静默跳过——「点了启用但没恢复」正是这么来的。
    uid = ''
    try:
        uid = str((wb2api.read_account_file_any(filename, base_dir).get('account') or {}).get('uid') or '')
    except Exception:  # noqa: BLE001
        uid = ''

    reloaded = False
    bit_msg = ''
    bit_code = 'skipped'

    if disabled:
        # 停用：先试状态位（语义更精确，且不影响任务）；不行再改名。
        if uid:
            ok, bit_msg, bit_code = await wb2api.set_manual_disabled(
                uid, True, reason, base_url=group['base_url'],
                api_key=upstreamsvc.forward_api_key(group))
            if ok:
                return {
                    'ok': True,
                    'file': filename,
                    'disabled': True,
                    'changed': True,
                    'via': 'manual_disabled',
                    'reload_triggered': False,  # 状态位由上游自己管，无需重载
                    'message': f'已停用该账号（{bit_msg}）',
                }
        try:
            result = wb2api.set_account_disabled(filename, True, base_dir)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if result['changed'] and body.get('reload', True) is not False:
            reloaded = reload.request_restart(upstream=_reload_target(group))
    else:
        # 启用：两条路都要清。先解掉改名标记（本地、幂等），再清状态位——
        # 两种机制理论上不会同时命中同一个账号，但手工改过文件、或跨版本
        # 升级后就可能出现叠加态，一起清掉才能保证「点了启用就真的启用」。
        try:
            result = wb2api.set_account_disabled(filename, False, base_dir)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        renamed_back = bool(result['changed'])
        if renamed_back and body.get('reload', True) is not False:
            reloaded = reload.request_restart(upstream=_reload_target(group))
        if uid:
            base_url = group['base_url']
            api_key = upstreamsvc.forward_api_key(group)
            ok, bit_msg, bit_code = await wb2api.set_manual_disabled(
                uid, False, base_url=base_url, api_key=api_key)
            if ok:
                # 「启用」要清的是**两位**，上面那步只清了 manual_disabled。
                # 上游的 enable **不管系统自动禁用**（其 admin.go 原话：「若账号仍被
                # 系统自动禁用（disabled），它不会因此回到选号池——那需要 revive」）。
                # 面板此前从不调 revive，于是被自动禁用的账号（12153 连败 3 次、
                # 或 11140 被封）点「启用」看着成功、实际还在池外 —— 这正是用户报的
                # 「重新登录后连通性测试通过、账号池就是调不动」。
                revived, revive_msg, still_disabled = await _revive_if_disabled(
                    uid, base_url=base_url, api_key=api_key)
                if still_disabled:
                    # 两位都清了才算真的启用。清不掉就**如实说**，不再像以前那样
                    # 无条件回一句「已启用」——那正是用户被误导的来源。
                    return {
                        'ok': False,
                        'file': result['file'],
                        'disabled': True,
                        'changed': True,
                        'via': 'manual_disabled',
                        'reload_triggered': reloaded,
                        'bit_code': bit_code,
                        # 文案走 toast（纯文本），别用 markdown 记号
                        'message': (f'已解除面板停用，但该账号仍被上游系统禁用，'
                                    f'现在仍不会被调用。{revive_msg}'),
                    }
                return {
                    'ok': True,
                    'file': result['file'],
                    'disabled': False,
                    'changed': True,
                    'via': 'manual_disabled',
                    'reload_triggered': reloaded,
                    'revived': revived,
                    # 两位都清了才算真的回到池子：`still_disabled is None` 表示这一趟
                    # **没读到**上游状态，那就别把话说满（上面 True 的分支已如实报失败）。
                    'message': (f'已启用该账号（{bit_msg}'
                                + ('；并已解除系统禁用' if revived else '')
                                + ('；但读不到上游状态，未能确认它是否仍被系统禁用'
                                   if still_disabled is None else '')
                                + '）')
                               + ('，正在重载上游使其生效' if reloaded else ''),
                }
            # 状态位没走通（多半是上游没开管理接口）。这时**必须先看一眼**它是不是
            # 被系统禁用了：若是，下面的改名回退救不了它 —— 改名只管「文件在不在
            # 池里」，管不了上游的 disabled 位；而 revive 与 enable 同属那组没注册
            # 的接口，所以此刻确实无解。如实说清，别跟着兜底文案一起报「已启用」。
            still = await wb2api.account_disabled(uid, base_url=base_url, api_key=api_key)
            if still:
                return {
                    'ok': False,
                    'file': result['file'],
                    'disabled': True,
                    'changed': renamed_back,
                    'via': 'rename',
                    'reload_triggered': reloaded,
                    'bit_code': bit_code,
                    'message': ('该账号被上游系统禁用，而解除它需要上游的「账号管理接口」；'
                                '当前上游没有提供这组接口，面板无法把它放回账号池。'
                                + _fallback_why(bit_code, False, group)),
                }

    return {
        'ok': True,
        **result,
        # 生效方式：manual_disabled = 上游状态位（任务照常）；rename = 改名（回退）
        'via': 'rename',
        # 状态位没走通的原因码（no_route / not_found / error / skipped），
        # 前端据此区分「上游没开管理接口」（给开启提示）与真失败。
        'bit_code': bit_code,
        'bit_message': bit_msg,
        # 是否已触发上游重载。未触发时调用方要自己重启，否则改名不生效。
        'reload_triggered': reloaded,
        'message': (
            f"已{'停用' if disabled else '启用'}该账号"
            # 回退路径要如实说清代价：这条路会让账号退出账号池，任务也停。
            # 回退路径要如实说清代价，并给出**可操作的下一步**：「该上游未启用管理
            # 接口」只说了现状，用户不知道去哪儿开（实测反馈正是这个——看到提示后
            # 只能来问）。所以带上开关位置与生效条件。
            + _fallback_why(bit_code, disabled, group)
            + ('，正在重载上游使其生效' if reloaded
               else ('；请手动重启上游以生效' if result.get('changed') else ''))
        ),
    }
