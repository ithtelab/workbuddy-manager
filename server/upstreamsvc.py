"""多上游接入点（账号池分组）的配置、校验与解析。

背景：本服务是**转发型**反代 —— 请求最终由上游的账号池承接，账号是上游挑的，
本端只做鉴权 / 限流 / 记日志。于是「让不同下游密钥走不同账号池」在本端唯一能
落地的形态是：**配置多个上游接入点，密钥绑定其中之一**；每个接入点天然就是
一个账号池分组（可以是另一个上游实例，也可以是同一个上游的另一个 api_key）。

三条设计约定（评审时最需要先看的部分）：

1. **默认上游不是数据库里的一行**，而是运行时读出来的 `WB2API_BASE` +
   `upstream_api_key()`。因此存量部署升级后无需任何迁移动作：密钥的
   `upstream_id` 为空 = 走默认上游 = 与升级前逐字相同的行为。
2. **绑定了上游的密钥，绝不静默回落**：上游被删 / 停用后，该密钥的请求一律
   明确报错（`UpstreamUnavailable`）。静默回落到默认上游等于**隔离失效**——
   用户以为自己发出去的密钥只能碰 A 组账号，实际却打到了默认上游的全池账号，
   而这种错误不报出来是没人会发现的。
3. **被密钥引用的上游拒绝删除**（409 由路由层回）：删除那一刻没人用，不代表
   安全 —— 更常见的场景是管理员忘了还有几把密钥绑着它。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from . import config, db

# 默认上游的 id 约定：None（而不是某个魔法数字）。
# 为什么不用 0：0 在 SQL 里是合法 id，前端序列化时 '' / 0 / null 极易混淆，
# 而 None 在 JSON 里就是 null，语义唯一。
DEFAULT_ID = None

MAX_NAME = 64
MAX_NOTE = 200
MAX_URL = 500
MAX_PATH = 500

# 上游接入点的 provider 类型。唯一定义处（全仓库的判定只写 `== 'qoder'`）：
# **禁止**写 `!= 'codebuddy'` 之类的放行条件——未知/脏值必须落回 codebuddy 原路径
# （fail-safe 方向：新分支只对显式声明者生效，存量部署升级后行为逐字不变）。
PROVIDERS: tuple[str, ...] = ('codebuddy', 'qoder')
DEFAULT_PROVIDER = 'codebuddy'
# 上游的版本归属取值（仅 provider='qoder' 时消费）：'' = 不限；
# 'cn'/'global' 与密钥的 realm 做「密钥×上游」匹配（见 resolve_for_key）。
QODER_REALMS: tuple[str, ...] = ('', 'cn', 'global')


class UpstreamUnavailable(Exception):
    """密钥绑定的上游不可用（已被删除或已停用）。

    单独一个异常类型而不是返回 None：调用方必须显式决定怎么处理，而不是
    不小心走到「拿不到就回落默认上游」那条**隔离失效**的路径（见模块 docstring）。
    """


def _clean(value: object, limit: int) -> str:
    return str(value or '').strip()[:limit]


def normalize_base_url(value: object) -> str:
    """归一化 Base URL：去掉末尾斜杠，必须是 http(s)://。

    为什么在这里拦：转发侧是 `f'{base}{path}'` 的字符串拼接，写错一个字符
    （少 http://、末尾多个斜杠、粘成 `http://host/v1`）都会变成请求另一个地址
    或 404 —— 而且只有真正调用时才暴露。写入时拒掉，报错里带着原文。
    """
    url = _clean(value, MAX_URL)
    if not url:
        raise ValueError('上游地址不能为空')
    if not url.startswith(('http://', 'https://')):
        raise ValueError(f'上游地址必须以 http:// 或 https:// 开头：{url}')
    return url.rstrip('/')


def normalize_auth_dir(value: object) -> str:
    """分组的本地账号目录：空 = 不管理；非空必须是**绝对路径**。

    这个值会直接进入文件系统操作（列目录、改文件名、移动账号文件），而面板
    进程的 cwd 与上游部署目录无关——相对路径会解析到不可预期的位置，故障现场
    也极难判断「它到底读了哪个目录」。部署约定本来就是「两边看到同一个绝对
    路径」（同机 / bind mount），所以只接受绝对路径。
    """
    s = _clean(value, MAX_PATH)
    if not s:
        return ''
    if not Path(s).is_absolute():
        raise ValueError(f'账号目录必须是绝对路径（面板按该路径读写账号文件）：{s}')
    return s


def normalize_container(value: object) -> str:
    """分组的容器名：空 = 不可从面板重启；非空必须符合 docker 名称字符集。

    为什么校验：这个名字会被拼进 `docker restart <name>`。脏字符轻则让 docker
    报错，重则变成对另一个对象的操作（甚至被解析成 docker 选项）。宁可在保存
    时拒绝，也不在重启时猜。
    """
    s = _clean(value, MAX_NAME)
    if not s:
        return ''
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,62}', s):
        raise ValueError(f'容器名不合法（只允许字母、数字、_ . -，且不以符号开头）：{s}')
    return s


def normalize_provider(value: object) -> str:
    """归一化上游类型；非法值直接报错（**不静默兜底**）。

    为什么与 realm 的「吃掉脏值」方向相反：上游类型是**结构性选择**——它决定
    模型清单裁剪、版本门、探活三处行为。猜一个（比如把拼错的 `qoder ` 兜成
    codebuddy）会把一个本该是 qoder 的接入点悄悄接进 CodeBuddy 池，调用全部
    401/404，而用户看到的是「保存成功了」。宁可当场报错让管理员改。
    """
    v = _clean(value, 32).lower()
    if v not in PROVIDERS:
        raise ValueError(f'上游类型必须是 {PROVIDERS} 之一：{v!r}')
    return v


def normalize_qoder_realm(value: object) -> str:
    """归一化上游的版本归属（仅 qoder 上游消费）；空 = 不限。

    同样不静默兜底：'cn' 拼成 'CN ' 之外的任何脏值都会让「密钥×上游」匹配
    变成一把没人能预测的锁，保存时拒掉比转发时猜要便宜。
    """
    v = _clean(value, 32).lower()
    if v not in QODER_REALMS:
        raise ValueError(f'版本归属必须是 {QODER_REALMS} 之一（空 = 不限）：{v!r}')
    return v


def _row_to_dict(row) -> dict:
    return {
        'id': int(row['id']),
        'name': str(row['name']),
        'base_url': str(row['base_url']),
        'api_key': str(row['api_key'] or ''),
        'note': str(row['note'] or ''),
        'enabled': bool(row['enabled']),
        'auth_dir': str(row['auth_dir'] or ''),
        'container': str(row['container'] or ''),
        # 新键只增不改：`or` 兜底迁移前的 NULL（ALTER 加列对已有行回填默认值，
        # 但极端情况下库可能被旧版本进程短写过）——兜底值与列默认值一致。
        'provider': str(row['provider'] or DEFAULT_PROVIDER),
        'realm': str(row['realm'] or ''),
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
        'is_default': False,
    }


def default_upstream() -> dict:
    """默认上游（环境变量 / 上游 config.json），永远可用、不可编辑、不可删除。"""
    return {
        'id': DEFAULT_ID,
        'name': '默认上游',
        'base_url': config.WB2API_BASE,
        'api_key': config.upstream_api_key(),
        'note': '来自 WB2API_BASE 与上游 config.json（未配置多上游时使用的接入点）',
        'enabled': True,
        # 默认分组的账号目录即部署时的 WB_AUTH_DIR —— 只读展示，不由界面修改
        # （改了它只会让面板与上游看到两个目录，属于配置漂移而不是功能）。
        'auth_dir': str(config.AUTH_DIR),
        'container': config.WB2API_CONTAINER,
        # 默认上游永远是 CodeBuddy 那一套（WB2API_BASE）：qoder 只能以显式
        # 建行的上游接入——它的桥地址与桥 token 都是配置项，没有「环境变量
        # 默认值」可言，硬编码任何一个都是错的。
        'provider': DEFAULT_PROVIDER,
        'realm': '',
        'created_at': None,
        'updated_at': None,
        'is_default': True,
    }


def get_upstream(upstream_id: object) -> dict | None:
    try:
        uid = int(upstream_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    row = db.query_one('SELECT * FROM upstreams WHERE id = ?', (uid,))
    return _row_to_dict(row) if row else None


def list_upstreams(*, include_default: bool = True) -> list[dict]:
    """全部上游：默认上游排在最前（id=None），其余按创建顺序。

    顺序稳定是有意的：前端下拉的「默认」总在第一位，而新增的上游不会突然插到
    最前面把已有选择顶掉。
    """
    out: list[dict] = []
    if include_default:
        out.append(default_upstream())
    for row in db.query('SELECT * FROM upstreams ORDER BY id ASC'):
        out.append(_row_to_dict(row))
    return out


def create_upstream(name: object, base_url: object, api_key: object = '',
                    note: object = '', enabled: object = True,
                    auth_dir: object = '', container: object = '',
                    provider: object = DEFAULT_PROVIDER,
                    realm: object = '') -> dict:
    nm = _clean(name, MAX_NAME)
    if not nm:
        raise ValueError('上游名称不能为空')
    url = normalize_base_url(base_url)
    # 类型与版本归属先归一：后面「qoder 必填桥 token」的判定要用归一后的值。
    prov = normalize_provider(provider)
    up_realm = normalize_qoder_realm(realm) if prov == 'qoder' else ''
    key = _clean(api_key, MAX_URL)
    # qoder 上游的 api_key 是**桥接 token**（qoder2api bridge authMiddleware 收
    # Bearer/x-api-key），必填但**不硬编码默认值**——上游默认 token 是它自己的
    # 配置，面板侧必须显式填写（值可从 console /api/settings 读取回显到表单）。
    # codebuddy 侧 api_key 仍允许为空（不鉴权的自建实例），语义不同、不共用校验。
    if prov == 'qoder' and not key:
        raise ValueError('qoder 上游必须填写桥接 token（qoder2api 的 bridge_token）')
    now = int(time.time())
    uid = db.execute(
        'INSERT INTO upstreams(name, base_url, api_key, note, enabled, auth_dir, container, '
        'provider, realm, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (nm, url, key, _clean(note, MAX_NOTE),
         1 if enabled else 0, normalize_auth_dir(auth_dir), normalize_container(container),
         prov, up_realm, now, now),
    )
    row = db.query_one('SELECT * FROM upstreams WHERE id = ?', (uid,))
    return _row_to_dict(row)


def update_upstream(upstream_id: object, patch: dict) -> dict | None:
    """局部更新（PATCH 语义）：只改传进来的字段。

    与 keysvc.update_key 同口径——`'key' in patch` 判定而不是真值判定，
    这样「把 api_key 清空」是能生效的动作，而不是被 `or` 吞掉的空操作。
    """
    current = get_upstream(upstream_id)
    if current is None:
        return None
    fields: dict[str, object] = {}
    if 'name' in patch:
        nm = _clean(patch['name'], MAX_NAME)
        if not nm:
            raise ValueError('上游名称不能为空')
        fields['name'] = nm
    if 'base_url' in patch:
        fields['base_url'] = normalize_base_url(patch['base_url'])
    if 'api_key' in patch:
        fields['api_key'] = _clean(patch['api_key'], MAX_URL)
    if 'note' in patch:
        fields['note'] = _clean(patch['note'], MAX_NOTE)
    if 'enabled' in patch:
        fields['enabled'] = 1 if patch['enabled'] else 0
    if 'auth_dir' in patch:
        fields['auth_dir'] = normalize_auth_dir(patch['auth_dir'])
    if 'container' in patch:
        fields['container'] = normalize_container(patch['container'])
    if 'provider' in patch:
        fields['provider'] = normalize_provider(patch['provider'])
    if 'realm' in patch:
        # 与 api_key 的 `'key' in patch` 同口径：显式传 '' 是「清空 = 不限」，
        # 能生效的动作，不能被真值判断吞掉。
        fields['realm'] = normalize_qoder_realm(patch['realm'])
    if fields:
        # 「改成 qoder」时按 patch 值 ∪ current 值合并后判桥 token 非空：
        # 只传 provider 不传 api_key 的 PATCH 也要拦——否则一个没带 token 的
        # qoder 上游会静默生效，转发到 bridge 全 401，管理员对着网关日志排查。
        # codebuddy 行不经过这段（provider 只在显式传键时才变）。
        target_provider = str(fields.get('provider', current['provider']))
        if target_provider == 'qoder':
            target_key = str(fields.get('api_key', current['api_key']) or '')
            if not target_key.strip():
                raise ValueError('qoder 上游必须填写桥接 token（qoder2api 的 bridge_token）')
        fields['updated_at'] = int(time.time())
        assignments = ', '.join(f'{k} = ?' for k in fields)
        db.execute(f'UPDATE upstreams SET {assignments} WHERE id = ?',
                   (*fields.values(), current['id']))
    return get_upstream(current['id'])


def bound_key_count(upstream_id: object) -> int:
    """有多少把密钥绑在这个上游上（删除守卫与界面提示共用一份判据）。"""
    try:
        uid = int(upstream_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    row = db.query_one('SELECT COUNT(*) AS n FROM api_keys WHERE upstream_id = ?', (uid,))
    return int(row['n']) if row else 0


def key_counts() -> dict[object, int]:
    """每组上游各被多少把密钥引用：{id: n}，外加 ('default', n) 表示未绑定上游的密钥数。

    一次 GROUP BY 取回（而不是每个上游查一遍）：列表接口要逐行显示，N+1 查询在
    上游数不多时也能忍，但没必要——而「有多少钥匙在用默认池」这个数字同样有用。
    """
    counts: dict[object, int] = {}
    for row in db.query('SELECT upstream_id, COUNT(*) AS n FROM api_keys GROUP BY upstream_id'):
        uid = row['upstream_id']
        counts['default' if uid is None else int(uid)] = int(row['n'])
    return counts


def delete_upstream(upstream_id: object) -> tuple[bool, int]:
    """删除上游，返回 (是否删除, 引用它的密钥数)。

    **有密钥引用时拒绝删除**（返回 (False, n)，由路由层回 409）：删除本身是
    合法动作，但它会让那几把密钥立刻变成「绑定缺失」而全部 5xx——先把引用清掉，
    或者让管理员显式改绑，比事后排查一堆报错强。计数与删除在同一个事务里做，
    避免「刚数完就被新建的密钥绑上」的竞态。
    """
    try:
        uid = int(upstream_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False, 0
    with db._lock:
        # 连接在锁内取：PG 恢复会在锁内关闭并重开共享连接，锁外拿到的句柄
        # 可能在随后使用时已经被关掉。
        conn = db.connect()
        try:
            conn.execute('BEGIN')
            row = conn.execute('SELECT COUNT(*) AS n FROM api_keys WHERE upstream_id = ?',
                               (uid,)).fetchone()
            used = int(row['n']) if row else 0
            if used:
                conn.rollback()
                return False, used
            cur = conn.execute('DELETE FROM upstreams WHERE id = ?', (uid,))
            deleted = int(cur.rowcount or 0) > 0
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return deleted, 0


def resolve_for_key(key: dict | None) -> dict:
    """解析本次请求该用哪个上游：密钥绑定优先，未绑定走默认上游。

    绑定缺失 / 停用时抛 `UpstreamUnavailable` —— 原因见模块 docstring 第 2 条，
    这里只强调一句：**默认上游不是「兜底」，它只是「未绑定」时的正常去处**。
    """
    uid = (key or {}).get('upstream_id')
    if uid in (None, '', 0, '0'):
        return default_upstream()
    row = get_upstream(uid)
    if row is None:
        raise UpstreamUnavailable(
            f'密钥绑定的上游 #{uid} 不存在（可能已被删除）——'
            '请到「设置 → 上游」重新绑定，或改用未绑定上游的密钥')
    if not row['enabled']:
        raise UpstreamUnavailable(
            f'密钥绑定的上游「{row["name"]}」已停用 —— 启用它，或把密钥改绑到其它上游')
    # 「密钥×上游」版本匹配（仅 qoder 上游）：keysvc.validate 的版本门按模型名的
    # `global:` 前缀判版本，而 qoder 模型 id 无前缀、恒归 cn——global 密钥会被
    # 那道门误杀。qoder 的版本语义在**上游行**上，所以在这道门被跳过之后（见
    # keysvc.validate），由这里承担等价的隔离：key.realm 为空放行一切；上游
    # realm 为空表示该池不区分版本，也放行；两者都非空则必须相等。
    # 本地归一而不 import keysvc：本模块只依赖 (db, config)，引 keysvc 会成环。
    if str(row['provider'] or DEFAULT_PROVIDER) == 'qoder':
        key_realm = str((key or {}).get('realm') or '').strip().lower()
        key_realm = key_realm if key_realm in ('cn', 'global') else ''
        up_realm = str(row['realm'] or '').strip().lower()
        if key_realm and up_realm and key_realm != up_realm:
            raise UpstreamUnavailable(
                f'密钥版本 {"国际版" if key_realm == "global" else "国内版"} 与上游'
                f'「{row["name"]}」的版本 '
                f'{"国际版" if up_realm == "global" else "国内版"} 不匹配——'
                '请把密钥改绑到同版本的上游，或把上游版本改为不限')
    return row


def _same_base(a: object, b: object) -> bool:
    """两个 Base URL 是否指同一套实例（去首尾空白与末尾斜杠后逐字比较）。"""
    return str(a or '').strip().rstrip('/') == str(b or '').strip().rstrip('/')


def forward_api_key(upstream: dict | None) -> str:
    """跟这套上游说话时该用的 api_key：自己填了用自己那把；留空且与默认分组
    同址的，沿用默认上游那把。

    为什么有后一条：账号页「添加分组」的默认形态就是**只填名称** —— 地址默认
    沿用默认分组的地址，而 api_key 留空（默认分组的钥匙明文不出接口，前端填不
    进去）。同址 = 同一套实例，钥匙本就是同一把：不沿用的话，绑定该分组的密钥
    调用、该分组的 /status、停用 / 启用位都会吃上游 401 —— 而且只填名称恰恰是
    产品的默认路径。

    地址不同的留空维持「不带鉴权头」：既支持不鉴权的自建实例，也不把默认钥匙
    发去别的地址。
    """
    key = str((upstream or {}).get('api_key') or '')
    if key or not upstream or upstream.get('is_default'):
        return key
    if _same_base(upstream.get('base_url'), default_upstream().get('base_url')):
        return str(default_upstream().get('api_key') or '')
    return ''


async def probe(upstream: dict) -> tuple[bool, str]:
    """探测某个上游是否可达（走它的 /healthz，与部署探活同一条判据）。"""
    base = str(upstream.get('base_url') or '')
    if not base:
        return False, '上游地址为空'
    # qoder 分支探 `/v1/models` 而非 `/healthz`：bridge 只暴露 4 个端点
    # （chat/messages/models/responses），没有 /healthz——拿它探活恒 404，
    # 会把一个健康的桥误报成「不可达」。用带桥接 token 的模型清单探测，
    # 顺带验证 token 本身是好的（401 说明 token 错，比「不通」更有指向性）。
    if str(upstream.get('provider') or DEFAULT_PROVIDER) == 'qoder':
        headers = {}
        token = forward_api_key(upstream)
        if token:
            headers['Authorization'] = f'Bearer {token}'
        try:
            async with config.http_client(10, connect=3) as client:
                resp = await client.get(f'{base}/v1/models', headers=headers)
            if resp.status_code == 200:
                return True, '桥接可达'
            if resp.status_code == 401:
                return False, '桥接 token 不对（bridge 返回 401 invalid api key）'
            return False, f'桥接返回 {resp.status_code}'
        except Exception as exc:                 # noqa: BLE001 —— 探测失败原因原样返回给界面
            return False, f'无法连接：{exc}'
    try:
        async with config.http_client(10, connect=3) as client:
            resp = await client.get(f'{base}/healthz')
        if resp.status_code == 200:
            return True, '上游可达'
        return False, f'上游返回 {resp.status_code}'
    except Exception as exc:                     # noqa: BLE001 —— 探测失败原因原样返回给界面
        return False, f'无法连接：{exc}'