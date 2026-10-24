"""SQLite 存储层：密钥、日志、用量、IP 规则与全局设置。"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import config

logger = logging.getLogger('workbuddy.db')

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def day_of(ts: int | float | None = None) -> str:
    """把时间戳换算成「哪一天」，全库统一用本地时区。

    必须统一口径：写入用量（bump_usage）与回填用量（backfill_usage_from_logs）
    过去一个用本地日期、一个用 UTC 日期，在 UTC+8 机器上凌晨 00:00-08:00 的调用
    会被算进两个不同的 day，导致回填把同一次调用重复计数。
    展示层（stats.py 的 today/_since）也用本地日期，故此处一律取本地。
    """
    t = time.time() if ts is None else float(ts)
    return time.strftime('%Y-%m-%d', time.localtime(t))


def day_sql(column: str = 'ts') -> str:
    """在 SQL 里按本地时区取日期的表达式（与 day_of 口径一致）。

    注意 SQLite 的 date(ts,'unixepoch') 是 UTC，不能直接用它——那正是
    之前造成口径不一致的原因。这里用 'unixepoch','localtime' 两个修饰符。

    **当心索引失效**：这个表达式把 `ts` 包在函数里，SQLite 无法再走 `ts` 上的
    索引（查询计划会退化成 SCAN，即全表扫描）。实测 5 万行时一次过滤要 13.8ms，
    而改成范围条件只要 6.0ms，且数据越多差距越大。

    所以：**只按天过滤**（`day >= X` / `day = X`）的场景请改用
    `day_start_ts()` 把它换算成时间戳范围（见那里的说明）；只有确实要**按天
    分组聚合**（GROUP BY 日期）时才用它——那种场景没法避免表达式。
    """
    return f"strftime('%Y-%m-%d', {column}, 'unixepoch', 'localtime')"


def hour_of(ts: float | None = None) -> int:
    """本地小时（0-23）。与 day_of 同一口径：**本地时区**，不是 UTC。"""
    t = time.time() if ts is None else float(ts)
    return int(time.strftime('%H', time.localtime(t)))


def hour_sql(column: str = 'ts') -> str:
    """在 SQL 里按本地小时分桶（'2026-09-29 09'）——「今日」按小时聚合用。

    与 day_sql 同样的注意事项：表达式包住列就没法走索引，所以过滤条件仍应写成
    `ts >= ?`（用 day_start_ts / hour_start_ts 换算），只有 GROUP BY 才用它。
    """
    return f"strftime('%Y-%m-%d %H', {column}, 'unixepoch', 'localtime')"


def hour_start_ts(day: str | None = None, hour: int = 0) -> int:
    """某天某小时（本地时区）的**起点时间戳**。day 为 None 时取今天。"""
    d = day or day_of()
    y, m, dd = (int(x) for x in d.split('-'))
    return int(time.mktime((y, m, dd, max(0, min(23, int(hour))), 0, 0, 0, 0, -1)))


def day_start_ts(day: str | None = None) -> int:
    """某一天（本地时区）的**零点时间戳**；day 为 None 时取今天。

    为什么需要它：`day_sql(ts) >= '2026-09-15'` 这种写法会把 `ts` 包在函数里，
    导致 `ts` 上的索引失效、退化成全表扫描。而「本地日期 >= X」与
    「ts >= X 当地零点」在语义上**完全等价**（两者都是「本地日期不早于 X」），
    换成后者就能走索引。

    ## 等价性的边界（已逐行验证，不要凭直觉改）

    两种写法都只有**下界**、没有上界 —— 也就是说未来的时间戳两边都算进来。
    `day_sql` 的写法看起来像「日期字符串比较」，但因为它同样没有上界，所以
    「本地日期 == 明天」的行在两种口径下都被包含。这一点用边界样本
    （当天零点前后一秒、次日零点）逐行比对过，结果完全一致。

    ## 时区

    用 `time.mktime(time.strptime(...))` 按**本地时区**解释 —— 与 `day_sql` 的
    `'localtime'` 修饰符同口径。两者都跟随本机时区，所以部署到不同时区的机器上
    仍然一致（这也是当初统一到本地时区的原因，见 `day_of` 的说明）。
    """
    if day is None:
        day = day_of()
    return int(time.mktime(time.strptime(day, '%Y-%m-%d')))


SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT    NOT NULL,
  key_hash      TEXT    NOT NULL,
  prefix        TEXT    NOT NULL,
  enabled       INTEGER NOT NULL DEFAULT 1,
  expires_at    INTEGER,
  max_ips       INTEGER NOT NULL DEFAULT 0,
  ip_allowlist  TEXT    NOT NULL DEFAULT '[]',
  models        TEXT    NOT NULL DEFAULT '[]',
  -- 版本归属（cn / global / 空 = 不限制）：这把密钥只能调用该版本的模型。
  -- 空值是**历史密钥**的存量形态（本列引入前创建的），保持其原有行为不变，
  -- 界面上单独标注以便管理员补填。新建密钥一律要选一个版本。
  realm         TEXT    NOT NULL DEFAULT '',
  quota         INTEGER NOT NULL DEFAULT 0,
  used_tokens   INTEGER NOT NULL DEFAULT 0,
  -- 积分额度与已用量（issue #27）：与 token 限额**各自独立**，0 = 不限。
  -- 两者可以同时设，任一超限即拒绝；用 REAL 是因为上游 credit 是小数
  -- （如 0.05 表示一次调用的倍率扣费）。
  quota_credit  REAL    NOT NULL DEFAULT 0,
  used_credit   REAL    NOT NULL DEFAULT 0,
  created_at    INTEGER NOT NULL,
  last_used_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_keys_prefix ON api_keys(prefix);

CREATE TABLE IF NOT EXISTS api_key_ips (
  key_id     INTEGER NOT NULL,
  ip         TEXT    NOT NULL,
  first_seen INTEGER NOT NULL,
  PRIMARY KEY (key_id, ip)
);

CREATE TABLE IF NOT EXISTS request_logs (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                INTEGER NOT NULL,
  key_id            INTEGER,
  ip                TEXT,
  model             TEXT,
  mapped_model      TEXT,
  status            INTEGER DEFAULT 0,
  prompt_tokens     INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0,
  latency_ms        INTEGER DEFAULT 0,
  -- 首字延迟（time-to-first-token，毫秒）：仅流式请求有意义。
  -- 与 latency_ms 不同——后者含模型生成全部内容的耗时，回答越长越大，
  -- 无法反映上游响应速度；首字延迟才是「上游多久开始回话」。
  -- NULL = 未采集到（非流式请求，或该版本之前的历史记录）。
  first_token_ms    INTEGER,
  ua                TEXT,
  error             TEXT,
  stream            INTEGER DEFAULT 0,
  -- 本次调用的真实扣费（来自上游 usage.credit）；NULL = 上游未返回，不等于 0
  credit            REAL,
  -- 版本（cn / global）：由请求的模型名前缀判定（上游的路由协议）。
  -- 为什么必须存：界面按版本切换时，日志与统计要跟着切；不存就无法回溯过滤。
  -- NULL = 该字段上线前的历史记录（或模型名无前缀）——按 cn 归类，见 realm_of_model。
  realm             TEXT
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON request_logs(ts);

CREATE TABLE IF NOT EXISTS usage_daily (
  day               TEXT    NOT NULL,
  key_id            INTEGER NOT NULL,
  model             TEXT    NOT NULL,
  requests          INTEGER NOT NULL DEFAULT 0,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  -- 当日实际扣费合计（来自上游 usage.credit；上游未返回时不计入）
  credit            REAL    NOT NULL DEFAULT 0,
  -- 版本（cn / global）；NULL 归 cn。主键含它，使两个版本的同名模型分开累计。
  realm             TEXT    NOT NULL DEFAULT 'cn',
  PRIMARY KEY (day, key_id, model, realm)
);

-- 按小时的用量累计：给「今日」趋势图用（粒度随范围走，见 stats 页）。
-- 为什么不直接查下面那张业务无关的请求日志：日志会被保留期裁剪、也能被用户
-- 手动清空（「日志」页有清除入口），而页头卡片来自 usage_daily —— 两处一旦
-- 不同源，清完日志就会出现「卡片 338、图上一片空」的自相矛盾。
-- 所以小时维度**与 usage_daily 同一写入路径、同一口径**，只是多一个 hour 列。
CREATE TABLE IF NOT EXISTS usage_hourly (
  day               TEXT    NOT NULL,
  hour              INTEGER NOT NULL,
  key_id            INTEGER NOT NULL,
  model             TEXT    NOT NULL,
  requests          INTEGER NOT NULL DEFAULT 0,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  credit            REAL    NOT NULL DEFAULT 0,
  realm             TEXT    NOT NULL DEFAULT 'cn',
  PRIMARY KEY (day, hour, key_id, model, realm)
);
CREATE INDEX IF NOT EXISTS idx_usage_hourly_day ON usage_hourly(day);

-- 管理端审计日志：登录、改密码、增删用户、改安全配置等敏感操作留痕。
-- 为什么单独一张表：这些操作不产生请求日志（那是网关的），出了事无从追溯。
CREATE TABLE IF NOT EXISTS audit_logs (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       INTEGER NOT NULL,
  actor    TEXT NOT NULL DEFAULT '',   -- 操作者用户名（anonymous = 未认证）
  action   TEXT NOT NULL DEFAULT '',   -- login / update_user / delete_user ...
  target   TEXT NOT NULL DEFAULT '',   -- 被操作对象（如被改的用户名）
  detail   TEXT NOT NULL DEFAULT '',
  ip       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(ts);

CREATE TABLE IF NOT EXISTS ip_rules (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT    NOT NULL,
  cidr       TEXT    NOT NULL,
  note       TEXT    NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ip_access_logs (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      INTEGER NOT NULL,
  ip      TEXT,
  path    TEXT,
  blocked INTEGER NOT NULL DEFAULT 0,
  ua      TEXT
);
CREATE INDEX IF NOT EXISTS idx_ip_logs_ts ON ip_access_logs(ts);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- 账号备注（issue #67）：给账号起个「人记得住」的名字。
--
-- 为什么本端存而不是写进账号文件：账号文件是**上游的**文件（它按自己 schema 读写，
-- 也会原子回写），往里塞自定义字段既可能被上游覆盖，也超出它的 schema。备注是
-- 「我们这边怎么看这些号」，按 uid 关联即可——uid 是账号的稳定标识，改文件名
-- （临时停用）或重新启用都不变，所以备注不会因为用户点了停用就丢。
CREATE TABLE IF NOT EXISTS account_notes (
  uid        TEXT PRIMARY KEY,
  note       TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

-- 签到 / 保活结果记录。上游只在失败时打日志、成功静默，
-- 因此本表用于留下我们自己触发的签到结果，便于事后追溯。
CREATE TABLE IF NOT EXISTS checkin_logs (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       INTEGER NOT NULL,
  uid      TEXT,
  nickname TEXT,
  source   TEXT NOT NULL DEFAULT 'manual',
  kind     TEXT NOT NULL DEFAULT 'checkin',
  success  INTEGER NOT NULL DEFAULT 0,
  code     INTEGER,
  message  TEXT
);
CREATE INDEX IF NOT EXISTS idx_checkin_ts ON checkin_logs(ts);

-- 上游自动任务日志（旅行 / 活跃上报 / 签到 / 保活）的结构化留痕。
-- 上游把这些结果打在容器日志里，容器重建（更新上游）后日志就没了，
-- 所以采集器解析后落到本表长期保留。dedup_key 由「容器日志时间戳 + 行内容」
-- 生成，重复采集同一条日志时用 INSERT OR IGNORE 天然去重。
CREATE TABLE IF NOT EXISTS task_logs (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        INTEGER NOT NULL,
  uid       TEXT,
  kind      TEXT NOT NULL,
  level     TEXT NOT NULL DEFAULT 'ok',
  credits   INTEGER NOT NULL DEFAULT 0,
  message   TEXT,
  dedup_key TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_task_logs_ts ON task_logs(ts);

-- 管理面**作用域化 API Token**（见 docs/api-tokens.md）。
--
-- 与 api_keys（数据面网关密钥）是**两套东西**：api_keys 只授权模型调用，
-- 与后台权限无关；本表授权的是管理面 /api/*，所以要求更严：
--   · 明文形如 wbt_<base64url>，**库中只存 SHA-256 哈希**，明文仅在创建时返回一次；
--   · prefix 用于定位（先按前缀取候选行，再常量时间比哈希），避免全表扫描；
--   · scope 决定角色（readonly → viewer，admin → admin）；
--   · 可吊销（enabled）与可过期（expires_at），鉴权时逐次校验、即时生效。
--
-- 历史教训：2026-09-14 的事故源于 users.json 里一个直接授予 admin 的
-- X-API-Key 数组。本表的形态与它**刻意不同**：在数据库而非配置文件、
-- 只存哈希、有 scope、可吊销、全程审计。
CREATE TABLE IF NOT EXISTS api_tokens (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT    NOT NULL,
  token_hash   TEXT    NOT NULL,
  prefix       TEXT    NOT NULL,
  scope        TEXT    NOT NULL,
  enabled      INTEGER NOT NULL DEFAULT 1,
  expires_at   INTEGER,
  created_at   INTEGER NOT NULL,
  created_by   TEXT    NOT NULL DEFAULT '',
  last_used_at INTEGER,
  last_used_ip TEXT
);
CREATE INDEX IF NOT EXISTS idx_tokens_prefix ON api_tokens(prefix);

-- 红包：一次创建 N 个密钥，额度按拼手气或均分分配（见 server/redpacket.py）。
--
-- 为什么只记「这批是怎么来的」，不重复存密钥信息：密钥本体（配额、用量、
-- 启停、过期）都在 api_keys 里，这里只用 key_id 指向它。同一件事存两份，
-- 迟早会出现「红包说额度 100、密钥实际是 80」的不一致。
CREATE TABLE IF NOT EXISTS red_packets (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  title         TEXT    NOT NULL DEFAULT '',
  -- 额度类别：credit（限制真实扣费）/ token（限制 token 数）。两者口径不同，
  -- 见 redpacket.py 模块头。
  quota_kind    TEXT    NOT NULL,
  total_amount  REAL    NOT NULL,
  shares        INTEGER NOT NULL,
  -- 分配方式：lucky（拼手气）/ even（均分）
  mode          TEXT    NOT NULL,
  -- 抽奖码（分享链接用）。高熵随机串，**没有它就拿不到红包里的密钥**，
  -- 所以它本身就是凭据 —— 界面上只展示给管理员，不写进日志。
  code          TEXT,
  -- 这批密钥限定的模型（JSON 数组）。
  --
  -- **token 红包必须非空，积分红包必须为空**（见 redpacket.validate）：
  -- 积分是「钱」（按真实扣费算，任何模型都能用），token 是「量」（与模型强
  -- 相关——同一段上下文在不同模型下的 token 数、输出长度、上下文窗口都不同，
  -- 不限定的话「10 万 token」这个说法就是浮动的）。记下来是为了在详情页
  -- 能回答「这个红包当初限的是哪几个模型」——密钥本身也有这份白名单，
  -- 但红包视角下看更直接。
  models        TEXT    NOT NULL DEFAULT '[]',
  created_by    TEXT    NOT NULL DEFAULT '',
  created_at    INTEGER NOT NULL,
  -- 这批密钥的失效时刻。默认 7 天（发出去的东西收不回，给个期限让它们自动清理）。
  expires_at    INTEGER NOT NULL
);

-- 每一份红包 = 一个密钥。amount 是这一份分到的额度，各份之和
-- **精确等于** red_packets.total_amount（浮点余数由 split_amount 兜底到最后一份）。
--
-- 为什么存明文 token（而 api_keys 里只存哈希）
-- -------------------------------------------
-- 密钥通常在创建时返回一次明文、之后只留哈希——但红包要**过一段时间**才由
-- 领取者抽走，抽的那一刻必须把明文给他。两个选择：
--   a) 抽奖时现场生成密钥（那就不能同时支持「管理员直接把 key 发出去」）；
--   b) 创建时把明文存下来，抽奖时取出来。
-- 这里选了 b，因为一个红包要能**同时**支持两种分发（管理员自己发 / 分享链接）。
--
-- 代价说清楚：库被读走 = 这批红包的密钥泄露。缓解措施是它们的**寿命短**
-- （默认 7 天）且**额度有限**（红包的本质就是小额分发）；而库里本来就有
-- 同等敏感的东西。真要更严，得引入加密依赖——但项目刻意保持窄依赖
-- （只有 fastapi/uvicorn/httpx/pydantic），自己写加密比明文更危险
-- （会让人以为它是安全的）。
CREATE TABLE IF NOT EXISTS red_packet_shares (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  packet_id INTEGER NOT NULL,
  key_id    INTEGER NOT NULL,
  amount    REAL    NOT NULL,
  -- 密钥明文。抽奖时原样返回给领取者；管理员自己发时也不用再查别处。
  token     TEXT    NOT NULL DEFAULT '',
  -- 领取者 IP 与时刻。NULL = 还没人抽到这一份。
  -- **每 IP 对同一个红包只能抽一次**，由 redpacket.draw 在事务里保证
  -- （不去建 UNIQUE 约束：SQLite 加约束要重建表，而这里靠事务已足够）。
  claimed_by_ip TEXT,
  claimed_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_packet_shares ON red_packet_shares(packet_id);

-- 多上游接入点（账号池分组）。
--
-- 为什么需要：本服务是**转发型**反代 —— 账号由上游挑，本端只做鉴权 / 限流 /
-- 记日志。要让「不同下游密钥走不同账号池」，本端唯一能落地的形态就是配置多个
-- 上**游接入点**，再让密钥绑定其中之一；每个接入点天然就是一个账号池分组。
--
-- 默认上游（环境变量 / 上游 config.json 那套）**不在这里** —— 它不是一行数据，
-- 而是 config.WB2API_BASE + config.upstream_api_key() 的运行时结果。这样存量
-- 部署升级后一行都不用改：密钥的 upstream_id 为空 = 走默认上游，行为与从前一致。
--
-- api_key 明文存储：与上游 config.json 里的 api_key 同等敏感，而本库权限已收到
-- 仅属主可读写（见 _restrict_db_permissions）。不引加密依赖的理由同 red_packet_shares
-- 的注释：自己写加密比明文更危险（会让人以为它是安全的）。
CREATE TABLE IF NOT EXISTS upstreams (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT    NOT NULL,
  base_url   TEXT    NOT NULL,
  api_key    TEXT    NOT NULL DEFAULT '',
  note       TEXT    NOT NULL DEFAULT '',
  enabled    INTEGER NOT NULL DEFAULT 1,
  -- 该分组的**本地账号目录**：面板按它列账号 / 添号 / 移动账号（见迁移注释）。
  auth_dir   TEXT    NOT NULL DEFAULT '',
  -- 该分组上游实例的容器名（可选）：面板「重启该分组」按它 docker restart。
  container  TEXT    NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);
"""


