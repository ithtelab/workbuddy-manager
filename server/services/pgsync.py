"""PostgreSQL 异地备份：把本地 SQLite 全量镜像到 PG，并能反向恢复。

## 定位：为什么不是「把面板搬到 PG 上跑」

面板**仍然读写本地 SQLite**（`data/manager.db`），PG 只做**异地保险库** ——
一键把当前数据推上去、需要时再拉回来。

这样定的理由是本项目的数据访问层形态：裸 SQL、约 174 条语句、19 张表、零 ORM。
若让业务查询直接跑在 PG 上，就要处理一整层方言差异（32 处
`strftime(...,'unixepoch','localtime')` 要换成 `to_char(... AT TIME ZONE ...)`、
11 处 `ON CONFLICT`、4 处 `INSERT OR IGNORE`、单连接+RLock 要换连接池），
以及 1893 个测试的双后端策略。而「只搬数据」的形态下，**写 PG 只需要
CREATE TABLE + 批量 INSERT，读 PG 只需要 SELECT *** —— 业务代码一行不改，
既有测试不受任何影响。

## 三个关键取舍

1. **表结构不写死，从源库现读**（`PRAGMA table_info`）再生成 PG 的
   `CREATE TABLE`。手写 19 份 DDL 会在下次加列时静默漂移：PG 侧缺列，直到
   有人真的恢复一次才炸，而那时源数据可能已经没了。现读的代价是丢掉注释与
   默认值 —— 对一张「随时可从源库重建」的镜像表，这个代价可以接受。

2. **一致性快照用 `VACUUM INTO`**，而不是逐表 SELECT。导出期间面板照常服务，
   逐表读会让不同表落在不同的时点上，拿到的是一份**自相矛盾**的快照（例如
   `request_logs` 已经写进了某次调用、`usage_daily` 还没累加）。`VACUUM INTO`
   一条语句拿到事务一致的完整副本，之后从副本慢慢搬，源库只被锁住一瞬间。

3. **恢复走「就地事务」而不是换文件**。把 PG 的行写回本地时，在一个
   `BEGIN IMMEDIATE` 事务里先清空再灌入：失败自动回滚、成功即刻生效，
   **不需要重启服务，也不用跟 WAL / -shm 伴生文件打交道**（那是换文件方案里
   最容易出错、也最难在本地复现的一步）。代价是恢复期间其它数据库调用被
   `db._lock` 挡住（大库几秒），换来「要么全成、要么全不动」。

## 与「备份」有关的一条安全约定

恢复是**破坏性**的（会用 PG 的内容替换本地数据），所以恢复前一律先把当前
本地库 `VACUUM INTO` 一份到 `data/pre-restore-<时间>.db` —— 恢复错了还能退回去。
导出是只读的（只读快照 + 只写 PG），不做额外备份。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Protocol
from urllib.parse import quote

from .. import config, db

logger = logging.getLogger('workbuddy.pgsync')

STATUS_FILE = config.DATA_DIR / 'pgsync-status.json'
CONFIG_KEY = 'pg_sync_config'
LAST_EXPORT_KEY = 'pg_sync_last_export_at'
LAST_IMPORT_KEY = 'pg_sync_last_import_at'

# PG 镜像里的元信息表：记录这次导出的表清单、行数、面板版本与结构指纹。
# 以 `_wb_` 开头是为了和业务表分开 —— 它不属于面板的 19 张表，
# 导出/恢复时都要显式排除（见 _is_mirror_table）。
META_TABLE = '_wb_mirror_meta'
_MIRROR_PREFIX = '_wb_'

# 每批搬运的行数。太小则往返次数多（每次 round-trip 有固定开销），太大则
# 单事务内存与体积上去、进度更新也变粗。1000 行 × 十几列对本项目约几百 KB。
BATCH = 1000

# 连接超时（秒）：见导出/恢复里的说明。探测接口用的是同一个值。
CONNECT_TIMEOUT = 8

# 状态文件里的日志上限：备份会逐表写日志，大库几十条就够，不必无限增长。
MAX_LOGS = 200

# 前端回填密码时用的占位。保存时见到它就**保持原值**——把掩码当新密码存回去
# 是「改了下配置，备份就连不上了」的经典事故。
_MASK = '********'

# 只允许在这里出现的字段被写入配置：patch 里多出来的键一律忽略，
# 免得界面（或手工调接口）塞进什么奇怪的东西。
DEFAULT_CONFIG: dict[str, Any] = {
    'enabled': False,           # 是否开启定时自动备份
    'host': '',
    'port': 5432,
    'dbname': '',
    'user': '',
    'password': '',
    'sslmode': 'prefer',
    'interval_minutes': 0,      # 0 = 只手动（即使 enabled 为真）
    'keep_local_backup': True,  # 恢复前是否自动备份本地库
}

_SECRET_FIELDS = ('password',)

# 导出/恢复互斥：同时只能有一个在跑（手动与定时共用同一把锁）。
_job_lock = threading.Lock()
_scheduler_thread: threading.Thread | None = None
_scheduler_stop = threading.Event()


# ── 配置 ─────────────────────────────────────────────────
def _as_int(value: object, default: int) -> int:
    try:
        return int(str(value).strip() or default)
    except (TypeError, ValueError):
        return default


def get_config() -> dict:
    raw = db.get_setting(CONFIG_KEY, None)
    cfg = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        for key in DEFAULT_CONFIG:
            if key in raw:
                cfg[key] = raw[key]
    cfg['port'] = _as_int(cfg['port'], 5432)
    cfg['interval_minutes'] = max(0, _as_int(cfg['interval_minutes'], 0))
    cfg['enabled'] = bool(cfg['enabled'])
    cfg['keep_local_backup'] = bool(cfg['keep_local_backup'])
    return cfg


def merge_form(cfg: dict, patch: dict) -> dict:
    """把界面表单的值并进一份配置里。

    密码的掩码（与空串）表示「沿用原值」：把掩码当新密码存回去是「改了下配置，
    备份就连不上了」的经典事故；空串同样保留 —— 真要把密码清空的场景（PG 免密）
    远少于「界面上没填密码就点了保存」。

    「保存」与「测试连接」都要做同一套判断（后者测的是表单里刚填、还没保存的
    值），两处各写一遍必然漂移，所以只留这一份。
    """
    out = dict(cfg)
    for key in DEFAULT_CONFIG:
        if key not in patch:
            continue
        value = patch[key]
        if key in _SECRET_FIELDS and str(value) in (_MASK, ''):
            continue
        out[key] = value
    return out


def save_config(patch: dict) -> dict:
    cfg = merge_form(get_config(), patch or {})
    cfg['port'] = _as_int(cfg['port'], 5432)
    cfg['interval_minutes'] = max(0, _as_int(cfg['interval_minutes'], 0))
    cfg['enabled'] = bool(cfg['enabled'])
    cfg['keep_local_backup'] = bool(cfg['keep_local_backup'])
    db.set_setting(CONFIG_KEY, cfg)
    return cfg


def public_config() -> dict:
    """给界面的配置：密码换成掩码，附带最近两次的完成时刻。"""
    cfg = get_config()
    out = dict(cfg)
    out['password'] = _MASK if cfg['password'] else ''
    out['last_export_at'] = _as_int(db.get_setting(LAST_EXPORT_KEY, 0), 0)
    out['last_import_at'] = _as_int(db.get_setting(LAST_IMPORT_KEY, 0), 0)
    return out


def build_dsn(cfg: dict) -> str:
    """把结构化字段拼成 libpq 连接串。

    用结构化字段而不是「让用户粘一整串 DSN」：一是界面上密码要能单独掩码，
    二是 libpq 的连接串在密码含 `@` / `/` / `:` 时会被解析错位 ——
    这类故障表现为「连不上」，而错误信息里看不出是密码被截断了。
    quote(..., safe='') 把密码里的这些字符转义掉，用户不必自己操心。
    """
    host = str(cfg.get('host') or '').strip()
    if not host:
        raise ValueError('PostgreSQL 地址不能为空')
    user = quote(str(cfg.get('user') or ''), safe='')
    pwd = quote(str(cfg.get('password') or ''), safe='')
    dbname = quote(str(cfg.get('dbname') or '').strip(), safe='')
    auth = f'{user}:{pwd}@' if user else ''
    dsn = f'postgresql://{auth}{host}:{_as_int(cfg.get("port"), 5432)}/{dbname}'
    sslmode = str(cfg.get('sslmode') or '').strip()
    # prefer 是 libpq 默认，写不写一样；不写能让连接串更短、日志更好看。
    if sslmode and sslmode != 'prefer':
        dsn += f'?sslmode={quote(sslmode, safe="")}'
    return dsn


def _require_psycopg():
    """按需导入 PG 驱动。

    放在函数里而不是模块顶层：没配 PG 的部署不该因为「驱动没装」而起不来 ——
    这个模块会被 main.py 在启动时 import（为了挂定时任务），顶层 import 失败
    就会把整个面板拖down。
    """
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - 取决于部署环境
        raise RuntimeError(
            '未安装 PostgreSQL 驱动。请在面板所在环境执行：'
            'pip install "psycopg[binary]"'
        ) from exc
    return psycopg


# ── 类型映射与建表 ────────────────────────────────────────
# SQLite 是动态类型（列上的类型名只是「亲和性」提示），PG 是强类型。
# 本项目建表语句里只用了 INTEGER / REAL / TEXT 三种，映射如下；
# 没列出来的（NUMERIC / BLOB 等）按最保守的方式处理：数值给 NUMERIC、
# 其余给 TEXT —— 宁可宽一点，也不要因为类型猜错让恢复直接失败。
_PG_TYPE = {
    'INTEGER': 'BIGINT',
    'INT': 'BIGINT',
    'BIGINT': 'BIGINT',
    'SMALLINT': 'SMALLINT',
    'REAL': 'DOUBLE PRECISION',
    'DOUBLE': 'DOUBLE PRECISION',
    'DOUBLE PRECISION': 'DOUBLE PRECISION',
    'FLOAT': 'DOUBLE PRECISION',
    'NUMERIC': 'NUMERIC',
    'DECIMAL': 'NUMERIC',
    'TEXT': 'TEXT',
    'VARCHAR': 'TEXT',
    'CHAR': 'TEXT',
    'BLOB': 'BYTEA',
}


def pg_type(declared: object) -> str:
    base = str(declared or '').strip().upper()
    # PRAGMA 给的是 `TEXT` / `INTEGER` 这类裸类型名，但也可能带 `(11)`；
    # 去掉长度后再查表，查不到就退回 TEXT。
    base = base.split('(')[0].strip()
    return _PG_TYPE.get(base, 'TEXT')


def pg_ddl(table: str, columns: list[dict]) -> str:
    """按源库的列定义生成 PG 的 CREATE TABLE。

    NOT NULL 跟着源库走：源库那一列非空，说明写入侧一直在保证它非空，
    镜像表照抄不会出问题；反过来漏掉 NOT NULL 只是少一层校验，不影响搬运。
    主键同样照抄 —— 镜像表不是给业务查询用的，主键的意义在于「恢复时若
    数据有重复能早点发现」，而不是性能。
    """
    defs: list[str] = []
    for col in columns:
        piece = f'"{col["name"]}" {pg_type(col.get("type"))}'
        if col.get('notnull'):
            piece += ' NOT NULL'
        defs.append(piece)
    pks = [c['name'] for c in columns if c.get('pk')]
    if pks:
        defs.append('PRIMARY KEY (' + ', '.join(f'"{p}"' for p in pks) + ')')
    body = ',\n  '.join(defs)
    return f'CREATE TABLE "{table}" (\n  {body}\n)'


def _is_mirror_table(name: str) -> bool:
    return str(name).startswith(_MIRROR_PREFIX)


# ── 数据源 / 目标（搬运的两端）──────────────────────────
#
# 抽出这一层是为了**可测**：导出与恢复的差别只是「谁当源、谁当目标」，
# 而搬运逻辑（分批、列投影、进度）完全一样。抽出之后，整套搬运可以用
# 「SQLite 文件 → SQLite 文件」在本地跑通，不需要真的起一个 PG。
class Source(Protocol):
    def tables(self) -> list[str]: ...
    def columns(self, table: str) -> list[dict]: ...
    def rows(self, table: str, columns: list[str], batch: int) -> Iterator[list]: ...
    def close(self) -> None: ...


class _SqliteSource:
    """本地 SQLite（导出时指向 `VACUUM INTO` 出来的快照文件）。"""

    def __init__(self, path: Path | str):
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row

    def tables(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [str(r[0]) for r in rows]

    def columns(self, table: str) -> list[dict]:
        rows = self._conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        # PRAGMA table_info 的列序：cid, name, type, notnull, dflt_value, pk
        return [{'name': str(r[1]), 'type': str(r[2]),
                 'notnull': bool(r[3]), 'pk': int(r[5] or 0)} for r in rows]

    def rows(self, table: str, columns: list[str], batch: int = BATCH) -> Iterator[list]:
        collist = ', '.join(f'"{c}"' for c in columns)
        cur = self._conn.execute(f'SELECT {collist} FROM "{table}"')
        while True:
            chunk = cur.fetchmany(batch)
            if not chunk:
                break
            yield [tuple(r) for r in chunk]

    def close(self) -> None:
        self._conn.close()


class _PgSource:
    """PG 镜像（恢复时作为数据源）。"""

    def __init__(self, conn):
        self._conn = conn

    def tables(self) -> list[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' "
                "ORDER BY table_name"
            )
            return [str(r[0]) for r in cur.fetchall() if not _is_mirror_table(r[0])]

    def columns(self, table: str) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s "
                "ORDER BY ordinal_position",
                (table,),
            )
            rows = cur.fetchall()
        return [{'name': str(r[0]), 'type': str(r[1]),
                 'notnull': str(r[2]).upper() == 'NO', 'pk': 0} for r in rows]

    def rows(self, table: str, columns: list[str], batch: int = BATCH) -> Iterator[list]:
        collist = ', '.join(f'"{c}"' for c in columns)
        cur = self._conn.cursor()
        try:
            cur.execute(f'SELECT {collist} FROM "{table}"')
            while True:
                chunk = cur.fetchmany(batch)
                if not chunk:
                    break
                yield [tuple(r) for r in chunk]
        finally:
            cur.close()

    def close(self) -> None:
        pass  # 连接由调用方管理


class _PgSink:
    """PG 镜像（导出时作为写入端）。

    每次导出都 **DROP + CREATE** 而不是 TRUNCATE：全量覆盖的语义下两者等价，
    但源侧加了列时 TRUNCATE 会留下旧结构的表，紧接着的 INSERT 直接失败。
    重建则天然跟上源库结构 —— 这正是「表结构不写死」的另一半。
    """

    def __init__(self, conn):
        self._conn = conn

    def accepts(self, table: str, columns: list[str]) -> list[str]:
        return list(columns)  # 表会被重建，来什么收什么

    def prepare(self, table: str, columns: list[dict]) -> None:
        with self._conn.cursor() as cur:
            # CASCADE：万一镜像里被别的东西（视图/外键）引用了，别让导出卡在
            # 「cannot drop table ... because other objects depend on it」。
            # 镜像表是我们自己建的，级联删掉依赖是预期行为。
            cur.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
            cur.execute(pg_ddl(table, columns))

    def write(self, table: str, columns: list[str], rows: list) -> int:
        if not rows:
            return 0
        collist = ', '.join(f'"{c}"' for c in columns)
        placeholders = ', '.join(['%s'] * len(columns))
        sql = f'INSERT INTO "{table}" ({collist}) VALUES ({placeholders})'
        with self._conn.cursor() as cur:
            cur.executemany(sql, rows)
        return len(rows)

    def drop(self, table: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')


class _SqliteSink:
    """本地 SQLite（恢复时作为写入端）。

    只写**两边都有**的列：PG 镜像是按当时的源库结构建的，而本地库可能已经
    升过级（多了列）或退过版（少了列）。取交集能让「用一份稍旧的镜像恢复」
    仍然可用 —— 少的那几列保持默认值，而不是整场恢复失败。
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def accepts(self, table: str, columns: list[str]) -> list[str]:
        local = {str(r[1]) for r in self._conn.execute(f'PRAGMA table_info("{table}")')}
        if not local:
            return []  # 本地根本没有这张表（镜像来自更新的版本）→ 整表跳过
        return [c for c in columns if c in local]

    def prepare(self, table: str, columns: list[dict]) -> None:
        pass  # 表已存在（accepts 已经确认过），结构不动

    def write(self, table: str, columns: list[str], rows: list) -> int:
        if not rows:
            return 0
        collist = ', '.join(f'"{c}"' for c in columns)
        placeholders = ', '.join(['?'] * len(columns))
        self._conn.executemany(
            f'INSERT INTO "{table}" ({collist}) VALUES ({placeholders})', rows)
        return len(rows)


