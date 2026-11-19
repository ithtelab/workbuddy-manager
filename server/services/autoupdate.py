"""自动更新：按设置定时检测新版本，并在「允许的时间窗」里自动执行一键更新。

## 为什么需要它

一键更新本身已经很完善（独立 worker、状态外置、锁、校验），但它只能**手点**：
面板长期无人值守的部署（典型如 VPS 上跑着、几周才登一次）会一直停在旧版本，
错过修复。这里只做一件事——把「检测 → 发现新版本 → 点更新」这个人工动作
按管理员的设置自动化，**不新增任何更新路径**：真正干活的仍是
`updater.start_update`（与按钮同一条路），所以校验、锁、日志、回显全部复用。

## 设计要点

  · 默认关闭。开启后每 `interval_hours` 小时强制检测一次（绕过 6 小时缓存）。
  · **时间窗**：更新会重启面板（以及可能重建上游容器），白天跑可能打断正在
    进行的请求，所以只在 `window_start`–`window_end`（服务器本地时间，整点）
    之间动手；两者相等表示不限时段。检测本身不受时间窗限制，结果会写进状态，
    界面上能看到「发现新版本，等待时间窗」。
  · 只在**确定**有新版本时动手：检测失败 / 结果是旧缓存（stale）/ 发布产物
    还没上传完（assets_ready=False）都只记录、不更新——宁可晚一轮，不能在
    半成品 Release 上自动升级。
  · 更新对象：新发布的上游与运行中不一致（upstream_changed）且允许连上游一起
    更新、本机能操作 docker 时用 `both`，否则只更新管理端（`manager`），与
    界面主按钮的逻辑一致。
  · **同一版本失败不连环重试**：上一次自动更新了同一个版本却没装上时，24 小时
    内不再自动重试（避免每轮都重启服务），由管理员看日志后手动处理。
  · 手动更新正在进行时不碰。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from .. import config, db, security
from . import updater

logger = logging.getLogger(__name__)

CONFIG_KEY = 'auto_update'
STATE_KEY = 'auto_update_state'

MIN_INTERVAL_HOURS = 1
MAX_INTERVAL_HOURS = 7 * 24
TARGETS = ('auto', 'manager')

# 同一版本自动更新失败后，多久内不再自动重试
RETRY_SAME_VERSION_SECONDS = 24 * 3600
# 检测**失败**后的重试间隔：一次网络抖动/限流不该把下一轮检测推到几小时甚至一周后
# （间隔最大 168 小时，按整段等等于这一周都不会自动更新）。成功一轮才重新按间隔排。
ERROR_RETRY_SECONDS = 3600

_TICK_SECONDS = 300
_FIRST_DELAY_SECONDS = 120

_task: asyncio.Task | None = None


# ── 配置 ────────────────────────────────────────────────────────────────
def unsupported_reason() -> str:
    """当前部署形态不支持网页一键更新时返回原因；支持时返回空串。

    判据直接取自 `updater.unsupported_reason()`（一键更新的前置拒绝）—— 两处
    各写一份的话，新增部署形态时只改一处，另一处就会拿过期的判断放行或误拒，
    而这种不一致只会在无人值守时才暴露。
    """
    return updater.unsupported_reason()


def default_config() -> dict:
    return {
        'enabled': False,
        'interval_hours': 6,
        # 默认凌晨 3 点到 6 点（服务器本地时间）才动手：避开白天的请求高峰
        'window_start': 3,
        'window_end': 6,
        # auto：发布包里的上游变了且能操作 docker 时连上游一起更新；manager：只更新管理端
        'target': 'auto',
    }


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_bool(value: object, default: bool) -> bool:
    """严格的布尔收敛。

    不能直接 `bool(value)`：HTTP 客户端把开关写成字符串 `"false"` 时它是 truthy，
    于是「关闭自动更新」的请求反而会**开启**它 —— 这是会让面板无人值守重启的
    开关，必须按字面值判断，认不出来就退回默认（关）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value) if value in (0, 1) else default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ('1', 'true', 'on', 'yes'):
            return True
        if text in ('0', 'false', 'off', 'no', ''):
            return False
    return default


def normalize_config(raw: object) -> dict:
    """把任意输入收敛成合法配置（未知字段丢弃，越界值夹到边界）。"""
    base = default_config()
    src = raw if isinstance(raw, dict) else {}
    cfg = dict(base)
    cfg['enabled'] = _as_bool(src.get('enabled'), base['enabled'])
    cfg['interval_hours'] = min(MAX_INTERVAL_HOURS, max(
        MIN_INTERVAL_HOURS, _as_int(src.get('interval_hours'), base['interval_hours'])))
    for key in ('window_start', 'window_end'):
        cfg[key] = min(23, max(0, _as_int(src.get(key), base[key])))
    target = str(src.get('target') or base['target'])
    cfg['target'] = target if target in TARGETS else base['target']
    return cfg


