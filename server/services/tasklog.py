"""上游自动任务日志的解析、采集与留痕。

背景：签到 / 猫猫旅行 / 活跃上报 / 保活这些任务由上游 workbuddy2api 定时执行，
结果只打在**容器日志**里，而且容器一重建（更新上游）日志就没了。
用户在界面上因此看不到「旅行领到了多少积分」这类记录。

这里做两件事：
  1. 把上游日志行解析成结构化事件（类型 / 账号 / 积分 / 成功失败）；
  2. 后台定期采集新产生的行，解析后落库长期保留。

上游日志形状（docker logs --timestamps，前面是 docker 加的 RFC3339 时间）：
    2026-09-11T17:43:44.123456789Z 2026/09/11 17:43:44 travel 89374120: claim ok record=1 reward=100
    2026-09-11T17:43:44.2Z travel 89374120: adopt ok (+300 credits)
    2026-09-11T17:43:44.3Z activity 89374120: streak days=3
    2026-09-11T17:43:44.4Z travel 89374120: status: <error>
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import datetime, timezone

from .. import config, db, upstreamsvc
from . import wb2api

# docker --timestamps 前缀
_DOCKER_TS = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)\s+(.*)$', re.S)

_KINDS = 'travel|activity|checkin|keepalive|user-resource'

# 账号 uid 的形态：字母数字（允许 - _），长度 >= 6。
_UID_SHAPE = re.compile(r'^[0-9A-Za-z_-]{6,64}$')

# 账号**标签**形态（上游 7e43884 起）：`昵称(uid8)`，昵称为空时退化成纯 uid8。
#
# 为什么必须认它：上游 `logfmt.Label()` 把调度日志里的账号标识从纯 uid8 改成
# `昵称(uid8)`（`internal/scheduler/scheduler.go:340` 等约 30 处），理由是排障时
# 人眼没法从 uid8 认出是哪个号。**而我们的解析器只认 `[0-9A-Za-z_-]`**，于是
# 全部账号维度的行会静默消失——不报错、不崩溃，只是「任务记录」里越来越少。
# 最要命的是 `travel …: claim ok record=… reward=…` 是唯一能拿到旅行积分的日志源，
# 丢掉它等于积分收益恒显示 0（把「赚到了」显示成「没赚」）。
#
# 解析出的 uid 一律**归一化回 uid8**（见 `_normalize_uid`）：界面按 uid 聚合
# 「某账号 N 条记录」，若直接拿 `昵称(uid8)` 当 uid，同一个号会因昵称变化而分裂成
# 好几个不存在的账号。昵称本身由 `routers/accounts.py` 的 `_nickname_resolver`
# 从账号表查，不依赖日志里的这一份。
_LABEL_SHAPE = re.compile(r'^(.{1,64}?)\(([0-9A-Za-z_-]{6,64})\)$')

# 账号标识的**贪婪**匹配式（含中文昵称与括号），用在下面那几种行形态里。
# 用非贪婪 + 回溯定位紧随其后的分隔符，比穷举字符集稳：昵称可以是任意文本。
_LABEL_TOKEN = r'(.+?)'

# 但上游除任务行外还会打「阶段行」与「汇总行」，形如
#   `checkin done: total=3 ok=1 ...`
#   `scheduled checkin skipped: ...`
# 它们同样是 `<kind> <单词>:` 的形态，会把 done / skipped 当成账号 uid，
# 在界面上凭空多出几个不存在的账号。因此用一个**保留字排除表**。
#
# 为什么不改成「uid 必须含数字」：uuid 的前 8 位是十六进制，
# 存在全是 a-f 的可能（约 0.05%/账号），那样整段日志会被静默丢掉——
# 用一个精确的排除表比赌 uid 的形状更安全。
_RESERVED_TOKENS = {
    'done', 'skipped', 'skip', 'refresh', 'save', 'total', 'ok', 'fail',
    'already', 'scheduled', 'start', 'end', 'begin', 'result', 'error',
}

# 主形态：[WARN:|ERR:] <kind> <uid 或 昵称(uid8)>: <rest>
# 上游 2026-09-12 起：uid 截前 8 位，可疑/失败行加 WARN:/ERR: 前缀
# 上游 7e43884 起：uid8 前面多了 `昵称(…)`，故改用 _LABEL_TOKEN 匹配。
_TASK_LINE = re.compile(
    r'(?:(WARN|ERR):\s+)?\b(' + _KINDS + r')\s+' + _LABEL_TOKEN + r':\s*(.*)$'
)

# 阶段形态：[WARN:|ERR:] <kind> <标签> <stage>: <rest>
# 例：`checkin 9b212d8c refresh: <err>`、`checkin 猫猫(9b212d8c) save: <err>`
# 注意标签与阶段名之间是空格而非冒号，主形态匹配不到，需单独认。
_STAGE_LINE = re.compile(
    r'(?:(WARN|ERR):\s+)?\b(' + _KINDS + r')\s+' + _LABEL_TOKEN + r'\s+'
    r'([a-z][a-z0-9_-]{1,20}):\s*(.*)$'
)

# 汇总形态：[WARN:|ERR:] <kind> done: <k=v ...>
_SUMMARY_LINE = re.compile(
    r'(?:(WARN|ERR):\s+)?\b(' + _KINDS + r')\s+done:\s*(.*)$'
)

# 跳过形态：`scheduled checkin skipped: <err>`
_SCHED_SKIP_LINE = re.compile(
    r'(?:(WARN|ERR):\s+)?scheduled\s+(' + _KINDS + r')\s+skipped:\s*(.*)$'
)

# ── 脚本类任务（第五、六类）──────────────────────────────
# 上游 2026-09-14 把「开学季」与「夜猫」从宿主机 crontab 迁入内置调度器，
# 它们不是「每账号一个 uid」的形态，而是**整批跑一个脚本**，因此日志只有
# 成败两行（见 internal/scheduler/school.go 的 runScript）：
#     <kind>: ok (<script>)             成功
#     WARN: <kind> (<script>): <err>    失败
# 上面四种形态都要求 `<kind> <token>:`，匹配不到这种，需单独处理——
# 否则这两类任务在「任务记录」里完全不可见。
_SCRIPT_KINDS = 'school|cat'
# 注意：这里不能用 \b 词边界——被补丁脚本写入时会被解释成退格符（\x08）。
# 用 (?:^|\s) 显式匹配行首或空白，效果等价且不会被转义吃掉。
_SCRIPT_OK_LINE = re.compile(
    r'(?:^|\s)(' + _SCRIPT_KINDS + r'):\s*ok\s*(?:\(([^)]*)\))?\s*$'
)
_SCRIPT_FAIL_LINE = re.compile(
    r'(?:WARN|ERR):\s*(' + _SCRIPT_KINDS + r')\s*(?:\(([^)]*)\))?\s*:\s*(.*)$'
)

# 脚本类任务的中文名（与 KIND_LABELS 合并展示）
SCRIPT_KIND_LABELS = {
    'school': '开学季任务',
    'cat': '夜猫任务',
}

# 幂等成功标志：腾讯把「今天已签到」当业务错误返回，但语义上是成功。
# 与上游 IsAlreadyCheckin 的判定保持一致（已签到 / already）。
_ALREADY_MARKERS = ('已签到', 'already', '重复签到')

# 中文类型名，前端与日志里共用一套说法
KIND_LABELS = {
    'travel': '猫猫旅行',
    'activity': '活跃上报',
    'checkin': '自动签到',
    'keepalive': '令牌保活',
    'user-resource': '余额查询',
    # 余额变动流水：由 credits.record_balance 写入，
    # 用于覆盖上游不打日志的获取渠道（签到、活跃上报等）
    'credit': '积分变动',
    # 脚本类任务（开学季 / 夜猫）：整批跑脚本，日志只有成败两行
    'school': '开学季任务',
    'cat': '夜猫任务',
    # 面板发起的「一键执行」（taskrun._record_history 写入）：claim / full 各记一行
    'taskrun': '一键执行',
}

# 明确表示「什么都没做，也不算失败」的前缀
_SKIP_MARKERS = ('skip', 'skipped')

_POLL_SECONDS = 45


def _event(ts: int, kind: str, uid: str, level: str, credits: int, message: str) -> dict:
    return {
        'ts': ts,
        'uid': uid,
        'kind': kind,
        'level': level,
        'credits': credits,
        'message': message,
        # 同一条容器日志行的时间戳精确到纳秒，配合内容即可唯一标识，
        # 因此反复采集不会重复入库
        'dedup_key': hashlib.sha1(f'{ts}|{kind}|{uid}|{message}'.encode('utf-8')).hexdigest(),
    }


def _normalize_uid(token: str) -> str:
    """把日志里的账号标识归一化成 uid8；不是已知形态则返回空串。

    ``昵称(uid8)`` → uid8；纯 uid8 原样返回；其余（含保留字 done / skipped）
    返回空串，调用方据此跳过该行。

    昵称里可能自带括号（如「猫猫(小)」），所以 `_LABEL_SHAPE` 用**非贪婪**匹配
    最外层的一对括号，并以「括号内必须是合法 uid 形态」为判据 —— `a(b)(c)` 这种
    会正确取到 `(c)`。
    """
    tok = token or ''
    if tok.lower() in _RESERVED_TOKENS:
        return ''
    m = _LABEL_SHAPE.match(tok)
    if m:
        return m.group(2)
    return tok if _UID_SHAPE.match(tok) else ''


def _is_uid(token: str) -> bool:
    return bool(_normalize_uid(token))


def _classify(rest: str, sev: str | None) -> tuple[int, str]:
    """从结果文案判断 (积分收益, 级别)。"""
    credits = 0
    lower = rest.lower()

    m_reward = re.search(r'reward=(\d+)', rest)
    if m_reward:
        return int(m_reward.group(1)), 'credit'

    # 通用收益形态：`<动作> ok (+N credit[s])`，括号里**只有** credit 一项。
    #
    # 为什么写成通用而不是逐个动作列举：上游这类「动作 + ok (+N credit)」的日志
    # 一直在增加，每加一个我们就要跟着补一条规则——漏掉的后果是收益显示为 0
    # （把「赚到了」显示成「没赚」，用户看到的数字是错的，但不会有任何报错）。
    # 已经出现过三次：`adopt ok (+300 credits)`、连登的 `gift` / `compensation`。
    # 收紧条件为「括号里只有 credit、后面直接是右括号」，因此不会误吞
    # `redeem tier=7d ok (+100 credit, +5 energy, +1 chances)` 那种多项括号
    # （那条由下面的 redeem 规则单独处理）。
    m_gain = re.search(r'ok\s*\(\+?(\d+)\s*credits?\s*\)', rest, re.IGNORECASE)
    if m_gain:
        return int(m_gain.group(1)), 'credit'

    # 连登奖励（上游 91418c5 新增）：`redeem tier=7d ok (+100 credit, +5 energy, +1 chances)`。
    # 不解析的话界面上收益显示为 0——而这是真实到账的积分，等于把「赚到了」显示成「没赚」。
    # 只取 credit 段（energy / chances 不是积分，不并入收益）。
    m_redeem = re.search(r'redeem\s+tier=\S+\s+ok\s*\(\+?(\d+)\s*credits?', rest, re.IGNORECASE)
    if m_redeem:
        return int(m_redeem.group(1)), 'credit'

    # 连登抽奖：上游的日志是
    #     `lottery drawn prize=<PrizeName> (<PrizeType>)`
    # 例如 `prize=10 积分 (credit)` / `prize=谢谢参与 (none)` / 实物奖。
    #
    # 注意**判据是括号里的 PrizeType，不是奖品名里的数字**：奖品名是腾讯返回的
    # 中文文本（`10 积分`），不是 `50 credits` 这种英文串 —— 上一版按
    # `prize=(\d+)\s*credits?` 写，匹配的是一个上游从未产生过的格式，于是真实
    # 日志一律被提取成 0（格式取自上游 scheduler.go:622 与它自己的测试夹具）。
    #
    # 积分奖的名字里带数量（`10 积分`），从中取第一个整数；取不到时仍标成 credit
    # 事件（说明这次确实发的是积分）但收益记 0，不编造数字。
    m_draw = re.search(r'lottery\s+drawn\s+prize=([^()]*?)\s*(?:\(([^)]*)\))?$',
                       rest.strip(), re.IGNORECASE)
    if m_draw:
        prize_name = (m_draw.group(1) or '').strip()
        prize_type = (m_draw.group(2) or '').strip().lower()
        if prize_type == 'credit':
            num = re.search(r'(\d+)', prize_name)
            return (int(num.group(1)), 'credit') if num else (0, 'credit')
        # 实物 / 未中奖等：算成功，但没有积分收益
        return 0, 'ok'

    # 账号被禁用属于严重结果，即使上游只标了 WARN 也按失败展示
    if '禁用' in rest or 'session dead' in lower:
        return 0, 'error'

    if sev == 'ERR':
        return 0, 'error'
    if sev == 'WARN':
        return 0, 'warn'

    # 「今天已签到」是幂等成功：腾讯以业务错误返回，但语义上没问题。
    # 不特判就会在界面上显示成红色的签到失败。
    if any(m in rest or m in lower for m in _ALREADY_MARKERS):
        return 0, 'ok'

    # 抽奖成功（无积分奖或未命中上面的 credit 形态）：不能落到末尾的 error——
    # 它没有 ` ok ` 字样，全靠这一条兜住，否则中奖反而显示成红色失败。
    if re.match(r'lottery\s+drawn\b', rest, re.IGNORECASE):
        return 0, 'ok'

    if re.match(r'report \d+/\d+:', lower):
        return 0, 'error'          # 5 连发上报中途失败
    if lower.startswith(_SKIP_MARKERS) or ('skip' in lower and 'ok' not in lower):
        return 0, 'info'
    if 'silent drop' in lower or 'failed' in lower or 'unknown state' in lower:
        return 0, 'warn'
    if ' ok' in lower or lower.startswith('ok') or 'days=' in lower:
        return 0, 'ok'
    if lower in ('refreshed',) or lower.startswith('refresh ok'):
        return 0, 'ok'
    # 其余形如 "<stage>: <error>" 保留为 error
    return 0, 'error'


def _parse_checkin_summary(ts: int, rest: str) -> dict:
    """解析 `checkin done: total=3 ok=1 already=1 fail=1 skipped=0`。

    这是上游新增的每轮汇总行，不属于任何账号；单独作为一条统计记录保留，
    便于在界面上对账「这轮签到到底成了几个」。
    """
    kv = dict(re.findall(r'(\w+)=(\d+)', rest))
    total = kv.get('total', '?')
    ok = kv.get('ok', '0')
    already = kv.get('already', '0')
    fail = kv.get('fail', '0')
    skipped = kv.get('skipped', '0')
    msg = f'本轮签到完成：共 {total} 个，成功 {ok}，已签到 {already}，失败 {fail}，跳过 {skipped}'
    # 新版汇总行把失败拆成「签到失败 / 余额查询失败」（issue #256）：口径不同，
    # 前者影响当天积分，后者只是余额没刷新，排障时不能混为一谈。
    split = []
    if 'fail_checkin' in kv:
        split.append(f"签到失败 {kv['fail_checkin']}")
    if 'fail_resource' in kv:
        split.append(f"余额查询失败 {kv['fail_resource']}")
    if split:
        msg += f"（{'，'.join(split)}）"
    level = 'warn' if fail not in ('0', '') else 'ok'
    return _event(ts, 'checkin', '', level, 0, msg)


def parse_line(line: str) -> dict | None:
    """把一行容器日志解析成结构化事件；与任务无关的行返回 None。

    上游日志有四种形态（2026-09-12 起）：
      1. <kind> <uid>: <rest>              任务结果（成功也打，如「今日已签到」）
      2. <kind> <uid> <stage>: <rest>      阶段失败（refresh / save）
      3. <kind> done: total=.. ok=.. ...   每轮汇总
      4. scheduled <kind> skipped: <rest>  计划阶段跳过
    只按形态 1 匹配会把 done / skipped 误当账号，故分别处理。
    """
    raw = (line or '').rstrip('\r\n')
    if not raw.strip():
        return None

    ts = 0
    m_ts = _DOCKER_TS.match(raw)
    if m_ts:
        ts = _iso_to_epoch(m_ts.group(1))
        body = m_ts.group(2)
    else:
        body = raw

    # 脚本类任务（开学季 / 夜猫）：`<kind>: ok (...)` 与 `WARN: <kind> (...): err`
    # 先判——它们的 kind 不在 _KINDS 里，不会被下面的形态误吞；
    # 但放在最前面更省事，也便于将来扩更多脚本类任务。
    m = _SCRIPT_OK_LINE.search(body)
    if m:
        script = (m.group(2) or '').strip()
        return _event(ts, m.group(1), '', 'ok', 0,
                      f'执行成功（{script}）' if script else '执行成功')
    m = _SCRIPT_FAIL_LINE.search(body)
    if m:
        script = (m.group(2) or '').strip()
        err = (m.group(3) or '').strip()
        msg = f'{script}：{err}' if script else err
        return _event(ts, m.group(1), '', 'warn', 0, f'执行失败（{msg}）')

    # 形态 3：每轮汇总（先判，避免 `checkin done:` 被当成 uid=done）
    m = _SUMMARY_LINE.search(body)
    if m and m.group(2):
        if m.group(2) == 'keepalive':
            counts = dict(re.findall(r'(checked|ok|failed|skipped)=(\d+)', m.group(3)))
            return _event(ts, 'keepalive', '', 'warn' if int(counts.get('failed', 0)) else 'ok', 0,
                          '本轮保活：尝试 {checked}，成功 {ok}，失败 {failed}，跳过 {skipped}'.format(
                              **{key: counts.get(key, '0') for key in ('checked', 'ok', 'failed', 'skipped')}))
        return _parse_checkin_summary(ts, m.group(3).strip())

    # 形态 4：计划阶段跳过
    m = _SCHED_SKIP_LINE.search(body)
    if m and m.group(2):
        reason = m.group(3).strip()
        return _event(
            ts, m.group(2), '', 'info', 0,
            f'计划任务未执行：{reason}' if reason else '计划任务未执行',
        )

    # 形态 1：任务结果（账号标识必须认得出来）
    m = _TASK_LINE.search(body)
    if m:
        uid = _normalize_uid(m.group(3))
        if uid:
            sev, kind = m.group(1), m.group(2)
            rest = m.group(4).strip()
            credits, level = _classify(rest, sev)
            return _event(ts, kind, uid, level, credits, rest)

    # 形态 2：阶段失败（标签后跟阶段名，如 `checkin <标签> refresh: ...`）
    m = _STAGE_LINE.search(body)
    if m:
        uid = _normalize_uid(m.group(3))
        if uid:
            sev, kind = m.group(1), m.group(2)
            stage, rest = m.group(4), m.group(5).strip()
            message = f'{stage}: {rest}'
            credits, level = _classify(message, sev)
            return _event(ts, kind, uid, level, credits, message)

    return None


def _iso_to_epoch(s: str) -> int:
    txt = s.strip()
    if txt.endswith('Z'):
        txt = txt[:-1] + '+00:00'
    # 截掉纳秒到微秒（datetime 只支持 6 位）
    m = re.match(r'^(.*\.\d{6})\d*(.*)$', txt)
    if m:
        txt = m.group(1) + m.group(2)
    try:
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:  # noqa: BLE001
        return 0


def parse_lines(lines: list[str]) -> list[dict]:
    out: list[dict] = []
    for ln in lines:
        ev = parse_line(ln)
        if ev:
            out.append(ev)
    return out


def strip_docker_ts(line: str) -> str:
    """去掉 docker --timestamps 加的时间前缀，只留应用自己的日志内容。"""
    m = _DOCKER_TS.match((line or '').rstrip('\r\n'))
    return m.group(2) if m else line


# ── 结果文案中文化 ───────────────────────────────────────
# 上游日志是英文原文，直接展示对中文用户不友好。这里在**展示层**翻译，
# 不动数据库里存的原文——排查问题时要能看到上游原话。
#   (匹配文本, 中文模板)  模板里的 {n} 会被捕获组依次填充
_MESSAGE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r'claim ok record=(\d+) reward=(\d+)'), '领奖成功：第 {0} 次行程，获得 {1} 积分'),
    (re.compile(r'adopt ok \(\+(\d+) credits?\)'), '领养成功：获得 {0} 积分'),
    (re.compile(r'depart ok location=(\d+)'), '已派出旅行（目的地 {0}）'),
    (re.compile(r'claim skipped \(arrived but no record_id\)'), '领奖跳过：已到站但无记录 ID'),
    (re.compile(r'claim record=(\d+):'), '领奖失败（行程 {0}）'),
    (re.compile(r'skip \(daily limit reached\)'), '跳过：今日次数已达上限'),
    (re.compile(r'skip \(traveling record=(\d+)\)'), '跳过：旅行进行中（行程 {0}）'),
    (re.compile(r'skip \(unknown state "([^"]*)"\)'), '跳过：状态未知（{0}）'),
    (re.compile(r'adopt skipped \(conversation threshold not reached, retry tomorrow\)'),
     '领养跳过：对话数未达门槛，明天重试'),
    (re.compile(r'report OK but streak\.days=0 \(silent drop\?\)'),
     '上报成功但连续天数仍为 0（疑似被静默丢弃）'),
    (re.compile(r'streak check failed \(report OK\):'), '连续天数校验失败（上报本身成功）'),
    (re.compile(r'report (\d+)/(\d+) ok'), '活跃上报成功（第 {0}/{1} 条）'),
    (re.compile(r'report (\d+)/(\d+):'), '活跃上报失败（第 {0}/{1} 条）'),
    (re.compile(r'streak days=(\d+)'), '连续登录 {0} 天'),
    (re.compile(r'keepalive ok expires=(\S+)'), '令牌保活成功（有效期 {0}）'),
    (re.compile(r'连续 (\d+) 次 12153 session dead — 禁用'), '连续 {0} 次会话失效，账号已禁用'),
    (re.compile(r'^(\d+) session dead$'), '会话失效（错误码 {0}）'),
    (re.compile(r'连续 (\d+) 次 12153 session dead'), '连续 {0} 次会话失效'),
    # ── 成长中心（连登 / 礼包 / 抽奖）───────────────────────────
    # 上游 2026-09-16 起新增的这一批日志，任务页会把原始英文当主文案显示，
    # 不翻译的话用户看到的是 `gift ok (+100 credit)` 这种开发者文本。
    # 顺序要紧：`redeem` 与 `lottery` 的形态更具体，必须排在通用 `ok (+N credit)`
    # 之前，否则会被它先匹配掉（`_MESSAGE_RULES` 是首次命中即返回）。
    (re.compile(r'redeem tier=(\S+) ok \(\+(\d+) credit'), '连登奖励领取成功（{0} 档，获得 {1} 积分）'),
    (re.compile(r'redeem tier=(\S+) skip'), '连登奖励跳过（{0} 档：已领过或天数不足）'),
    (re.compile(r'redeem tier=(\S+):'), '连登奖励领取失败（{0} 档）'),
    (re.compile(r'lottery drawn prize=([^()]*?)\s*\(credit\)'), '抽奖中奖：{0}'),
    (re.compile(r'lottery drawn prize=([^()]*?)\s*\(physical\)'), '抽奖中奖（实物）：{0}'),
    (re.compile(r'lottery drawn prize=([^()]*?)\s*\(none\)'), '抽奖未中奖'),
    (re.compile(r'lottery draw ok'), '抽奖成功'),
    (re.compile(r'lottery skip \(no chances or disabled\)'), '抽奖跳过：无次数或未开启'),
    (re.compile(r'lottery skip \(no chances\)'), '抽奖跳过：无抽奖次数'),
    (re.compile(r'lottery-chances:'), '查询抽奖次数失败'),
    (re.compile(r'lottery draw:'), '抽奖失败'),
    (re.compile(r'makeup ok (\S+) \(\+streak kept\)'), '已用补签卡保住连登（{0}）'),
    (re.compile(r'makeup (\S+):'), '补签失败（{0}）'),
    (re.compile(r'gift ok \(\+(\d+) credit\)'), '新手礼包领取成功（获得 {0} 积分）'),
    (re.compile(r'compensation ok \(\+(\d+) credit\)'), '活动补偿领取成功（获得 {0} 积分）'),
    (re.compile(r'gift already claimed'), '新手礼包已领过'),
    (re.compile(r'compensation already claimed'), '活动补偿已领过'),
    (re.compile(r'reward-state:'), '查询奖励状态失败'),
    # 通用兜底放最后：`<动作> ok (+N credit)`（上游还在持续加新动作）
    (re.compile(r'^(\w[\w-]*) ok \(\+(\d+) credits?\)$'), '任务成功（获得 {0} 积分）'),
)

_MESSAGE_EXACT = {
    'checkin ok code=0': '签到成功',
    'keepalive ok': '令牌保活成功',
    'buddy-info': '获取 Buddy 信息失败',
    'agreement': '签署协议失败',
    'adopt': '领养失败',
    'depart': '派出失败',
    'status': '查询旅行状态失败',
    'claim': '领奖失败',
}

# 「阶段名: 错误详情」的失败行：把阶段名换成中文，错误详情保留原文
_STAGE_LABELS = {
    'refresh': '刷新令牌失败',
    'buddy-info': '获取 Buddy 信息失败',
    'agreement': '签署协议失败',
    'adopt': '领养失败',
    'depart': '派出失败',
    'status': '查询旅行状态失败',
    'claim': '领奖失败',
    'save': '保存令牌失败',
}

_TRUNC_RE = re.compile(r'^(.*?)\s*\.\.\.\s*（已截断）$')

# 常见技术错误的短语替换（作用在展示文案上，覆盖「阶段: <英文错误>」的详情部分）
_PHRASES: tuple[tuple[str, str], ...] = (
    ('context deadline exceeded', '请求超时'),
    ('Client.Timeout exceeded while awaiting headers', '等待响应头超时'),
    ('unexpected end of JSON input', '响应内容不完整（JSON 解析失败）'),
    ('connection refused', '连接被拒绝'),
    ('no such host', '域名解析失败'),
    ('i/o timeout', '网络超时'),
    ('EOF', '连接被提前关闭'),
    ('token invalid', '令牌无效'),
    ('invalid token', '令牌无效'),
    ('session dead', '会话失效'),
    ('unauthorized', '未授权'),
)


def _apply_phrases(text: str) -> str:
    out = text
    for en, cn in _PHRASES:
        if en in out:
            out = out.replace(en, cn)
    return out


def translate_message(message: str) -> str:
    """把上游英文结果翻成中文；认不出的原样返回（并保留截断标记）。"""
    raw = (message or '').strip()
    if not raw:
        return raw

    # 被截断时先剥掉标记，翻完再补回；否则标记里的中文会干扰判断
    m_trunc = _TRUNC_RE.match(raw)
    body = m_trunc.group(1).strip() if m_trunc else raw
    suffix = ' …（已截断）' if m_trunc else ''

    # 本管理端自己写的中文流水（如「余额 +100（…）」）无需翻译
    if body.startswith('余额 '):
        return raw

    if body in _MESSAGE_EXACT:
        return _MESSAGE_EXACT[body] + suffix

    for pattern, tpl in _MESSAGE_RULES:
        m = pattern.search(body)
        if m:
            try:
                return tpl.format(*m.groups()) + suffix
            except (IndexError, KeyError):
                return tpl + suffix

    # 「阶段: 详情」形式（详情里的常见英文错误一并转中文）
    m_stage = re.match(r'^([a-z-]+):\s*(.+)$', body)
    if m_stage and m_stage.group(1) in _STAGE_LABELS:
        return _apply_phrases(f'{_STAGE_LABELS[m_stage.group(1)]}：{m_stage.group(2)}') + suffix

    # 单行无参数文案
    if body in _STAGE_LABELS:
        return _STAGE_LABELS[body] + suffix

    return _apply_phrases(raw)


# ── 后台采集 ─────────────────────────────────────────────
_collector: asyncio.Task | None = None
_last: dict = {'at': 0, 'added': 0, 'scanned': 0, 'error': ''}


def state() -> dict:
    return dict(_last)


# 每次回看的日志行数：上游会为每个请求打日志，行数消耗很快，
# 太小可能在两次轮询之间漏掉任务行；这里取一个明显大于 45 秒产出量的值。
TAIL_LINES = 3000


def _log_containers() -> list[str | None]:
    """本次要读日志的容器：默认上游 + 各分组登记的上游容器（issue #389）。

    默认上游用 `None` 表示「按当前部署模式读取」（原生文件 / systemd / 默认容器）；
    分组容器必须显式给名字，走 `docker logs`。同一容器只读一次——默认容器与某个
    分组填了同一个名字时，否则会把同一批日志解析两遍（dedup 虽然会挡住入库，但
    浪费一次 `docker logs`，而且 `parsed` 统计会翻倍）。
    """
    out: list[str | None] = [None]
    default = str(config.WB2API_CONTAINER or '').strip()
    seen = {default} if default else set()
    for upstream in upstreamsvc.list_upstreams(include_default=False):
        name = str(upstream.get('container') or '').strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


async def _read_logs(name: str | None, limit: int) -> list[str]:
    if name is None:
        return await asyncio.to_thread(wb2api.read_container_logs, limit, True)
    return await asyncio.to_thread(
        wb2api.read_container_logs, limit, True, container=name)


async def _collect_once(limit: int = TAIL_LINES) -> tuple[int, int]:
    """读一次所有上游容器的日志并入库，返回 (解析到的任务行数, 新增条数)。"""
    events: list[dict] = []
    for name in _log_containers():
        lines = await _read_logs(name, limit)
        events.extend(parse_lines(lines))
    if not events:
        return 0, 0
    added = await asyncio.to_thread(db.add_task_logs, events)
    from . import accountrisk
    await accountrisk.handle_tasks(events)
    return len(events), added


async def _loop() -> None:
    while True:
        try:
            parsed, added = await _collect_once()
            _last.update({
                'at': int(time.time()),
                'parsed': parsed,
                'added': added,
                'error': '',
            })
        except Exception as exc:  # noqa: BLE001
            _last.update({'at': int(time.time()), 'error': str(exc)[:200]})
        await asyncio.sleep(_POLL_SECONDS)


def start_collector() -> bool:
    """启动后台采集任务（幂等）。需要运行中的事件循环。"""
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
