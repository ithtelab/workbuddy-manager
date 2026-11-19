"""代理线路表（代理池）：读写上游 `config.json` 里的 `proxies` 段。

方案参照 Antigravity-Manager 的代理池：**池 + 权重 + 启用开关 + 账号单绑定 + 全局兜底**
（其 `ProxyPoolSettings` / `ProxyBindingManager`）。要点：

  · 线路表 = 池：每条有 `url` / `weight` / `enabled`；
  · **每个账号最多绑一条**线路，不绑 = 跟随池策略（见 `config.resolve_account_proxy`）；
  · 「几个号共用一条出口」= 把它们绑到同一条线路；「各自一个 IP」= 各自绑不同线路
    （页面上的「自动分配 N 台」可以批量铺）；
  · 池开关 / 策略 / 间隔存在面板设置里（`config.proxy_pool_config`）。

三条必须守住的性质：

1. **只动 `proxies` 段**：上游 config.json 里还有 api_key、schedule、prompt 等，
   写线路时一个键都不能碰。
2. **凭据不出网**：地址通常带用户名密码，列表一律回脱敏串（`***`）；编辑时若提交的
   仍是脱敏串，就沿用原来那份密码 —— 为了改个域名不必重新输密码。
3. **删除 / 改名要管绑定**：绑着它的账号在它消失后会直接失败（按设计不静默换出口），
   所以这两条路径要把绑定一并处理，并回报影响了几台账号。
"""
from __future__ import annotations

import ipaddress
import json
import re
import time
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .. import config
from . import wb2api
from .tencent import _atomic_write_json

# 线路名会出现在账号文件与界面上：限字母、数字与 . _ -（1~32 位）。
NAME_RE = re.compile(r'^[A-Za-z0-9._-]{1,32}$')

SCHEMES = ('http', 'https', 'socks5', 'socks5h')
MASK = '***'
# 一次批量导入的上限：导入是「每条一次读+写配置」，粘贴错文件不该变成几千次写盘。
MAX_IMPORT = 200


class RouteError(ValueError):
    """线路表相关的**用户可见**错误（路由层翻成对应状态码）。"""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _config_path() -> Path:
    return config.UPSTREAM_CONFIG


def _load() -> dict:
    path = _config_path()
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError as exc:
        raise RouteError(
            f'未找到上游配置文件 {path}。多分组部署请确认该分组的配置文件已配置，'
            f'容器部署请确认它已挂载。', 409) from exc
    except (OSError, ValueError) as exc:
        raise RouteError(f'上游配置文件无法读取：{exc}') from exc
    if not isinstance(raw, dict):
        raise RouteError('上游配置文件格式异常（顶层不是对象）')
    return raw


def _save(cfg: dict) -> None:
    if not cfg.get('proxies'):
        cfg.pop('proxies', None)      # 空表就把键删掉，保持文件干净（读侧两种都认）
    path = _config_path()
    try:
        _atomic_write_json(path, cfg)
    except OSError as exc:
        # 写不进去**必须**翻译成用户能处理的话（issue #341 的现场：界面上只弹
        # 一句英文 Internal Server Error，而真实原因是面板进程对该文件没有写
        # 权限 —— 只读挂载、root 属主、或 SELinux 标签）。读得到、写不了是最
        # 迷惑的一种：页面能把线路全列出来，一保存就 500。
        raise RouteError(
            f'上游配置文件写不进去：{path}（{exc}）。'
            '请确认面板进程对该文件所在目录有写权限（容器部署注意不要以 :ro 挂载），'
            '宿主机部署可执行 chown 让面板用户可写。', 409) from exc
    except ValueError as exc:
        raise RouteError(f'上游配置文件写入失败：{exc}', 409) from exc


def routes() -> dict[str, str]:
    """线路名 → 原始地址（**含凭据**，只供服务端使用）。"""
    return config.proxy_routes()


