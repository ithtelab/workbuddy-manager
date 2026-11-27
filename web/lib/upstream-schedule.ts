/**
 * 上游排程草稿：从上游 `config.json` 的 schedule 段挑出可视化字段、做脏值收敛、
 * 生成**稀疏 patch**。
 *
 * 设计约束：本模块**不得有任何运行时 import**。`web/lib/upstream-schedule.test.mjs`
 * 直接用 Node 的 type stripping 跑这份源码；一旦引入运行时依赖（`@/lib/i18n` 之类），
 * 解析就会失败，那条行为测试也就跟着失效。需要译文的 `parseHours` 因此改成由调用方
 * 传入翻译函数；与 schedule 无关的延迟校验留在 `schedule-validation.ts`，由界面在
 * 提交前调用。
 */

export interface UpstreamScheduleDraft {
  checkin_enabled: boolean;
  checkin_hours: number[];
  travel_enabled: boolean;
  travel_hours: number[];
  activity_enabled: boolean;
  activity_hours: number[];
  activity_report_count: number;
  keepalive_enabled: boolean;
  keepalive_hours: number[];
  school_enabled: boolean;
  school_hours: number[];
  cat_enabled: boolean;
  cat_hours: number[];
  random_delay_minutes: number;
  account_delay_min_seconds: number;
  account_delay_max_seconds: number;
}

/** 与后端 `server/services/wb2api.py` 的 `_INT_RANGES` 保持一致（1..50 条）。 */
export const ACTIVITY_REPORT_COUNT_MIN = 1;
export const ACTIVITY_REPORT_COUNT_MAX = 50;

/** 用于渲染提示的翻译函数（与 i18n 的 `t` 同签名）。 */
export type Translate = (key: string, params?: Record<string, string | number>) => string;

/** 把时刻数组格式化为可读文本，如 [9,21] -> "9, 21" */
export function hoursToText(v: unknown): string {
  if (Array.isArray(v)) {
    return v
      .filter((x): x is number => typeof x === 'number')
      .sort((a, b) => a - b)
      .join(', ');
  }
  return '';
}

/** 解析用户输入的时刻列表：返回 {ok, hours, error?}。`tr` 负责把错误码翻成文案。 */
export function parseHours(
  text: string,
  tr: Translate,
): {ok: boolean; hours: number[]; error?: string} {
  const parts = text.split(/[,，\s]+/).filter(Boolean);
  if (!parts.length) return {ok: false, hours: [], error: tr('settings.errNeedOneHour')};
  const out: number[] = [];
  for (const p of parts) {
    const n = Number(p);
    if (!Number.isInteger(n) || n < 0 || n > 23) {
      return {ok: false, hours: [], error: tr('settings.errHourRange', {v: p})};
    }
    if (!out.includes(n)) out.push(n);
  }
  return {ok: true, hours: out.sort((a, b) => a - b)};
}

function cleanHours(raw: unknown, fallback: number[]): number[] {
  if (!Array.isArray(raw)) return [...fallback];
  const valid = Array.from(
    new Set(raw.filter((x): x is number => typeof x === 'number' && Number.isInteger(x) && x >= 0 && x <= 23)),
  ).sort((a, b) => a - b);
  return valid.length ? valid : [...fallback];
}

function cleanInt(raw: unknown, min: number, max: number, fallback: number): number {
  const n = typeof raw === 'number' ? raw : Number(raw);
  if (!Number.isInteger(n) || n < min || n > max) return fallback;
  return n;
}

/**
 * 保留整数原值：非整数 / 空白才回落默认。
 *
 * `activity_report_count` 不能像延迟那样「越界就换默认」——上游配置里可能存着
 * 超出界面下拉范围（但后端仍接受）的值，静默改成 5 会改变实际上报强度，用户却
 * 看不到任何提示。超出 `ACTIVITY_REPORT_COUNT_MIN..MAX` 时由界面显式提示。
 */
function keepInt(raw: unknown, fallback: number): number {
  if (typeof raw === 'number' && Number.isInteger(raw)) return raw;
  if (typeof raw === 'string' && raw.trim() !== '') {
    const n = Number(raw);
    if (Number.isInteger(n)) return n;
  }
  return fallback;
}

