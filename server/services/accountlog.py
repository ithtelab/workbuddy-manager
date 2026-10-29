"""把「这次调用实际用了哪个上游账号」回填进请求日志（issue #69）。

为什么要靠日志、而不是直接问上游
--------------------------------
账号是**上游**在它内部选出来的，本端转发时并不知道用了哪个号；上游也不在
响应头里回传（只有 `X-Service`），`/v1/stats` 也只按模型聚合、没有账号维度。
唯一的信息源是它的**运行日志**：

    | #971 | 14:58:13 | deepseek-v4.1-flash | stream | 200 | 6509授(3a3a19b1) | TTFB=1083ms | ...

Docker 模式下 `docker logs --timestamps` 会给每行加**纳秒级**的 RFC3339 前缀；
原生模式写文件时没有这层前缀，而**行内只有 `HH:MM:SS`**（上游 `logging.go` 用的
就是 `time.Now().Format("15:04:05")`，不带日期）——所以原生模式要靠日志文件的
mtime 作日期锚点、再按行内时钟还原秒数，见 `_native_timestamps`。两种情况都要能
还原出「请求结束」时刻：本端 `request_logs.ts` 记的也是该时刻（`_record` 在流
收尾时取 `int(time.time())`），所以两边可以直接对齐——实测同一批请求误差 < 1 秒。

为什么值得记（issue 里提的两件事）
----------------------------------
  · **是否在跳账号**：同一把密钥的连续请求若被分散到不同账号，说明上游的轮换
    在工作；若一直落在同一个号上，可能就是「为什么这个号先耗尽」的答案。
  · **缓存为什么没命中**：前缀缓存是**按账号**存的，换了号就必然 miss。
    把账号与 `cache_hit_tokens` 放在一起看才能解释这件事。

失败时的行为
------------
填不上就留空（界面显示「—」）。回填是**旁路**：docker 不可用、日志已滚掉、
模型对不上，都只是少几行数据，绝不能影响转发——与 tasklog / 用量统计同口径。
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import re
import time

from .. import config, db
from . import updater, wb2api

logger = logging.getLogger('workbuddy.accountlog')

# 轮询周期（秒）。上游日志是请求**结束时**才写出的，所以略微滞后即可；
# 间隔取长一些：回填晚几秒对「看谁在跳账号」没有影响，而读一次 docker
# logs 有进程开销，没必要太频繁。
_POLL_SECONDS = 15

# 每次采集读多少行。上游一行一个请求，500 行足以覆盖 15 秒内的活动
# （实测高峰期也就每 15 秒几十条）；读太多只是白白解析。
_TAIL = 500

# 上游的对话请求行。格式见 workbuddy2api `internal/server/logging.go`：
#   | #%03d | HH:MM:SS | model | stream/nostream | status | 昵称(uid8) | TTFB= | tok= | ... | total= |
#
# 开头的 docker 时间戳单独用 `_TS_PREFIX` 剥离，不写进这个正则。
_CHAT_LINE = re.compile(
    r'\|\s*#\d+\s*\|'          # | #971 |
    r'\s*[\d:]+\s*\|'          # | 14:58:13 |
    r'\s*[^|]+\s*\|'           # | deepseek-v4.1-flash |
    r'\s*\S+\s*\|'             # | stream |
    r'\s*(?P<status>\d+)\s*\|'   # | 200 |   ← 状态码
    r'\s*(?P<acct>[^|]+?)\s*\|'  # | 6509授(3a3a19b1) |   ← 账号
    r'\s*TTFB=',               # 用它锚定「后面还有请求字段」，避免误吃任务日志
)

# 模型名在上面那条正则里没单独捕获（它前面有两个「任意非竖线」段，写成命名组
# 会和 status 的匹配打架）。这里按竖线切开后**按位置取**，位置由上游格式固定。
_MODEL_POS = 3

# docker --timestamps 的前缀：2026-09-23T06:58:13.123895379Z
_TS_PREFIX = re.compile(r'^(?P<stamp>\d{4}-\d{2}-\d{2}T[\d:.]+Z)\s+')

# 原生写日志时上游只有时钟，没有日期：| #971 | 14:58:13 | ...
_NATIVE_CLOCK = re.compile(r'\|\s*#\d+\s*\|\s*(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2})\s*\|')

def _parse_docker_ts(raw: str) -> int | None:
    """解析 docker --timestamps 的 RFC3339 UTC 前缀。"""
    try:
        text = raw.strip()
        if not text.endswith('Z'):
            return None
        text = text[:-1]
        head, _, frac = text.replace(' ', 'T').partition('.')
        stamp = datetime.datetime.strptime(head, '%Y-%m-%dT%H:%M:%S')
        if frac:
            stamp = stamp.replace(microsecond=int(frac[:6].ljust(6, '0')))
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)
        return int(stamp.timestamp())
    except (ValueError, TypeError, OverflowError):
        return None


def _normalize_account(raw: str) -> str:
    """把账号标签归一成「可以直接拿去比对」的字符串。

    我们手上的上游源码用的是 `logfmt.Label`，聊天行里输出的是 **`昵称(uid8)`**
    （见 `internal/server/logging.go`）——这种保留原文，界面正要靠它显示昵称。
    `uid=<uid8>` 这种形态**不是**当前上游聊天行的写法（`uid=` 只出现在它的错误串
    里），但实测有构建/日志路径会这么打，所以顺手容错：见到前缀就剥掉。
    两者最后都交给数据库按 64 字符清洗。
    """
    value = raw.strip()
    if value.lower().startswith('uid='):
        value = value[4:].strip()
    return value


def _native_clock_seconds(line: str) -> int | None:
    """原生日志行内的 HH:MM:SS 转成当天秒数；没有时钟返回 None。"""
    m = _NATIVE_CLOCK.match(line)
    if not m:
        return None
    try:
        return int(m.group('h')) * 3600 + int(m.group('m')) * 60 + int(m.group('s'))
    except (TypeError, ValueError):
        return None


def _native_timestamps(lines: list[str], mtime: float | None) -> list[int | None]:
    """给原生日志行恢复 epoch 秒。

    原生行只有 `HH:MM:SS`，没有日期。我们把**最后一行**锚在日志文件 mtime 上，
    再按行序和时钟回推：只在同一个自然日/跨午夜且时钟单调时接受。日志轮转、
    时钟回拨或长于一天的旧行都返回 None，宁可漏填，不可填到别人的记录上。
    """
    if not lines or mtime is None:
        return [None] * len(lines)
    clocks = [_native_clock_seconds(ln) for ln in lines]
    # 最后一行可能不是请求行（pool/watch 等）；向前找最近一个有时钟的行。
    # 这不代表它在 mtime 那一秒：mtime 是文件最后一次写入时间，通常就是这一行
    # 附近；我们只用它作日期的锚点，行内时钟负责具体秒数。
    anchor_idx = next((i for i in range(len(lines) - 1, -1, -1) if clocks[i] is not None), None)
    if anchor_idx is None:
        return [None] * len(lines)
    anchor_clock = clocks[anchor_idx]
    assert anchor_clock is not None
    anchor_day = datetime.datetime.fromtimestamp(mtime).date()
    out: list[int | None] = [None] * len(lines)
    prev_clock: int | None = anchor_clock
    day = anchor_day
    for i in range(anchor_idx, -1, -1):
        clock = clocks[i]
        if clock is None:
            continue
        # 从锚点向左回推；遇到时钟回升说明跨了午夜，日期减一天。
        if clock > prev_clock:
            day -= datetime.timedelta(days=1)
        stamp = datetime.datetime.combine(day, datetime.time(clock // 3600,
                                                             (clock % 3600) // 60,
                                                             clock % 60))
        ts = int(stamp.timestamp())
        out[i] = ts
        prev_clock = clock
    return out


def parse_request_lines(lines: list[str]) -> list[dict]:
    """从上游日志里解析出对话请求记录。

    返回 `[{'ts': 结束时刻(epoch 秒), 'model': 模型名, 'account': '昵称(uid8)'}]`。

    时间来源分两层：
      · Docker 日志行：用行首的 RFC3339 UTC 时间戳；
      · 原生日志行：只有 `HH:MM:SS`，由调用方结合日志文件 mtime 恢复（见
        `parse_request_lines_with_mtime`）；直接调用本函数时仍跳过，避免猜日期。
    """
    out: list[dict] = []
    for line in lines:
        m = _CHAT_LINE.search(line)
        if not m:
            continue

        t = _TS_PREFIX.match(line)
        ts = _parse_docker_ts(t.group('stamp')) if t else None
        if ts is None:
            continue

        parts = [p.strip() for p in line.split('|')]
        model = parts[_MODEL_POS] if len(parts) > _MODEL_POS else ''
        acct = _normalize_account(m.group('acct'))
        if not model or not acct or acct == '-':
            continue
        out.append({'ts': ts, 'model': model, 'account': acct})
    return out


def parse_request_lines_with_mtime(lines: list[str], mtime: float | None) -> list[dict]:
    """解析日志行；原生无日期行用 mtime 恢复时间，Docker 行照旧解析。"""
    docker = parse_request_lines(lines)
    if docker or mtime is None:
        return docker
    native_ts = _native_timestamps(lines, mtime)
    out: list[dict] = []
    for line, ts in zip(lines, native_ts):
        m = _CHAT_LINE.search(line)
        if not m or ts is None:
            continue
        parts = [p.strip() for p in line.split('|')]
        model = parts[_MODEL_POS] if len(parts) > _MODEL_POS else ''
        acct = _normalize_account(m.group('acct'))
        if not model or not acct or acct == '-':
            continue
        out.append({'ts': ts, 'model': model, 'account': acct})
    return out


# 最近一次采集的事实快照，供「为什么账号列一直是空的」自诊断用。
# 采集循环每轮覆盖，读侧拿到的是一份一致的小字典。
#
# `already`：这批条目里有多少条在匹配窗口内**已经存在**本地记录（不论账号填没
# 填）。回填只更新 `account IS NULL` 的行，所以「本轮填了 0 条」绝大多数时候
# 只是「这批请求早填过了」——没有这个字段，诊断只能拿 `filled` 当唯一判据，
# 于是每轮都会误报「对不上本地记录」（issue #232）。
_last: dict = {
    'checked_at': 0, 'lines': 0, 'parsed': 0, 'filled': 0, 'already': 0, 'error': '',
}


def _collect_once() -> int:
    """读一次上游日志并回填账号。返回回填条数（同步，供 to_thread 调用）。"""
    # 必须读**对话日志**那个文件：上游把每请求一行（含账号）写到 stdout，
    # 签到/活跃/旅行等业务编排走 Go log → stderr，原生模式下是两个文件
    # （见 config 的 WB2API_CHAT_LOG_FILE）。读错文件时一条 chat 行都解析不到，
    # 账号列恒为空。Docker 模式这个参数被忽略，`docker logs` 本来就合并两路。
    lines, mtime = wb2api.read_container_logs(
        _TAIL, timestamps=True, with_mtime=True, path=config.WB2API_CHAT_LOG_FILE,
    )
    _last['checked_at'] = int(time.time())
    _last['lines'] = len(lines)
    if not lines:
        _last.update(parsed=0, filled=0, already=0, error='')
        return 0
    entries = parse_request_lines_with_mtime(lines, mtime)
    _last['parsed'] = len(entries)
    if not entries:
        _last.update(filled=0, already=0, error='')
        return 0
    filled = db.attach_request_accounts(entries)
    # 只在「一条都没填」时才去数一遍「其实早就填过多少条」：这是唯一会让人
    # 怀疑回填坏了的场景，正常轮次（填了 1~2 条）没必要多查一次。
    already = db.count_entries_present(entries) if not filled else 0
    _last.update(filled=filled, already=already, error='')
    return filled


def status() -> dict:
    """账号回填的自诊断：日志从哪读、读到几行、解析出几条、为什么可能一直是空的。

    为什么需要它（issue #232）：这一列空的时候，用户只看到「未采集（稍后自动
    回填）」，而真实原因可能是「Windows 装成了 docker 模式」「日志路径写错」
    「上游换了行格式」「时钟不同步」——处理方式完全不同，却都得靠维护者反复
    索要现场信息。把事实直接摆出来，用户自己就能判断。
    """
    mode = config.WB2API_MODE
    snap = dict(_last)
    info: dict = {
        'mode': mode,
        'source': (str(config.WB2API_CHAT_LOG_FILE) if mode == 'native'
                   else config.WB2API_SERVICE if mode == 'systemd'
                   else config.WB2API_CONTAINER),
        **snap,
    }
    if mode == 'native':
        path = config.WB2API_CHAT_LOG_FILE
        info['exists'] = path.is_file()
        if not info['exists']:
            info['hint'] = (f'对话日志文件不存在：{path}。确认上游以原生模式运行，'
                            '并把它的 stdout 重定向到这个文件（.env 的 WB2API_CHAT_LOG_FILE）。')
            return info
        try:
            info['size'] = path.stat().st_size
        except OSError as exc:
            info['hint'] = f'读不到日志文件：{exc}'
            return info
        if not info['size']:
            info['hint'] = '日志文件是空的：上游还没写过任何请求，或输出没被重定向到这里。'
            return info
    elif mode == 'systemd':
        # journalctl 读不到时也返回空列表，和「docker 不可用」一样是静默的；
        # 这里至少把 unit 名与需要做的事说清楚（journal 权限 / unit 名写错）。
        info['exists'] = True
        if not snap['checked_at']:
            info['hint'] = '采集还没跑过第一轮（启动后最多 15 秒）。稍等再看。'
        elif not snap['lines']:
            info['hint'] = (f'journalctl 读不到 {config.WB2API_SERVICE} 的日志：确认 unit 名正确，'
                            '且面板进程能读 journal（加入 systemd-journal 组，或改用 '
                            'WB2API_MODE=native + WB2API_LOG_FILE）。')
        return info
    else:
        info['exists'] = updater.can_control_docker()
        if not info['exists']:
            info['hint'] = ('当前是 docker 模式但读不到 docker（面板进程没有 docker.sock 权限）。'
                            '账号回填依赖 `docker logs`：挂载 socket 与 DOCKER_GID，'
                            '或改用 WB2API_MODE=native / systemd。')
            return info
    if not snap['checked_at']:
        info['hint'] = '采集还没跑过第一轮（启动后最多 15 秒）。稍等再看。'
    elif snap['lines'] and not snap['parsed']:
        info['hint'] = ('读到了日志，但没有一行符合上游的请求格式。多半是上游版本变化改了'
                        '日志排版 —— 请把日志里一条 chat 请求的原文贴出来。')
    elif snap['parsed'] and not snap['filled']:
        if snap.get('already'):
            # 正常状态：这批请求早就回填过了，只是本轮没有新记录可填。
            # 这里必须留空，否则每 15 秒弹一次「对不上」的黄条（issue #232）。
            info['hint'] = ''
        else:
            info['hint'] = ('解析出了请求行，但在匹配窗口内找不到对应的本地记录'
                            '（时间窗口 3 秒、模型名需一致）。常见于上游与面板时钟不同步，'
                            '或日志里只有更早的请求。')
    else:
        info['hint'] = ''
    return info


async def _loop() -> None:
    while True:
        try:
            # 读 docker logs 是阻塞调用（subprocess），放线程池里跑——
            # 直接 await 会把事件循环卡住（网关的转发也在同一个循环上）。
            filled = await asyncio.to_thread(_collect_once)
            if filled:
                logger.debug('回填账号 %d 条', filled)
        except Exception as exc:  # noqa: BLE001
            # 采集失败只是这一轮没有数据，下一轮继续；不影响任何请求
            _last['error'] = str(exc)[:200]
            logger.warning('账号回填失败（不影响请求）: %s', exc)
        await asyncio.sleep(_POLL_SECONDS)


def start_collector() -> bool:
    """启动后台回填任务（幂等）。需要运行中的事件循环。"""
    global _collector
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if _collector is None or _collector.done():
        _collector = loop.create_task(_loop())
    return True


def stop_collector() -> None:
    global _collector
    if _collector and not _collector.done():
        _collector.cancel()
    _collector = None


_collector: asyncio.Task | None = None