def validate_name(name: object) -> str:
    value = str(name or '').strip()
    if not NAME_RE.match(value):
        raise RouteError('线路名只能是字母、数字与 . _ -（1~32 位）')
    return value


def normalize_url(url: object) -> str:
    """校验并规范化代理地址（只校验形状，不探测可达性）。"""
    value = str(url or '').strip()
    if not value:
        raise RouteError('代理地址不能为空')
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise RouteError('代理地址格式不对') from exc
    if parts.scheme not in SCHEMES:
        raise RouteError('代理地址要以 http:// https:// socks5:// 或 socks5h:// 开头')
    if not parts.hostname:
        raise RouteError('代理地址缺少主机名')
    if not port:
        raise RouteError('代理地址缺少端口')
    netloc = f'{_quote_userinfo(parts)}@{parts.hostname}' if parts.username is not None \
        else parts.hostname
    if port != {'http': 80, 'https': 443}.get(parts.scheme):
        netloc = f'{netloc}:{port}'
    return urlunsplit((parts.scheme, netloc, '', '', ''))


def _restore_placeholder(text: str) -> str:
    """把被百分号编码的 `{account}` 还原成字面量（旧版会话模板的占位符）。

    用户名与地址都要经过 `quote`，而占位符一旦变成 `%7Baccount%7D` 就再也渲染不
    出来了（`config.render_route_url` 只认字面量）。列表展示同理：界面该显示用户
    当初写的那串，而不是转义后的样子。
    """
    return text.replace(quote(config.SESSION_PLACEHOLDER, safe=''), config.SESSION_PLACEHOLDER)


def _quote_userinfo(parts) -> str:  # noqa: ANN001 —— urlsplit 结果
    """用户名 / 密码按 URL 规则转义：不转义的话特殊字符会让拼回去的地址变样。"""
    user = _restore_placeholder(quote(unquote(parts.username or ''), safe=''))
    if not parts.password:
        return user
    return f'{user}:{quote(unquote(parts.password), safe="")}'


