/**
 * 积分收支图的数据选择与轴参数（`lib/credits-flow-chart.ts` 的行为测试）。
 *
 * 跑法（Node ≥ 22.6）：
 *     node --experimental-strip-types web/lib/credits-flow-chart.test.mjs
 * 或走 Python 包装：`python -m unittest server.tests.test_credits_flow_chart`
 *
 * 为什么值得测：这几条错了都不会报错，只是图退回难读的形态：
 *   · 当天仍喂天序列 → 一个点画不出线（issue #285 要修的现象本身）；
 *   · 后端升级窗口内没带 hourly → 得退回天序列，不能抛错白屏；
 *   · 24 个小时标签全画出来 → X 轴糊成一片；
 *   · 上限取了数据极值 → 柱子贴边、0 线不在中间。
 */
import {flowSeries, flowAxis, flowTop, flowBars} from './credits-flow-chart.ts';

let pass = 0;
const ok = (cond, label, detail = '') => {
  if (!cond) {
    console.error(`  FAIL  ${label}${detail ? ' -- ' + detail : ''}`);
    process.exitCode = 1;
    return;
  }
  pass += 1;
  console.log(`  ok  ${label}${detail ? ' -- ' + detail : ''}`);
};

const daily = [
  {day: '2026-10-24', income: 100, expense: 20, net: 80},
  {day: '2026-10-25', income: 50, expense: 5, net: 45},
];
const hourly = [
  {day: '2026-10-25', hour: 0, income: 0, expense: 0, net: 0},
  {day: '2026-10-25', hour: 9, income: 40, expense: 3.5, net: 36.5},
  {day: '2026-10-25', hour: 14, income: 0, expense: 2, net: -2},
];

// ── 序列选择 ───────────────────────────────────────────────────
console.log('flowSeries：granularity=hour 且带 hourly 时下探到小时');
{
  const {points, byHour} = flowSeries({daily, hourly, granularity: 'hour'});
  ok(byHour, '标记为小时序列');
  ok(points === hourly, 'points 就是 hourly 本身（不复制不裁剪）');
}
console.log('flowSeries：多日范围仍用天序列');
{
  const {points, byHour} = flowSeries({daily, granularity: 'day'});
  ok(!byHour, '标记为天序列');
  ok(points === daily, 'points 就是 daily 本身');
}
console.log('flowSeries：旧后端没带 granularity / hourly 时退回天序列，不抛错');
{
  const a = flowSeries({daily});
  const b = flowSeries({daily, granularity: 'day', hourly: undefined});
  ok(!a.byHour && a.points === daily, '缺 granularity 退回 daily');
  ok(!b.byHour && b.points === daily, 'granularity 不是 hour 时忽略 hourly');
}
console.log('flowSeries：granularity=hour 但 hourly 不是数组（缺字段）也不炸');
{
  const {points, byHour} = flowSeries({daily, granularity: 'hour', hourly: null});
  ok(!byHour && points === daily, '退回天序列');
}

// ── X 轴参数 ───────────────────────────────────────────────────
console.log('flowAxis：小时轴每 3 个显示 1 个，标签补零成 09:00');
{
  const axis = flowAxis(true);
  ok(axis.dataKey === 'hour', 'dataKey 用 hour');
  ok(axis.interval === 2, 'interval=2（24 桶不糊）');
  ok(axis.format(9) === '09:00', `9 → 09:00（实际 ${axis.format(9)}）`);
  ok(axis.format(0) === '00:00', '0 → 00:00');
}
console.log('flowAxis：天轴自动稀疏，标签只留月-日');
{
  const axis = flowAxis(false);
  ok(axis.dataKey === 'day', 'dataKey 用 day');
  ok(axis.interval === 'preserveStartEnd', '区间交给 recharts 保留首尾');
  ok(axis.format('2026-10-25') === '10-25', '完整日期截成 MM-DD');
}

// ── 发散柱上限 ─────────────────────────────────────────────────
console.log('flowTop：取峰值的整齐上限（1/2/2.5/5/10 档）');
{
  ok(flowTop([{income: 730, expense: 0}]) === 1000, '730 → 1000');
  ok(flowTop([{income: 1800, expense: 0}]) === 2000, '1800 → 2000');
  ok(flowTop([{income: 2600, expense: 0}]) === 5000, '2600 跨 2.5 档 → 5000');
  ok(flowTop([{income: 40, expense: 220}]) === 250, '支出是峰值时同样取档（220 → 250）');
  ok(flowTop([{income: 0, expense: 0}]) === 1, '全 0 给 1，domain 不塌成 [0,0]');
}

// ── 发散柱数据 ─────────────────────────────────────────────────
console.log('flowBars：支出取负向下生长，其余字段原样保留');
{
  const bars = flowBars([{day: '2026-10-25', income: 40, expense: 3.5, net: 36.5}]);
  ok(bars[0].expenseDown === -3.5, 'expenseDown = -expense');
  ok(bars[0].income === 40 && bars[0].net === 36.5, 'income / net 原样');
}

console.log(pass > 0 ? `all passed (${pass})` : 'no assertion ran');
