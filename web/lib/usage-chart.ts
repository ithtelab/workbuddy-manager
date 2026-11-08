/**
 * 用量趋势图的**粒度与形态**（纯函数，可被 Node 直接测）。
 *
 * 为什么要单独一个模块：原来的趋势图**粒度固定按天**，而范围选择器的默认值是
 * 「今日」—— 于是每个用户打开统计页看到的第一张图就是**一根柱子**（一天只有一个
 * 数据点）。问题不在柱状图本身，而在「粒度没有跟着范围走」。
 *
 * 这里把「范围 → 粒度 / 形态 / 刻度密度」写成一张表，并且把两个数据源都补齐成
 * **定长**序列：
 *   · 今日 → 24 个小时桶（后端已经补零，这里只做标签）；
 *   · 多日 → 按天补齐到窗口长度（后端不补零，缺的那天会整个消失 —— 图上表现为
 *     X 轴跳过一天，读者会以为「那天没数据」和「那天不存在」是一回事）。
 *
 * ⚠️ 与 `lib/account-status.ts` 同规矩：本模块**不得引入任何运行时 import**
 * （只有 `import type`），这样 `web/lib/*.test.mjs` 能用 Node 的 type stripping
 * 直接跑源码。
 */
import type {UsagePoint, UsageHourPoint} from './types';

/** 图的形态 */
export type ChartShape = 'bar' | 'area';

export interface ChartSpec {
  /** 每个数据点代表什么 */
  granularity: 'hour' | 'day';
  shape: ChartShape;
  /** X 轴每多少个刻度显示一个标签（避免 24/90 个标签挤成一团） */
  tickEvery: number;
}

/**
 * 范围 → 粒度 / 形态 / 刻度密度。
 *
 * 判据：
 *   · 只有一天时按天聚合毫无意义（一个点），必须下探到小时；
 *   · 7 天 7 根柱子正好，读得出「哪几天高」；
 *   · 30 / 90 天用柱子会细得看不清起伏（90 根 ≈ 每根 2px），改面积图 ——
 *     面积图对「总量趋势」这种连续量更自然，也让尖峰一眼可见。
 */
export function chartSpecFor(days: number): ChartSpec {
  if (days <= 1) return {granularity: 'hour', shape: 'bar', tickEvery: 3};
  if (days <= 7) return {granularity: 'day', shape: 'bar', tickEvery: 1};
  if (days <= 30) return {granularity: 'day', shape: 'area', tickEvery: 5};
  return {granularity: 'day', shape: 'area', tickEvery: 15};
}

/** 图上的一个点（两种粒度统一成同一个形状，图组件因此不用分支） */
export interface ChartPoint {
  /** X 轴标签：小时是 `09:00`，天是 `09-29` */
  label: string;
  /** 完整标识，给 tooltip 用（小时 `2026-09-29 09:00`） */
  full: string;
  tokens: number;
  requests: number;
  failed: number;
  success: number;
  /** 小时粒度下：这是不是「现在」这一小时（UI 会把它标出来） */
  isNow?: boolean;
}

const pad = (n: number) => String(n).padStart(2, '0');

/**
 * 小时序列 → 24 个点。
 *
 * 后端已固定返回 24 桶（补零），这里只做标签与「现在这一小时」的标记；
 * 顺序按 hour 排，缺的桶补 0（双保险：万一后端换了实现，图不会因此少几根柱子）。
 */
export function hourlySeries(points: readonly UsageHourPoint[], nowHour?: number): ChartPoint[] {
  const byHour = new Map<number, UsageHourPoint>();
  for (const p of points) byHour.set(Number(p.hour), p);
  return Array.from({length: 24}, (_, h) => {
    const p = byHour.get(h);
    return {
      label: `${pad(h)}:00`,
      full: `${p?.day ?? ''} ${pad(h)}:00`,
      tokens: (p?.prompt_tokens ?? 0) + (p?.completion_tokens ?? 0),
      requests: p?.requests ?? 0,
      failed: p?.failed ?? 0,
      success: p?.success ?? 0,
      isNow: nowHour === h,
    };
  });
}

/**
 * 天序列 → 窗口内定长序列（缺的天补 0）。
 *
 * `todayKey` 由调用方传（本地时区的 `YYYY-MM-DD`）—— 模块内不读时钟，测试才确定。
 * 已经超出窗口的旧点会被丢掉（后端按 `day >= 起点` 过滤，理论上不会出现，
 * 但定长承诺由这里兜住）。
 */
export function dailySeries(
  points: readonly UsagePoint[],
  days: number,
  todayKey: string,
): ChartPoint[] {
  const n = Math.max(1, Math.floor(days));
  const byDay = new Map<string, UsagePoint>();
  for (const p of points) byDay.set(String(p.day), p);
  const end = dayKeyToUtcMidnight(todayKey);
  return Array.from({length: n}, (_, i) => {
    const key = utcMidnightToDayKey(end - (n - 1 - i) * 86400000);
    const p = byDay.get(key);
    return {
      label: key.slice(5),
      full: key,
      tokens: (p?.prompt_tokens ?? 0) + (p?.completion_tokens ?? 0),
      requests: p?.requests ?? 0,
      failed: p?.failed ?? 0,
      success: p?.success ?? 0,
    };
  });
}

/** `YYYY-MM-DD` → 该日的 UTC 零点毫秒（**只用做日期加减**，不参与展示） */
function dayKeyToUtcMidnight(key: string): number {
  const [y, m, d] = key.split('-').map((x) => parseInt(x, 10));
  return Date.UTC(y, (m || 1) - 1, d || 1);
}

function utcMidnightToDayKey(ms: number): string {
  const d = new Date(ms);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
}

/** 本地时区的今天（`YYYY-MM-DD`）—— 与后端 day_of() 同一口径 */
export function todayKeyLocal(now: Date = new Date()): string {
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}