def _restrict_db_permissions() -> None:
    """把数据库文件（含 WAL/SHM 伴生文件）收紧到仅属主可读写。

    为什么：库里存着 **API 密钥的哈希与前缀、全部请求日志（含来源 IP 与 UA）、
    审计日志**。默认创建的 SQLite 文件是 0644——同主机的其他用户，或任何能读到
    该目录的进程，都能直接读走：密钥前缀可用于针对性爆破，日志则暴露调用方与
    内部拓扑。

    WAL 模式下还有 `-wal` / `-shm` 两个伴生文件，同样含尚未落盘的数据，
    必须一并收紧（只 chmod 主库文件是不够的）。

    Windows 上 chmod 语义有限，失败静默忽略——不影响功能。
    """
    import os
    import stat as _stat
    for suffix in ('', '-wal', '-shm'):
        p = Path(str(config.DB_PATH) + suffix)
        try:
            if p.exists():
                os.chmod(p, _stat.S_IRUSR | _stat.S_IWUSR)
        except OSError:
            pass


def _explain_db_open_failure(exc: Exception) -> str:
    """把 `unable to open database file` 翻译成「该怎么做」。

    这个报错的原始信息**完全无法定位**（issue #30）：它既不说哪个路径，
    也不说为什么。实测最常见的原因是**目录/文件的属主不对** ——
    容器以 uid 10001 运行，而 bind mount（`./data:/app/data`）的属主由宿主机
    决定：docker 首次自动创建 `./data` 时归 root，容器内的 10001 就写不进去。

    上游自己的 compose 对 `./data` 写了 chown 提示，我们的漏了 —— 于是同样的坑
    在管理端这边表现为一条 sqlite 报错，用户完全猜不到是权限。
    """
    import os as _os

    path = config.DB_PATH
    parent = path.parent
    lines = [f'无法打开数据库文件：{path}']
    reasons: list[str] = []

    try:
        if not parent.exists():
            reasons.append(f'目录不存在：{parent}')
        elif not _os.access(str(parent), _os.W_OK):
            reasons.append(f'目录不可写：{parent}')
        elif path.exists() and not _os.access(str(path), _os.W_OK):
            reasons.append(f'数据库文件不可写：{path}')
    except OSError:
        pass

    if reasons:
        lines.append('  原因：' + '；'.join(reasons))
    lines.append(f'  原始错误：{exc}')
    lines.append(
        '  最常见的成因是**目录属主不对**：容器以 uid 10001 运行，而 bind mount\n'
        '  （compose 里的 `./data:/app/data`）属主由宿主机决定 —— docker 首次自动\n'
        '  创建该目录时归 root，容器内的 10001 写不进去。宿主机执行一次即可：\n'
        '    chown -R 10001:10001 ./data\n'
        '  （在 docker-compose.yml 所在目录执行；10001 是镜像内 app 用户的 uid）\n'
        '  若目录是网络文件系统（NFS/SMB）且不支持属主修改，改用命名卷：\n'
        '    volumes:\n      - wb-manager-data:/app/data\n'
        '  volumes:\n    wb-manager-data:'
    )
    return '\n'.join(lines)


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        config.ensure_dirs()
        try:
            _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute('PRAGMA journal_mode=WAL')
            _conn.execute('PRAGMA synchronous=NORMAL')
            _conn.executescript(SCHEMA)
            _migrate(_conn)
        except sqlite3.OperationalError as exc:
            # 失败时不要留下半开的连接：否则后续调用会拿到一个不能用的 _conn，
            # 报出更莫名的错误（例如「attempt to write a readonly database」）
            if _conn is not None:
                try:
                    _conn.close()
                except Exception:  # noqa: BLE001
                    pass
                _conn = None
            raise RuntimeError(_explain_db_open_failure(exc)) from exc
        _conn.commit()
        # 建表之后再收紧权限：库文件此刻才确定存在，WAL 伴生文件也在初始化后出现
        _restrict_db_permissions()
        # 升级兜底：usage_hourly 是后加的表，存量库升级后它是空的 —— 用户点开
        # 「今日」趋势图会是一片空白，而卡片数字明明有值。用请求日志补一次今天
        # （幂等、只补今天、失败不影响启动）。
        ensure_hourly_today()
        # 启动时也清一次侧表：低流量部署可能很久攒不够触发周期清理的写入量。
        _prune_side_tables()
    return _conn


