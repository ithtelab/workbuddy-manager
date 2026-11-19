/**
 * 积分收支图的数据与轴参数（纯函数，可被 Node 直接测）。
 *
 * 为什么要单独一个模块：单日范围下探到小时（issue #285）后，「用小时序列还是
 * 天序列」「X 轴刻度怎么稀疏、标签怎么格式化」「发散柱的整齐上限取多少」这三件事
 * 分散在仪表盘组件里，且柱状 / 折线两个分支各写了一份 X 轴配置。规则一旦走样
 * 都不会报错，只是图退回难读的形态（当天只剩一个孤点、24 个小时标签糊成一片、
 * 柱子贴边 0 线不在中间），所以收进纯函数用 Node 直测。
 *
 * 与 `lib/usage-chart.ts` 同规矩：本模块不得引入任何运行时 import（只有
 * `import type`），`web/lib/*.test.mjs` 才能用 Node 的 type stripping 直接跑。
 */
import type {CreditsFlow, CreditsFlowPoint, CreditsFlowHourPoint} from './types';

/** 折线 / 柱状共用的一份图数据：小时或天序列，二选一 */
export interface FlowSeries {
  points: (CreditsFlowPoint | CreditsFlowHourPoint)[];
  /** true 表示 points 是小时桶（单日下探），X 轴按小时画 */
  byHour: boolean;
}

/**
 * 选喂给图的序列：单日（后端 `granularity === 'hour'` 且带 `hourly`）用小时桶，
 * 其余用天序列。旧后端不返回 hourly 时自然退回天序列，维持现状而不是报错。
 */
export function flowSeries(flow: Pick<CreditsFlow, 'daily' | 'hourly' | 'granularity'>): FlowSeries {
  const hours = flow.granularity === 'hour' && Array.isArray(flow.hourly)
    ? flow.hourly
    : null;
  return {points: hours ?? flow.daily, byHour: hours !== null};
}

/** X 轴参数：小时每 3 个显示 1 个（24 桶不糊），天序列交给 recharts 自动稀疏。 */
export function flowAxis(byHour: boolean): {
  dataKey: 'hour' | 'day';
  interval: 2 | 'preserveStartEnd';
  format: (v: string | number) => string;
} {
  return byHour
    ? {
        dataKey: 'hour',
        interval: 2,
        format: (v) => `${String(v).padStart(2, '0')}:00`,
      }
    : {
        dataKey: 'day',
        interval: 'preserveStartEnd',
        format: (v) => String(v).slice(5),
      };
}

/**
 * 发散柱的单轴上限：收入与支出共用一根轴，取两者峰值的整齐上限
 * （1 / 2 / 2.5 / 5 / 10 档）。没有这一步，recharts 会把上下限取成数据极值，
 * 柱子贴边且 0 线不在中间。峰值非正时给 1，保证 domain=[-top, top] 有意义。
 */
export function flowTop(points: {income: number; expense: number}[]): number {
  const peak = points.reduce((m, p) => Math.max(m, p.income, p.expense), 0);
  if (peak <= 0) return 1;
  const mag = 10 ** Math.floor(Math.log10(peak));
  const step = [1, 2, 2.5, 5, 10].find((s) => peak <= s * mag) ?? 10;
  return step * mag;
}

/** 发散柱的数据行：支出取负让柱子向下生长（收入向上、净增折线叠在中间）。 */
export function flowBars<P extends {income: number; expense: number; net: number}>(points: P[]): (P & {expenseDown: number})[] {
  return points.map((p) => ({...p, expenseDown: -p.expense}));
}