def copy_data(src: Source, sink, *, on_table=None, on_batch=None) -> dict:
    """把 src 的每一张表整表搬进 sink。返回 {表名: 行数} 与合计。"""
    per_table: dict[str, int] = {}
    tables = src.tables()
    for index, table in enumerate(tables, start=1):
        src_cols = src.columns(table)
        names = [c['name'] for c in src_cols]
        wanted = sink.accepts(table, names)
        if not wanted:
            per_table[table] = 0
            if on_table:
                on_table(table, index, len(tables), 0, 'skip')
            continue
        sink.prepare(table, [c for c in src_cols if c['name'] in wanted])
        moved = 0
        for chunk in src.rows(table, wanted, BATCH):
            moved += sink.write(table, wanted, chunk)
            if on_batch:
                on_batch(table, moved)
        per_table[table] = moved
        if on_table:
            on_table(table, index, len(tables), moved, 'ok')
    return {'tables': per_table, 'rows': sum(per_table.values())}


# ── 状态文件（与 updater 的 update-status.json 同构，前端复用一套渲染）──
def read_status() -> dict:
    data: dict = {}
    try:
        if STATUS_FILE.is_file():
            data = json.loads(STATUS_FILE.read_text(encoding='utf-8'))
    except Exception:  # noqa: BLE001 —— 状态文件坏了不该让接口 500
        data = {}
    if not isinstance(data, dict) or not data:
        data = {
            'running': False, 'kind': '', 'ok': None, 'step': '', 'percent': 0,
            'tables_total': 0, 'tables_done': 0, 'rows': 0,
            'logs': [], 'started_at': 0, 'finished_at': 0,
        }
    data.setdefault('logs', [])
    # 进程重启后残留的 running=true 会让界面永远转圈 —— 这里只能靠「本次进程
    # 没在跑任何任务」来判断。_job_lock 是进程内的，未持锁即视为没有任务。
    if data.get('running') and not _job_lock.locked():
        data['running'] = False
        if not data.get('finished_at'):
            data['finished_at'] = int(STATUS_FILE.stat().st_mtime)
        if data.get('ok') is None:
            data['ok'] = False
            data['step'] = '任务被中断（面板重启过）'
    # 最近两次完成时刻**随状态一起给出**（issue #122）：面板的空闲轮询只拉
    # 这个轻量接口，靠这两个字段让「上次迁移 / 上次恢复」在定时备份跑完后
    # 自动刷新——否则面板要么显示过期时刻，要么就得整份拉配置并覆盖表单。
    data['last_export_at'] = _as_int(db.get_setting(LAST_EXPORT_KEY, 0), 0)
    data['last_import_at'] = _as_int(db.get_setting(LAST_IMPORT_KEY, 0), 0)
    return data


