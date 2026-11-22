"""Typed, account-scoped refresh backoff and durable warnings."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import re
import time

from .. import config, db, upstreamsvc
from . import wb2api

_task: asyncio.Task | None = None
MIN_WAIT, MAX_WAIT = 300, 21600
_RISK_TASK_KINDS = (
    'keepalive', 'checkin', 'travel', 'activity', 'user-resource', 'school', 'cat', 'taskrun',
)


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
    if re.search(r'\b429\b', message):
        return ('token_refresh' if kind == 'keepalive' or 'refresh:' in message else kind, 429)
    server_error = re.search(r'http (5\d\d)', message, re.I)
    if server_error or 'grant credits failed' in message:
        return kind, int(server_error.group(1)) if server_error else 500
    return None


def _backfill_detail(row) -> str:
    raw = str(row['detail'] or '') if 'detail' in row.keys() else ''
    if raw:
        return raw
    uid = str(row['uid'] or '')
    if not uid:
        return ''
    kind = 'keepalive' if row['operation'] == 'token_refresh' else str(row['operation'] or '')
    log_row = db.query_one(
        'SELECT message FROM task_logs WHERE ts=? AND kind=? AND (uid=? OR uid=?) LIMIT 1',
        (int(row['ts'] or 0), kind, uid, uid[:8]),
    )
    if log_row and log_row['message']:
        found = ' '.join(str(log_row['message']).split())[:300]
        db.execute("UPDATE account_risk_events SET detail=? WHERE id=? AND detail=''",
                   (found, row['id']))
        return found
    return ''


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


async def warnings(*, group_id: int | None = None, all_groups: bool = False, realm: str | None = None,
                   include_acknowledged: bool = False) -> dict:
    initialize()
    from . import tasklog
    notes = db.account_notes()
    groups = _groups() if all_groups else [upstreamsvc.default_upstream() if group_id is None else upstreamsvc.get_upstream(group_id)]
    items, failed_groups, total = [], [], 0
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
        rows = db.query('SELECT * FROM account_risk_events WHERE scope=? ORDER BY ts DESC LIMIT 200', (scope,))
        seen_paused = set()
        for row in rows:
            if realm in ('cn', 'global') and row['realm'] != realm:
                continue
            total += 1
            risk = (pool.get(row['uid'], {}).get('refresh_risk') or {}) if known else {
                'until': row['pause_until'], 'needs_review': bool(row['needs_review']),
                'failures': row['failures'], 'last_failure_at': row['last_failure_at']}
            token_warning = row['operation'] == 'token_refresh' and row['http_status'] == 429
            active = is_risk_pause({'refresh_risk': risk}) and token_warning and row['ts'] == risk.get('last_failure_at')
            if row['acknowledged'] and not active and not include_acknowledged:
                continue
            if active and row['uid'] in seen_paused:
                continue
            if active:
                seen_paused.add(row['uid'])
            detail = _backfill_detail(row)
            nickname = str(local.get(row['uid'], {}).get('nickname') or row['nickname'] or row['uid'])
            items.append({k: row[k] for k in ('id', 'uid', 'realm', 'operation', 'http_status', 'ts', 'action_error')} |
                         {'nickname': nickname, 'note': notes.get(str(row['uid'] or ''), ''),
                          'detail': detail, 'detail_cn': tasklog.translate_message(detail) if detail else '',
                          'paused': active if known else None, 'pause_until': risk.get('until', 0),
                          'needs_review': bool(risk.get('needs_review')), 'failures': risk.get('failures', 0),
                          'group': group['name'], 'historical': bool(row['historical']), 'acknowledged': bool(row['acknowledged'])})
    return {'items': sorted(items, key=lambda x: x['ts'], reverse=True), 'failed_groups': failed_groups, 'total': total}


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
            await warnings(all_groups=True)
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
