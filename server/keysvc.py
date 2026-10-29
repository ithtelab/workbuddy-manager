"""API 密钥的生成、校验与限额判定（网关与管理端共用）。"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time

from . import db
from .iputil import ip_matches

TOKEN_PREFIX = 'wbk_'

# 延迟引用的模块别名：upstreamsvc 只依赖 (db, config)，keysvc → upstreamsvc
# 不成环（upstreamsvc **不得**反向 import keysvc，见架构文档 §3.0 的方向约定）。
# 放函数体内 import 也行，但判定在热路径（每次请求一次），顶层 import 更省。
from . import upstreamsvc as _upstreamsvc  # noqa: E402  （语义上是本文件的一部分）


class Rejection(str):
    """拒绝原因 + 该用哪个 HTTP 状态码回。

    为什么不是纯字符串：一批客户端（实测 DeepSeek Harness——它的 chat-completions
    适配层写的是 `status === 401 || 403 → code 'AUTH'`，界面再 `code === 'AUTH' ?
    'API 密钥无效' : message`）会把 **401/403 的真实报文整段丢掉**，只显示本地化的
    「API 密钥无效」。于是「密钥限定了国内版、却去调国际版模型」这种**配置问题**
    被显示成密钥坏了，用户就反复新建密钥——而在国内版界面里新建的每一把都还是
    国内版专用，永远好不了（issue #18 的现场）。

    所以按「该怪谁」分流，而不是一律 403：

      · 请求与密钥不匹配（版本归属 / 模型白名单 / 缺 model）→ ``400
        invalid_request_error``：这是「这次请求的参数不对」，客户端会把原因原样
        显示出来，用户一眼看到该改什么。
      · 凭据本身不可用（已停用 / 已过期）→ ``403``：显示成「密钥无效」是贴切的。
      · 用量类（配额用尽）→ ``429``：避免被当成认证失败（OpenAI 同语义用 429
        ``insufficient_quota``）。

    继承 str 是为了兼容既有调用方——它们只做 `if reason:` 真假判断或把原因当
    文本用，`str` 子类在这两种用法下行为不变。
    """

    def __new__(cls, message: str, status: int = 403,
                err_type: str = 'permission_error', code: str = 'forbidden'):
        self = super().__new__(cls, message)
        self.status = status
        self.err_type = err_type
        self.code = code
        return self


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _norm_realm(value: object) -> str:
    """归一化密钥的版本归属；'' = 不限制（存量密钥的形态，见 db._MIGRATIONS）。"""
    v = str(value or '').strip().lower()
    return v if v in ('cn', 'global') else ''


def _norm_upstream_id(value: object) -> int | None:
    """归一化密钥绑定的上游 id：空 / 非法一律当「未绑定」（= 默认上游）。

    与 realm 的归一化同口径：**不报错**。原因是这个值来自管理端表单，
    而「未绑定」本身是合法且有意义的取值（存量密钥全是这个形态），
    把脏值吃掉成「未绑定」既安全又不会让保存整个失败。
    真正「绑了一个不存在的上游」由路由层拦（见 routers/keys.py），
    那里能拿到准确的 400 文案。
    """
    if value in (None, '', 0, '0'):
        return None
    try:
        uid = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return uid if uid > 0 else None


def _norm_credit_quota(value: object) -> float:
    """归一化积分额度。0 = 不限（与 token 额度同口径）。

    脏数据一律吃掉成 0 而不是抛错：这个值来自管理端表单，格式问题不该让
    保存整个失败；而**钳到 0 是安全的**——0 表示「不限」，不会把一把设了
    负数的密钥悄悄放行成「已超限」（那会让线上调用突然全 429）。
    """
    try:
        v = float(value if value not in (None, '') else 0)
    except (TypeError, ValueError):
        return 0.0
    if v != v or v in (float('inf'), float('-inf')):  # NaN / inf
        return 0.0
    return max(0.0, v)


def _bare_model(model: object) -> str:
    """模型名归一化：去 `cn:` 前缀（`global:` 保留），用于白名单比对。

    `cn:` 是上游的路由约定而非模型名的一部分，界面上显示的是腾讯裸名；存量密钥
    的白名单里可能是带前缀的写法。归一化后两种写法等价，避免「升级后原本能用的
    密钥突然报模型不在白名单里」。

    **不动 `global:`**：它决定路由到哪个账号池，两个版本的同名模型不是一回事。
    """
    m = str(model or '').strip()
    return m[3:] if m.lower().startswith('cn:') else m


def _norm_cidrs(items: object) -> list[str]:
    """归一化 IP 白名单：逐项去空白、丢弃空项。

    **为什么必须在写入时归一化**（而不是只在路由里校验）：路由校验的是
    `str(raw).strip()`，若存库时存了**原始值**，两者就会不一致 ——
    实测 `" 10.0.0.0/8"`（带空格）能通过校验，存进库后
    `ip_matches()` 却匹配不上任何 IP（`ip_network` 解析失败），于是这把密钥
    **对所有来源都被拒绝**，而报错只说「不在白名单内」，用户完全看不出
    是自己粘进了一个多余空格。校验与存储必须是同一份数据。
    """
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for raw in items:
        s = str(raw).strip()
        if s:
            out.append(s)
    return out


def _json_list(raw: object) -> list[str]:
    """把库里存的 JSON 数组文本解析成列表；**解析不了按「该列未设置」处理**。

    为什么必须容错：`ip_allowlist` / `models` 是 JSON 文本列，而读取它的
    `_parse` 被**列表接口**用到——只要**任意一把**密钥的这两列存了非法 JSON
    （历史版本写入过、手工改过库、写入被截断、JSON 编码变更），整个
    `GET /api/keys` 就抛 JSONDecodeError → 500，界面上**一把密钥都看不到**。

    而这会伪装成「创建失败」：新建密钥的 POST 本身是成功的（数据已入库），
    紧接着前端刷新列表才炸——用户看到 500 以为没建成，再点一次就多一把重复的。
    症状极具误导性（数据明明进去了），所以这里宁可少显示一个字段，
    也绝不让一列坏数据拖垮整个列表。

    **注意这里的方向**：解析失败按空列表返回，而 `validate` 里是
    `if allow and ...` / `if key['models'] and ...` —— 空列表意味着**跳过该项
    检查**（不限制 IP / 不限制模型）。也就是说坏数据会**放宽**该密钥，不是收紧。
    这是有意的取舍，理由见下：

      * 坏数据只可能来自**已经损坏的库**，而那种库在修复前整个密钥页都打不开，
        管理员连删掉它的入口都没有——修好列表是当务之急；
      * 「不限制」不等于「无保护」：密钥本身仍要过哈希校验、启停、有效期、
        配额与限流；IP 与模型白名单是**附加**收紧项，不是唯一防线；
      * 反向选择（坏数据一律拒绝）会让那把密钥的所有调用都失败，而管理员
        看不到任何原因（列表都打不开），排查成本更高。

    所以界面上会把「解析失败」当作空列表展示——管理员看到某把密钥的白名单
    是空的，若记得自己设过，就知道要重新设置一次。
    """
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(x) for x in parsed if x]


def _parse(row) -> dict:
    return {
        'id': row['id'],
        'name': row['name'],
        'prefix': row['prefix'],
        'enabled': bool(row['enabled']),
        'expires_at': row['expires_at'],
        'max_ips': row['max_ips'],
        'ip_allowlist': _json_list(row['ip_allowlist']),
        'models': _json_list(row['models']),
        'realm': _norm_realm(row['realm']),
        # 绑定的上游接入点（多上游 / 分组隔离）。None = 默认上游 —— 见 upstreamsvc
        # 的模块注释：默认上游不是数据库里的某一行，所以这里保持 None 而不是补 0。
        'upstream_id': _norm_upstream_id(row['upstream_id']),
        'quota': row['quota'],
        'used_tokens': row['used_tokens'],
        'quota_credit': row['quota_credit'],
        'used_credit': row['used_credit'],
        'created_at': row['created_at'],
        'last_used_at': row['last_used_at'],
    }


def list_keys() -> list[dict]:
    """全部密钥，附带 `packet_id`（来源红包）。

    为什么要在列表里带这个：红包一次生成一批、额度零碎，混在手工建的密钥里
    很难看，界面上要能单独分组。用**标量子查询**而不是 JOIN —— JOIN 在
    「一个 key 意外对应多条 share」时会把同一把密钥返回两遍（接口返回重复行
    是最难查的一类问题），子查询天然只取一条。
    """
    rows = db.query(
        'SELECT k.*, (SELECT s.packet_id FROM red_packet_shares s '
        '             WHERE s.key_id = k.id LIMIT 1) AS packet_id '
        'FROM api_keys k ORDER BY k.id DESC'
    )
    out = []
    for r in rows:
        item = _parse(r)
        item['packet_id'] = r['packet_id']      # None = 手工建的
        out.append(item)
    return out


def create_key(
    name: str,
    expires_at: int | None = None,
    max_ips: int = 0,
    ip_allowlist: list[str] | None = None,
    models: list[str] | None = None,
    quota: int = 0,
    realm: str = '',
    quota_credit: float = 0,
    upstream_id: int | None = None,
    *,
    _conn: sqlite3.Connection | None = None,
) -> dict:
    """创建一个密钥。返回含**明文 token** 的字典（库里只存哈希）。

    `_conn`：传入一个**已开启事务**的连接时，本函数在它上面执行且**不自行提交**，
    由调用方负责 commit/rollback。红包（`redpacket.create_packet`）用它来保证
    「N 个密钥 + 红包记录」整批原子——中途失败必须整体回滚，否则会留下几个
    没人知道出处的密钥。默认 None 时行为与从前完全一致（自己提交）。

    之所以做成参数而不是让红包自己写一份 INSERT：SQL 抄第二遍就是第二份事实，
    改一处漏一处——本项目在 count_tokens 的鉴权上正是这么漂移出真漏洞的。
    """
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    sql = ('INSERT INTO api_keys(name, key_hash, prefix, enabled, expires_at, max_ips, '
           'ip_allowlist, models, realm, quota, used_tokens, quota_credit, used_credit, '
           'created_at, upstream_id) VALUES(?, ?, ?, 1, ?, ?, ?, ?, ?, ?, 0, ?, 0, ?, ?)')
    args = (
        name,
        _hash(token),
        token[:12],
        expires_at,
        max_ips,
        json.dumps(_norm_cidrs(ip_allowlist)),
        json.dumps(models or []),
        _norm_realm(realm),
        quota,
        _norm_credit_quota(quota_credit),
        int(time.time()),
        _norm_upstream_id(upstream_id),
    )
    if _conn is not None:
        key_id = int(_conn.execute(sql, args).lastrowid or 0)
        row = _conn.execute('SELECT * FROM api_keys WHERE id = ?', (key_id,)).fetchone()
    else:
        key_id = db.execute(sql, args)
        row = db.query_one('SELECT * FROM api_keys WHERE id = ?', (key_id,))
    out = _parse(row)
    out['key'] = token  # 仅此一次返回明文
    return out


def update_key(key_id: int, patch: dict) -> dict | None:
    row = db.query_one('SELECT * FROM api_keys WHERE id = ?', (key_id,))
    if not row:
        return None
    fields: dict[str, object] = {}
    if 'name' in patch and patch['name']:
        fields['name'] = str(patch['name'])
    if 'enabled' in patch:
        fields['enabled'] = 1 if patch['enabled'] else 0
    if 'expires_at' in patch:
        fields['expires_at'] = patch['expires_at']
    if 'max_ips' in patch:
        fields['max_ips'] = int(patch['max_ips'] or 0)
    if 'ip_allowlist' in patch:
        # 与创建同一条归一化路径：校验与存储必须是同一份数据（见 _norm_cidrs）
        fields['ip_allowlist'] = json.dumps(_norm_cidrs(patch['ip_allowlist']))
    if 'models' in patch:
        fields['models'] = json.dumps(patch['models'] or [])
    if 'realm' in patch:
        # 显式传 null/'' 是**允许**的：管理员可以把旧密钥改回「不限制」。
        # 所以这里不能用 `patch['realm'] or ...` 之类的真值判断。
        fields['realm'] = _norm_realm(patch['realm'])
    if 'quota' in patch:
        fields['quota'] = int(patch['quota'] or 0)
    if 'quota_credit' in patch:
        fields['quota_credit'] = _norm_credit_quota(patch['quota_credit'])
    if 'upstream_id' in patch:
        # 显式传 null / 0 是**允许**的：管理员可以把密钥改回「默认上游」。
        # 与 realm 同口径，不能用真值判断（否则改不回默认上游）。
        fields['upstream_id'] = _norm_upstream_id(patch['upstream_id'])
    if fields:
        assignments = ', '.join(f'{k} = ?' for k in fields)
        db.execute(f'UPDATE api_keys SET {assignments} WHERE id = ?', (*fields.values(), key_id))
    return _parse(db.query_one('SELECT * FROM api_keys WHERE id = ?', (key_id,)))


def delete_key(key_id: int) -> bool:
    if not db.query_one('SELECT id FROM api_keys WHERE id = ?', (key_id,)):
        return False
    db.execute('DELETE FROM api_keys WHERE id = ?', (key_id,))
    db.execute('DELETE FROM api_key_ips WHERE key_id = ?', (key_id,))
    return True


def reset_usage(key_id: int) -> bool:
    """把已用 Token 与已用积分一起归零。返回是否真的命中了密钥。

    返回布尔值是为了让路由能对「不存在的 id」报 404 —— 原来静默成功会让
    前端提示「已重置」，而实际什么都没发生。

    两个量一起归零是刻意的：界面上它们是同一个「重置用量」按钮，只清 token
    不清积分会留下一个看不见的残留额度，下次超额时用户会莫名其妙（「我明明
    重置过」）。若只想放开其中一项，正确做法是把对应的**额度**调大，而不是
    靠重置。
    """
    if not db.query_one('SELECT id FROM api_keys WHERE id = ?', (key_id,)):
        return False
    db.execute('UPDATE api_keys SET used_tokens = 0, used_credit = 0 WHERE id = ?', (key_id,))
    return True


def resolve(token: str) -> dict | None:
    """按前缀定位后比对哈希，避免全表扫描。"""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    rows = db.query('SELECT * FROM api_keys WHERE prefix = ?', (token[:12],))
    digest = _hash(token)
    for row in rows:
        if secrets.compare_digest(row['key_hash'], digest):
            return _parse(row)
    return None


def model_allowed(key: dict, model: object) -> bool:
    """该密钥的**模型白名单**是否放行这个模型。

    **调用侧与 `/v1/models` 裁剪共用这一份判据**（issue #46）：两处各写一遍
    迟早漂移，而漂移的表现正是那个 issue 要修的问题——列表里给出的模型，
    调用时却被白名单拒掉（客户端把它当模型选择器，于是「能选、一选就失败」）。

    白名单为空 = 不限制，一律放行。
    """
    allow = key.get('models') or []
    if not allow:
        return True
    if not isinstance(model, str) or not model.strip():
        return False
    # 白名单比对**去掉 cn: 前缀**再比，两种写法都认。
    #
    # 为什么必须这样：`cn:` 只是上游的路由约定，不是模型名的一部分。界面上
    # 现在显示的是腾讯自带的裸名（glm-5.2），用户照着填；而**存量密钥**的
    # 白名单里可能写着带前缀的 `cn:glm-5.2`。若按字面比对，改版后前者会
    # 被后者拒掉（用户看到「模型不在白名单内」却查不出哪里不对）。
    # `global:` 不归一化——它决定路由，两个版本的同名模型是**不同的东西**。
    #
    # 例外：密钥**已锁定版本**（realm=cn/global）时，前缀不再承担路由信息
    # （另一版本根本进不来），两边都按裸名比。否则会出现一种死锁（issue #255）：
    # 国际版密钥的白名单写裸名 `deepseek-v4.1-flash`，客户端按界面提示加
    # `global:` 前缀调用，反而被白名单拒掉；不加前缀又被版本校验拒掉。
    norm = _strip_realm_prefix if _norm_realm(key.get('realm')) else _bare_model
    return norm(model) in {norm(x) for x in allow}


def _strip_realm_prefix(name: str) -> str:
    """去掉 `cn:` / `global:` 前缀（两者都去）。

    与 `_bare_model` 的区别，以及**为什么校验里必须用这个而不是它**：

      · `_bare_model` 只去 `cn:`、**保留 `global:`** —— 那是**白名单比对**要的
        语义，因为 `global:` 决定路由，两个版本的同名模型不是一回事；
      · 而模型目录里的 id 是**裸名**（`_strip_realm_prefix` 已把两种前缀都去掉，
        因为目录本身就是按版本分开取的：在「国际版目录」里 `gpt-5.6-sol` 本来就
        是国际版那个）。

    所以「某个名字在不在**它所属版本**的目录里」这个判断，必须把两边的前缀都
    去掉再比——否则 `global:gpt-5.6-sol` 会去跟目录里的 `gpt-5.6-sol` 比，
    永远不相等，把**正确**的名字报成"找不到"（实测踩到：假警报）。
    """
    low = name.lower()
    for pref in ('cn:', 'global:'):
        if low.startswith(pref):
            return name[len(pref):]
    return name


def unknown_whitelist_entries(models: list[str], known_by_realm: dict,
                             aliases: list[str] | set[str] = (),
                             extra_known: set[str] | None = None) -> list[str]:
    """白名单里**匹配不到任何已知模型**的条目（多半是拼错了）。

    为什么需要（issue #46 的可选做法 2）：白名单是自由文本框，填错了不会报错，
    只会在下游表现为「模型列表是空的」。而空列表本身看不出原因——用户不知道自己
    是少打了一个连字符、还是填成了显示名。所以在**编辑处**当场点出来，比事后裁剪
    更根本。

    ## 参数

    `known_by_realm`：`{'cn': {...} | None, 'global': {...} | None}`，
    值为该版本的模型 id 集合；`None` = **该版本的清单拿不到**。

    `extra_known`：**不分组**的额外已知模型 id（当前来源：qoder bridge 的清单
    缓存）。qoder 模型名无 cn/global 前缀，按「前缀定 realm」的规则会落进
    `cn` 桶、拿腾讯清单去判——必然判成"找不到"（C5 假警报）。它得有第三个
    出口：在这个集合里就放行，不进 realm 判定。None = 来源不可用，退化为
    原两清单判定（调用方约定：拿不到就不判）。

    ## 每个名字只跟自己版本的清单比

    `global:` 前缀的条目只能拿国际版清单判，裸名只能拿国内版清单判——两个版本的
    清单是**分开取的**，用一份判另一份必然出错。更关键的是：**清单拿不到就不判**
    （而不是判成"找不到"）。否则「另一个版本还没缓存」会被显示成「你这个名字写错了」，
    用户会去改一个本来正确的名字——假警报比不提示更糟。

    ## 前缀要两边都去掉再比

    目录里的 id 是裸名（见 `_strip_realm_prefix` 的说明），白名单里往往带
    `global:` 前缀。只去掉一边就是假警报。

    别名要算：它本来就是给下游用的合法名字（鉴权判请求名，映射在其后）。
    别名的存在性与模型清单无关，所以无条件认。
    """
    if not models:
        return []
    alias_bare = {_strip_realm_prefix(str(x)).strip() for x in aliases}
    extra = {str(x).strip() for x in (extra_known or ()) if str(x).strip()}
    out: list[str] = []
    for raw in models:
        entry = str(raw).strip()
        if not entry:
            continue
        # 先按**请求名**判别名：别名匹配发生在鉴权之后，用的是客户端发来的原样名字
        if entry in alias_bare or _strip_realm_prefix(entry) in alias_bare:
            continue
        # 第三出口：跨源清单（qoder bridge）的 id 不参与 realm 分桶，直接认
        if extra and (entry in extra or _strip_realm_prefix(entry) in extra):
            continue
        realm = 'global' if entry.lower().startswith('global:') else 'cn'
        known = known_by_realm.get(realm)
        if known is None:
            continue            # 该版本清单不可用 → 不判（宁可这次不提示）
        # 两边都去前缀：目录是裸名，白名单可能带前缀
        if _strip_realm_prefix(entry) in {_strip_realm_prefix(str(x)) for x in known}:
            continue
        out.append(entry)
    return out


def _bound_provider(key: dict) -> str:
    """该密钥绑定的上游类型（'codebuddy' | 'qoder'）。

    未绑定 / 绑定的上游不存在 → 'codebuddy'：后者该请求稍后会被
    `upstreamsvc.resolve_for_key` 以 UpstreamUnavailable 打回，版本门无需
    先行猜——猜错（比如猜成 qoder）反而会在 503 之前多拦一道说不清的 400。
    """
    uid = (key or {}).get('upstream_id')
    if uid in (None, '', 0, '0'):
        return 'codebuddy'
    row = _upstreamsvc.get_upstream(uid)
    if row is None:
        return 'codebuddy'
    return str(row.get('provider') or 'codebuddy')


def validate(key: dict, ip: str, model: str | None,
             *, is_model_list: bool = False,
             mapped_model: str | None = None) -> str | None:
    """返回 None 表示放行，否则返回拒绝原因（`Rejection`，自带状态码）。

    is_model_list：请求是 `/v1/models`（模型发现，不带 model）。版本归属在这种
    请求上不拦——它没有版本可言，拦了会让限定版本的密钥连「我有哪些模型」都
    问不到；真正的隔离由调用时的模型名把关（见下）。

    mapped_model：`model` 经「模型映射」后的名字（issue #47）。**两处判据的对象
    不同，不能混为一谈**：

      · **版本归属判映射后的名字**——真正发往上游、决定走哪个账号池的是它。
        用请求名判，配了别名映射的密钥会永远被 realm 检查打回（issue #47：
        别名 `claude-fable-5 → global:deepseek-v4.1-flash` 被判成国内版模型）。
      · **模型白名单判请求名**——白名单约束的是「客户端可以发哪些名字」，而
        `/v1/models` 的裁剪就是这么算的（别名条目以**别名**为 id 下发，见
        `gateway._scope_models`）。改成判映射后的名字，会让「白名单里写别名」
        的密钥反被拒掉，与列表自相矛盾——issue #46 修的正是这处一致性。
    """
    if not key['enabled']:
        return Rejection('密钥已停用', 403, 'permission_error', 'key_disabled')
    if key['expires_at'] and key['expires_at'] < time.time():
        return Rejection('密钥已过期', 403, 'permission_error', 'key_expired')
    if key['quota'] and key['used_tokens'] >= key['quota']:
        # 配额用尽与认证无关，用 429 才对（403 会被客户端读成「密钥无效」：
        # 用户于是去查密钥、而不是去调额度，方向就被带偏了）。
        return Rejection('密钥配额已用尽', 429, 'insufficient_quota', 'quota_exhausted')
    # 积分额度（issue #27）：与 token 额度**各自独立**，任一超限即拦。
    # 文案里带上具体数字：这个额度是按真实扣费算的，用户需要知道「超了多少、
    # 该充多少」，只说「已用尽」会让他去翻日志。
    if key['quota_credit'] and key['used_credit'] >= key['quota_credit']:
        return Rejection(
            f'密钥积分额度已用尽（已用 {key["used_credit"]:g} / 上限 {key["quota_credit"]:g}）',
            429, 'insufficient_quota', 'credit_quota_exhausted')

    allow = key['ip_allowlist']
    if allow and not any(ip_matches(ip, c) for c in allow):
        # 观感上像认证失败，但它不是「密钥错」而是「来源不对」，客户端原样显示
        # 才能让用户知道要加白名单，故用 400 让报文得以透出。
        return Rejection(f'来源 IP {ip} 不在密钥白名单内', 400,
                         'invalid_request_error', 'ip_not_allowed')

    if key['max_ips']:
        known = db.query('SELECT ip FROM api_key_ips WHERE key_id = ?', (key['id'],))
        ips = {r['ip'] for r in known}
        if ip not in ips and len(ips) >= key['max_ips']:
            return Rejection(
                f'密钥已绑定 {len(ips)} 个 IP，超出上限 {key["max_ips"]}', 400,
                'invalid_request_error', 'too_many_ips')

    # 版本归属：密钥限定版本后，只能调用该版本的模型。
    #
    # 判定依据与实际路由**同一来源**：上游按模型名的 `global:` 前缀选账号池，
    # 所以「这次请求走哪个版本」由**实际要用的那个名字**决定（见 db.realm_of_model，
    # 日志与统计也用它）。用别的东西判（比如当前界面切到哪版）会与真实流量对不上。
    #
    # 而「实际要用的名字」是**映射之后**的：请求名可能是个别名，上游看不懂它，
    # 真正路由的是映射目标（issue #47）。所以这里用 mapped_model —— 否则配了
    # 「别名 → 带前缀的真名」的密钥永远被判成错版本。
    effective = (mapped_model or model)
    want = _norm_realm(key.get('realm'))
    # qoder 分支（C2）：`db.realm_of_model` 按 `global:` 前缀判版本，而 qoder
    # 模型名无前缀、恒归 cn——global 密钥调 qmodel 会被这里误杀。qoder 的版本
    # 语义在**上游行**上，由 `upstreamsvc.resolve_for_key` 的「密钥×上游」匹配
    # 承担；两道门合起来恰好等价于「密钥版本 ⊆ 上游版本」。唯一触发条件是
    # `== 'qoder'`：codebuddy / 默认上游（全部存量）逐字走原分支。
    bound_qoder = _bound_provider(key) == 'qoder'
    if want and not is_model_list and not bound_qoder:
        if not isinstance(effective, str) or not effective.strip():
            return Rejection(
                f'该密钥限定了{"国际版" if want == "global" else "国内版"}模型，请求必须指定 model',
                400, 'invalid_request_error', 'realm_mismatch')
        got = 'global' if effective.strip().lower().startswith('global:') else 'cn'
        if got != want:
            label = {'cn': '国内版', 'global': '国际版'}[want]
            other = '国际版' if want == 'cn' else '国内版'
            # 配了映射时，正确的做法是改映射或改密钥的版本，不是给请求名加前缀
            # （那个名字客户端根本不发到上游）——所以提示分两种。
            if mapped_model and mapped_model != model:
                hint = f'{model} 按「模型映射」指向 {mapped_model}'
            else:
                hint = '模型名需带 global: 前缀' if want == 'global' else '请去掉 global: 前缀'
            return Rejection(
                f'该密钥仅限{label}模型，当前请求是{other}模型（{hint}）',
                400, 'invalid_request_error', 'realm_mismatch')

    # 模型白名单：**不能因为 model 缺失就跳过检查**。
    # 原写法 `if key['models'] and model and model not in ...` 在 body 不带 model
    # （或传空串/非字符串）时整段跳过，于是限定单模型的密钥可用「不带 model」的
    # 请求走上游默认模型——白名单形同虚设。请求侧已在网关把缺失/非法的 model
    # 拦成 400；这里再兜一层，任何非字符串或空值一律拒绝。
    if key['models'] and not is_model_list:
        if not isinstance(model, str) or not model.strip():
            return Rejection('请求未指定 model，而该密钥启用了模型白名单', 400,
                             'invalid_request_error', 'model_not_allowed')
        # 判据见 `model_allowed`：与 `/v1/models` 的裁剪共用同一份，
        # 避免「列表里有、调用被拒」那种漂移（issue #46）。
        if not model_allowed(key, model):
            return Rejection(f'模型 {model} 不在密钥白名单内', 400,
                             'invalid_request_error', 'model_not_allowed')
    return None


def touch(key: dict, ip: str, tokens: int = 0, credit: float | None = None) -> None:
    """记账：来源 IP、最近使用、已用 token 与已用积分。

    credit 为本次调用的**真实扣费**（上游 usage.credit）。None / 0 表示上游
    没返回该字段（本版本之前的上游、或非计费端点）——**不计入而不是按 0 记**，
    与 request_logs.credit、usage_daily.credit 的口径一致。
    """
    db.execute(
        'INSERT OR IGNORE INTO api_key_ips(key_id, ip, first_seen) VALUES(?, ?, ?)',
        (key['id'], ip, int(time.time())),
    )
    db.execute('UPDATE api_keys SET last_used_at = ? WHERE id = ?', (int(time.time()), key['id']))
    if tokens:
        db.execute('UPDATE api_keys SET used_tokens = used_tokens + ? WHERE id = ?', (tokens, key['id']))
    if credit:
        db.execute('UPDATE api_keys SET used_credit = used_credit + ? WHERE id = ?',
                   (float(credit), key['id']))
