import {t as tGlobal} from '@/lib/i18n';
import {scheduleDelayError} from '@/lib/schedule-validation';

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

/** 解析用户输入的时刻列表：返回 {ok, hours, error?} */
export function parseHours(text: string): {ok: boolean; hours: number[]; error?: string} {
  const parts = text.split(/[,，\s]+/).filter(Boolean);
  if (!parts.length) return {ok: false, hours: [], error: tGlobal('settings.errNeedOneHour')};
  const out: number[] = [];
  for (const p of parts) {
    const n = Number(p);
    if (!Number.isInteger(n) || n < 0 || n > 23) {
      return {ok: false, hours: [], error: tGlobal('settings.errHourRange', {v: p})};
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
    activity_report_count: cleanInt(source.activity_report_count, 1, 20, def.activity_report_count),
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

function sameVal(a: unknown, b: unknown): boolean {
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((v, i) => v === b[i]);
  }
  return a === b;
}

export function isUpstreamScheduleDirty(cur: UpstreamScheduleDraft, org: UpstreamScheduleDraft): boolean {
  return (Object.keys(cur) as (keyof UpstreamScheduleDraft)[]).some((k) => !sameVal(cur[k], org[k]));
}

export function diffUpstreamSchedule(
  cur: UpstreamScheduleDraft,
  org: UpstreamScheduleDraft,
): {ok: true; patch: Record<string, boolean | number | number[]>} | {ok: false; error: string} {
  const delayErr = scheduleDelayError({
    random_delay_minutes: cur.random_delay_minutes,
    account_delay_min_seconds: cur.account_delay_min_seconds,
    account_delay_max_seconds: cur.account_delay_max_seconds,
  });
  if (delayErr) return {ok: false, error: delayErr};

  const patch: Record<string, boolean | number | number[]> = {};
  for (const k of Object.keys(cur) as (keyof UpstreamScheduleDraft)[]) {
    if (!sameVal(cur[k], org[k])) {
      patch[k] = Array.isArray(cur[k]) ? [...(cur[k] as number[])] : (cur[k] as boolean | number);
    }
  }
  return {ok: true, patch};
}