def mask_url(url: str) -> str:
    """脱敏：保留协议、用户名、主机与端口，密码换成 `***`。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return MASK
    if not parts.password:
        return url
    user = _restore_placeholder(quote(unquote(parts.username or ''), safe=''))
    netloc = f'{user}:{MASK}@{parts.hostname}'
    if parts.port:
        netloc = f'{netloc}:{parts.port}'
    return urlunsplit((parts.scheme, netloc, '', '', ''))


def _is_local(host: str) -> bool:
    """内网 / 本机地址（界面上标一下：当出口通常只在特殊部署里有意义）。"""
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host == 'localhost' or host.endswith(('.local', '.internal', '.lan'))
    return bool(addr.is_private or addr.is_loopback or addr.is_link_local)


def describe(name: str, url: str, *, weight: int = 100, enabled: bool = True) -> dict:
    """给界面用的一条线路（凭据已脱敏）。

    `enabled` 是池里的开关：停用的线路不会被**自动挑选**（池策略跳过它），但被账号
    显式绑定时仍按「绑定优先」报错 —— 让用户知道这条线暂停了，而不是悄悄换出口。
    """
    try:
        parts = urlsplit(url)
        scheme, host, port = parts.scheme, parts.hostname or '', parts.port
        has_auth = bool(parts.password)
    except ValueError:
        scheme, host, port, has_auth = '', '', None, False
    return {
        'name': name,
        'scheme': scheme,
        'host': host,
        'port': port,
        'has_auth': has_auth,
        'masked': mask_url(url),
        'weight': int(weight),
        'enabled': bool(enabled),
        'local': _is_local(host),
    }


def keep_secret(url: str, stored: str) -> str:
    """把 `***` 占位换回已知的那份密码（改地址不必重输密码）。

    传入的地址里密码是 `***` 或空、而 `stored`（已存的那份完整地址）有密码时，
    沿用后者；否则原样返回。线路表与面板设置两处都用它。
    """
    try:
        incoming = urlsplit(url)
    except ValueError:
        return url
    if incoming.password and incoming.password != MASK:
        return url
    if not stored:
        # 没存过：`***` 无从还原，直接当作没有密码。
        return url.replace(f':{MASK}@', '@')
    try:
        known = urlsplit(stored)
    except ValueError:
        return url
    if not known.password:
        return url.replace(f':{MASK}@', '@')
    user = quote(unquote(incoming.username or known.username or ''), safe='')
    secret = quote(unquote(known.password), safe='')
    host = incoming.hostname or known.hostname or ''
    port = incoming.port or known.port
    netloc = f'{user}:{secret}@{host}' + (f':{port}' if port else '')
    return urlunsplit((incoming.scheme or known.scheme, netloc, '', '', ''))


def list_routes(auth_dir: Path | None = None) -> list[dict]:
    """池里的线路列表（脱敏、按名字排序）：带权重、启用开关与绑定概览。"""
    weights = config.proxy_route_weights()
    enabled = config.proxy_route_enabled()
    accounts = account_rows(auth_dir)
    out: list[dict] = []
    for name, url in sorted(routes().items()):
        bound = [a for a in accounts if a['proxy'] == name]
        item = describe(name, url, weight=weights.get(name, 100),
                        enabled=enabled.get(name, True))
        item['bound'] = len(bound)
        item['bound_names'] = [a['nickname'] or a['uid'] for a in bound[:3]]
        out.append(item)
    return out


def upsert(name: object, url: object, *, weight: object = None,
           enabled: object = None, allow_overwrite: bool = True) -> dict:
    clean_name = validate_name(name)
    raw_url = str(url or '')
    stored = routes().get(clean_name, '')
    if MASK in raw_url and not stored:
        # 没存过密码时 `***` 无从还原：与其存成一条「没密码」的记录（调用时才失败、
        # 报错还看不出原因），不如当场说清楚。
        raise RouteError('地址里还有 ***，请粘贴完整地址')
    clean_url = normalize_url(keep_secret(raw_url, stored))
    cfg = _load()
    table = cfg.get('proxies') if isinstance(cfg.get('proxies'), dict) else {}
    if clean_name in table and not allow_overwrite:
        raise RouteError(f'线路 {clean_name!r} 已存在', 409)
    current = table.get(clean_name)
    clean_weight = _validate_weight(
        weight if weight is not None
        else (current.get('weight') if isinstance(current, dict) else None))
    if enabled is None:
        clean_enabled = bool(current.get('enabled', True)) if isinstance(current, dict) else True
    else:
        clean_enabled = bool(enabled)
    # 权重 100 + 启用 + 原本是字符串写法 → 保持字符串：老配置改动最小、肉眼也好读。
    if clean_weight == 100 and clean_enabled and not isinstance(current, dict):
        table[clean_name] = clean_url
    else:
        table[clean_name] = {'url': clean_url, 'weight': clean_weight,
                             'enabled': clean_enabled}
    cfg['proxies'] = table
    _save(cfg)
    return describe(clean_name, clean_url, weight=clean_weight, enabled=clean_enabled)


def _validate_weight(weight: object) -> int:
    if weight in (None, ''):
        return 100
    try:
        value = int(weight)
    except (TypeError, ValueError) as exc:
        raise RouteError('权重必须是 1~1000 的整数') from exc
    if not 1 <= value <= 1000:
        raise RouteError('权重必须在 1~1000 之间（越大越常被选中）')
    return value


def delete(name: object, *, auth_dir: Path | None = None,
           clear_bindings: bool = False) -> dict:
    """删除线路。仍被账号绑着时必须显式 `clear_bindings`（路由层用 force）。

    不默认清绑定的理由：绑定被悄悄清掉 = 那些账号的出口**换了**（改成跟随池策略），
    而出口换了在风控眼里是特征变化 —— 这种事要让用户点过一次确认。
    """
    clean_name = validate_name(name)
    if clean_name not in routes():
        raise RouteError(f'线路 {clean_name!r} 不存在', 404)
    bound = bindings(clean_name, auth_dir)
    if bound and not clear_bindings:
        names = '、'.join(a['nickname'] or a['uid'] for a in bound[:3])
        more = f' 等 {len(bound)} 台' if len(bound) > 3 else ''
        raise RouteError(f'仍有账号绑着这条线路（{names}{more}）——'
                         f'确认删除会让它们改为跟随池策略', 409)
    cfg = _load()
    table = cfg.get('proxies') if isinstance(cfg.get('proxies'), dict) else {}
    table.pop(clean_name, None)
    cfg['proxies'] = table
    _save(cfg)
    cleared = 0
    for account in bound:
        try:
            _write_binding(account['file'], Path(account['dir']), '')
            cleared += 1
        except (OSError, ValueError):
            continue
    return {'name': clean_name, 'bound_cleared': cleared}


def rename(old: object, new: object, *, auth_dir: Path | None = None) -> dict:
    """改名：绑着它的账号一并改到新名字上（不改的话那些账号会直接失败）。"""
    clean_old = validate_name(old)
    clean_new = validate_name(new)
    if clean_old == clean_new:
        return {'name': clean_new, 'old_name': clean_old, 'bound_updated': 0}
    cfg = _load()
    table = cfg.get('proxies') if isinstance(cfg.get('proxies'), dict) else {}
    if clean_old not in table:
        raise RouteError(f'线路 {clean_old!r} 不存在', 404)
    if clean_new in table:
        raise RouteError(f'线路 {clean_new!r} 已存在', 409)
    table[clean_new] = table.pop(clean_old)
    cfg['proxies'] = table
    _save(cfg)
    updated = 0
    for account in bindings(clean_old, auth_dir):
        try:
            _write_binding(account['file'], Path(account['dir']), clean_new)
            updated += 1
        except (OSError, ValueError):
            continue
    return {'name': clean_new, 'old_name': clean_old, 'bound_updated': updated}


def _write_binding(filename: str, directory: Path, name: str) -> None:
    """把账号的绑定写回账号文件（空串 = 解除绑定，跟随池策略）。

    顺带清掉上一版留下的 `proxy_pool`（那版按「多绑」设计，字段留着会让人以为
    还有效）；`proxy_session` **保留**：旧版 `{account}` 模板地址还靠它认会话名，
    清掉就等于给那个账号换一个出口 IP。
    """
    wb2api.set_account_proxy(filename, name, directory)
    path = directory / filename
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise RouteError(f'账号文件读取失败：{exc}') from exc
    if not isinstance(raw, dict):
        raise RouteError('账号文件格式异常')
    if 'proxy_pool' in raw:
        raw.pop('proxy_pool', None)
        _atomic_write_json(path, raw)


def set_binding(name: object, files: list[str], *, auth_dir: Path | None = None,
                bound: bool = True) -> dict:
    """把一批账号绑定到这条线路（`bound=False` 则解除，改为跟随池策略）。

    每个账号最多一条：绑定就是覆盖它原来的那条（界面上「默认（跟随策略）」即解除）。
    """
    clean_name = validate_name(name)
    if clean_name not in routes():
        raise RouteError(f'线路 {clean_name!r} 不存在', 404)
    wanted = {str(f) for f in files}
    changed = 0
    skipped: list[str] = []
    for row in account_rows(auth_dir):
        if row['file'] not in wanted:
            continue
        try:
            _write_binding(row['file'], Path(row['dir']), clean_name if bound else '')
            changed += 1
        except (OSError, ValueError, RouteError) as exc:  # noqa: BLE001
            skipped.append(f'{row["nickname"] or row["uid"]}: {exc}')
    return {'updated': changed, 'skipped': skipped, 'name': clean_name, 'bound': bound}


def auto_assign(name: object, count: int, *, auth_dir: Path | None = None,
                include_bound: bool = False) -> dict:
    """把 N 台账号自动绑到这条线路（挑人规则写死在这里，别让它漂移）。

    挑人顺序：
      1. **没绑任何线路的账号优先**（跟随池策略的那些，最该先安排上）；
      2. 其次按「昵称 → uid」稳定排序 —— 顺序稳定才不会每次挑到不同的一批
         （挑到不同批次就意味着给那几台换了出口，是风控特征）。
    已经绑着这条线路的账号不算候选（`count` 是「再绑几台」而不是「一共几台」）。
    """
    clean_name = validate_name(name)
    if clean_name not in routes():
        raise RouteError(f'线路 {clean_name!r} 不存在', 404)
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise RouteError('要分配的数量必须是正整数')
    if count > 1000:
        raise RouteError('一次最多分配 1000 台（再多请分批，避免误操作）')

    rows = [row for row in account_rows(auth_dir) if row['proxy'] != clean_name]
    if not include_bound:
        rows = [row for row in rows if not row['proxy']] + \
               [row for row in rows if row['proxy']]
    rows.sort(key=lambda row: (0 if not row['proxy'] else 1,
                               row['nickname'] or row['uid'], row['uid']))
    picked = [row['file'] for row in rows[:count]]
    result = set_binding(clean_name, picked, auth_dir=auth_dir) if picked else {
        'updated': 0, 'skipped': [], 'name': clean_name, 'bound': True}
    remaining = len([row for row in account_rows(auth_dir) if not row['proxy']])
    return {**result, 'requested': count, 'candidates': len(rows),
            'unbound_left': remaining}


def _auth_dirs(auth_dir: Path | None) -> list[Path]:
    """要扫的账号目录：指定分组就只扫它，否则扫**全部分组**。

    为什么默认跨分组：线路表是全局的（读的是同一份 config.json 的 `proxies`），
    而绑定关系散在各组的 auths 目录下 —— 只看默认分组会漏掉别的组里绑着它的账号。
    """
    if auth_dir is not None:
        return [auth_dir]
    from .. import upstreamsvc

    dirs: list[Path] = []
    for group in upstreamsvc.list_upstreams():
        raw = str(group.get('auth_dir') or '').strip()
        if raw:
            dirs.append(Path(raw))
    return dirs


def _accounts(auth_dir: Path | None) -> list[tuple[Path, dict]]:
    """(所在目录, 账号) 列表 —— 重绑时要写回**它自己那份目录**。"""
    out: list[tuple[Path, dict]] = []
    for directory in _auth_dirs(auth_dir):
        try:
            accounts = wb2api.list_auth_accounts(directory)
        except OSError:
            continue
        out.extend((directory, account) for account in accounts)
    return out


def account_rows(auth_dir: Path | None = None) -> list[dict]:
    """全部账号 + 各自绑定的线路（空 = 跟随池策略）。"""
    rows: list[dict] = []
    for directory, account in _accounts(auth_dir):
        rows.append({
            'file': account['file'],
            'uid': account.get('uid') or '',
            'nickname': account.get('nickname') or '',
            'proxy': str(account.get('proxy') or ''),
            'dir': str(directory),
        })
    return rows


def bindings(name: str, auth_dir: Path | None = None) -> list[dict]:
    """哪些账号绑在这条线路上。"""
    return [{'file': row['file'], 'uid': row['uid'], 'nickname': row['nickname'],
             'dir': row['dir']}
            for row in account_rows(auth_dir) if row['proxy'] == name]


def bound_counts(auth_dir: Path | None = None) -> dict[str, int]:
    """线路名 → 绑定账号数（列表页一次算完，避免每行查一次目录）。"""
    out: dict[str, int] = {}
    for row in account_rows(auth_dir):
        if row['proxy']:
            out[row['proxy']] = out.get(row['proxy'], 0) + 1
    return out


def parse_import(text: str) -> tuple[dict[str, dict], list[str]]:
    """批量导入：每行 `名字=地址`（也认 `名字 地址` 与 `名字: 地址`）。

    返回 (可导入的表, 逐行问题)。**逐行报问题而不是整批拒绝**：从别处粘贴时
    常混着空行、注释与重复条目，全拒会让人反复试。
    """
    ok: dict[str, dict] = {}
    problems: list[str] = []
    for lineno, raw_line in enumerate(str(text or '').splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.match(r'^([^=\s:]+)\s*[:=]?\s*(\S.*)$', line)
        if not match:
            problems.append(f'第 {lineno} 行：认不出格式（应写成 名字=地址）')
            continue
        name, url = match.group(1), match.group(2).strip()
        try:
            clean_name = validate_name(name)
            clean_url = normalize_url(keep_secret(url, routes().get(clean_name, '')))
        except RouteError as exc:
            problems.append(f'第 {lineno} 行：{exc}')
            continue
        ok[clean_name] = {'url': clean_url}
    if not ok and not problems:
        problems.append('没有可导入的内容')
    return ok, problems


def import_routes(text: str) -> dict:
    table, problems = parse_import(text)
    if len(table) > MAX_IMPORT:
        # 每条导入都要读+写一次上游配置：粘贴一个别的大文件进来会变成几千次写盘，
        # 而且线路表本身也不该有这么多条（选线路时是人眼看的下拉）。
        raise RouteError(f'一次最多导入 {MAX_IMPORT} 条（这次有 {len(table)} 条），请分批')
    for name, entry in table.items():
        upsert(name, entry['url'])
    return {'imported': len(table), 'problems': problems}


def pool_settings() -> dict:
    """池配置 + 一览（界面用）：开关 / 策略 / 间隔 / 启用中的线路数。"""
    pool = config.proxy_pool_config()
    enabled_routes = [name for name, on in config.proxy_route_enabled().items() if on]
    return {**pool, 'strategies': list(config.STRATEGIES),
            'routes_total': len(config.proxy_routes()),
            'routes_enabled': len(enabled_routes)}


def save_pool_settings(body: dict) -> dict:
    """保存池配置（开关 / 策略 / 间隔）。策略必须是已知值，间隔 0~86400 秒。"""
    from .. import db

    current = config.proxy_pool_config()
    strategy = str(body.get('strategy', current['strategy']) or current['strategy'])
    if strategy not in config.STRATEGIES:
        raise RouteError('未知的池策略')
    try:
        interval = int(body.get('interval_seconds', current['interval_seconds']))
    except (TypeError, ValueError) as exc:
        raise RouteError('间隔必须是整数秒') from exc
    if interval < 0 or interval > 86400:
        raise RouteError('间隔要在 0~86400 秒之间（0 = 每次重新挑）')
    db.set_setting('proxy_pool', {'enabled': bool(body.get('enabled', current['enabled'])),
                                  'strategy': strategy,
                                  'interval_seconds': interval})
    return pool_settings()


def record_probe(name: str, result: dict) -> None:
    """把探测结果记进面板设置：列表刷新后仍能显示「上次测试」的状态。"""
    try:
        from .. import db
        table = db.get_setting('proxy_health', {}) or {}
        if not isinstance(table, dict):
            table = {}
        table[name] = {'ok': bool(result.get('ok')), 'ms': int(result.get('ms') or 0),
                       'at': int(time.time()),
                       'exit_ip': str(result.get('exit_ip') or ''),
                       'error': str(result.get('error') or '')[:200]}
        if len(table) > 500:
            keep = sorted(table.items(), key=lambda kv: int(kv[1].get('at') or 0),
                          reverse=True)[:500]
            table = dict(keep)
        db.set_setting('proxy_health', table)
    except Exception:            # noqa: BLE001 —— 旁路信息，写不进去不影响探测本身
        pass


def health() -> dict[str, dict]:
    """线路名 → 上次探测结果（没有记录就不出现在返回里）。"""
    try:
        from .. import db
        table = db.get_setting('proxy_health', {}) or {}
    except Exception:            # noqa: BLE001
        return {}
    return table if isinstance(table, dict) else {}


def _proxy_error_hint(exc: Exception) -> str:
    """把常见代理错误翻译成可行动的提示（issue #379）。

    用户看到的原始错误是 `ProxyError: ... Malformed reply` / `SSL:
    WRONG_VERSION_NUMBER`，很难从中判断「是协议填错、端口填错，还是代理拒绝」。
    这里只做**归类提示**，原始错误仍原样返回，排障时两者都要看。
    """
    text = f'{type(exc).__name__}: {exc}'
    low = text.lower()
    if 'malformed reply' in low:
        return '代理协议不匹配：地址可能不是 SOCKS5，或端口不是代理端口。确认服务商给的是 http:// 还是 socks5://'
    if 'wrong_version_number' in low or ('ssl' in low and 'wrong version' in low):
        return 'TLS 握手失败：多半是协议/端口填错（HTTP 代理却填了 https://，或把网页端口当代理端口）'
    if 'connection not allowed by ruleset' in low:
        return '代理服务器拒绝了这个目标：检查代理的白名单/规则、账号密码，或该代理是否允许访问外网'
    if '407' in low or 'proxy authentication' in low or 'authentication required' in low:
        return '代理认证失败：检查地址里的用户名和密码（特殊字符需要 URL 编码）'
    if 'connection refused' in low:
        return '代理端口拒绝连接：确认地址、端口和代理进程是否在运行'
    if 'timeout' in low or 'timed out' in low:
        return '连接代理超时：检查网络可达性与代理地址'
    if 'name or service not known' in low or 'no such host' in low:
        return '代理域名解析失败：检查地址拼写与 DNS'
    return ''


async def probe_url(proxy: str, *, timeout: float = 8.0,
                    check_url: str | None = None) -> dict:
    """经这个代理地址发一次轻请求（默认查出口 IP 与归属地）。

    探测失败原因**原样回给界面**：配住宅代理时最该确认的就是「出口到底落在哪」，
    而失败原因（连不上 / 认证失败 / 超时）正是排查起点。
    """
    import httpx

    target = check_url or config.PROXY_PROBE_URL
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=timeout, trust_env=False) as client:
            resp = await client.get(target)
        elapsed = int((time.monotonic() - started) * 1000)
        if resp.status_code != 200:
            return {'ok': False, 'ms': elapsed, 'error': f'出口返回 HTTP {resp.status_code}'}
        data = resp.json() if 'json' in resp.headers.get('content-type', '') else {}
        return {
            'ok': True,
            'ms': elapsed,
            'exit_ip': str(data.get('ip') or data.get('query') or ''),
            'exit_info': ' · '.join(str(x) for x in (data.get('country'), data.get('city'),
                                                     data.get('org')) if x),
        }
    except Exception as exc:  # noqa: BLE001 —— 探测失败原因原样回给界面
        result = {'ok': False, 'ms': int((time.monotonic() - started) * 1000),
                  'error': f'{type(exc).__name__}: {exc}'}
        hint = _proxy_error_hint(exc)
        if hint:
            result['hint'] = hint
        return result


async def probe_route(name: str, *, timeout: float = 8.0,
                      check_url: str | None = None) -> dict:
    """按线路名探测（界面列表里的「测试」按钮）。"""
    url = routes().get(validate_name(name))
    if not url:
        raise RouteError(f'线路 {name!r} 不存在', 404)
    if config.SESSION_PLACEHOLDER in url:
        # 旧版会话模板：拿字面量 `{account}` 去连必然认证失败，换个示例会话名探
        url = config.render_route_url(url, 'wb-probe')
    return await probe_url(url, timeout=timeout, check_url=check_url)
