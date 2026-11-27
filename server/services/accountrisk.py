"""Typed, account-scoped refresh backoff and durable warnings."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import re
import threading
import time
import weakref

from .. import config, db, upstreamsvc
from . import wb2api

_task: asyncio.Task | None = None
MIN_WAIT, MAX_WAIT = 300, 21600
# 页面每 30 秒心跳一次，后台采集循环也是 30 秒。缓存略短于心跳，既能让同一
# 时间窗内的多个标签页共享一次分组状态拉取，又不会把暂停/恢复状态压太久。
WARNINGS_CACHE_TTL = 20.0
_WARNINGS_LIMIT = 200
_RISK_TASK_KINDS = (
    'keepalive', 'checkin', 'travel', 'activity', 'user-resource', 'school', 'cat', 'taskrun',
)

# warnings() 的短 TTL 缓存。缓存的是「所有 realm」的完整快照（缓存键不含
# realm），这样后台 all_groups 循环与不同版本页面可以共享同一次计算；realm
# 只是在返回前做一次内存投影。缓存写入/读取由线程锁保护，单次计算由
# 每个事件循环各自的 asyncio.Lock 串行化，避免多标签页同时打满上游。
_warnings_cache: dict[tuple, tuple[float, dict]] = {}
_warnings_locks: 'weakref.WeakKeyDictionary[object, asyncio.Lock]' = weakref.WeakKeyDictionary()
_warnings_state_lock = threading.Lock()


def _clear_warnings_cache() -> None:
    """作废所有预警快照；任何会改变 account_risk_events 的写操作都必须调用。"""
    with _warnings_state_lock:
        _warnings_cache.clear()


def _cached_warnings(key: tuple) -> dict | None:
    now = time.time()
    with _warnings_state_lock:
        entry = _warnings_cache.get(key)
        if entry is None:
            return None
        expires_at, payload = entry
        if expires_at <= now:
            _warnings_cache.pop(key, None)
            return None
        return payload


def _store_warnings(key: tuple, payload: dict) -> None:
    with _warnings_state_lock:
        _warnings_cache[key] = (time.time() + WARNINGS_CACHE_TTL, payload)


def _warnings_lock() -> asyncio.Lock:
    """为当前事件循环取一把锁。

    unittest 会在同一个进程里反复 ``asyncio.run``，全局 asyncio.Lock 一旦
    在旧循环上等待过就会拒绝跨循环使用；按循环分桶可以同时服务测试与线上
    单循环。WeakKeyDictionary 让测试创建的循环可以正常回收。
    """
    loop = asyncio.get_running_loop()
    with _warnings_state_lock:
        lock = _warnings_locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            _warnings_locks[loop] = lock
        return lock


def initialize() -> int:
    db.execute('''CREATE TABLE IF NOT EXISTS account_risk_events (
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, uid TEXT NOT NULL,
        nickname TEXT NOT NULL, realm TEXT NOT NULL, operation TEXT NOT NULL,
        http_status INTEGER NOT NULL, ts INTEGER NOT NULL, historical INTEGER NOT NULL,
        acknowledged INTEGER NOT NULL DEFAULT 0, action_error TEXT NOT NULL DEFAULT '',
        protection_active INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '')''')
    with db._lock:
        columns = {row['name'] for row in db.query('PRAGMA table_info(account_risk_events)')}
        for name in ('pause_until', 'needs_review', 'failures', 'last_failure_at'):
            if name not in columns:
                db.execute(f'ALTER TABLE account_risk_events ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0')
        if 'detail' not in columns:
            db.execute("ALTER TABLE account_risk_events ADD COLUMN detail TEXT NOT NULL DEFAULT ''")
        enabled = db.get_setting('account_risk_enabled_at')
        if not isinstance(enabled, int) or enabled <= 0:
            enabled = int(time.time())
            db.set_setting('account_risk_enabled_at', enabled)
    return enabled


def scope_of(directory: Path | str | None) -> str:
    return str(Path(directory or config.AUTH_DIR).resolve())


def _groups() -> list[dict]:
    return [g for g in upstreamsvc.list_upstreams() if g.get('auth_dir')]


def _group(directory: Path | str | None) -> dict | None:
    return next((g for g in _groups() if scope_of(g['auth_dir']) == scope_of(directory)), None)


def is_risk_pause(account: dict) -> bool:
    risk = account.get('refresh_risk') or {}
    return bool(risk.get('needs_review') or int(risk.get('until') or 0) > time.time())


def record(directory: Path | str | None, account: dict, operation: str, status: int,
           ts: int, *, historical: bool = False, detail: str = '') -> tuple[str, bool]:
    initialize()
    scope, uid = scope_of(directory), str(account.get('uid') or '')
    clean_detail = ' '.join(str(detail or '').split())[:300]
    ident = hashlib.sha256(f'{scope}\0{uid}\0{operation}\0{status}\0{ts}'.encode()).hexdigest()[:32]
    with db._lock:
        conn = db.connect()
        cursor = conn.execute('''INSERT OR IGNORE INTO account_risk_events
            (id,scope,uid,nickname,realm,operation,http_status,ts,historical,detail)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (ident, scope, uid, str(account.get('nickname') or uid), account.get('realm') or 'cn',
             operation, status, ts, int(historical), clean_detail))
        inserted = cursor.rowcount == 1
        if not inserted and clean_detail:
            conn.execute("UPDATE account_risk_events SET detail=? WHERE id=? AND detail=''",
                         (clean_detail, ident))
        conn.commit()
    _clear_warnings_cache()
    return ident, inserted


def observe_pause(directory: Path | str | None, account: dict) -> None:
    """Import structured metadata, including expired warnings; never parse reasons."""
    risk = account.get('refresh_risk') or {}
    ts = int(risk.get('last_failure_at') or 0)
    if ts <= 0:
        return
    scope, uid = scope_of(directory), str(account['uid'])
    record(scope, account, 'token_refresh', 429, ts)
    db.execute('''UPDATE account_risk_events SET pause_until=?,needs_review=?,failures=?,last_failure_at=?,
        protection_active=? WHERE scope=? AND uid=? AND operation='token_refresh' ''',
        (int(risk.get('until') or 0), int(bool(risk.get('needs_review'))), int(risk.get('failures') or 0),
         ts, int(is_risk_pause(account)), scope, uid))
    _clear_warnings_cache()


async def paused_targets(directory: Path | str | None) -> set[str]:
    initialize()
    scope = scope_of(directory)
    # Cached deadlines expire during outages too. Old protection_active flags
    # and reason prefixes cannot create permanent automatic stops.
    protected = {r['uid'] for r in db.query('''SELECT DISTINCT uid FROM account_risk_events
        WHERE scope=? AND (pause_until>? OR needs_review=1)''', (scope, int(time.time())))}
    group = _group(directory)
    if group is None:
        return protected
    status = await wb2api.get_status(base_url=group['base_url'], api_key=upstreamsvc.forward_api_key(group))
    if status.get('connected'):
        for account in status.get('accounts') or []:
            uid = str(account.get('uid') or '')
            observe_pause(scope, account)
            if is_risk_pause(account):
                protected.add(uid)
            else:
                protected.discard(uid)
                db.execute('''UPDATE account_risk_events SET protection_active=0,pause_until=0,needs_review=0
                    WHERE scope=? AND uid=?''', (scope, uid))
                _clear_warnings_cache()
    return protected


async def paused(uid: str, directory: Path | str | None) -> bool:
    return uid in await paused_targets(directory)


async def pause_refresh(uid: str, directory: Path | str | None, *, ts: int | None = None,
                        retry_after: int = MIN_WAIT, detail: str = '') -> bool:
    now = int(time.time()) if ts is None else ts
    account = next((a for a in wb2api.list_auth_accounts(Path(scope_of(directory))) if a.get('uid') == uid), None)
    if account is None:
        return False
    ident, inserted = record(directory, account, 'token_refresh', 429, now, detail=detail)
    group = _group(directory)
    if group is None:
        db.execute('UPDATE account_risk_events SET action_error=? WHERE id=?', ('group_unavailable', ident))
        return False
    if ts is not None:
        status = await wb2api.get_status(base_url=group['base_url'], api_key=upstreamsvc.forward_api_key(group))
        live = next((a for a in status.get('accounts') or [] if str(a.get('uid')) == uid), {})
        observe_pause(directory, {**account, **live})
        # Native callbacks already applied these events. Replayed logs must not
        # extend holds or undo recovery, even when a deadline has already passed.
        if not inserted or int((live.get('refresh_risk') or {}).get('last_failure_at') or 0) >= ts:
            return is_risk_pause(live)
    else:
        db.execute('UPDATE account_risk_events SET acknowledged=0 WHERE id=?', (ident,))
    ok, _, code, payload = await wb2api.set_refresh_risk(uid, max(MIN_WAIT, min(MAX_WAIT, retry_after)),
        base_url=group['base_url'], api_key=upstreamsvc.forward_api_key(group))
    if ok and isinstance(payload.get('refresh_risk'), dict):
        observe_pause(directory, {**account, **payload})
        db.execute('UPDATE account_risk_events SET action_error=? WHERE id=?', ('', ident))
        return is_risk_pause(payload)
    # Older gateways remain warning-only: never rename files or set manual_disabled.
    db.execute('UPDATE account_risk_events SET action_error=? WHERE id=?', (code if not ok else 'pause_unconfirmed', ident))
    return False


def classify_task(event: dict) -> tuple[str, int] | None:
    message, kind = str(event.get('message') or ''), str(event.get('kind') or '')
    if kind not in _RISK_TASK_KINDS:
        return None
    # 重试中间态不是最终结果（issue #405）：上游偶发 5xx 时先记一条
    # 「— 5s 后重试(1/3)」，同一轮往往随后成功。这类行会长期留在容器日志
    # 末尾被反复重放，据此告警等于噪音。
    if '后重试' in message:
        return None
    if re.search(r'\b429\b', message):
        return ('token_refresh' if kind == 'keepalive' or 'refresh:' in message else kind, 429)
    server_error = re.search(r'http (5\d\d)', message, re.I)
    if server_error or 'grant credits failed' in message:
        return kind, int(server_error.group(1)) if server_error else 500
    return None


def _backfill_details(rows) -> dict[str, str]:
    """一次性为多行历史预警补齐 detail。

    旧实现对每一行单独 ``SELECT ... LIMIT 1`` + ``UPDATE``；一次 200 行的 GET
    会放大成数百条 SQL。这里按 (ts, kind, uid/uid 前缀) 批量取候选日志，
    一次查询后在内存里匹配，再用一次 executemany 写回。找不到的行保留空串。
    """
    details: dict[str, str] = {}
    pending: list[tuple[str, int, str, str]] = []
    for row in rows:
        raw = str(row['detail'] or '') if 'detail' in row.keys() else ''
        if raw:
            details[str(row['id'])] = raw
            continue
        uid = str(row['uid'] or '')
        if not uid:
            details[str(row['id'])] = ''
            continue
        kind = 'keepalive' if row['operation'] == 'token_refresh' else str(row['operation'] or '')
        pending.append((str(row['id']), int(row['ts'] or 0), kind, uid))
    if not pending:
        return details

    ts_values = sorted({p[1] for p in pending})
    kind_values = sorted({p[2] for p in pending})
    uid_values = sorted({u for _, _, _, u in pending} | {u[:8] for _, _, _, u in pending})
    def marks(values: list) -> str:
        return ','.join('?' for _ in values)

    logs = db.query(
        'SELECT ts,kind,uid,message FROM task_logs '
        f'WHERE ts IN ({marks(ts_values)}) AND kind IN ({marks(kind_values)}) '
        f'AND uid IN ({marks(uid_values)}) AND message<>\'\' ORDER BY id ASC',
        (*ts_values, *kind_values, *uid_values),
    )
    found: dict[tuple[int, str, str], str] = {}
    for log in logs:
        key = (int(log['ts'] or 0), str(log['kind'] or ''), str(log['uid'] or ''))
        found.setdefault(key, str(log['message'] or ''))

    updates: list[tuple[str, str]] = []
    for ident, ts, kind, uid in pending:
        message = found.get((ts, kind, uid)) or found.get((ts, kind, uid[:8])) or ''
        clean = ' '.join(message.split())[:300]
        details[ident] = clean
        if clean:
            updates.append((clean, ident))
    if updates:
        db.executemany("UPDATE account_risk_events SET detail=? WHERE id=? AND detail=''", updates)
    return details


async def handle_tasks(events: list[dict], directory: Path | str | None = None) -> None:
    enabled = initialize()
    if directory is not None:
        target = Path(scope_of(directory))
        dir_accounts = [(target, a) for a in wb2api.list_auth_accounts(target)]
    else:
        seen_scopes: set[str] = set()
        dir_accounts = []
        for raw_dir in [config.AUTH_DIR, *(g['auth_dir'] for g in _groups())]:
            sc = scope_of(raw_dir)
            if sc in seen_scopes:
                continue
            seen_scopes.add(sc)
            dir_accounts.extend((Path(sc), a) for a in wb2api.list_auth_accounts(Path(sc)))
    fallback_dir = directory or config.AUTH_DIR
    for event in events:
        classified = classify_task(event)
        if not classified or not event.get('uid'):
            continue
        uid_prefix = str(event['uid'])
        matches = [(d, a) for d, a in dir_accounts
                   if str(a.get('uid') or '').startswith(uid_prefix)]
        operation, status = classified
        ts = int(event.get('ts') or 0)
        if ts <= 0:
            continue
        detail = str(event.get('message') or '')
        if len(matches) != 1:
            ident, _ = record(fallback_dir, {'uid': uid_prefix}, operation, status, ts,
                              historical=ts < enabled, detail=detail)
            db.execute('UPDATE account_risk_events SET action_error=? WHERE id=?',
                       ('account_id_ambiguous' if matches else 'account_not_found', ident))
            continue
        matched_dir, matched_acct = matches[0]
        if operation == 'token_refresh' and status == 429 and ts >= enabled:
            await pause_refresh(matched_acct['uid'], matched_dir, ts=ts, detail=detail)
        else:
            record(matched_dir, matched_acct, operation, status, ts,
                   historical=ts < enabled, detail=detail)


def _warning_severity(operation: str, status: int, active: bool) -> str:
    """分级只用于页面降噪；保护性暂停仍是最高优先级。"""
    if active:
        return 'critical'
    if operation == 'taskrun' and int(status or 0) >= 500:
        # 面板发起的一键执行失败通常只影响一次批量任务，不应该在主页常驻。
        return 'info'
    return 'warning'


def _project_warnings(payload: dict, realm: str | None) -> dict:
    """把「全部 realm」的缓存快照投影成调用方要的版本。"""
    if realm in ('cn', 'global'):
        items = [item for item in payload['items'] if item['realm'] == realm]
        total = payload['realm_totals'].get(realm, 0)
    else:
        items = list(payload['items'])
        total = payload['total']
    return {'items': items, 'failed_groups': payload['failed_groups'], 'total': total}


async def _collect_warnings(*, group_id: int | None, all_groups: bool,
                            include_acknowledged: bool, active_only: bool) -> dict:
    initialize()
    from . import tasklog
    notes = db.account_notes()
    groups = _groups() if all_groups else [upstreamsvc.default_upstream() if group_id is None else upstreamsvc.get_upstream(group_id)]
    items, failed_groups, total = [], [], 0
    realm_totals: dict[str, int] = {}
    for group in groups:
        if not group or not group.get('auth_dir'):
            continue
        scope = scope_of(group['auth_dir'])
        status = await wb2api.get_status(base_url=group['base_url'], api_key=upstreamsvc.forward_api_key(group))
        known = status.get('connected') is True
        if not known:
            failed_groups.append(group['name'])
        pool = {str(a.get('uid')): a for a in status.get('accounts') or []}
        local = {a['uid']: a for a in wb2api.list_auth_accounts(Path(scope))}
        for uid, account in pool.items():
            observe_pause(scope, {**local.get(uid, {}), **account})
        where = ['scope=?']
        args: list = [scope]
        if active_only:
            # 在 SQL 层先丢掉「已确认且未暂停」与历史回填行，避免每组拉 200 行
            # 再在 Python 里筛。保护性暂停行即使已确认也保留（与页面语义一致）。
            where.append("((acknowledged=0 AND historical=0) OR (operation='token_refresh' "
                         "AND http_status=429 AND (needs_review=1 OR pause_until>?)))")
            args.append(int(time.time()))
        rows = db.query(
            f"SELECT * FROM account_risk_events WHERE {' AND '.join(where)} "
            f'ORDER BY ts DESC LIMIT {_WARNINGS_LIMIT}', args)
        seen_paused: set[str] = set()
        visible = []
        for row in rows:
            risk = (pool.get(row['uid'], {}).get('refresh_risk') or {}) if known else {
                'until': row['pause_until'], 'needs_review': bool(row['needs_review']),
                'failures': row['failures'], 'last_failure_at': row['last_failure_at']}
            token_warning = row['operation'] == 'token_refresh' and row['http_status'] == 429
            active = is_risk_pause({'refresh_risk': risk}) and token_warning and row['ts'] == risk.get('last_failure_at')
            if active_only and not (active or (not row['acknowledged'] and not row['historical'])):
                # SQL 会带上「有暂停标记」的候选行；若上游状态已恢复且已确认，
                # 这里只做最后一道状态校验，不再承担全量筛选。
                continue
            if row['acknowledged'] and not active and not include_acknowledged:
                continue
            if active and row['uid'] in seen_paused:
                continue
            # 计数必须在**所有过滤之后**：issue #405 的现场是「确认后列表清空、
            # 但接口仍返回 total=4」，前端拿 total 做角标，于是怎么点都消不掉。
            # total / realm_totals 都只统计本次真正返回的条目。
            total += 1
            realm_totals[row['realm']] = realm_totals.get(row['realm'], 0) + 1
            if active:
                seen_paused.add(row['uid'])
            visible.append((row, active, risk))
        details = _backfill_details([row for row, _, _ in visible])
        for row, active, risk in visible:
            detail = details.get(str(row['id']), '')
            nickname = str(local.get(row['uid'], {}).get('nickname') or row['nickname'] or row['uid'])
            items.append({k: row[k] for k in ('id', 'uid', 'realm', 'operation', 'http_status', 'ts', 'action_error')} |
                         {'nickname': nickname, 'note': notes.get(str(row['uid'] or ''), ''),
                          'detail': detail, 'detail_cn': tasklog.translate_message(detail) if detail else '',
                          'paused': active if known else None, 'pause_until': risk.get('until', 0),
                          'needs_review': bool(risk.get('needs_review')), 'failures': risk.get('failures', 0),
                          'severity': _warning_severity(row['operation'], row['http_status'], active),
                          'group': group['name'], 'historical': bool(row['historical']), 'acknowledged': bool(row['acknowledged'])})
    return {'items': sorted(items, key=lambda x: x['ts'], reverse=True),
            'failed_groups': failed_groups, 'total': total, 'realm_totals': realm_totals}


async def warnings(*, group_id: int | None = None, all_groups: bool = False, realm: str | None = None,
                   include_acknowledged: bool = False, active_only: bool = False) -> dict:
    """读取预警快照。

    缓存键不包含 realm：缓存保存全 realm 结果，返回前只做内存投影。这样后台
    ``warnings(all_groups=True, active_only=True)`` 与仪表盘/账号页（带 realm）
    能命中同一条缓存；``active_only`` 仍由 SQL 过滤，缓存里不会存 200 行历史。
    """
    initialize()
    key = (group_id, bool(all_groups), bool(include_acknowledged), bool(active_only))
    cached = _cached_warnings(key)
    if cached is not None:
        return _project_warnings(cached, realm)
    async with _warnings_lock():
        cached = _cached_warnings(key)
        if cached is not None:
            return _project_warnings(cached, realm)
        payload = await _collect_warnings(group_id=group_id, all_groups=all_groups,
                                          include_acknowledged=include_acknowledged,
                                          active_only=active_only)
        _store_warnings(key, payload)
        return _project_warnings(payload, realm)


async def resume(ident: str) -> tuple[bool, str]:
    initialize()
    row = db.query_one('SELECT * FROM account_risk_events WHERE id=?', (ident,))
    if row is None:
        return False, '预警不存在'
    group = _group(row['scope'])
    if group is None:
        return False, '分组不可用'
    ok, message, _, payload = await wb2api.clear_refresh_risk(row['uid'], base_url=group['base_url'],
        api_key=upstreamsvc.forward_api_key(group))
    if not ok:
        return False, message
    if is_risk_pause(payload):
        return False, '暂停尚未解除'
    db.execute('''UPDATE account_risk_events SET pause_until=0,needs_review=0,protection_active=0
        WHERE scope=? AND uid=?''', (row['scope'], row['uid']))
    _clear_warnings_cache()
    return True, ''


async def note_refresh_success(uid: str, directory: Path | str | None) -> None:
    initialize()
    if not db.query_one('SELECT id FROM account_risk_events WHERE scope=? AND uid=? AND failures>0 LIMIT 1',
                        (scope_of(directory), uid)):
        return
    group = _group(directory)
    if group:
        await wb2api._admin_account_call(uid, 'refresh-risk/success', base_url=group['base_url'],
            api_key=upstreamsvc.forward_api_key(group))


def acknowledge(ident: str) -> bool:
    initialize()
    with db._lock:
        conn = db.connect()
        cursor = conn.execute('UPDATE account_risk_events SET acknowledged=1 WHERE id=?', (ident,))
        conn.commit()
    if cursor.rowcount == 1:
        _clear_warnings_cache()
    return cursor.rowcount == 1


async def _loop() -> None:
    initialize()
    placeholders = ','.join('?' for _ in _RISK_TASK_KINDS)
    rows = db.query(
        f"SELECT * FROM task_logs WHERE ts>=? AND kind IN ({placeholders}) "
        "AND (message LIKE '%429%' OR lower(message) LIKE '%http 5%' OR message LIKE '%grant credits failed%') "
        "ORDER BY ts DESC LIMIT 500",
        (int(time.time()) - 48 * 3600, *_RISK_TASK_KINDS),
    )
    await handle_tasks([dict(r) for r in rows])
    while True:
        try:
            # 与页面顶栏使用同一份 active_only 快照：后台循环既是状态采集，
            # 也顺带为多标签页提供 20 秒缓存，避免每个标签页各自拉一遍分组状态。
            await warnings(all_groups=True, active_only=True)
        except Exception:
            import logging
            logging.getLogger(__name__).exception('账号保护预警采集失败')
        await asyncio.sleep(30)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_loop())


def stop() -> None:
    if _task:
        _task.cancel()
