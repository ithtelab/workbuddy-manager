"""按 DeepSeek 官方价目估算每次调用的费用（issue #380）。

## 这个模块解决什么

用户想知道「这些 token 如果按官方价买，花了多少钱」。数据本来就在库里：
`request_logs` 有 `prompt_tokens` / `completion_tokens` / `cache_hit_tokens` /
`cache_miss_tokens` / `model` / `ts`，缺的只是一张价目表和一次乘法。

## 三条硬约束（决定了代码为什么长这样）

1. **价目会变，历史不能跟着变。**
   DeepSeek 调过价，峰谷时段和节假日也可能改。所以价目不是一张常量表，而是
   `pricing.json` 里**一串带 `effective_from` 的版本**：每笔费用按它自己的
   `ts` 去找**当时生效**的那一版。改价 = 追加一条新版本，上个月的账不受影响。

2. **峰值/空闲两档差一倍，不能拍成平摊。**
   官方规则：北京时间周一至周五（不含中国法定节假日）9:00-12:00、14:00-18:00
   为高峰，其余时段（含周末、节假日全天）为空闲，空闲价是高峰价的一半。
   判定必须用**北京时间**而不是服务器本地时区——容器里通常是 UTC，直接用
   `localtime` 会把高峰窗口整体错开 8 小时，误差正好是一倍。

3. **未知模型返回 `None`，绝不返回 0。**
   本项目的既有口径是「没数据」与「是 0」必须分开（见 request_logs 的
   cache 三列）。没收录价目的模型（glm / kimi / minimax …）算不出费用，
   返回 `None`，界面显示「—」；返回 0 会让用户以为这些调用不要钱。

## 缓存口径

`prompt_tokens` 是**含**缓存的（上游 `usage.prompt_tokens` 的语义），所以：
- 上游给了 `cache_hit_tokens`：命中部分按命中价，其余按未命中价；
- 上游没给（NULL，老上游或非对话请求）：整段输入按未命中价——这是**高估**方向
  的保守做法，比按命中价低估安全。

## 可覆盖

内置价目表可以被打包更新覆盖；管理员也可以在面板里改。覆盖内容存在 settings
表的 `pricing_override` 键里（与内置表同构），合并规则：按 `effective_from`
排序，同一天以覆盖版为准。改了价目**只影响之后**的调用，已落库的费用不重算
（要重算就点统计页的「重建用量」，它会按当前价目重跑一遍）。
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_PRICING_FILE = Path(__file__).with_name('pricing.json')

# 官方价目表的时区：北京时间（UTC+8，无夏令时）。
#
# 优先用 zoneinfo（有 tzdata 时最准），拿不到就退回固定偏移——Android/proot 的
# 精简镜像里不一定装了 tzdata，而这里只需要一个固定偏移就够了。
try:  # pragma: no cover - 取决于运行环境
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo('Asia/Shanghai')
except Exception:  # noqa: BLE001
    _TZ = timezone(timedelta(hours=8), 'Asia/Shanghai')

_lock = threading.RLock()
_builtin: dict | None = None
_override: dict | None = None
_override_loaded = False

MONEY_UNIT = '元'


# ── 价目表的读取与合并 ────────────────────────────────────


def _read_builtin() -> dict:
    global _builtin
    with _lock:
        if _builtin is None:
            try:
                _builtin = json.loads(_PRICING_FILE.read_text(encoding='utf-8'))
            except Exception:  # noqa: BLE001
                # 价目表坏了不能让整个面板挂掉：费用一律显示「—」，其余功能照常。
                _builtin = {'currency': 'CNY', 'versions': [], 'model_aliases': {}}
        return _builtin


def _read_override() -> dict | None:
    """管理员覆盖的价目（存在 settings 表里）。

    延迟导入 db：db 反过来要 import pricing 算费用，顶层互相 import 会成环。
    第一次读之后缓存在进程内；保存覆盖时由 `set_override` 直接刷新缓存。
    """
    global _override, _override_loaded
    with _lock:
        if not _override_loaded:
            _override_loaded = True
            try:
                from .. import db
                raw = db.get_setting('pricing_override')
                _override = raw if isinstance(raw, dict) else None
            except Exception:  # noqa: BLE001
                _override = None
        return _override


def set_override(data: dict | None) -> None:
    """保存（或清除）管理员覆盖的价目。data 与 pricing.json 同构。"""
    global _override, _override_loaded
    from .. import db
    if data:
        db.set_setting('pricing_override', data)
    else:
        db.set_setting('pricing_override', None)
    with _lock:
        _override = data or None
        _override_loaded = True


def invalidate() -> None:
    """丢掉进程内缓存（测试与「重建用量」后调用）。"""
    global _builtin, _override, _override_loaded
    with _lock:
        _builtin = None
        _override = None
        _override_loaded = False


def _merged() -> dict:
    """内置表 + 覆盖表的合并结果（覆盖表优先，按 effective_from 归并）。"""
    base = dict(_read_builtin())
    over = _read_override()
    if not over:
        return base
    versions = list(base.get('versions') or []) + list(over.get('versions') or [])
    versions.sort(key=lambda v: str(v.get('effective_from') or ''))
    # 只改价格的版本可以只写 `models`：峰谷时段、工作日、节假日这些「规则」
    # 从更早的版本继承下来。否则管理员在面板里只改单价，新版本就会因为缺少
    # peak_windows 而被判成「永远空闲」，费用正好算成一半 —— 错得还很安静。
    inherited: dict[str, Any] = {}
    filled: list[dict] = []
    for v in versions:
        item = dict(v)
        for key in ('peak_days', 'peak_windows', 'holidays'):
            if item.get(key) is None and key in inherited:
                item[key] = inherited[key]
            if item.get(key) is not None:
                inherited[key] = item[key]
        filled.append(item)
    versions = filled
    merged = dict(base)
    merged['versions'] = versions
    # 覆盖表的其它顶层字段（币种、别名）按「覆盖优先」处理
    for key in ('currency', 'model_aliases'):
        if over.get(key):
            merged[key] = over[key]
    return merged


def versions() -> list[dict]:
    """全部价目版本（按生效日期升序），给设置面板展示用。"""
    return [dict(v) for v in (_merged().get('versions') or [])]


def currency() -> str:
    return str(_merged().get('currency') or 'CNY')


def version_for(ts: float | None = None) -> dict | None:
    """取 `ts` 时刻生效的那一版价目；早于最早一版时返回 None（不猜）。"""
    day = _local_date(ts)
    chosen = None
    for v in _merged().get('versions') or []:
        if str(v.get('effective_from') or '') <= day:
            chosen = v
    return dict(chosen) if chosen else None


def _local_date(ts: float | None = None) -> str:
    t = datetime.now(_TZ) if ts is None else datetime.fromtimestamp(float(ts), _TZ)
    return t.strftime('%Y-%m-%d')


# ── 模型名归一 ────────────────────────────────────────────


def normalize_model(model: str | None) -> str | None:
    """把上游模型名折成价目表里的键。

    `cn:deepseek-v4.1-flash` / `global:deepseek-v4.1-flash` 这类版本前缀要剥掉：
    两个版本跑的是同一个官方模型，价格一样。没收录的名字返回 None（不猜价格）。
    """
    name = (model or '').strip().lower()
    if not name:
        return None
    for prefix in ('cn:', 'global:'):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    aliases = _merged().get('model_aliases') or {}
    return aliases.get(name) or (name if _has_model(name) else None)


def _has_model(name: str) -> bool:
    for v in _merged().get('versions') or []:
        if name in (v.get('models') or {}):
            return True
    return False


# ── 峰谷判定 ──────────────────────────────────────────────


def is_peak(ts: float | None, version: dict | None = None) -> bool:
    """这一刻是否落在高峰时段（北京时间周一至周五、非节假日、9-12 / 14-18）。"""
    t = datetime.now(_TZ) if ts is None else datetime.fromtimestamp(float(ts), _TZ)
    v = version or version_for(ts)
    if not v:
        return False
    peak_days = v.get('peak_days') or [1, 2, 3, 4, 5]
    if t.isoweekday() not in peak_days:
        return False
    if t.strftime('%Y-%m-%d') in _holidays_of(v, t.year):
        return False
    for window in v.get('peak_windows') or []:
        try:
            start, end = int(window[0]), int(window[1])
        except (TypeError, ValueError, IndexError):
            continue
        if start <= t.hour < end:
            return True
    return False


def _holidays_of(version: dict, year: int) -> set[str]:
    table = version.get('holidays') or {}
    return set(table.get(str(year)) or [])


def missing_holiday_years(ts: float | None = None) -> list[int]:
    """价目表里缺节假日安排的年份（面板据此提示「节假日会按高峰价算」）。"""
    v = version_for(ts) or {}
    table = v.get('holidays') or {}
    year = (datetime.now(_TZ) if ts is None else datetime.fromtimestamp(float(ts), _TZ)).year
    return [y for y in (year, year + 1) if not table.get(str(y))]


# ── 费用计算 ──────────────────────────────────────────────


def unit_prices(model: str | None, ts: float | None = None) -> dict | None:
    """某个模型在某时刻的单价（元 / 百万 tokens，已按峰谷选档）。

    返回 `{'cache_hit': x, 'cache_miss': y, 'output': z, 'peak': bool,
    'version': '2026-09-01'}`；模型没收录时返回 None。
    """
    key = normalize_model(model)
    if not key:
        return None
    version = version_for(ts)
    if not version:
        return None
    table = (version.get('models') or {}).get(key)
    if not table:
        return None
    peak = is_peak(ts, version)
    band = 'peak' if peak else 'off_peak'

    def _pick(field: str) -> float | None:
        raw = table.get(field)
        if not isinstance(raw, dict):
            return None
        val = raw.get(band)
        if val is None:
            val = raw.get('peak' if band == 'off_peak' else 'off_peak')
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    hit, miss, out = _pick('cache_hit'), _pick('cache_miss'), _pick('output')
    if miss is None or out is None:
        return None
    return {
        'cache_hit': hit if hit is not None else miss,
        'cache_miss': miss,
        'output': out,
        'peak': peak,
        'band': band,
        'version': str(version.get('effective_from') or ''),
        'model': key,
        'currency': currency(),
    }


def cost_of(model: str | None,
            prompt_tokens: int | None,
            completion_tokens: int | None,
            cache_hit: int | None = None,
            cache_miss: int | None = None,
            ts: float | None = None) -> float | None:
    """一次调用的估算费用（元）。模型没收录价目时返回 None（界面显示「—」）。"""
    prices = unit_prices(model, ts)
    if not prices:
        return None
    pt = max(0, int(prompt_tokens or 0))
    ct = max(0, int(completion_tokens or 0))
    hit = max(0, int(cache_hit or 0)) if cache_hit is not None else 0
    if cache_hit is not None and cache_miss is not None:
        miss = max(0, int(cache_miss))
    else:
        # 上游没给命中数：整段输入按未命中价（高估方向，见模块说明）
        miss = max(0, pt - hit) if cache_hit is not None else pt
    total = (hit * prices['cache_hit'] + miss * prices['cache_miss']
             + ct * prices['output']) / 1_000_000
    return round(total, 6)


def is_priced(model: str | None, ts: float | None = None) -> bool:
    return unit_prices(model, ts) is not None


# ── 面板展示用 ────────────────────────────────────────────


def describe(ts: float | None = None) -> dict:
    """给设置页「计费价格」面板的数据：价目版本、当前生效档、覆盖情况、过期提醒。"""
    merged = _merged()
    over = _read_override()
    active = version_for(ts)
    now = datetime.now(_TZ)
    out_versions = []
    for v in merged.get('versions') or []:
        item = {
            'effective_from': str(v.get('effective_from') or ''),
            'verified_at': str(v.get('verified_at') or ''),
            'source': str(v.get('source') or ''),
            'source_note': str(v.get('source_note') or ''),
            'holiday_source': str(v.get('holiday_source') or ''),
            'models': v.get('models') or {},
            'peak_windows': v.get('peak_windows') or [],
            'peak_days': v.get('peak_days') or [],
            'holiday_years': sorted((v.get('holidays') or {}).keys()),
            'overridden': bool(over and v in (over.get('versions') or [])),
        }
        try:
            verified = datetime.strptime(item['verified_at'], '%Y-%m-%d').replace(tzinfo=_TZ)
            item['age_days'] = (now - verified).days
        except Exception:  # noqa: BLE001
            item['age_days'] = None
        out_versions.append(item)
    return {
        'currency': currency(),
        'money_unit': MONEY_UNIT,
        'versions': out_versions,
        'active': active,
        'is_peak_now': is_peak(ts),
        'override': over,
        'has_override': bool(over),
        'missing_holiday_years': missing_holiday_years(ts),
        # 价目表超过这个天数没核对过就在界面上标黄，避免悄悄用旧价算下去
        'stale_after_days': 90,
        'aliases': merged.get('model_aliases') or {},
        'priced_models': sorted({m for v in merged.get('versions') or []
                                 for m in (v.get('models') or {})}),
    }