export function defaultUpstreamSchedule(): UpstreamScheduleDraft {
  return {
    checkin_enabled: true,
    checkin_hours: [9, 21],
    travel_enabled: true,
    travel_hours: [9, 21],
    activity_enabled: true,
    activity_hours: [10],
    activity_report_count: 5,
    keepalive_enabled: true,
    keepalive_hours: [22],
    school_enabled: true,
    school_hours: [12],
    cat_enabled: true,
    cat_hours: [1],
    random_delay_minutes: 0,
    account_delay_min_seconds: 0,
    account_delay_max_seconds: 0,
  };
}

/** 从上游 schedule 段挑出可视化字段。未知键原样保留在上游配置里（不进草稿、不回写）。 */
export function pickUpstreamSchedule(source: Record<string, unknown> | undefined): UpstreamScheduleDraft {
  const def = defaultUpstreamSchedule();
  if (!source) return def;
  return {
    checkin_enabled: typeof source.checkin_enabled === 'boolean' ? source.checkin_enabled : def.checkin_enabled,
    checkin_hours: cleanHours(source.checkin_hours, def.checkin_hours),
    travel_enabled: typeof source.travel_enabled === 'boolean' ? source.travel_enabled : def.travel_enabled,
    travel_hours: cleanHours(source.travel_hours, def.travel_hours),
    activity_enabled: typeof source.activity_enabled === 'boolean' ? source.activity_enabled : def.activity_enabled,
    activity_hours: cleanHours(source.activity_hours, def.activity_hours),
    activity_report_count: keepInt(source.activity_report_count, def.activity_report_count),
    keepalive_enabled: typeof source.keepalive_enabled === 'boolean' ? source.keepalive_enabled : def.keepalive_enabled,
    keepalive_hours: cleanHours(source.keepalive_hours, def.keepalive_hours),
    school_enabled: typeof source.school_enabled === 'boolean' ? source.school_enabled : def.school_enabled,
    school_hours: cleanHours(source.school_hours, def.school_hours),
    cat_enabled: typeof source.cat_enabled === 'boolean' ? source.cat_enabled : def.cat_enabled,
    cat_hours: cleanHours(source.cat_hours, def.cat_hours),
    random_delay_minutes: cleanInt(source.random_delay_minutes, 0, 60, def.random_delay_minutes),
    account_delay_min_seconds: cleanInt(source.account_delay_min_seconds, 0, 600, def.account_delay_min_seconds),
    account_delay_max_seconds: cleanInt(source.account_delay_max_seconds, 0, 600, def.account_delay_max_seconds),
  };
}

/** 上报条数是否落在后端接受的区间内（不在区间时界面要显式提示，不能静默改写）。 */
export function activityReportCountInRange(n: number): boolean {
  return Number.isInteger(n) && n >= ACTIVITY_REPORT_COUNT_MIN && n <= ACTIVITY_REPORT_COUNT_MAX;
}

function sameVal(a: unknown, b: unknown): boolean {
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((v, i) => v === b[i]);
  }
  return a === b;
}

export function isUpstreamScheduleDirty(cur: UpstreamScheduleDraft, org: UpstreamScheduleDraft): boolean {
  return (Object.keys(cur) as (keyof UpstreamScheduleDraft)[]).some((k) => !sameVal(cur[k], org[k]));
}

export type UpstreamSchedulePatch = Record<string, boolean | number | number[]>;

/**
 * 生成**稀疏** patch：只包含真正变化的字段。
 *
 * 稀疏是硬要求：上游 schedule 段还会随版本增加新键（如 school/cat 就是后迁进来的），
 * 把整份草稿回写等于用「本前端认识的键」覆盖掉不认识的键。延迟越界由调用方在提交前
 * 用 `scheduleDelayError` 拦截（本模块不引运行时依赖，也不在这里吞掉越界值）。
 */
export function diffUpstreamSchedule(
  cur: UpstreamScheduleDraft,
  org: UpstreamScheduleDraft,
): UpstreamSchedulePatch {
  const patch: UpstreamSchedulePatch = {};
  for (const k of Object.keys(cur) as (keyof UpstreamScheduleDraft)[]) {
    if (!sameVal(cur[k], org[k])) {
      patch[k] = Array.isArray(cur[k]) ? [...(cur[k] as number[])] : (cur[k] as boolean | number);
    }
  }
  return patch;
}