def validate_config(raw: object) -> tuple[dict | None, str]:
    """保存前的严格校验：越界直接报错（而不是悄悄夹值），让管理员知道填错了。"""
    if not isinstance(raw, dict):
        return None, '参数不合法'
    interval = _as_int(raw.get('interval_hours'), -1)
    if not MIN_INTERVAL_HOURS <= interval <= MAX_INTERVAL_HOURS:
        return None, f'检测间隔需在 {MIN_INTERVAL_HOURS}–{MAX_INTERVAL_HOURS} 小时之间'
    for key in ('window_start', 'window_end'):
        v = _as_int(raw.get(key), -1)
        if not 0 <= v <= 23:
            return None, '时间窗的小时需在 0–23 之间'
    if str(raw.get('target') or 'auto') not in TARGETS:
        return None, '更新对象不合法'
    return normalize_config(raw), ''


def get_config() -> dict:
    return normalize_config(db.get_setting(CONFIG_KEY, None))


def save_config(cfg: dict) -> dict:
    clean = normalize_config(cfg)
    db.set_setting(CONFIG_KEY, clean)
    return clean


def get_state() -> dict:
    raw = db.get_setting(STATE_KEY, None)
    return raw if isinstance(raw, dict) else {}


def _save_state(state: dict) -> None:
    db.set_setting(STATE_KEY, state)