def _close_connection_locked() -> None:
    """关掉共享连接。调用方必须持有 `_lock`。

    先做一次 TRUNCATE checkpoint：WAL 模式下未合并的页还在 `-wal` 里，只 close
    不 checkpoint 的话，随后删除伴生文件会连带丢掉最后那批已提交的数据。
    """
    global _conn
    conn, _conn = _conn, None
    if conn is None:
        return
    try:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    except sqlite3.Error:
        pass
    try:
        conn.close()
    except sqlite3.Error:
        pass


def replace_database(staging: Path) -> None:
    """用 *staging* 替换当前数据库，并重新打开共享连接。

    **调用方不得持有 `_lock`**：这里是「先关闭、再改名、再重开」的窗口，
    只在替换的瞬间持锁（毫秒级）；耗时的那部分（把镜像写进临时库）由调用方
    在锁外完成 —— 这正是恢复不再阻塞网关的原因。

    staging 必须是本进程独占的完整 SQLite 库（调用方负责关闭它的连接）。
    """
    staging = Path(staging)
    if not staging.is_file():
        raise FileNotFoundError(f'待替换的数据库不存在：{staging}')

    target = Path(config.DB_PATH)
    old = Path(str(target) + '.swap-old')
    wal = Path(str(target) + '-wal')
    shm = Path(str(target) + '-shm')

    with _lock:
        # 上一次替换在第 1、2 步之间崩溃时，正式库不在、.swap-old 才是最后
        # 一份成功数据。先把它放回去再继续，绝不能当成残骸删掉。
        if not target.exists() and old.exists():
            os.replace(old, target)
        # 清掉上一次异常中断留下的残骸（正常路径下不存在）。
        try:
            old.unlink()
        except FileNotFoundError:
            pass

        _close_connection_locked()
        # 关闭连接时 SQLite 会做最后一次 checkpoint 并删除 WAL。若还剩非空
        # WAL，说明还有未合并的已提交数据（异常关闭/别的连接占着），此时
        # 删掉它就等于丢数据 —— 重开连接、放弃换库，让旧库连同 WAL 原样保留。
        if wal.exists() and wal.stat().st_size > 0:
            connect()
            raise RuntimeError('数据库 WAL 未能合并，已放弃替换以免丢数据')
        # WAL/SHM 必须在连接关闭之后删：Windows 上打开着的句柄不能删，
        # 硬删会抛 WinError 32；POSIX 上虽然能删，但旧 WAL 留在原地更危险。
        for path in (wal, shm):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        moved = False
        try:
            if target.exists():
                os.replace(target, old)
                moved = True
            os.replace(staging, target)
            # 旧库的 WAL/SHM **绝不能**留给新库：文件名相同，SQLite 会把它们
            # 当成新库自己的日志重放，轻则数据对不上，重则直接损坏。
            for path in (wal, shm):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            connect()
            if moved:
                try:
                    old.unlink()
                except OSError:
                    pass
        except Exception:
            # 回滚：关掉可能已经打开的新库，把旧文件放回去再重开。
            # 这一步失败意味着磁盘/权限出了大问题，只能留日志让人工处理。
            _close_connection_locked()
            try:
                if moved:
                    if target.exists():
                        target.unlink()
                    if old.exists():
                        os.replace(old, target)
                connect()
            except Exception:
                logger.exception('数据库替换回滚失败，需要人工检查：%s', target)
            raise


