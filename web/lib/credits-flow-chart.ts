/**
 * 积分收支折线图的数据行（纯函数，可被 Node 直接测）。
 *
 * 为什么要单独一个模块：`/api/stats/credits` 只在 days=1（当天）时额外返回
 * 24 个小时桶（issue #285 的单日下探）。把 `flow.daily` 直接喂给图的话，当天
 * 仍是一个点、画不出线。这里集中决定当天用小时序列、其余时段用天序列；升级
 * 窗口内后端还没带上 hourly 时退回天序列，维持现状而不是报错。
 *
 * 与 `lib/usage-chart.ts` 同规矩：本模块不得引入任何运行时 import（只有
 * `import type`），`web/lib/*.test.mjs` 才能用 Node 的 type stripping 直接跑。
 */
import type {CreditsFlow} from './types';

/** 折线图的一行：天粒度 label 是完整日期串，小时粒度是 09:00 这种 */
export interface CreditsFlowRow {
  label: string;
  income: number;
  expense: number;
  net: number;
}

export interface CreditsFlowChart {
  rows: CreditsFlowRow[];
  /**
   * recharts XAxis 的 interval：24 个小时标签每 3 个显示 1 个（与用量趋势图
   * 的 tickEvery 3 一致），天序列交给 recharts 自动稀疏。
   */
  xInterval: number | 'preserveEnd';
}

const pad = (n: number) => String(n).padStart(2, '0');

/** 当天（days<=1）且后端带了 hourly 时用 24 个小时点，其余用天序列。 */
export function creditsFlowChart(
  flow: Pick<CreditsFlow, 'daily' | 'hourly'>,
  days: number,
): CreditsFlowChart {
  if (days <= 1 && Array.isArray(flow.hourly)) {
    return {
      rows: flow.hourly.map((p) => ({
        label: `${pad(p.hour)}:00`,
        income: p.income,
        expense: p.expense,
        net: p.net,
      })),
      xInterval: 2,
    };
  }
  return {
    rows: flow.daily.map((p) => ({
      label: p.day,
      income: p.income,
      expense: p.expense,
      net: p.net,
    })),
    xInterval: 'preserveEnd',
  };
}