def _write_status(data: dict) -> None:
    try:
        config.ensure_dirs()
        tmp = STATUS_FILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, STATUS_FILE)
    except OSError:
        logger.exception('写 pgsync 状态文件失败')


class _Reporter:
    """边跑边写状态文件，供前端轮询。写盘做了节流（见 flush）。"""

    def __init__(self, kind: str):
        self.data: dict = {
            'running': True, 'kind': kind, 'ok': None, 'step': '准备中',
            'percent': 0, 'tables_total': 0, 'tables_done': 0, 'rows': 0,
            'logs': [], 'started_at': int(time.time()), 'finished_at': 0,
        }
        self._last = 0.0
        _write_status(self.data)

    def log(self, text: str, level: str = 'info') -> None:
        self.data['logs'].append(
            {'ts': int(time.time()), 'level': level, 'text': str(text)[:500]})
        if len(self.data['logs']) > MAX_LOGS:
            del self.data['logs'][:-MAX_LOGS]
        self.flush(force=True)

    def step(self, text: str, percent: int | None = None) -> None:
        self.data['step'] = str(text)[:200]
        if percent is not None:
            self.data['percent'] = max(0, min(100, int(percent)))
        self.flush(force=True)

    def flush(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last < 0.4:
            return
        self._last = now
        _write_status(self.data)

    def finish(self, ok: bool, message: str) -> None:
        self.data['running'] = False
        self.data['ok'] = bool(ok)
        self.data['step'] = str(message)[:200]
        if ok:
            self.data['percent'] = 100
        self.data['finished_at'] = int(time.time())
        _write_status(self.data)


# ── 导出（SQLite → PG）──────────────────────────────────
def _snapshot_path() -> Path:
    config.ensure_dirs()
    return config.DATA_DIR / f'.pgsync-snapshot-{os.getpid()}.db'


def export_to_pg(cfg: dict, reporter: _Reporter) -> dict:
    psycopg = _require_psycopg()
    dsn = build_dsn(cfg)
    snapshot = _snapshot_path()
    if snapshot.exists():
        snapshot.unlink()
    conn = db.connect()
    with db._lock:
        # VACUUM INTO 必须在没有活动事务时执行；db.execute 每次都 commit，
        # 正常路径下连接是空闲的。这里加锁是为了挡住并发的写入。
        conn.execute('VACUUM INTO ?', (str(snapshot),))
    reporter.log(f'已生成一致性快照（{snapshot.stat().st_size // 1024} KB）')
    src = _SqliteSource(snapshot)
    try:
        # connect_timeout：黑洞地址（SYN 被丢）下 libpq 会挂到 OS 默认超时（分钟级），
        # 期间任务锁一直占着、界面「进行中」不动。8 秒足够建立内网/公网连接。
        with psycopg.connect(dsn, connect_timeout=CONNECT_TIMEOUT) as pg:
            sink = _PgSink(pg)
            tables = src.tables()
            reporter.data['tables_total'] = len(tables)
            # 上一轮导出过、这一轮已经不在源库里的表（版本回退或表被删）：
            # 留在 PG 里会变成一份「看起来有数据、其实没人再写」的僵尸表。
            # 只删我们自己在元信息里登记过的，不碰用户库里别的东西。
            for stale in _prev_tables(pg) - set(tables):
                sink.drop(stale)
                reporter.log(f'清理镜像里已不存在的表：{stale}', 'warn')

            def on_table(table, index, total, moved, status):
                reporter.data['tables_done'] = index
                if status == 'skip':
                    reporter.log(f'跳过 {table}（源库没有这张表）', 'warn')
                else:
                    reporter.data['rows'] = int(reporter.data.get('rows') or 0) + moved
                    reporter.log(f'{table}：{moved} 行')
                reporter.step(f'导出 {table}（{index}/{total}）',
                              percent=int(index * 100 / max(1, total)))

            result = copy_data(src, sink, on_table=on_table)
            _write_meta(pg, result['tables'])
            pg.commit()
        db.set_setting(LAST_EXPORT_KEY, int(time.time()))
        return result
    finally:
        src.close()
        try:
            snapshot.unlink()
        except OSError:
            pass


def _prev_tables(pg) -> set[str]:
    """上一轮导出登记过的表清单；没有（首次导出）就返回空集。

    **必须放在 SAVEPOINT 里**，而且要用 `to_regclass` 先探存在性。
    理由是 PG 与 SQLite 在这一点上语义不同：SQLite 里一条语句失败只是那条
    语句失败，而 PG 会把**整个事务**置为 aborted —— 之后任何语句都直接报
    「current transaction is aborted」，直到 rollback。

    首次导出时元信息表根本不存在，`SELECT ... FROM _wb_mirror_meta` 必然失败；
    若只用 try/except 吞掉异常，紧接着的建表与 COPY 全部会被拒 ——
    表现为**第一次备份必定失败**，而且报错指向后面那条无辜的语句。
    """
    try:
        with pg.cursor() as cur:
            cur.execute('SAVEPOINT pgsync_prev')
            try:
                cur.execute('SELECT to_regclass(%s)', (META_TABLE,))
                found = cur.fetchone()
                if not found or found[0] is None:
                    cur.execute('RELEASE SAVEPOINT pgsync_prev')
                    return set()
                cur.execute(f'SELECT value FROM "{META_TABLE}" WHERE key = %s', ('tables',))
                row = cur.fetchone()
                cur.execute('RELEASE SAVEPOINT pgsync_prev')
            except Exception:  # noqa: BLE001 —— 表在但结构不对/已损坏
                cur.execute('ROLLBACK TO SAVEPOINT pgsync_prev')
                return set()
        if row:
            return set(json.loads(row[0]) or [])
    except Exception:  # noqa: BLE001 —— 连 SAVEPOINT 都建不了（只读事务等）
        logger.warning('读取镜像元信息失败，按首次导出处理', exc_info=True)
    return set()


def _write_meta(pg, tables: dict) -> None:
    info = {
        'tables': sorted(tables),
        'rows': {k: int(v) for k, v in tables.items()},
        'exported_at': int(time.time()),
        'app_version': _app_version(),
    }
    with pg.cursor() as cur:
        cur.execute(
            f'CREATE TABLE IF NOT EXISTS "{META_TABLE}" '
            '(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        cur.execute(f'DELETE FROM "{META_TABLE}"')
        for key, value in info.items():
            cur.execute(f'INSERT INTO "{META_TABLE}" (key, value) VALUES (%s, %s)',
                        (key, json.dumps(value, ensure_ascii=False)))


def _app_version() -> str:
    try:
        from . import updater
        return updater.current_version()
    except Exception:  # noqa: BLE001
        return ''


# ── 恢复（PG → SQLite）──────────────────────────────────
def import_from_pg(cfg: dict, reporter: _Reporter) -> dict:
    psycopg = _require_psycopg()
    dsn = build_dsn(cfg)

    if cfg.get('keep_local_backup', True):
        backup = _local_backup()
        if backup:
            reporter.log(f'恢复前已备份当前数据到 {backup.name}')

    conn = db.connect()
    # 同导出：连接阶段也要有超时（见上面的说明）。这里刻意**在取 db._lock 之前**连接，
    # 所以连不上时面板不会跟着卡住。
    with psycopg.connect(dsn, connect_timeout=CONNECT_TIMEOUT) as pg:
        src = _PgSource(pg)
        available = set(src.tables())
        local = _local_tables(conn)
        missing = sorted(local - available)
        if missing:
            # 措辞必须与行为一致：这些表**不会被清空**。恢复的语义虽然有「让本地等于
            # 镜像」的一面，但镜像可能来自更早的版本（那时这张表还不存在），清空等于
            # 把镜像里根本没有的数据删掉 —— 那是不可恢复的损失。所以保持现状，只提示。
            reporter.log(
                '镜像里没有这些表，恢复不会改动它们（保持现状）：' + '、'.join(missing),
                'warn')
        total = len(available & local)
        reporter.data['tables_total'] = total

        sink = _SqliteSink(conn)
        with db._lock:
            try:
                conn.execute('BEGIN IMMEDIATE')
                for index, table in enumerate(sorted(available & local), start=1):
                    src_cols = src.columns(table)
                    names = [c['name'] for c in src_cols]
                    wanted = sink.accepts(table, names)
                    if not wanted:
                        # 两边同名却没有任何公共列 —— 结构已经对不上了。
                        # 这时**不清空**：清掉等于把一张表的数据凭空删掉，
                        # 而镜像里那份又灌不进来。
                        # （遍历的是 available & local 的交集，所以这里的「没有公共列」
                        #   就是字面意思；镜像有、本地没有的表根本不会进这个循环。）
                        reporter.log(f'跳过 {table}（与镜像没有公共列）', 'warn')
                        reporter.data['tables_done'] = index
                        continue
                    # 先清空再灌入：恢复的语义是「让本地等于镜像」，而不是合并
                    # —— 合并会把镜像里已经删掉的记录留在本地。
                    conn.execute(f'DELETE FROM "{table}"')
                    for chunk in src.rows(table, wanted, BATCH):
                        sink.write(table, wanted, chunk)
                    reporter.data['tables_done'] = index
                    reporter.step(f'恢复 {table}（{index}/{total}）',
                                  percent=int(index * 100 / max(1, total)))
                    reporter.log(f'{table}：已恢复')
                conn.commit()
            except Exception:
                # 整体回滚：恢复失败时本地库保持原样（这正是选「就地事务」
                # 而不是换文件的原因 —— 换文件方案下失败会留下半个库）。
                conn.rollback()
                raise
        db.set_setting(LAST_IMPORT_KEY, int(time.time()))
        return {'tables': total}


def _local_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return {str(r[0]) for r in rows if not _is_mirror_table(r[0])}


def _local_backup() -> Path | None:
    """恢复前把当前本地库整份快照出来，供「恢复错了」时退回。"""
    try:
        config.ensure_dirs()
        path = config.DATA_DIR / f'pre-restore-{time.strftime("%Y%m%d-%H%M%S")}.db'
        conn = db.connect()
        with db._lock:
            conn.execute('VACUUM INTO ?', (str(path),))
        return path
    except Exception:  # noqa: BLE001 —— 备份失败不该阻止恢复，但要留下痕迹
        logger.exception('恢复前备份本地库失败')
        return None


# ── 任务调度 ─────────────────────────────────────────────
def start_job(kind: str) -> tuple[bool, str]:
    """启动一次导出/恢复。返回 (是否启动, 说明)。"""
    if kind not in ('export', 'import'):
        return False, '未知的任务类型'
    if not _job_lock.acquire(blocking=False):
        return False, '已有备份或恢复任务在进行中，请等它结束'
    # 拿到锁之后的任何一条提前返回都必须先还锁 —— 漏掉一处，这个功能就永久
    # 卡在「已有任务在进行中」，而且只有重启面板才能恢复。
    try:
        cfg = get_config()
        if not str(cfg.get('host') or '').strip():
            _job_lock.release()
            return False, '请先填写 PostgreSQL 地址并保存'
        thread = threading.Thread(target=_run_job, args=(kind, cfg),
                                  name=f'pgsync-{kind}', daemon=True)
        thread.start()
    except Exception:
        _job_lock.release()
        raise
    return True, '已开始'


def _run_job(kind: str, cfg: dict) -> None:
    reporter = _Reporter(kind)
    label = '导出' if kind == 'export' else '恢复'
    try:
        if kind == 'export':
            result = export_to_pg(cfg, reporter)
            reporter.finish(True, f'导出完成：{len(result["tables"])} 张表、{result["rows"]} 行')
        else:
            result = import_from_pg(cfg, reporter)
            reporter.finish(True, f'恢复完成：{result["tables"]} 张表')
        logger.info('PG %s 完成', label)
    except Exception as exc:  # noqa: BLE001 —— 任务线程的异常只能记在这里
        logger.exception('PG %s 失败', label)
        reporter.log(f'{label}失败：{exc}', 'error')
        reporter.finish(False, f'{label}失败：{exc}')
    finally:
        _job_lock.release()


def test_connection(cfg: dict) -> dict:
    """探测连通性。不抛异常 —— 结果直接回给界面。

    **先验配置、再要驱动**：地址没填是用户当下要改的东西，而缺驱动是环境问题。
    反过来的话，没装驱动的机器上填了个空地址，用户看到的是「未安装驱动」——
    照着它去装驱动，回来还是连不上（因为地址仍然空着）。
    """
    try:
        dsn = build_dsn(cfg)
    except ValueError as exc:
        return {'ok': False, 'message': str(exc)}
    try:
        psycopg = _require_psycopg()
    except RuntimeError as exc:
        return {'ok': False, 'message': str(exc)}
    try:
        with psycopg.connect(dsn, connect_timeout=8) as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT version()')
                row = cur.fetchone()
                cur.execute('SELECT current_database(), current_user')
                who = cur.fetchone()
        version = str(row[0]).split(',')[0] if row else ''
        return {
            'ok': True,
            'message': f'连接成功：{who[0]} / {who[1]}' if who else '连接成功',
            'server_version': version,
        }
    except Exception as exc:  # noqa: BLE001 —— 驱动异常类型很杂，统一转文案
        return {'ok': False, 'message': f'连接失败：{exc}'}


def start_scheduler() -> None:
    """定时自动导出。与手动导出共用 _job_lock，不会并发。"""
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop,
                                         name='pgsync-scheduler', daemon=True)
    _scheduler_thread.start()


def stop_scheduler() -> None:
    _scheduler_stop.set()


def _scheduler_loop() -> None:
    # 一分钟巡检一次。间隔本身由 interval_minutes 决定，这里只是「多久看一眼
    # 到点没有」——比 60 秒更密没有意义（间隔配置的最小粒度就是分钟）。
    while not _scheduler_stop.wait(60):
        try:
            cfg = get_config()
            interval = int(cfg.get('interval_minutes') or 0)
            if not cfg.get('enabled') or interval <= 0:
                continue
            if _job_lock.locked():
                continue
            last = _as_int(db.get_setting(LAST_EXPORT_KEY, 0), 0)
            if time.time() - last < interval * 60:
                continue
            ok, message = start_job('export')
            if ok:
                logger.info('PG 定时备份已启动（间隔 %d 分钟）', interval)
            else:
                logger.warning('PG 定时备份未启动：%s', message)
        except Exception:  # noqa: BLE001 —— 巡检线程绝不能因为一次异常退出
            logger.exception('PG 定时备份巡检失败')