# 增量迁移：SQLite 的 CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，
# 因此新增字段必须显式 ALTER。每条用 PRAGMA 检测后再加，可重复执行。
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # (表名, 列名, 列定义)
    ('request_logs', 'credit', 'REAL'),
    ('usage_daily', 'credit', 'REAL NOT NULL DEFAULT 0'),
    # 首字延迟：可空（历史记录与非流式请求为 NULL）
    ('request_logs', 'first_token_ms', 'INTEGER'),
    # 版本（cn / global）：界面按版本切换时日志与统计要跟着切。
    # 历史记录为 NULL —— 读的时候按 cn 归类（见 realm_of_model 的注释）。
    ('request_logs', 'realm', 'TEXT'),
    # usage_daily 的 realm 列。注意：**光加列不够**——写入侧用的是四列 UPSERT，
    # 而旧库主键仍是三列，会直接抛 ON CONFLICT 不匹配（issue #9：统计静默停摆）。
    # 主键的迁移由 _rebuild_usage_daily_pk 重建表完成（SQLite 不能 ALTER 主键）。
    ('usage_daily', 'realm', "TEXT NOT NULL DEFAULT 'cn'"),
    # API 密钥的版本归属（cn / global / 空 = 不限制）。
    # 存量密钥一律为空——即保持它们原本「两版都能调」的行为，不因为升级就把
    # 人家正在用的密钥悄悄限死（那会让线上调用突然 403）。管理员在界面上
    # 看到「未限定」标记后可按需补填。
    ('api_keys', 'realm', "TEXT NOT NULL DEFAULT ''"),
    # 密钥绑定的上游接入点（多上游 / 分组隔离）。可空 = **默认上游** ——
    # 存量密钥一律为空，行为与升级前完全一致（走 WB2API_BASE 那一套）。
    # 不设外键约束：SQLite 的外键要开 PRAGMA 且删除上游时语义是「级联删密钥」还是
    # 「拒绝删」都不合适 —— 本项目在应用层做「被引用就拒绝删」（见 upstreamsvc）。
    ('api_keys', 'upstream_id', 'INTEGER'),
    # 分组（账号池）的**本地账号目录**：面板据此列账号 / 添号 / 移动账号。
    # 空 = 不做账号管理（该分组只用于密钥转发）。默认分组的目录来自 WB_AUTH_DIR，
    # 是运行时结果、不落库——与「默认上游不是数据库里的一行」同一个道理。
    ('upstreams', 'auth_dir', "TEXT NOT NULL DEFAULT ''"),
    # 分组上游实例的容器名（可选）：面板的「重启」用 `docker restart <name>`。
    # 空 = 从面板重启该分组时明确报错；绝不猜一个名字去重启别的服务。
    ('upstreams', 'container', "TEXT NOT NULL DEFAULT ''"),
    # 积分额度与已用量（issue #27）。存量密钥为 0/0 = **不限积分**，行为不变。
    #
    # 为什么要在 token 之外单独记一笔：两者**不成比例** —— 同样 1M token，
    # 便宜模型与贵模型的实际扣费能差几十倍，按 token 限额估不出花了多少积分
    # （提需求的人遇到的正是这个问题）。上游从 2026-09-13 起在末帧 usage 里
    # 带回真实 credit，我们已按请求存进 request_logs.credit，所以这里算得准。
    # 提示词缓存的三段 token（issue #69）：腾讯在流式末帧 usage 里给
    # prompt_cache_hit_tokens / prompt_cache_miss_tokens / prompt_cache_write_tokens。
    # **可空**：老上游不给这三个字段时是 NULL（「没数据」），与「给了但是 0」不是
    # 一回事——后者代表这次请求真的没命中缓存，而前者代表我们不知道。
    ('request_logs', 'cache_hit_tokens', 'INTEGER'),
    ('request_logs', 'cache_miss_tokens', 'INTEGER'),
    ('request_logs', 'cache_write_tokens', 'INTEGER'),
    ('api_keys', 'quota_credit', 'REAL NOT NULL DEFAULT 0'),
    ('api_keys', 'used_credit', 'REAL NOT NULL DEFAULT 0'),
    # 入站请求被拦的**原因**（issue #33）。
    #
    # 此前这张表只有一个 blocked 布尔：界面显示「已拦截」，但看不出是「没带
    # 密钥」「密钥不认识」还是「IP 规则拦的」。三者的处置方式完全不同（改客户端
    # 配置 / 重新发密钥 / 改 IP 规则），只报「已拦截」等于把排查成本全推给用户
    # ——实测有用户发了 issue 也说不清是哪一种。
    # 存量记录为 NULL（那时没记原因），界面按「未记录」展示。
    ('ip_access_logs', 'reason', 'TEXT'),
    # 本次请求**实际用了哪个上游账号**（issue #69）。
    #
    # 为什么需要单独记：账号是**上游**选的，本端转发时并不知道。此前日志里
    # 只有「请求了什么」，没有「谁答的」，于是两件事都无法回答：
    #   · 同一把密钥的连续请求是否被分散到了不同账号（上游的轮换是否在工作）；
    #   · 某个账号是不是在拖后腿（错误率/延迟异常）。
    # 客户端的 system prompt 命中缓存时，同一账号才会命中同一份前缀缓存——
    # 所以这一列也是判断「为什么这次没走缓存」的线索。
    #
    # 值形如 `昵称(uid8)`，与上游日志里的写法一致；脱敏与截断在写入侧做。
    # NULL = 未关联上（采集不可用、或该条在上游日志里已滚掉），界面显示「—」。
    ('request_logs', 'account', 'TEXT'),
    # 输入侧命中缓存的 token 数（上游 usage.prompt_cache_hit_tokens）。
    #
    # 为什么值得单独记：prompt_tokens 是**含**缓存的，光看它看不出这次省了
    # 多少——同一段 8k 前缀，命中与不命中的扣费能差约 17 倍（上游实测）。
    # 缓存是否生效与「账号是否稳定」强相关，和上面那列一起看才有意义。
    # NULL = 上游未返回该字段（旧版上游/非对话类请求），与「命中 0」是两回事。
    ('request_logs', 'cache_hit_tokens', 'INTEGER'),
    # 红包的抽奖支持：抽奖码 + 明文密钥 + 领取记录。
    #
    # 必须走迁移而不只是改建表语句：`CREATE TABLE IF NOT EXISTS` 对**已存在**
    # 的表什么都不做（红包那两张表在本功能的前半部分就建好了），不补列的话
    # 升级上来的部署一抽奖就报「no such column」。新装不受影响，两条路径
    # 都要能走通，所以建表语句与迁移两处都得有。
    ('red_packets', 'code', 'TEXT'),
    ('red_packet_shares', 'token', "TEXT NOT NULL DEFAULT ''"),
    ('red_packet_shares', 'claimed_by_ip', 'TEXT'),
    ('red_packet_shares', 'claimed_at', 'INTEGER'),
    # 思考强度（reasoning_effort）：客户端显式传的档位（low / medium / high / max…）。
    # NULL = 客户端没传（走模型默认档）或该字段上线前的历史记录——两者界面都显示
    # 「—」，不做推断，避免把「默认」写成某个具体档位误导排查。
    ('request_logs', 'effort', 'TEXT'),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in _MIGRATIONS:
        try:
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
        except sqlite3.Error:
            continue
        if not cols or column in cols:
            continue
        try:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {decl}')
        except sqlite3.Error:
            # 并发启动时可能已被另一进程加过，忽略即可
            pass
    _rebuild_usage_daily_pk(conn)


def _rebuild_usage_daily_pk(conn: sqlite3.Connection) -> bool:
    """把存量库的 usage_daily 主键从三列迁到四列（含 realm）。返回是否迁移了。

    为什么必须重建表：SQLite 不能改主键，`ALTER TABLE` 只加得上列。而写入侧
    用的是四列 UPSERT（`ON CONFLICT(day, key_id, model, realm)`），在旧库上会
    直接抛 `OperationalError: ON CONFLICT clause does not match any PRIMARY KEY
    or UNIQUE constraint`。**这个异常在网关里被兜住只记 warning**（旁路统计
    不该影响转发），于是表现为彻底静默：统计数字冻结在升级前、每笔请求刷一条
    日志，而页面看不出任何异常（issue #9 的现象）。

    上一版这里只做了 ADD COLUMN，注释里写「接受旧库在这一维度上的精度损失」
    —— 判断错了：精度损失指的是「两个版本的同名模型合并累计」，而实际后果是
    **一行都写不进去**，比精度损失严重得多。

    迁移要点：
      * 用 `INSERT OR REPLACE` 而非普通 INSERT：旧库合并累计的行在四列主键下
        不会冲突，但万一有重复（(day,key_id,model) 不同而 realm 相同的脏数据），
        替换比让整个迁移失败好——迁移失败会让服务起不来。
      * 先建新表、数据搬运、再 DROP 旧表、最后改名：**旧表在新表数据就位之前
        一直存在**，所以任何一步失败都不会丢数据。
      * 开头先 `DROP TABLE IF EXISTS usage_daily_new`：SQLite **不回滚 DDL**，
        迁移失败一次就会把那张半成品表留下，导致下次启动因重名再失败——形成
        永远修不好的死循环（实测确认，见 test_usage_daily_migration）。
      * realm 不需要 COALESCE 兜底：旧库该列是迁移时 `ADD COLUMN ... NOT NULL
        DEFAULT 'cn'` 建的，NULL 不存在于该列（实测写不进去）。
    """
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='usage_daily'"
        ).fetchone()
    except sqlite3.Error:
        return False
    if not row or not row[0]:
        return False  # 表不存在（全新库由 SCHEMA 建好，就是四列主键）
    ddl = row[0]
    # 已经是四列主键（新库）→ 无需处理。判据取 "realm" 是否出现在主键括号里，
    # 而不是简单看 DDL 里有没有 realm 字样——旧库也有 realm 列（ALTER 加的）。
    pk_part = ddl[ddl.upper().find('PRIMARY KEY'):] if 'PRIMARY KEY' in ddl.upper() else ''
    if 'realm' in pk_part.lower():
        return False

    try:
        # 先清掉可能残留的临时表。**这一步是必须的**（实测确认）：SQLite 的
        # 事务**不回滚 DDL** —— 在事务里 CREATE TABLE 之后即使抛异常回滚，
        # 那张表依然留在库里。于是只要迁移失败过一次，下次的 CREATE TABLE
        # 就会因重名失败，形成「每次启动都失败、统计永远修不好」的死循环。
        # 幂等的前提是能重来，所以先把上次的残骸清掉。
        conn.execute('DROP TABLE IF EXISTS usage_daily_new')
    except sqlite3.Error:
        pass

    try:
        with conn:  # 事务：任一步失败自动回滚
            conn.execute('''
                CREATE TABLE usage_daily_new (
                  day               TEXT    NOT NULL,
                  key_id            INTEGER NOT NULL,
                  model             TEXT    NOT NULL,
                  requests          INTEGER NOT NULL DEFAULT 0,
                  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
                  completion_tokens INTEGER NOT NULL DEFAULT 0,
                  credit            REAL    NOT NULL DEFAULT 0,
                  realm             TEXT    NOT NULL DEFAULT 'cn',
                  PRIMARY KEY (day, key_id, model, realm)
                )
            ''')
            conn.execute('''
                INSERT OR REPLACE INTO usage_daily_new
                  (day, key_id, model, requests, prompt_tokens, completion_tokens, credit, realm)
                SELECT day, key_id, model, requests, prompt_tokens, completion_tokens,
                       credit, realm
                FROM usage_daily
            ''')
            conn.execute('DROP TABLE usage_daily')
            conn.execute('ALTER TABLE usage_daily_new RENAME TO usage_daily')
        logger.info('usage_daily 主键已迁移为四列（含 realm），历史数据已保留')
        return True
    except sqlite3.Error as exc:
        # 迁移失败不阻断启动（服务照常跑，统计在下次启动重试）——起不来比统计
        # 不准严重得多。但**必须留下 error 级日志**：这个缺陷当初之所以拖了
        # 好几个版本才被发现，正是因为它的表现是静默的（统计冻结、页面看不出
        # 异常）。迁移再失败一次不能还是没人知道。
        logger.error(
            'usage_daily 主键迁移失败，用量统计将无法累计（历史数据未受影响，'
            '重启会重试）：%s', exc,
        )
        try:
            conn.execute('DROP TABLE IF EXISTS usage_daily_new')
        except sqlite3.Error:
            pass
        return False