def server_tz_offset_minutes() -> int:
    """服务器本地时区相对 UTC 的偏移（分钟），界面据此说明时间窗按哪个时区算。"""
    lt = time.localtime()
    # tm_gmtoff 在 CPython 3.6+（含 Windows）的 struct_time 上都有；取不到就退回 0，
    # 不能因为读时区把整个设置页打挂（这个值只用于界面说明）。
    return int((getattr(lt, 'tm_gmtoff', 0) or 0) // 60)


# ── 判定（纯函数，便于测试）────────────────────────────────────────────
def in_window(hour: int, start: int, end: int) -> bool:
    """整点时间窗 [start, end)，支持跨午夜；start == end 表示全天。"""
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def check_due(cfg: dict, state: dict, now: float) -> bool:
    """是否到了下一次**强制**检测的时刻。

    优先用 `next_check_at`（检测失败时按短间隔排的下一轮）；旧状态只有
    `last_check_at`，按当前间隔现算，升级后不必再等旧间隔走完。
    """
    next_at = state.get('next_check_at')
    if next_at is not None:
        try:
            return now >= float(next_at)
        except (TypeError, ValueError):
            pass
    last = float(state.get('last_check_at') or 0)
    return now - last >= cfg['interval_hours'] * 3600


def decide(cfg: dict, check: dict, *, hour: int, running: bool, state: dict,
           now: float, docker_ok: bool, busy: bool = False) -> tuple[str, str, str]:
    """根据一次检测结果决定动作。返回 (action, target, reason)。

    action：
      · ``update`` —— 应该现在更新，target 为 ``manager`` / ``both``；
      · ``wait``   —— 有新版本但现在不动手（时间窗外 / 产物未就位 / 最近失败过）；
      · ``none``   —— 没有新版本或检测不可信。
    """
    m = (check or {}).get('manager') or {}
    if m.get('error') or m.get('stale'):
        return 'none', '', f"检测失败：{m.get('error') or '结果来自旧缓存'}"
    if not m.get('has_update'):
        return 'none', '', '已是最新版本'
    latest = str(m.get('latest') or '')
    if m.get('assets_ready') is False:
        return 'wait', '', f'发现 {latest}，但发布产物还没就位'
    if running:
        return 'wait', '', '已有更新任务正在执行'
    if (state.get('last_attempt_version') == latest
            and now - float(state.get('last_attempt_at') or 0) < RETRY_SAME_VERSION_SECONDS):
        return 'wait', '', f'{latest} 上次自动更新未成功，24 小时内不再自动重试'
    if not in_window(hour, cfg['window_start'], cfg['window_end']):
        return 'wait', '', (
            f"发现 {latest}，等待时间窗 {cfg['window_start']:02d}:00–{cfg['window_end']:02d}:00")
    if busy:
        # 更新会重启面板，systemd 默认按 cgroup 收掉子进程 —— 正在跑的签到/领奖
        # 会被打断在半路。等它跑完（下一轮再看）比抢这 5 分钟划算。
        return 'wait', '', '有任务正在执行（签到 / 领奖 / 任务），等它结束后再更新'
    target = 'manager'
    if cfg.get('target') == 'auto' and m.get('upstream_changed') and docker_ok:
        target = 'both'
    return 'update', target, f'发现 {latest}，开始自动更新（{target}）'


# ── 执行 ────────────────────────────────────────────────────────────────
def _tasks_busy() -> bool:
    """上游账号任务（签到 / 领奖 / 做任务）是否正在跑。

    更新会重启面板；systemd 默认 KillMode=control-group，重启会带走同 cgroup 的
    子进程，跑到一半的签到/领奖就此中断。取不到状态时按「不忙」处理 —— 不能
    因为读不到任务状态就让自动更新永远不动。
    """
    try:
        from . import taskrun
        return bool(taskrun.status().get('running'))
    except Exception:  # noqa: BLE001
        return False


def run_once(now: float | None = None, *, force: bool = False) -> dict:
    """执行一轮：到期就检测，满足条件就启动更新。返回本轮写入的状态。"""
    cfg = get_config()
    state = get_state()
    now = time.time() if now is None else now
    if not cfg['enabled'] and not force:
        return state
    blocked = unsupported_reason()
    if blocked:
        state.update(last_action='unsupported', last_message=blocked, pending_version='')
        _save_state(state)
        return state

    # 上一轮自动启动的更新已经落地：清掉失败重试保护
    if state.get('last_attempt_version') and \
            updater.current_version() == state.get('last_attempt_version'):
        state.pop('last_attempt_version', None)
        state.pop('last_attempt_at', None)

    has_pending = bool(state.get('pending_version'))
    if not force and not check_due(cfg, state, now) and not has_pending:
        return state

    due = force or check_due(cfg, state, now)
    before = json.dumps(state, sort_keys=True, default=str)
    try:
        # 到期才强制刷新；只是「等时间窗」的轮次用缓存即可，不必每 5 分钟打 GitHub
        check = updater.check_updates(force=due)
    except Exception as exc:  # noqa: BLE001
        state.update(last_check_at=now, next_check_at=now + ERROR_RETRY_SECONDS,
                     last_action='error', last_message=str(exc)[:200])
        _save_state(state)
        return state

    if due:
        state['last_check_at'] = now
        manager = (check or {}).get('manager') or {}
        # 检测失败（网络 / 限流）不按管理员设的间隔排下一轮：间隔最大 168 小时，
        # 一次抖动就会让自动更新停摆一周。成功的检测才重新按间隔排。
        failed = bool(manager.get('error') or manager.get('stale'))
        state['next_check_at'] = now + (
            ERROR_RETRY_SECONDS if failed else cfg['interval_hours'] * 3600)
    running = bool(updater.read_status().get('running'))
    hour = time.localtime(now).tm_hour
    action, target, reason = decide(
        cfg, check, hour=hour, running=running, state=state, now=now,
        docker_ok=updater.can_control_docker() if cfg['target'] == 'auto' else False,
        busy=_tasks_busy())

    latest = str((check.get('manager') or {}).get('latest') or '')
    state['pending_version'] = latest if action == 'wait' else ''
    state['last_action'] = action
    state['last_message'] = reason

    if action == 'update':
        ok, message = updater.start_update(target)
        state['last_attempt_version'] = latest
        state['last_attempt_at'] = now
        state['last_action'] = 'started' if ok else 'error'
        state['last_message'] = message if not ok else reason
        if ok:
            security.audit({'username': 'system'}, 'auto_update', latest, f'自动更新已开始（{target}）')
            logger.info('自动更新已开始：%s → %s（%s）', updater.current_version(), latest, target)
        else:
            logger.warning('自动更新未能启动：%s', message)
    # 「等时间窗」的轮次每 5 分钟走一遍，状态没变就不必回写数据库。
    if json.dumps(state, sort_keys=True, default=str) != before:
        _save_state(state)
    return state


async def _loop() -> None:
    await asyncio.sleep(_FIRST_DELAY_SECONDS)
    while True:
        try:
            if get_config()['enabled']:
                # check_updates / start_update 都是阻塞调用（网络 + 子进程），丢给线程
                await asyncio.to_thread(run_once)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning('自动更新巡检异常: %s', exc)
        await asyncio.sleep(_TICK_SECONDS)


def start_scheduler() -> bool:
    """启动自动更新巡检（幂等）。无事件循环时返回 False（如 CLI 场景）。"""
    global _task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if _task is None or _task.done():
        _task = loop.create_task(_loop())
    return True


def stop_scheduler() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