def query(sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with _lock:
        return list(connect().execute(sql, tuple(args)).fetchall())


def query_one(sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
    with _lock:
        return connect().execute(sql, tuple(args)).fetchone()


def execute(sql: str, args: Iterable[Any] = ()) -> int:
    with _lock:
        conn = connect()
        cur = conn.execute(sql, tuple(args))
        conn.commit()
        return int(cur.lastrowid or 0)


def executemany(sql: str, seq: Iterable[Iterable[Any]]) -> None:
    with _lock:
        conn = connect()
        conn.executemany(sql, [tuple(x) for x in seq])
        conn.commit()


# ── settings ─────────────────────────────────────────────
def get_setting(key: str, default: Any = None) -> Any:
    row = query_one('SELECT value FROM settings WHERE key = ?', (key,))
    if not row:
        return default
    try:
        return json.loads(row['value'])
    except Exception:
        return default


# ── 账号备注（issue #67）────────────────────────────────
#
# 备注按 **uid** 关联，不按文件名：临时停用会把文件改成 `.disabled`，按文件名存
# 会让备注在停用/启用之间丢掉。调用方负责先解析出 uid（见 accounts 路由）。
def set_account_note(uid: str, note: str) -> None:
    execute(
        'INSERT INTO account_notes(uid, note, updated_at) VALUES(?, ?, ?) '
        'ON CONFLICT(uid) DO UPDATE SET note = excluded.note, updated_at = excluded.updated_at',
        (str(uid), str(note), int(time.time())),
    )


def delete_account_note(uid: str) -> None:
    execute('DELETE FROM account_notes WHERE uid = ?', (str(uid),))


def account_notes() -> dict[str, str]:
    """全部备注 {uid: note}。账号列表一次取回，避免按账号逐条查。"""
    return {str(r['uid']): str(r['note']) for r in query('SELECT uid, note FROM account_notes')}


def set_setting(key: str, value: Any) -> None:
    execute(
        'INSERT INTO settings(key, value) VALUES(?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
        (key, json.dumps(value, ensure_ascii=False)),
    )


# ── 用量累计 ─────────────────────────────────────────────
def bump_usage(
    key_id: int,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    credit: float | None = None,
    realm: str | None = None,
) -> None:
    """累计当日用量。credit 为本次真实扣费，缺省不计入（不按 0 记）。

    realm（cn / global）由调用方按模型名前缀判定后传入；缺省归 cn
    （与 realm_of_model 的口径一致：无前缀即国内版）。
    """
    day = day_of()
    r = realm if realm in ('cn', 'global') else realm_of_model(model)
    execute(
        'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, completion_tokens, credit, realm) '
        'VALUES(?, ?, ?, 1, ?, ?, ?, ?) '
        'ON CONFLICT(day, key_id, model, realm) DO UPDATE SET '
        '  requests = requests + 1, '
        '  prompt_tokens = prompt_tokens + excluded.prompt_tokens, '
        '  completion_tokens = completion_tokens + excluded.completion_tokens, '
        '  credit = credit + excluded.credit',
        (day, key_id, model, prompt_tokens, completion_tokens, float(credit or 0), r),
    )
    # 同一份数据的**小时**维度，给「今日」趋势图用。两处必须一起写：只写一处
    # 就会出现「卡片有数、小时图没有」或反过来的分歧（口径一致性见建表注释）。
    execute(
        'INSERT INTO usage_hourly(day, hour, key_id, model, requests, prompt_tokens, completion_tokens, credit, realm) '
        'VALUES(?, ?, ?, ?, 1, ?, ?, ?, ?) '
        'ON CONFLICT(day, hour, key_id, model, realm) DO UPDATE SET '
        '  requests = requests + 1, '
        '  prompt_tokens = prompt_tokens + excluded.prompt_tokens, '
        '  completion_tokens = completion_tokens + excluded.completion_tokens, '
        '  credit = credit + excluded.credit',
        (day, hour_of(), key_id, model, prompt_tokens, completion_tokens, float(credit or 0), r),
    )


def realm_of_model(model: str | None) -> str:
    """从模型名判定版本：上游的路由协议是 `cn:` / `global:` 前缀。

    为什么以模型名为准：网关把模型名原样转发给上游，由上游按前缀选择账号池
    ——所以「这次调用走的是哪个版本」完全由前缀决定，与调用方用哪个密钥无关。

    无前缀 → cn：存量客户端与历史数据都是这个形态（升级前只有一个版本），
    归到 cn 才能让它们落在原来的那一侧，不凭空改变历史归属。
    """
    m = str(model or '').strip().lower()
    if m.startswith('global:'):
        return 'global'
    return 'cn'


# ── 签到 / 保活记录 ──────────────────────────────────────
def add_checkin_log(
    uid: str,
    nickname: str,
    source: str,
    success: bool,
    code: int | None = None,
    message: str = '',
    kind: str = 'checkin',
) -> None:
    execute(
        'INSERT INTO checkin_logs(ts, uid, nickname, source, kind, success, code, message) '
        'VALUES(?, ?, ?, ?, ?, ?, ?, ?)',
        (int(time.time()), uid or '', nickname or '', source, kind, 1 if success else 0, code, message),
    )


def checkin_done_since(ts_from: int) -> dict[str, int]:
    """每个账号在 `ts_from` 之后**最近一次成功**签到的时刻（uid → ts）。

    没有记录的账号不出现在结果里，调用方用 `.get(uid)` 判空即可。

    为什么成功判定直接用 `success = 1` 而不看 code：签到侧把两种都记成成功——
    `0`（本次签到成功）与 `10001`（今天已签过）——而这两者对「今天签没签」是
    同一个答案。`-2`（国际版无签到体系）与各种失败都是 `success = 0`，不会污染。
    换句话说：这里问的是「今天签到这件事有没有办成过」，不是「是谁办的」。

    仅取时间不取 code / source：界面要的是「签没签、什么时候签的」，多取列反而
    得为「同一 ts 多条」写去重。
    """
    rows = query(
        'SELECT uid, MAX(ts) AS ts FROM checkin_logs '
        "WHERE success = 1 AND ts >= ? AND uid != '' GROUP BY uid",
        (int(ts_from),),
    )
    return {str(r['uid']): int(r['ts']) for r in rows if r['ts'] is not None}


# days 参数的统一上限。**必须有上限**：超大整数在 SQLite 绑定时会溢出抛错
# （实测 /api/logs?days=999999999999999 返回 500）。db 层是唯一的收敛点，
# 在这里钳一次就覆盖了所有调用方（logs/stats/accounts 各处）。
_DAYS_MAX = 3650


def clamp_days(days: int | None) -> int | None:
    """把 days 钳到 [1, _DAYS_MAX]；None/非法值返回 None（= 不按时间过滤）。"""
    if days is None:
        return None
    try:
        d = int(days)
    except (TypeError, ValueError):
        return None
    return min(_DAYS_MAX, max(1, d))


def _checkin_where(uid: str | None = None, days: int | None = None) -> tuple[str, list[Any]]:
    where: list[str] = []
    args: list[Any] = []
    if uid:
        where.append('uid = ?')
        args.append(uid)
    d = clamp_days(days)
    if d:
        where.append('ts >= ?')
        args.append(int(time.time()) - d * 86400)
    return ((' WHERE ' + ' AND '.join(where)) if where else '', args)


def list_checkin_logs(
    limit: int = 200,
    uid: str | None = None,
    *,
    offset: int = 0,
    days: int | None = None,
) -> list[dict]:
    clause, args = _checkin_where(uid, days)
    rows = query(
        f'SELECT * FROM checkin_logs{clause} ORDER BY id DESC LIMIT ? OFFSET ?',
        (*args, min(2000, max(1, limit)), max(0, int(offset))),
    )
    return [
        {
            'id': r['id'],
            'ts': r['ts'],
            'uid': r['uid'],
            'nickname': r['nickname'],
            'source': r['source'],
            'kind': r['kind'],
            'success': bool(r['success']),
            'code': r['code'],
            'message': r['message'],
        }
        for r in rows
    ]


def count_checkin_logs(uid: str | None = None, days: int | None = None) -> int:
    """当前筛选下的总条数（分页用；不传筛选即全量）。"""
    clause, args = _checkin_where(uid, days)
    row = query_one(f'SELECT COUNT(*) AS n FROM checkin_logs{clause}', args)
    return int(row['n']) if row else 0


def clear_checkin_logs() -> None:
    execute('DELETE FROM checkin_logs')


# ── 上游自动任务日志 ─────────────────────────────────────
def add_task_logs(entries: list[dict]) -> int:
    """批量写入自动任务日志，返回**实际新增**条数（重复的按 dedup_key 忽略）。

    用 `INSERT OR IGNORE` + `total_changes` 差值统计，避免反复采集同一批
    日志时把「已存在」也算成新增。
    """
    if not entries:
        return 0
    rows = [
        (
            int(e.get('ts') or 0),
            str(e.get('uid') or ''),
            str(e.get('kind') or ''),
            str(e.get('level') or 'ok'),
            int(e.get('credits') or 0),
            str(e.get('message') or '')[:500],
            str(e.get('dedup_key') or ''),
        )
        for e in entries
    ]
    with _lock:
        conn = connect()
        before = conn.total_changes
        conn.executemany(
            'INSERT OR IGNORE INTO task_logs(ts, uid, kind, level, credits, message, dedup_key) '
            'VALUES(?, ?, ?, ?, ?, ?, ?)',
            rows,
        )
        conn.commit()
        return conn.total_changes - before


def _clean(text: object, limit: int = 500) -> str:
    """把外部文本清成单行：去控制字符并截断。

    为什么必须做：登录取的用户名、网关记的 UA/路径都来自外部输入，若含换行
    就能在日志/审计里**伪造出额外的行**，污染排查与事后追溯。日志是给人看的，
    单行是硬要求。
    """
    t = str(text if text is not None else '')
    # 先按字符过滤控制字符（含 \r \n），再兜底替换残留的转义序列
    t = ''.join(ch for ch in t if ch >= ' ')
    t = t.replace(chr(13), ' ').replace(chr(10), ' ')
    return t[:limit]


def add_audit_log(actor: str, action: str, target: str = '',
                  detail: str = '', ip: str = '') -> None:
    """写一条管理端审计日志。由 security.audit 调用（那里已兜底异常）。"""
    execute(
        'INSERT INTO audit_logs(ts, actor, action, target, detail, ip) VALUES(?, ?, ?, ?, ?, ?)',
        (int(time.time()), _clean(actor, 64), _clean(action, 32),
         _clean(target, 128), _clean(detail, 500), _clean(ip, 64)),
    )


def list_audit_logs(limit: int = 200, offset: int = 0) -> list[dict]:
    rows = query(
        'SELECT * FROM audit_logs ORDER BY id DESC LIMIT ? OFFSET ?',
        (min(1000, max(1, limit)), max(0, int(offset))),
    )
    return [dict(r) for r in rows]


# 入站访问日志（ip_access_logs）保留的最大行数。
#
# 为什么必须有上限：这张表的写入点在网关鉴权**之前**（缺 token / token 无效
# 都会记一行），也就是**未鉴权可达**。此前它既无清洗也无上限、更没有清理机制，
# 于是任何匿名者都能用「无效 key + 超长 UA」反复写库把磁盘灌满，进而拖垮
# 管理端与上游（实测：2KB UA 每条约 1.3KB，百万请求约 2GB）。
#
# 取 2 万行：按每次拒绝一行估算足够回溯近期攻击，占用约几 MB；超出后按最旧丢弃
# （滚动窗口），写入成本是常数级的。
_IP_ACCESS_LOG_MAX = 20000

# 清理的触发间隔：不必每次写入都查一次 COUNT（那是一趟全表扫描）。
# 每 500 次写入检查一次，超限时一次删到上限以下，均摊成本可忽略。
_IP_ACCESS_LOG_CHECK_EVERY = 500
_ip_log_writes = 0


# 请求日志（request_logs）的保留期（天）。
#
# 为什么需要：这张表**每笔请求都写一行**（无条件，区别于用量汇总），线上实测
# 每天 1400+ 行。此前没有任何清理机制——`ip_access_logs` 有行数上限，它没有。
# 按当前速率一年约 50 万行、上百 MB，`/api/logs` 的查询与页面都会越来越慢。
#
# 取 90 天：统计页最长的展示窗口是 30 天（`_DAYS_MAX` 之外的实际用法），
# 「修复统计 / 重建统计」也只需覆盖到用量表还有意义的时段；再久的明细对用户
# 没有用途，而按月归档属于另一件事（真要长期留存应导出，不是留在大表里）。
#
# 注意与 `rebuild_usage_from_logs` 的关系：重建以 request_logs 为唯一依据，
# 删掉 90 天前的日志后，那段时期的用量统计**不能再重建**（现有数据不受影响，
# 只是无法重算）。这是刻意的：90 天前的用量早已定稿，不值得为它永久保留明细。
_REQUEST_LOG_RETAIN_DAYS = 90

# 清理间隔：按行数触发（每 2000 次写入检查一次），与 ip 日志同款思路——
# 不必每次写入都查一次。检查本身用索引列 ts，成本可忽略。
_REQUEST_LOG_CHECK_EVERY = 2000
_request_log_writes = 0


# 侧表保留策略（审计 2026-10-18）：这几张表以前只增不减。
#   · usage_hourly：每天 × 每密钥 × 每模型 × 24 行，长期跑会拖慢查询与备份；
#   · task_logs：采集器持续写入；
#   · audit_logs：**未认证路径也能写**（失败令牌会留痕），没有上限等于给了一个
#     填磁盘的入口（请求日志有 90 天保留，审计反而没有）。
_HOURLY_RETAIN_DAYS = 90
_TASK_LOG_RETAIN_DAYS = 90
_AUDIT_LOG_MAX_ROWS = 20000


def _prune_side_tables() -> None:
    """给只增不减的侧表加保留期 / 条数上限。旁路操作，失败绝不影响主流程。"""
    now = int(time.time())
    hourly_cutoff = time.strftime('%Y-%m-%d',
                                  time.localtime(now - _HOURLY_RETAIN_DAYS * 86400))
    task_cutoff = now - _TASK_LOG_RETAIN_DAYS * 86400
    for sql, args in (
        ('DELETE FROM usage_hourly WHERE day < ?', (hourly_cutoff,)),
        ('DELETE FROM task_logs WHERE ts < ?', (task_cutoff,)),
        # 保留最新 N 行：删除 id <= 第 N+1 新那行的 id；不足 N 行时子查询为 NULL，
        # `id <= NULL` 不成立，什么都不会删。
        ('DELETE FROM audit_logs WHERE id <= (SELECT id FROM audit_logs '
         'ORDER BY id DESC LIMIT 1 OFFSET ?)', (_AUDIT_LOG_MAX_ROWS,)),
    ):
        try:
            execute(sql, args)
        except Exception:  # noqa: BLE001
            pass


def _prune_request_logs() -> None:
    """按保留期清理请求日志（滚动删除最旧的），顺手清理侧表。

    旁路操作：任何失败都不能影响写入本身（更不能影响转发）。
    """
    cutoff = int(time.time()) - _REQUEST_LOG_RETAIN_DAYS * 86400
    try:
        execute('DELETE FROM request_logs WHERE ts < ?', (cutoff,))
    except Exception:  # noqa: BLE001
        pass
    _prune_side_tables()


def add_request_log(**fields: object) -> None:
    """写一条请求日志，并按保留期做滚动清理。

    参数用 kwargs（调用方能按名字传，列多时不易错位），但**传给 SQLite 时必须
    展开成位置元组**：`execute()` 内部做 `tuple(args)`，直接塞 dict 会把它当成
    单个参数，于是键名被当值写进库（实测：`credit` 列存进了字符串 `'credit'`，
    `rebuild` 随即因 `NOT NULL constraint failed` 崩掉）。这里显式按列序展开。
    """
    global _request_log_writes
    execute(
        'INSERT INTO request_logs(ts, key_id, ip, model, mapped_model, status, '
        'prompt_tokens, completion_tokens, latency_ms, first_token_ms, ua, error, '
        'stream, credit, realm, account, cache_hit_tokens, cache_miss_tokens, '
        'cache_write_tokens, effort) '
        'VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (fields.get('ts'), fields.get('key_id'), fields.get('ip'),
         fields.get('model'), fields.get('mapped_model'), fields.get('status'),
         fields.get('prompt_tokens'), fields.get('completion_tokens'),
         fields.get('latency_ms'), fields.get('first_token_ms'), fields.get('ua'),
         fields.get('error'), fields.get('stream'), fields.get('credit'),
         fields.get('realm'), fields.get('account'), fields.get('cache_hit_tokens'),
         fields.get('cache_miss_tokens'), fields.get('cache_write_tokens'),
         fields.get('effort')),
    )
    _request_log_writes += 1
    if _request_log_writes >= _REQUEST_LOG_CHECK_EVERY:
        _request_log_writes = 0
        _prune_request_logs()


# 账号回填的匹配窗口（秒）。
#
# 上游日志的时间戳是「请求结束」时刻，与本端 `request_logs.ts` 同义（也在
# 请求结束时取的 `int(time.time())`），所以能直接比。实测两者相差 < 1 秒
# （RTT + 双方时钟），窗口给 3 秒覆盖慢链路与时钟漂移。
_ACCOUNT_MATCH_WINDOW = 3


def attach_request_accounts(entries: Iterable[dict]) -> int:
    """把从上游日志采集到的账号回填到对应的请求日志行（issue #69）。返回回填条数。

    entries 形如 `[{'ts': 结束时刻(epoch 秒), 'model': 上游侧模型名,
    'account': '昵称(uid8)'}]`。

    匹配规则：**同模型 + 时间最接近 + 尚未回填**。

    为什么用「最接近」而不是相等：本端记的是 `int(time.time())`（秒级截断），
    上游给的是纳秒 —— 两者不可能相等，只能就近匹配。实测「最近的一行」总是
    正确的那条；但**同一秒内有多个同模型请求**时无法区分，所以只挑
    `account IS NULL` 的行，避免把已经填对的行改错（宁可漏填，不可填错）。

    模型名两边都取**映射后**的名字（`mapped_model`），另兼看 `model` ——
    映射表变更时两边可能只对得上一个。
    """
    filled = 0
    for e in entries:
        ts = e.get('ts')
        acct = _clean(e.get('account'), 64)
        # bool 是 int 的子类：`ts=True` 会当成「1970-01-01 00:00:01」，虽然只会
        # 匹配到空窗（几乎没有这种行），但没有时间戳就不该参与匹配。
        if not isinstance(ts, int) or isinstance(ts, bool) or not acct:
            continue
        model = _clean(e.get('model'), 128)
        # 模型名为空时**不能**匹配：`model = ''` 会命中「模型列也是空」的那些行
        # （历史记录里存在），等于把账号填到一条毫不相干的日志上。宁可漏填。
        if not model:
            continue
        try:
            row = query_one(
                'SELECT id FROM request_logs '
                'WHERE account IS NULL AND ts BETWEEN ? AND ? '
                '  AND (mapped_model = ? OR model = ?) '
                'ORDER BY ABS(ts - ?) LIMIT 1',
                (ts - _ACCOUNT_MATCH_WINDOW, ts + _ACCOUNT_MATCH_WINDOW,
                 model, model, ts),
            )
            if row is None:
                continue
            execute('UPDATE request_logs SET account = ? WHERE id = ?',
                    (acct, row['id']))
            filled += 1
        except Exception:  # noqa: BLE001
            # 回填是旁路：任何异常都不能影响采集循环的其余部分
            continue
    return filled


def add_ip_access_log(ip: object, path: object, blocked: bool, ua: object,
                      reason: object = None) -> None:
    """写入一条入站访问日志：清洗 + 截断 + 行数上限。

    清洗与截断是**硬要求**（不只是节省空间）：`ua` / `path` / `reason` 来自
    外部输入，含换行就能在日志界面上伪造出额外行，污染事后排查。审计表一直
    这么做（见 `add_audit_log`），但网关这张表此前漏了 —— 文档里「日志写入前
    统一 `_clean()`」的说法与实际不符，这里补齐。

    reason 是**被拦的原因**（issue #33）：只有 blocked 布尔时，界面上的
    「已拦截」无法区分「没带密钥」「密钥不认识」「IP 规则拦的」——三者的处置
    方式完全不同，用户只能靠猜或来提 issue。
    """
    global _ip_log_writes
    # 空原因存 NULL 而不是空串：读的那一侧要区分「没记原因」（升级前的历史
    # 记录 / 放行）与「记了一个空字符串」，两者在库里的语义不同。
    cleaned_reason = _clean(reason, 200) or None
    execute(
        'INSERT INTO ip_access_logs(ts, ip, path, blocked, ua, reason) '
        'VALUES(?, ?, ?, ?, ?, ?)',
        (int(time.time()), _clean(ip, 64), _clean(path, 256),
         1 if blocked else 0, _clean(ua, 512), cleaned_reason),
    )
    _ip_log_writes += 1
    if _ip_log_writes < _IP_ACCESS_LOG_CHECK_EVERY:
        return
    _ip_log_writes = 0
    try:
        row = query_one('SELECT COUNT(*) AS n FROM ip_access_logs')
        n = int(row['n']) if row else 0
        if n > _IP_ACCESS_LOG_MAX:
            # 删掉最旧的一批，留出余量（避免下一次写入又立刻触发清理）
            execute(
                'DELETE FROM ip_access_logs WHERE id IN ('
                '  SELECT id FROM ip_access_logs ORDER BY id LIMIT ?'
                ')',
                (n - _IP_ACCESS_LOG_MAX + _IP_ACCESS_LOG_MAX // 10,),
            )
    except Exception:  # noqa: BLE001
        # 清理是旁路，失败不能影响写入本身（更不能影响转发）
        pass


def count_audit_logs() -> int:
    row = query_one('SELECT COUNT(*) AS n FROM audit_logs')
    return int(row['n']) if row else 0


def clear_audit_logs() -> None:
    execute('DELETE FROM audit_logs')


def _task_log_where(
    uid: str | None = None,
    kind: str | None = None,
    days: int | None = None,
) -> tuple[str, list[Any]]:
    where: list[str] = []
    args: list[Any] = []
    if uid:
        where.append('uid = ?')
        args.append(uid)
    if kind:
        where.append('kind = ?')
        args.append(kind)
    d = clamp_days(days)
    if d:
        where.append('ts >= ?')
        args.append(int(time.time()) - d * 86400)
    return ((' WHERE ' + ' AND '.join(where)) if where else '', args)


def list_task_logs(
    limit: int = 200,
    uid: str | None = None,
    kind: str | None = None,
    *,
    offset: int = 0,
    days: int | None = None,
) -> list[dict]:
    clause, args = _task_log_where(uid, kind, days)
    rows = query(
        f'SELECT * FROM task_logs{clause} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?',
        (*args, min(2000, max(1, limit)), max(0, int(offset))),
    )
    return [
        {
            'id': r['id'],
            'ts': r['ts'],
            'uid': r['uid'],
            'kind': r['kind'],
            'level': r['level'],
            'credits': r['credits'],
            'message': r['message'],
        }
        for r in rows
    ]


def count_task_logs(
    uid: str | None = None,
    kind: str | None = None,
    days: int | None = None,
) -> int:
    """当前筛选下的总条数（分页用）。

    分页必须用筛选后的总数：此前界面徽章取的是全局统计，而列表只取前 500 条，
    会出现「徽章说 2200 条、实际只能看到 500 条」且更早记录翻不到的情况。
    """
    clause, args = _task_log_where(uid, kind, days)
    row = query_one(f'SELECT COUNT(*) AS n FROM task_logs{clause}', args)
    return int(row['n']) if row else 0


def task_log_stats(days: int | None = None) -> dict:
    """按类型汇总条数与累计积分，用于页面上方的概览。

    days 让概览与列表的时间范围保持一致，否则筛选后数字会对不上。
    """
    clause, args = _task_log_where(days=days)
    rows = query(
        'SELECT kind, COUNT(*) AS n, COALESCE(SUM(credits), 0) AS credits '
        f'FROM task_logs{clause} GROUP BY kind',
        args,
    )
    by_kind = {r['kind']: {'count': int(r['n']), 'credits': int(r['credits'])} for r in rows}
    total = query_one(
        f'SELECT COUNT(*) AS n, COALESCE(SUM(credits),0) AS credits FROM task_logs{clause}',
        args,
    )
    return {
        'by_kind': by_kind,
        'total': int(total['n']) if total else 0,
        'total_credits': int(total['credits']) if total else 0,
    }


def clear_task_logs() -> None:
    execute('DELETE FROM task_logs')


# ── 用量回填 ─────────────────────────────────────────────
# 归一化表达式：**必须同时**用在 SELECT 与 GROUP BY，且空串要与 Python 的
# `or 'cn'` 同一规则。为什么这么讲究，见 rebuild_usage_from_logs 的长注释
# （GROUP BY 复用别名会绑定到源列 → 撞 usage_daily 主键 → 接口 500）。
# 收成常量是为了让「按天」与「按小时」两条回填路径共用同一份规则 —— 分别写就会漂移。
_USAGE_MODEL_EXPR = "COALESCE(NULLIF(model,''),'')"
_USAGE_REALM_EXPR = "COALESCE(NULLIF(realm,''),'cn')"


def _usage_expected_rows(bucket_expr: str) -> list[dict]:
    """按 `bucket_expr`（`day_sql('ts')` 或 `hour_sql('ts')`）聚合请求日志。

    返回的每一行是「这个桶 × 密钥 × 模型 × 版本」应有的用量 —— 回填与重建都以
    它为准（请求日志是唯一的原始事实）。
    """
    return query(
        f"SELECT {bucket_expr} AS bucket, key_id, "
        f"{_USAGE_MODEL_EXPR} AS model, {_USAGE_REALM_EXPR} AS realm, "
        "COUNT(*) AS requests, COALESCE(SUM(prompt_tokens),0) AS pt, "
        "COALESCE(SUM(completion_tokens),0) AS ct, COALESCE(SUM(credit),0) AS cr "
        "FROM request_logs WHERE key_id IS NOT NULL "
        f"GROUP BY {bucket_expr}, key_id, {_USAGE_MODEL_EXPR}, {_USAGE_REALM_EXPR}"
    )

def backfill_usage_from_logs() -> dict:
    """把 request_logs 里尚未计入 usage_daily 的用量补进统计。

    用途：修复历史缺陷（曾因统计函数缺失，导致部分调用的用量没有累计）。
    以「已记录的调用」推算应有用量，再把差额写入 usage_daily，
    因此可重复执行而不会重复计数。
    """
    # 应有用量（按天 × 密钥 × 模型）
    # 必须与 bump_usage 用同一时区口径（本地），否则凌晨的调用会被算成两天
    #
    # 归一化表达式同样要**同时**用在 SELECT 与 GROUP BY，且空串也要折成默认值
    # （原因见 rebuild_usage_from_logs 里的说明：GROUP BY 复用别名会绑定到源列；
    #   SQL 的 COALESCE 不处理空串、Python 的 `or` 会 —— 两边规则必须一致）。
    model_expr = _USAGE_MODEL_EXPR
    realm_expr = _USAGE_REALM_EXPR
    day_expr = day_sql('ts')
    expected = query(
        f"SELECT {day_expr} AS day, key_id, {model_expr} AS model, {realm_expr} AS realm, "
        "COUNT(*) AS requests, COALESCE(SUM(prompt_tokens),0) AS pt, "
        "COALESCE(SUM(completion_tokens),0) AS ct, COALESCE(SUM(credit),0) AS cr "
        f"FROM request_logs WHERE key_id IS NOT NULL "
        f"GROUP BY {day_expr}, key_id, {model_expr}, {realm_expr}"
    )
    # 键含 realm：两个版本的同名模型是不同行，否则回填会把它们并成一条
    current = {
        (r['day'], r['key_id'], r['model'], r['realm'] or 'cn'): r
        for r in query(
            'SELECT day, key_id, model, realm, requests, prompt_tokens, completion_tokens, credit FROM usage_daily'
        )
    }

    fixed = 0
    added_requests = added_tokens = 0
    for row in expected:
        key = (row['day'], row['key_id'], row['model'], row['realm'] or 'cn')
        cur = current.get(key)
        cur_req = int(cur['requests']) if cur else 0
        cur_pt = int(cur['prompt_tokens']) if cur else 0
        cur_ct = int(cur['completion_tokens']) if cur else 0

        d_req = int(row['requests']) - cur_req
        d_pt = int(row['pt']) - cur_pt
        d_ct = int(row['ct']) - cur_ct
        if d_req <= 0 and d_pt <= 0 and d_ct <= 0:
            continue
        execute(
            'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, completion_tokens, credit, realm) '
            'VALUES(?, ?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(day, key_id, model, realm) DO UPDATE SET '
            '  requests = MAX(requests, excluded.requests), '
            '  prompt_tokens = MAX(prompt_tokens, excluded.prompt_tokens), '
            '  completion_tokens = MAX(completion_tokens, excluded.completion_tokens), '
            '  credit = MAX(credit, excluded.credit)',
            (row['day'], row['key_id'], row['model'], int(row['requests']),
             int(row['pt']), int(row['ct']), float(row['cr'] or 0),
             str(row['realm'] or 'cn')),
        )
        fixed += 1
        added_requests += max(0, d_req)
        added_tokens += max(0, d_pt) + max(0, d_ct)

    # 小时维度用同一套规则再补一遍（「今日」趋势图读这张表）。与上面**同源同规则**：
    # 请求日志 → 应有值 → 与现值比差额 → MAX 写入，所以重复执行不会重复计数。
    fixed_h = _backfill_hourly_from_logs()

    return {
        'repaired': fixed,
        'repaired_hourly': fixed_h,
        'requests': added_requests,
        'tokens': added_tokens,
    }


def _backfill_hourly_from_logs() -> int:
    """按小时回填 usage_hourly（幂等，MAX 语义，与按天那条同一规则）。返回修复桶数。"""
    expected = _usage_expected_rows(hour_sql('ts'))
    current = {
        (r['day'], r['hour'], r['key_id'], r['model'], r['realm'] or 'cn'): r
        for r in query('SELECT day, hour, key_id, model, realm, requests, prompt_tokens, '
                       'completion_tokens FROM usage_hourly')
    }
    fixed = 0
    for row in expected:
        bucket = str(row['bucket'] or '')
        if len(bucket) < 13:            # 'YYYY-MM-DD HH' —— 形态不对就跳过（不猜）
            continue
        day, hour = bucket[:10], int(bucket[11:13])
        key = (day, hour, row['key_id'], row['model'], row['realm'] or 'cn')
        cur = current.get(key)
        cur_req = int(cur['requests']) if cur else 0
        cur_pt = int(cur['prompt_tokens']) if cur else 0
        cur_ct = int(cur['completion_tokens']) if cur else 0
        if (int(row['requests']) <= cur_req and int(row['pt']) <= cur_pt
                and int(row['ct']) <= cur_ct):
            continue
        execute(
            'INSERT INTO usage_hourly(day, hour, key_id, model, requests, prompt_tokens, '
            'completion_tokens, credit, realm) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(day, hour, key_id, model, realm) DO UPDATE SET '
            '  requests = MAX(requests, excluded.requests), '
            '  prompt_tokens = MAX(prompt_tokens, excluded.prompt_tokens), '
            '  completion_tokens = MAX(completion_tokens, excluded.completion_tokens), '
            '  credit = MAX(credit, excluded.credit)',
            (day, hour, row['key_id'], row['model'], int(row['requests']),
             int(row['pt']), int(row['ct']), float(row['cr'] or 0),
             str(row['realm'] or 'cn')),
        )
        fixed += 1
    return fixed


def ensure_hourly_today() -> bool:
    """升级兜底：今天的小时表若还是空的，就用请求日志补一次（幂等）。

    为什么需要它：这张表是后加的，存量库升级后它一行都没有 —— 用户点开「今日」
    趋势图会看到一片空白，而卡片数字明明有值（那正是最不想看到的观感）。
    只补**今天**：它只被「今日」用到，补历史没有意义；代价是一次聚合查询。
    返回是否真的补了。
    """
    try:
        today = day_of()
        if query_one('SELECT 1 FROM usage_hourly WHERE day = ? LIMIT 1', (today,)):
            return False
        if not query_one('SELECT 1 FROM request_logs WHERE ts >= ? LIMIT 1',
                         (day_start_ts(today),)):
            return False                # 今天还没有任何请求（或日志被清过）：无从补起
        _backfill_hourly_from_logs()
        return True
    except Exception:                   # noqa: BLE001
        return False                    # 旁路操作：失败不影响启动


def hourly_rows(day: str, realm: str | None = None) -> list[dict]:
    """某天**按小时**的用量（`/api/stats/hourly` 用）。realm 为空不过滤。"""
    rf = ' AND realm = ?' if realm in ('cn', 'global') else ''
    args: tuple = (day,) + ((realm,) if rf else ())
    return query(
        'SELECT hour, SUM(requests) AS requests, '
        'SUM(prompt_tokens) AS prompt_tokens, '
        'SUM(completion_tokens) AS completion_tokens, '
        'COALESCE(SUM(credit),0) AS credit '
        f'FROM usage_hourly WHERE day = ?{rf} GROUP BY hour ORDER BY hour ASC',
        args,
    )


def rebuild_usage_from_logs() -> dict:
    """以请求日志为准**重建**用量统计（会替换 usage_daily 的内容）。

    用途：修复历史时区口径不一致造成的污染——凌晨的调用曾被同时算进
    本地日与 UTC 日两行，导致总量偏高。回填（MAX 语义）只能补缺口、
    无法删除多出来的行，因此需要一次重建。

    注意：本操作以 request_logs 为唯一依据。若请求日志曾被清空，
    那部分历史汇总会随之丢失（接口上已明确标注）。
    """
    # 归一化表达式必须**同时**用在 SELECT 与 GROUP BY 上，不能只在 SELECT 里写
    # 别名、GROUP BY 里复用别名。
    #
    # 原因（线上 bug）：SQLite 解析 `GROUP BY realm` 时，因为 FROM 的表里**也有**
    # 名为 realm 的列，该名字绑定到**源列**而不是输出别名 `COALESCE(realm,'cn')`。
    # 于是 `realm IS NULL` 与 `realm='cn'` 被分成两组，但两组的输出值都是 'cn' ——
    # 随后 INSERT 就撞上 usage_daily 的主键 (day,key_id,model,realm)，
    # 报 `UNIQUE constraint failed`，接口 500（界面上是「Internal Server Error」）。
    # model 列同理（`COALESCE(model,'')` vs 源列 model）。
    #
    # 历史日志里 realm 为 NULL 是常态（该列是后加的），所以这不是理论风险。
    #
    # 第二处必须对齐：**空串也要归一**。SQL 的 COALESCE 只处理 NULL，而 Python 的
    # `x or 'cn'` 连空串一起兜住 —— 两边规则不同就会出现「SQL 分成两组、写库时
    # 都变成 cn」的第二次撞键。所以 SQL 侧用 NULLIF 把空串也折成 NULL，
    # 与 Python 的 `or 'cn'` 完全一致。
    model_expr = _USAGE_MODEL_EXPR
    realm_expr = _USAGE_REALM_EXPR
    day_expr = day_sql('ts')
    expected = query(
        f"SELECT {day_expr} AS day, key_id, {model_expr} AS model, {realm_expr} AS realm, "
        "COUNT(*) AS requests, COALESCE(SUM(prompt_tokens),0) AS pt, "
        "COALESCE(SUM(completion_tokens),0) AS ct, COALESCE(SUM(credit),0) AS cr "
        f"FROM request_logs WHERE key_id IS NOT NULL "
        f"GROUP BY {day_expr}, key_id, {model_expr}, {realm_expr}"
    )
    before = query_one('SELECT COUNT(*) AS c, COALESCE(SUM(requests),0) AS r, '
                       'COALESCE(SUM(prompt_tokens+completion_tokens),0) AS t FROM usage_daily')
    # 删除与重建必须在**同一个事务**里。此前是「先 DELETE（已提交）再逐条 INSERT」，
    # 一旦插入中途失败（就是上面那个归一化 bug），统计表已经被清空、只剩半份数据，
    # 而 request_logs 完好 —— 用户看到的就是「今天有 N 次调用、统计却是 0」，
    # 且**每次点重建都在继续破坏数据**。原子化之后，失败就整体回滚，
    # 原有的统计原样保留（宁可暂时不准，也不能把仅有的数据弄丢）。
    conn = connect()
    with _lock:
        try:
            conn.execute('BEGIN')
            conn.execute('DELETE FROM usage_daily')
            conn.executemany(
                'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, '
                'completion_tokens, credit, realm) VALUES(?, ?, ?, ?, ?, ?, ?, ?)',
                [(row['day'], row['key_id'], row['model'], int(row['requests']),
                  int(row['pt']), int(row['ct']), float(row['cr'] or 0),
                  str(row['realm'] or 'cn')) for row in expected],
            )
            # 小时维度与按天**同一事务**重建：只重建一处会让「今日」图与卡片
            # 在重建后对不上，而那正是分两张表要避免的事。
            conn.execute('DELETE FROM usage_hourly')
            hourly_rows = _usage_expected_rows(hour_sql('ts'))
            conn.executemany(
                'INSERT INTO usage_hourly(day, hour, key_id, model, requests, prompt_tokens, '
                'completion_tokens, credit, realm) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)',
                [(str(row['bucket'])[:10], int(str(row['bucket'])[11:13]), row['key_id'],
                  row['model'], int(row['requests']), int(row['pt']), int(row['ct']),
                  float(row['cr'] or 0), str(row['realm'] or 'cn'))
                 for row in hourly_rows if len(str(row['bucket'] or '')) >= 13],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    after = query_one('SELECT COUNT(*) AS c, COALESCE(SUM(requests),0) AS r, '
                      'COALESCE(SUM(prompt_tokens+completion_tokens),0) AS t FROM usage_daily')
    return {
        'rows_before': int(before['c']) if before else 0,
        'rows_after': int(after['c']) if after else 0,
        # 差值可正可负：负数说明此前确实被重复计数了
        'requests_delta': int(after['r'] or 0) - int(before['r'] or 0),
        'tokens_delta': int(after['t'] or 0) - int(before['t'] or 0),
    }
